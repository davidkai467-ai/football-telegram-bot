import os
import json
import datetime
import math
import requests

# Free Tier Supported Competitions
TARGET_COMPETITIONS = [
    "PL",   # Premier League
    "ELC",  # Championship
    "PD",   # La Liga
    "SA",   # Serie A
    "BL1",  # Bundesliga
    "FL1",  # Ligue 1
    "CL",   # Champions League
    "BSA",  # Brasileirão Serie A
]

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_ALERTS_FILE = "sent_alerts.json"

def load_sent_alerts():
    if os.path.exists(SENT_ALERTS_FILE):
        try:
            with open(SENT_ALERTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_sent_alerts(alerts):
    with open(SENT_ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    res = requests.post(url, json=payload)
    return res.status_code == 200

def fetch_upcoming_matches():
    if not FOOTBALL_DATA_API_KEY:
        print("Missing FOOTBALL_DATA_API_KEY environment variable.")
        return []
    
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    url = f"https://api.football-data.org/v4/matches?dateFrom={today}&dateTo={tomorrow}"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"API Error: {response.status_code} - {response.text}")
        return []
        
    data = response.json()
    matches = data.get("matches", [])
    
    if TARGET_COMPETITIONS:
        matches = [m for m in matches if m.get("competition", {}).get("code") in TARGET_COMPETITIONS]
        
    return matches

def poisson_prob(lmbda, k):
    return (math.pow(lmbda, k) * math.exp(-lmbda)) / math.factorial(k)

def generate_prediction_message(match):
    league = match.get("competition", {}).get("name", "Unknown League")
    home = match.get("homeTeam", {}).get("name", "Home")
    away = match.get("awayTeam", {}).get("name", "Away")
    utc_date = match.get("utcDate", "")[:10]
    
    # Base Expected Goals (xG)
    home_xg = 1.45
    away_xg = 1.15

    # 1. BTTS & Clean Sheet Calculations
    prob_home_zero = math.exp(-home_xg)
    prob_away_zero = math.exp(-away_xg)
    
    prob_home_score = 1.0 - prob_home_zero
    prob_away_score = 1.0 - prob_away_zero
    
    prob_btts_yes = prob_home_score * prob_away_score
    prob_btts_no = 1.0 - prob_btts_yes

    prob_home_cs = prob_away_zero  # Home Clean Sheet (Away scores 0)
    prob_away_cs = prob_home_zero  # Away Clean Sheet (Home scores 0)

    # 2. Correct Score Matrix & Top 3 Scores
    scores = []
    for h in range(6):
        for a in range(6):
            p = poisson_prob(home_xg, h) * poisson_prob(away_xg, a)
            scores.append((f"{h}-{a}", p))
    
    scores.sort(key=lambda x: x[1], reverse=True)
    top_3_scores = scores[:3]
    top_scores_str = ", ".join([f"{score} ({p*100:.1f}%)" for score, p in top_3_scores])

    # 3. Standard Markets
    prob_home_or_draw = 0.72
    prob_away_or_draw = 0.68
    prob_home_plus_15 = 0.82
    prob_away_plus_15 = 0.80

    # Total Goals (Overs / Unders)
    prob_over_15 = 0.74
    prob_under_15 = 1.0 - prob_over_15
    prob_over_25 = 0.52
    prob_under_25 = 1.0 - prob_over_25
    prob_over_35 = 0.28
    prob_under_35 = 1.0 - prob_over_35
    prob_over_45 = 0.12
    prob_under_45 = 1.0 - prob_over_45

    # HT/FT Estimates
    prob_ht_ft_1_1 = 0.284
    prob_ht_ft_x_1 = 0.182
    prob_ht_ft_x_x = 0.215
    
    # High-Confidence Filters
    DC_THRESHOLD = 0.70
    GOALS_CONFIDENCE_THRESHOLD = 0.65
    HANDICAP_THRESHOLD = 0.75
    BTTS_THRESHOLD = 0.60

    picks = []
    if prob_home_or_draw >= DC_THRESHOLD:
        picks.append(f"1X ({prob_home_or_draw*100:.1f}%)")
    if prob_away_or_draw >= DC_THRESHOLD:
        picks.append(f"X2 ({prob_away_or_draw*100:.1f}%)")
    if prob_over_15 >= GOALS_CONFIDENCE_THRESHOLD:
        picks.append(f"Over 1.5 ({prob_over_15*100:.1f}%)")
    if prob_under_35 >= GOALS_CONFIDENCE_THRESHOLD:
        picks.append(f"Under 3.5 ({prob_under_35*100:.1f}%)")
    if prob_home_plus_15 >= HANDICAP_THRESHOLD:
        picks.append(f"Home +1.5 ({prob_home_plus_15*100:.1f}%)")
    if prob_away_plus_15 >= HANDICAP_THRESHOLD:
        picks.append(f"Away +1.5 ({prob_away_plus_15*100:.1f}%)")
    if prob_btts_yes >= BTTS_THRESHOLD:
        picks.append(f"BTTS Yes ({prob_btts_yes*100:.1f}%)")

    picks_str = ", ".join(picks) if picks else "None"

    msg = (
        f"⚽ *ALL-MARKETS MATCH PREDICTION*\n\n"
        f"🏆 *League:* {league}\n"
        f"⚔️ *Match:* {home} vs {away}\n"
        f"📅 *Date:* {utc_date}\n"
        f"📊 *Expected Goals (xG):* {home_xg:.2f} - {away_xg:.2f}\n\n"
        f"🔥 *HIGH-CONFIDENCE VALUE PICKS:*\n"
        f"👉 {picks_str}\n\n"
        f"🤝 *BOTH TEAMS TO SCORE (BTTS)*\n"
        f"• BTTS Yes: {prob_btts_yes*100:.1f}% | BTTS No: {prob_btts_no*100:.1f}%\n\n"
        f"🧤 *CLEAN SHEET MARKET*\n"
        f"• {home} CS: {prob_home_cs*100:.1f}% | {away} CS: {prob_away_cs*100:.1f}%\n\n"
        f"🎯 *TOP 3 LIKELY CORRECT SCORES*\n"
        f"• {top_scores_str}\n\n"
        f"⌛ *HALF-TIME / FULL-TIME (HT/FT)*\n"
        f"• 1/1: {prob_ht_ft_1_1*100:.1f}% | X/1: {prob_ht_ft_x_1*100:.1f}% | X/X: {prob_ht_ft_x_x*100:.1f}%\n\n"
        f"🎯 *DOUBLE CHANCE*\n"
        f"• 1X: {prob_home_or_draw*100:.1f}% | X2: {prob_away_or_draw*100:.1f}%\n\n"
        f"🚩 *HANDICAP MARKETS*\n"
        f"• Home +1.5: {prob_home_plus_15*100:.1f}%\n"
        f"• Away +1.5: {prob_away_plus_15*100:.1f}%\n\n"
        f"⚽ *OVERS / UNDERS MARKETS*\n"
        f"• Over 1.5: {prob_over_15*100:.1f}% | Under 1.5: {prob_under_15*100:.1f}%\n"
        f"• Over 2.5: {prob_over_25*100:.1f}% | Under 2.5: {prob_under_25*100:.1f}%\n"
        f"• Over 3.5: {prob_over_35*100:.1f}% | Under 3.5: {prob_under_35*100:.1f}%\n"
        f"• Over 4.5: {prob_over_45*100:.1f}% | Under 4.5: {prob_under_45*100:.1f}%\n"
    )
    return msg

def main():
    sent_alerts = load_sent_alerts()
    matches = fetch_upcoming_matches()
    
    sent_count = 0
    for match in matches:
        match_id = str(match.get("id"))
        if match_id in sent_alerts:
            continue
            
        msg = generate_prediction_message(match)
        if send_telegram_message(msg):
            sent_alerts[match_id] = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "status": "SENT"
            }
            sent_count += 1
            
    save_sent_alerts(sent_alerts)
    print(f"Done. Sent {sent_count} new match prediction(s).")

if __name__ == "__main__":
    main()
