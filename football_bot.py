import os
import json
import datetime
import math
import requests

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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
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

def fetch_head_to_head(match_id):
    if not FOOTBALL_DATA_API_KEY:
        return None
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    url = f"https://api.football-data.org/v4/matches/{match_id}/head2head?limit=5"
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        return res.json()
    return None

def fetch_team_form_multiplier(team_id):
    if not FOOTBALL_DATA_API_KEY or not team_id:
        return 1.0
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    url = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=5"
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return 1.0
    
    data = res.json()
    matches = data.get("matches", [])
    if not matches:
        return 1.0

    pts = 0
    for m in matches:
        ft = m.get("score", {}).get("fullTime", {})
        h_goals = ft.get("home", 0) or 0
        a_goals = ft.get("away", 0) or 0
        is_home = m.get("homeTeam", {}).get("id") == team_id
        
        if is_home:
            if h_goals > a_goals: pts += 3
            elif h_goals == a_goals: pts += 1
        else:
            if a_goals > h_goals: pts += 3
            elif h_goals == a_goals: pts += 1

    total_pts_possible = len(matches) * 3
    pts_pct = pts / total_pts_possible if total_pts_possible > 0 else 0.5
    return round(0.85 + (pts_pct * 0.30), 2)

def negative_binomial_prob(mu, k, dispersion=0.2):
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    r = 1.0 / dispersion
    try:
        coef = math.gamma(k + r) / (math.factorial(k) * math.gamma(r))
        p_val = coef * (r / (r + mu))**r * (mu / (r + mu))**k
        return max(0.0, p_val)
    except Exception:
        return (math.pow(mu, k) * math.exp(-mu)) / math.factorial(k)

def dixon_coles_tau(x, y, home_xg, away_xg, rho=-0.11):
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

def calculate_dynamic_xg_with_h2h(match):
    league_avg_home_goals = 1.48
    league_avg_away_goals = 1.18

    home_team = match.get("homeTeam", {})
    away_team = match.get("awayTeam", {})
    home_id = home_team.get("id")
    away_id = away_team.get("id")
    match_id = match.get("id")

    home_mult = fetch_team_form_multiplier(home_id)
    away_mult = fetch_team_form_multiplier(away_id)

    h2h_data = fetch_head_to_head(match_id)
    h2h_home_boost = 1.0
    h2h_away_boost = 1.0

    if h2h_data and "aggregates" in h2h_data:
        agg = h2h_data["aggregates"]
        total_matches = agg.get("numberOfMatches", 0)
        if total_matches > 0:
            home_wins = agg.get("homeTeam", {}).get("wins", 0)
            away_wins = agg.get("awayTeam", {}).get("wins", 0)
            
            home_h2h_ratio = home_wins / total_matches
            away_h2h_ratio = away_wins / total_matches
            
            h2h_home_boost = 0.92 + (home_h2h_ratio * 0.16)
            h2h_away_boost = 0.92 + (away_h2h_ratio * 0.16)

    home_xg = max(0.40, league_avg_home_goals * home_mult * h2h_home_boost)
    away_xg = max(0.30, league_avg_away_goals * away_mult * h2h_away_boost)

    return round(home_xg, 2), round(away_xg, 2)

def generate_prediction_message(match):
    league = match.get("competition", {}).get("name", "Unknown League")
    home_data = match.get("homeTeam", {})
    away_data = match.get("awayTeam", {})
    home = home_data.get("name", "Home")
    away = away_data.get("name", "Away")
    utc_date = match.get("utcDate", "")[:10]
    
    home_xg, away_xg = calculate_dynamic_xg_with_h2h(match)

    prob_matrix = {}
    total_prob = 0.0

    for h in range(8):
        for a in range(8):
            p_raw = negative_binomial_prob(home_xg, h) * negative_binomial_prob(away_xg, a)
            tau = dixon_coles_tau(h, a, home_xg, away_xg)
            p_adj = max(0.0, p_raw * tau)
            prob_matrix[(h, a)] = p_adj
            total_prob += p_adj

    for key in prob_matrix:
        prob_matrix[key] /= total_prob

    prob_home_win = sum(p for (h, a), p in prob_matrix.items() if h > a)
    prob_draw = sum(p for (h, a), p in prob_matrix.items() if h == a)
    prob_away_win = sum(p for (h, a), p in prob_matrix.items() if a > h)

    prob_home_or_draw = prob_home_win + prob_draw
    prob_away_or_draw = prob_away_win + prob_draw

    prob_over_15 = sum(p for (h, a), p in prob_matrix.items() if (h + a) > 1.5)
    prob_under_35 = sum(p for (h, a), p in prob_matrix.items() if (h + a) < 3.5)

    combo_12_over15 = sum(p for (h, a), p in prob_matrix.items() if (h != a) and ((h + a) > 1.5))
    combo_x2_over05 = sum(p for (h, a), p in prob_matrix.items() if (a >= h) and ((h + a) > 0.5))
    combo_x2_over15 = sum(p for (h, a), p in prob_matrix.items() if (a >= h) and ((h + a) > 1.5))
    combo_btts_x2 = sum(p for (h, a), p in prob_matrix.items() if (h > 0 and a > 0) and (a >= h))
    combo_btts_12 = sum(p for (h, a), p in prob_matrix.items() if (h > 0 and a > 0) and (h != a))
    combo_btts_over25 = sum(p for (h, a), p in prob_matrix.items() if (h > 0 and a > 0) and ((h + a) > 2.5))

    candidates = []

    # --- ADJUSTED THRESHOLDS FOR DIVERSE & ACCURATE PICKS ---
    if prob_home_or_draw >= 0.68:
        candidates.append(("1X", prob_home_or_draw, f"1X ({prob_home_or_draw*100:.1f}%)"))
    if prob_away_or_draw >= 0.68:
        candidates.append(("X2", prob_away_or_draw, f"X2 ({prob_away_or_draw*100:.1f}%)"))
    if prob_over_15 >= 0.65:
        candidates.append(("Over 1.5 Goals", prob_over_15, f"Over 1.5 Goals ({prob_over_15*100:.1f}%)"))
        
    # STRICTER THRESHOLD FOR UNDER 3.5 GOALS (BUMPED FROM 0.70 TO 0.82)
    if prob_under_35 >= 0.82:
        candidates.append(("Under 3.5 Goals", prob_under_35, f"Under 3.5 Goals ({prob_under_35*100:.1f}%)"))

    if combo_12_over15 >= 0.52:
        candidates.append(("12 & Over 1.5 Goals", combo_12_over15, f"{home}/{away} & Over 1.5 Goals ({combo_12_over15*100:.1f}%)"))
    if combo_x2_over05 >= 0.52:
        candidates.append(("X2 & Over 0.5 Goals", combo_x2_over05, f"Draw/{away} & Over 0.5 Goals ({combo_x2_over05*100:.1f}%)"))
    if combo_x2_over15 >= 0.52:
        candidates.append(("X2 & Over 1.5 Goals", combo_x2_over15, f"Draw/{away} & Over 1.5 Goals ({combo_x2_over15*100:.1f}%)"))
    if combo_btts_x2 >= 0.52:
        candidates.append(("BTTS & X2", combo_btts_x2, f"BTTS Yes & Draw/{away} ({combo_btts_x2*100:.1f}%)"))
    if combo_btts_12 >= 0.52:
        candidates.append(("BTTS & 12", combo_btts_12, f"BTTS Yes & {home}/{away} ({combo_btts_12*100:.1f}%)"))
    if combo_btts_over25 >= 0.52:
        candidates.append(("BTTS & Over 2.5 Goals", combo_btts_over25, f"BTTS Yes & Over 2.5 Goals ({combo_btts_over25*100:.1f}%)"))

    formatted_picks = [c[2] for c in candidates]
    verification_keys = [c[0] for c in candidates]

    picks_str = ", ".join(formatted_picks) if formatted_picks else "None"

    best_pick_key = None
    if candidates:
        top_pick = max(candidates, key=lambda x: x[1])
        best_pick_key = top_pick[0]
        best_pick_str = f"🎯 *BEST VALUE PICK:* {top_pick[2]}"
    else:
        best_pick_str = "🎯 *BEST VALUE PICK:* No high-confidence pick found"

    msg = (
        f"⚽ *MATCH ANALYSIS & VALUE PICKS [NB]*\n\n"
        f"🏆 *League:* {league}\n"
        f"⚔️ *Match:* {home} vs {away}\n"
        f"📅 *Date:* {utc_date}\n"
        f"📊 *xG [H2H + NegBinom]:* {home_xg:.2f} - {away_xg:.2f}\n\n"
        f"🔥 *HIGH-CONFIDENCE VALUE PICKS:*\n"
        f"👉 {picks_str}\n\n"
        f"{best_pick_str}\n"
    )
    return msg, verification_keys, best_pick_key, home, away, league

def evaluate_single_pick(pick, home_goals, away_goals):
    total_goals = home_goals + away_goals
    if pick == "1X" and home_goals >= away_goals:
        return True
    elif pick == "X2" and away_goals >= home_goals:
        return True
    elif pick == "Over 1.5 Goals" and total_goals > 1.5:
        return True
    elif pick == "Under 3.5 Goals" and total_goals < 3.5:
        return True
    elif pick == "BTTS & X2" and (home_goals > 0 and away_goals > 0) and (away_goals >= home_goals):
        return True
    elif pick == "BTTS & 12" and (home_goals > 0 and away_goals > 0) and (home_goals != away_goals):
        return True
    elif pick == "12 & Over 1.5 Goals" and (home_goals != away_goals) and (total_goals > 1.5):
        return True
    elif pick == "X2 & Over 0.5 Goals" and (away_goals >= home_goals) and (total_goals > 0.5):
        return True
    elif pick == "X2 & Over 1.5 Goals" and (away_goals >= home_goals) and (total_goals > 1.5):
        return True
    elif pick == "BTTS & Over 2.5 Goals" and (home_goals > 0 and away_goals > 0) and (total_goals > 2.5):
        return True
    return False

def verify_predictions(picks, best_pick, home_goals, away_goals):
    results = []
    best_pick_result_str = None

    for pick in picks:
        won = evaluate_single_pick(pick, home_goals, away_goals)
        status_emoji = "✅ WON" if won else "❌ LOST"
        
        if best_pick and pick == best_pick:
            best_pick_result_str = f"🎯 *BEST VALUE PICK ({pick}):* {status_emoji}"
            results.append(f"• *{pick} (Best Pick)*: {status_emoji}")
        else:
            results.append(f"• {pick}: {status_emoji}")

    verification_body = "\n".join(results) if results else "• No active high-confidence picks."
    
    if best_pick_result_str:
        header = f"{best_pick_result_str}\n\n📋 *All High-Confidence Picks:*\n"
    else:
        header = "📋 *High-Confidence Pick Verification:*\n"

    return header + verification_body

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
                    best_pick = info.get("best_pick")
                    verification_summary = verify_predictions(stored_picks, best_pick, home_goals, away_goals)

                    result_msg = (
                        f"🏁 *MATCH RESULT & PICK VERIFICATION*\n\n"
                        f"🏆 *League:* {league}\n"
                        f"⚔️ *Match:* {home_team} {home_goals} - {away_goals} {away_team}\n"
                        f"📌 *Status:* FINISHED\n\n"
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

        msg, picks, best_pick, home_name, away_name, league_name = generate_prediction_message(match)
        if send_telegram_message(msg):
            sent_alerts[match_id] = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "status": "SENT",
                "home": home_name,
                "away": away_name,
                "league": league_name,
                "picks": picks,
                "best_pick": best_pick
            }
            sent_count += 1

    save_sent_alerts(sent_alerts)
    print(f"Done. Processed results and sent {sent_count} new match prediction(s).")

if __name__ == "__main__":
    main()
