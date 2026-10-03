import os
import json
import datetime
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
    
    # Fetch fixtures scheduled for today and tomorrow
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    url = f"https://api.football-data.org/v4/matches?dateFrom={today}&dateTo={tomorrow}"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"API Error: {response.status_code} - {response.text}")
        return []
        
    data = response.json()
    matches = data.get("matches", [])
    
    # Filter by TARGET_COMPETITIONS if specified
    if TARGET_COMPETITIONS:
        matches = [m for m in matches if m.get("competition", {}).get("code") in TARGET_COMPETITIONS]
        
    return matches

def generate_prediction_message(match):
    league = match.get("competition", {}).get("name", "Unknown League")
    home = match.get("homeTeam", {}).get("name", "Home")
    away = match.get("awayTeam", {}).get("name", "Away")
    utc_date = match.get("utcDate", "")[:10]
    
    # Mock/Poisson Probability Estimation Engine
    prob_home_or_draw = 0.72
    prob_away_or_draw = 0.68
    prob_over_15 = 0.74
    prob_home_plus_15 = 0.82
    prob_away_plus_15 = 0.80
    
    # Updated Confidence Thresholds
    DC_THRESHOLD = 0.70
    OVER_15_THRESHOLD = 0.65
    HANDICAP_THRESHOLD = 0.75

    picks = []
    if prob_home_or_draw >= DC_THRESHOLD:
        picks.append(f"1X ({prob_home_or_draw*100:.1f}%)")
    if prob_away_or_draw >= DC_THRESHOLD:
        picks.append(f"X2 ({prob_away_or_draw*100:.1f}%)")
    if prob_over_15 >= OVER_15_THRESHOLD:
        picks.append(f"Over 1.5 Goals ({prob_over_15*100:.1f}%)")
    if prob_home_plus_15 >= HANDICAP_THRESHOLD:
        picks.append(f"Home +1.5 ({prob_home_plus_15*100:.1f}%)")
    if prob_away_plus_15 >= HANDICAP_THRESHOLD:
        picks.append(f"Away +1.5 ({prob_away_plus_15*100:.1f}%)")

    picks_str = ", ".join(picks) if picks else "None"

    msg = (
        f"⚽ *ALL-MARKETS MATCH PREDICTION*\n\n"
        f"🏆 *League:* {league}\n"
        f"⚔️ *Match:* {home} vs {away}\n"
        f"📅 *Date:* {utc_date}\n\n"
        f"🔥 *HIGH-CONFIDENCE VALUE PICKS:*\n"
        f"👉 {picks_str}\n\n"
        f"🎯 *DOUBLE CHANCE*\n"
        f"• 1X: {prob_home_or_draw*100:.1f}% | X2: {prob_away_or_draw*100:.1f}%\n\n"
        f"🚩 *HANDICAP MARKETS*\n"
        f"• Home +1.5: {prob_home_plus_15*100:.1f}%\n"
        f"• Away +1.5: {prob_away_plus_15*100:.1f}%\n"
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
