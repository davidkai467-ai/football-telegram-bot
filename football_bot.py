from datetime import datetime, timedelta, timezone
import json
import math
import os
import time
import requests

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_LOG_FILE = "sent_alerts.json"

TARGET_COMPETITIONS = [
    "PL",
    "ELC",
    "PD",
    "SA",
    "BL1",
    "FL1",
    "PPL",
    "DED",
    "CL",
    "BSA",
]

STANDINGS_CACHE = {}


def load_sent_alerts():
  if not os.path.exists(SENT_LOG_FILE):
    return {}
  try:
    with open(SENT_LOG_FILE, "r") as f:
      return json.load(f)
  except json.JSONDecodeError:
    return {}


def save_sent_alerts(sent_alerts):
  with open(SENT_LOG_FILE, "w") as f:
    json.dump(sent_alerts, f, indent=2)


def get_league_standings(comp_code):
  if comp_code in STANDINGS_CACHE:
    return STANDINGS_CACHE[comp_code]

  url = f"https://api.football-data.org/v4/competitions/{comp_code}/standings"
  headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

  try:
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
      data = res.json()
      standings = data.get("standings", [])
      if standings:
        table = standings[0].get("table", [])
        team_stats = {}
        for row in table:
          team_id = row.get("team", {}).get("id")
          played = max(row.get("playedGames", 1), 1)
          gf = row.get("goalsFor", 0)
          ga = row.get("goalsAgainst", 0)
          team_stats[team_id] = {
              "avg_gf": gf / played,
              "avg_ga": ga / played,
          }
        STANDINGS_CACHE[comp_code] = team_stats
        return team_stats
  except Exception as e:
    print(f"Error fetching standings for {comp_code}: {e}")

  return {}


def calculate_dynamic_expected_goals(comp_code, home_team_id, away_team_id):
  stats = get_league_standings(comp_code)
  home_exp = 1.45
  away_exp = 1.15

  if stats and home_team_id in stats and away_team_id in stats:
    home_stat = stats[home_team_id]
    away_stat = stats[away_team_id]

    home_exp = max(
        0.4, round((home_stat["avg_gf"] + away_stat["avg_ga"]) / 2, 2)
    )
    away_exp = max(
        0.3, round((away_stat["avg_gf"] + home_stat["avg_ga"]) / 2, 2)
    )

  ht_home_exp = round(home_exp * 0.45, 2)
  ht_away_exp = round(away_exp * 0.45, 2)

  return home_exp, away_exp, ht_home_exp, ht_away_exp


def poisson_probability(k, lambd):
  return (math.pow(lambd, k) * math.exp(-lambd)) / math.factorial(k)


def calculate_comprehensive_predictions(
    home_exp, away_exp, ht_home_exp, ht_away_exp
):
  ft_matrix = {}
  ht_matrix = {}

  for h in range(7):
    for a in range(7):
      ft_matrix[(h, a)] = poisson_probability(h, home_exp) * poisson_probability(
          a, away_exp
      )

  for h in range(5):
    for a in range(5):
      ht_matrix[(h, a)] = poisson_probability(
          h, ht_home_exp
      ) * poisson_probability(a, ht_away_exp)

  p_ft_home = sum(prob for (h, a), prob in ft_matrix.items() if h > a)
  p_ft_draw = sum(prob for (h, a), prob in ft_matrix.items() if h == a)
  p_ft_away = sum(prob for (h, a), prob in ft_matrix.items() if h < a)

  dc_1x = p_ft_home + p_ft_draw
  dc_x2 = p_ft_away + p_ft_draw
  dc_12 = p_ft_home + p_ft_away

  p_ht_home = sum(prob for (h, a), prob in ht_matrix.items() if h > a)
  p_ht_draw = sum(prob for (h, a), prob in ht_matrix.items() if h == a)
  p_ht_away = sum(prob for (h, a), prob in ht_matrix.items() if h < a)

  ft_over_1_5 = sum(prob for (h, a), prob in ft_matrix.items() if h + a > 1.5)
  ft_over_2_5 = sum(prob for (h, a), prob in ft_matrix.items() if h + a > 2.5)
  ft_under_2_5 = 1.0 - ft_over_2_5

  ht_over_0_5 = sum(prob for (h, a), prob in ht_matrix.items() if h + a > 0.5)
  ht_under_1_5 = sum(prob for (h, a), prob in ht_matrix.items() if h + a < 1.5)

  ft_btts_yes = sum(
      prob for (h, a), prob in ft_matrix.items() if h > 0 and a > 0
  )
  ft_btts_no = 1.0 - ft_btts_yes
  ht_btts_yes = sum(
      prob for (h, a), prob in ht_matrix.items() if h > 0 and a > 0
  )

  penalty_prob = min(round((home_exp + away_exp) * 0.09 * 100, 1), 35.0)
  header_goal_prob = min(round((home_exp + away_exp) * 0.18 * 100, 1), 65.0)

  value_picks = []
  raw_picks = []
  if dc_1x * 100 >= 75.0:
    value_picks.append(f"Double Chance 1X ({round(dc_1x * 100, 1)}%)")
    raw_picks.append("1X")
  if dc_x2 * 100 >= 75.0:
    value_picks.append(f"Double Chance X2 ({round(dc_x2 * 100, 1)}%)")
    raw_picks.append("X2")
  if ft_over_1_5 * 100 >= 70.0:
    value_picks.append(f"Over 1.5 Goals ({round(ft_over_1_5 * 100, 1)}%)")
    raw_picks.append("OVER_1.5")

  high_conf_str = (
      ", ".join(value_picks) if value_picks else "No high-confidence pick"
  )

  return {
      "ft_home": round(p_ft_home * 100, 1),
      "ft_draw": round(p_ft_draw * 100, 1),
      "ft_away": round(p_ft_away * 100, 1),
      "dc_1x": round(dc_1x * 100, 1),
      "dc_x2": round(dc_x2 * 100, 1),
      "dc_12": round(dc_12 * 100, 1),
      "ht_home": round(p_ht_home * 100, 1),
      "ht_draw": round(p_ht_draw * 100, 1),
      "ht_away": round(p_ht_away * 100, 1),
      "ft_over_1_5": round(ft_over_1_5 * 100, 1),
      "ft_over_2_5": round(ft_over_2_5 * 100, 1),
      "ft_under_2_5": round(ft_under_2_5 * 100, 1),
      "ht_over_0_5": round(ht_over_0_5 * 100, 1),
      "ht_under_1_5": round(ht_under_1_5 * 100, 1),
      "ft_btts_yes": round(ft_btts_yes * 100, 1),
      "ft_btts_no": round(ft_btts_no * 100, 1),
      "ht_btts_yes": round(ht_btts_yes * 100, 1),
      "penalty_prob": penalty_prob,
      "header_goal_prob": header_goal_prob,
      "value_picks": high_conf_str,
      "raw_picks": raw_picks,
  }


def send_telegram_alert(message):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }
  requests.post(url, json=payload)


def verify_completed_matches(sent_alerts):
  """Checks finished matches from the last 3 days and posts win/loss results for alerts."""
  today = datetime.now(timezone.utc).date()
  date_from = (today - timedelta(days=3)).strftime("%Y-%m-%d")
  date_to = today.strftime("%Y-%m-%d")

  url = f"https://api.football-data.org/v4/matches?status=FINISHED&dateFrom={date_from}&dateTo={date_to}"
  headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

  try:
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
      print(f"Result Verification API Notice: HTTP {response.status_code}")
      return

    matches = response.json().get("matches", [])
    for match in matches:
      match_id = str(match.get("id"))

      if match_id in sent_alerts:
        data = sent_alerts[match_id]

        if not isinstance(data, dict):
          continue

        if data.get("status") == "VERIFIED":
          continue

        raw_picks = data.get("picks", [])
        if not raw_picks:
          data["status"] = "VERIFIED"
          continue

        home_goals = match.get("score", {}).get("fullTime", {}).get("home")
        away_goals = match.get("score", {}).get("fullTime", {}).get("away")

        if home_goals is None or away_goals is None:
          continue

        home_team = match.get("homeTeam", {}).get("name")
        away_team = match.get("awayTeam", {}).get("name")
        total_goals = home_goals + away_goals

        results_summary = []
        for pick in raw_picks:
          won = False
          if pick == "1X" and (home_goals >= away_goals):
            won = True
          elif pick == "X2" and (away_goals >= home_goals):
            won = True
          elif pick == "OVER_1.5" and total_goals > 1.5:
            won = True

          status_icon = "✅ WIN" if won else "❌ LOSS"
          results_summary.append(f"• Pick: *{pick}* -> {status_icon}")

        msg = (
            f"📊 *MATCH RESULT VERIFICATION* 📊\n\n"
            f"⚔️ *Match:* {home_team} {home_goals} - {away_goals} {away_team}\n"
            f"📝 *Predictions Outcome:*\n"
            + "\n".join(results_summary)
        )

        send_telegram_alert(msg)
        sent_alerts[match_id]["status"] = "VERIFIED"
        time.sleep(6)

  except Exception as e:
    print(f"Error checking results: {e}")


def main():
  sent_alerts = load_sent_alerts()

  # Step 1: Check past predictions outcomes
  verify_completed_matches(sent_alerts)

  # Step 2: Fetch upcoming matches
  url = "https://api.football-data.org/v4/matches"
  headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

  response = requests.get(url, headers=headers)
  if response.status_code != 200:
    print(f"API Error: {response.status_code}")
    return

  matches = response.json().get("matches", [])
  new_alerts_count = 0

  for match in matches:
    match_id = str(match.get("id"))
    comp_code = match.get("competition", {}).get("code")

    if TARGET_COMPETITIONS and comp_code not in TARGET_COMPETITIONS:
      continue

    if match_id in sent_alerts:
      continue

    home_team_id = match.get("homeTeam", {}).get("id")
    away_team_id = match.get("awayTeam", {}).get("id")
    home_team = match.get("homeTeam", {}).get("name", "Home")
    away_team = match.get("awayTeam", {}).get("name", "Away")
    competition = match.get("competition", {}).get("name", "League")
    match_date = match.get("utcDate", "")[:10]

    h_exp, a_exp, ht_h_exp, ht_a_exp = calculate_dynamic_expected_goals(
        comp_code, home_team_id, away_team_id
    )

    res = calculate_comprehensive_predictions(
        h_exp, a_exp, ht_h_exp, ht_a_exp
    )

    alert_msg = (
        f"⚽ *ALL-MARKETS MATCH PREDICTION* ⚽\n\n"
        f"🏆 *League:* {competition}\n"
        f"⚔️ *Match:* {home_team} vs {away_team}\n"
        f"📅 *Date:* {match_date}\n"
        f"📊 *Expected Goals (xG):* {h_exp} - {a_exp}\n\n"
        f"🔥 *HIGH-CONFIDENCE VALUE PICK:*\n"
        f"👉 *{res['value_picks']}*\n\n"
        f"🎯 *FULL TIME 1X2*\n"
        f"• Home Win (1): *{res['ft_home']}%*\n"
        f"• Draw (X): *{res['ft_draw']}%*\n"
        f"• Away Win (2): *{res['ft_away']}%*\n\n"
        f"🛡️ *DOUBLE CHANCE*\n"
        f"• 1X: *{res['dc_1x']}%* | X2: *{res['dc_x2']}%* | 12: *{res['dc_12']}%*\n\n"
        f"⏱️ *HALF TIME 1X2*\n"
        f"• HT Home: *{res['ht_home']}%* | HT Draw: *{res['ht_draw']}%* | HT Away: *{res['ht_away']}%*\n\n"
        f"⚽ *OVERS / UNDERS*\n"
        f"• FT Over 1.5: *{res['ft_over_1_5']}%*\n"
        f"• FT Over 2.5: *{res['ft_over_2_5']}%* | FT Under 2.5: *{res['ft_under_2_5']}%*\n"
        f"• HT Over 0.5: *{res['ht_over_0_5']}%* | HT Under 1.5: *{res['ht_under_1_5']}%*\n\n"
        f"🔄 *BOTH TEAMS TO SCORE (BTTS)*\n"
        f"• FT BTTS Yes: *{res['ft_btts_yes']}%* | FT BTTS No: *{res['ft_btts_no']}%*\n"
        f"• HT BTTS Yes: *{res['ht_btts_yes']}%*\n\n"
        f"⚡ *MATCH PROPS*\n"
        f"• Penalty Awarded: *{res['penalty_prob']}%*\n"
        f"• Header Goal Scored: *{res['header_goal_prob']}%*"
    )

    send_telegram_alert(alert_msg)

    sent_alerts[match_id] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "picks": res["raw_picks"],
        "status": "PENDING",
    }
    new_alerts_count += 1

    time.sleep(6)

  save_sent_alerts(sent_alerts)
  print(f"Done. Sent {new_alerts_count} new match prediction(s).")


if __name__ == "__main__":
  main()
