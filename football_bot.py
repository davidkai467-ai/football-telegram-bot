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
    end_date = (datetime.datetime.utcnow() + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    
    url = f"https://api.football-data.org/v4/matches?dateFrom={today}&dateTo={end_date}"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"API Error: {response.status_code} - {response.text}")
        return []
        
    data = response.json()
    matches = data.get("matches", [])
    
    if TARGET_COMPETITIONS:
        matches = [m for m in matches if m.get("competition", {}).get("code") in TARGET_COMPETITIONS]
        
    return matches

def fetch_match_details(match_id):
    if not FOOTBALL_DATA_API_KEY:
        return None
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    url = f"https://api.football-data.org/v4/matches/{match_id}"
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        return res.json()
    return None

def poisson_prob(lmbda, k):
    return (math.pow(lmbda, k) * math.exp(-lmbda)) / math.factorial(k)

def dixon_coles_tau(x, y, home_xg, away_xg, rho=-0.11):
    """
    Dixon-Coles adjustment parameter (rho) for low scorelines:
    (0,0), (1,0), (0,1), (1,1)
    """
    if x == 0 and y == 0:
        return 1.0 - (home_xg * away_xg * rho)
    elif x == 1 and y == 0:
        return 1.0 + (away_xg * rho)
    elif x == 0 and y == 1:
        return 1.0 + (home_xg * rho)
    elif x == 1 and y == 1:
        return 1.0 - rho
    else:
        return 1.0

def calculate_dynamic_xg(home_team, away_team):
    """
    Dynamic xG estimation with form adjustment multiplier based on recent match stats.
    """
    league_avg_home_goals = 1.48
    league_avg_away_goals = 1.18

    # Form/strength multipliers derived from recent performance metrics
    home_form = home_team.get("form", "") or ""
    away_form = away_team.get("form", "") or ""

    def parse_form_multiplier(form_str):
        if not form_str:
            return 1.0
        wins = form_str.count("W")
        draws = form_str.count("D")
        losses = form_str.count("L")
        total = len(form_str) if len(form_str) > 0 else 1
        pts_pct = (wins * 3 + draws * 1) / (total * 3)
        # Scale between 0.88x and 1.12x
        return 0.88 + (pts_pct * 0.24)

    home_mult = parse_form_multiplier(home_form)
    away_mult = parse_form_multiplier(away_form)

    # Base xG boosted or softened by momentum form
    home_xg = max(0.40, league_avg_home_goals * home_mult)
    away_xg = max(0.30, league_avg_away_goals * away_mult)

    return round(home_xg, 2), round(away_xg, 2)

def generate_prediction_message(match):
    league = match.get("competition", {}).get("name", "Unknown League")
    home_data = match.get("homeTeam", {})
    away_data = match.get("awayTeam", {})
    home = home_data.get("name", "Home")
    away = away_data.get("name", "Away")
    utc_date = match.get("utcDate", "")[:10]
    
    # 1. Calculate Dynamic xG with Form Adjustments
    home_xg, away_xg = calculate_dynamic_xg(home_data, away_data)

    # 2. Build Dixon-Coles Adjusted Probability Matrix (Scores up to 7-7)
    prob_matrix = {}
    total_prob = 0.0

    for h in range(8):
        for a in range(8):
            p_raw = poisson_prob(home_xg, h) * poisson_prob(away_xg, a)
            tau = dixon_coles_tau(h, a, home_xg, away_xg)
            p_adj = max(0.0, p_raw * tau)
            prob_matrix[(h, a)] = p_adj
            total_prob += p_adj

    # Normalize matrix to ensure total probability equals 1.0
    for key in prob_matrix:
        prob_matrix[key] /= total_prob

    # 3. Aggregate Probabilities Across Markets
    prob_home_win = sum(p for (h, a), p in prob_matrix.items() if h > a)
    prob_draw = sum(p for (h, a), p in prob_matrix.items() if h == a)
    prob_away_win = sum(p for (h, a), p in prob_matrix.items() if a > h)

    prob_home_or_draw = prob_home_win + prob_draw
    prob_away_or_draw = prob_away_win + prob_draw

    prob_over_15 = sum(p for (h, a), p in prob_matrix.items() if (h + a) > 1.5)
    prob_under_35 = sum(p for (h, a), p in prob_matrix.items() if (h + a) < 3.5)

    prob_home_plus_15 = sum(p for (h, a), p in prob_matrix.items() if (h + 1.5) > a)
    prob_away_plus_15 = sum(p for (h, a), p in prob_matrix.items() if (a + 1.5) > h)

    prob_btts_yes = sum(p for (h, a), p in prob_matrix.items() if h > 0 and a > 0)

    # Thresholds for High-Confidence Value Picks
    DC_THRESHOLD = 0.70
    GOALS_CONFIDENCE_THRESHOLD = 0.65
    HANDICAP_THRESHOLD = 0.75
    BTTS_THRESHOLD = 0.60

    candidates = []

    if prob_home_or_draw >= DC_THRESHOLD:
        candidates.append(("1X", prob_home_or_draw, f"1X ({prob_home_or_draw*100:.1f}%)"))
    if prob_away_or_draw >= DC_THRESHOLD:
        candidates.append(("X2", prob_away_or_draw, f"X2 ({prob_away_or_draw*100:.1f}%)"))
    if prob_over_15 >= GOALS_CONFIDENCE_THRESHOLD:
        candidates.append(("Over 1.5 Goals", prob_over_15, f"Over 1.5 Goals ({prob_over_15*100:.1f}%)"))
    if prob_under_35 >= GOALS_CONFIDENCE_THRESHOLD:
        candidates.append(("Under 3.5 Goals", prob_under_35, f"Under 3.5 Goals ({prob_under_35*100:.1f}%)"))
    if prob_home_plus_15 >= HANDICAP_THRESHOLD:
        candidates.append(("Home +1.5", prob_home_plus_15, f"Home +1.5 ({prob_home_plus_15*100:.1f}%)"))
    if prob_away_plus_15 >= HANDICAP_THRESHOLD:
        candidates.append(("Away +1.5", prob_away_plus_15, f"Away +1.5 ({prob_away_plus_15*100:.1f}%)"))
    if prob_btts_yes >= BTTS_THRESHOLD:
        candidates.append(("BTTS Yes", prob_btts_yes, f"BTTS Yes ({prob_btts_yes*100:.1f}%)"))

    formatted_picks = [c[2] for c in candidates]
    verification_keys = [c[0] for c in candidates]

    picks_str = ", ".join(formatted_picks) if formatted_picks else "None"

    if candidates:
        top_pick = max(candidates, key=lambda x: x[1])
        best_pick_str = f"🎯 *BEST VALUE PICK:* {top_pick[2]}"
    else:
        best_pick_str = "🎯 *BEST VALUE PICK:* No high-confidence pick found"

    msg = (
        f"⚽ *MATCH ANALYSIS & VALUE PICKS*\n\n"
        f"🏆 *League:* {league}\n"
        f"⚔️ *Match:* {home} vs {away}\n"
        f"📅 *Date:* {utc_date}\n"
        f"📊 *Expected Goals (xG):* {home_xg:.2f} - {away_xg:.2f}\n\n"
        f"🔥 *HIGH-CONFIDENCE VALUE PICKS:*\n"
        f"👉 {picks_str}\n\n"
        f"{best_pick_str}\n"
    )
    return msg, verification_keys, home, away, league

def verify_predictions(picks, home_goals, away_goals):
    results = []
    total_goals = home_goals + away_goals

    for pick in picks:
        won = False
        if pick == "1X" and home_goals >= away_goals:
            won = True
        elif pick == "X2" and away_goals >= home_goals:
            won = True
        elif pick == "Over 1.5 Goals" and total_goals > 1.5:
            won = True
        elif pick == "Under 3.5 Goals" and total_goals < 3.5:
            won = True
        elif pick == "Home +1.5" and (home_goals + 1.5) > away_goals:
            won = True
        elif pick == "Away +1.5" and (away_goals + 1.5) > home_goals:
            won = True
        elif pick == "BTTS Yes" and home_goals > 0 and away_goals > 0:
            won = True

        status_emoji = "✅ WON" if won else "❌ LOST"
        results.append(f"• {pick}: {status_emoji}")

    return "\n".join(results) if results else "• No active high-confidence picks."

def process_match_results(sent_alerts):
    updated = False
    for match_id, info in list(sent_alerts.items()):
        if info.get("status") == "SENT":
            details = fetch_match_details(match_id)
            if not details:
                continue

            status = details.get("status")
            if status == "FINISHED":
                full_time = details.get("score", {}).get("fullTime", {})
                home_goals = full_time.get("home")
                away_goals = full_time.get("away")

                if home_goals is not None and away_goals is not None:
                    home_team = details.get("homeTeam", {}).get("name", "Home")
                    away_team = details.get("awayTeam", {}).get("name", "Away")
                    league = details.get("competition", {}).get("name", "League")

                    stored_picks = info.get("picks", [])
                    verification_summary = verify_predictions(stored_picks, home_goals, away_goals)

                    result_msg = (
                        f"🏁 *MATCH RESULT & PICK VERIFICATION*\n\n"
                        f"🏆 *League:* {league}\n"
                        f"⚔️ *Match:* {home_team} {home_goals} - {away_goals} {away_team}\n"
                        f"📌 *Status:* FINISHED\n\n"
                        f"📊 *High-Confidence Pick Verification:*\n"
                        f"{verification_summary}\n"
                    )

                    if send_telegram_message(result_msg):
                        sent_alerts[match_id]["status"] = "VERIFIED"
                        sent_alerts[match_id]["score"] = f"{home_goals}-{away_goals}"
                        updated = True

    return updated

def main():
    sent_alerts = load_sent_alerts()

    process_match_results(sent_alerts)

    matches = fetch_upcoming_matches()
    sent_count = 0

    for match in matches:
        match_id = str(match.get("id"))
        if match_id in sent_alerts:
            continue

        msg, picks, home_name, away_name, league_name = generate_prediction_message(match)
        if send_telegram_message(msg):
            sent_alerts[match_id] = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "status": "SENT",
                "home": home_name,
                "away": away_name,
                "league": league_name,
                "picks": picks
            }
            sent_count += 1

    save_sent_alerts(sent_alerts)
    print(f"Done. Processed results and sent {sent_count} new match prediction(s).")

if __name__ == "__main__":
    main()
