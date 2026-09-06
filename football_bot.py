from datetime import datetime, timedelta, timezone
import json
import math
import os
import requests

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_LOG_FILE = "sent_alerts.json"
RETENTION_DAYS = 7

# Targeted Competition Codes (Football-Data.org supported tier codes)
TARGET_COMPETITIONS = [
    "PL",  # Premier League (England)
    "ELC",  # Championship (England)
    "PD",  # La Liga (Spain)
    "SA",  # Serie A (Italy)
    "BL1",  # Bundesliga (Germany)
    "FL1",  # Ligue 1 (France)
    "PPD",  # Primeira Liga (Portugal)
    "DED",  # Eredivisie (Netherlands)
    "CL",  # UEFA Champions League
    "EL",  # UEFA Europa League
    "CLI",  # Copa Libertadores
    "BSA",  # Brasileirão Série A
]


def load_and_clean_sent_alerts():
  if not os.path.exists(SENT_LOG_FILE):
    return {}
  try:
    with open(SENT_LOG_FILE, "r") as f:
      data = json.load(f)
  except json.JSONDecodeError:
    return {}

  current_time = datetime.now(timezone.utc)
  valid_alerts = {}
  for match_id, timestamp_str in data.items():
    try:
      alert_time = datetime.fromisoformat(timestamp_str)
      if current_time - alert_time < timedelta(days=RETENTION_DAYS):
        valid_alerts[match_id] = timestamp_str
    except ValueError:
      continue
  return valid_alerts


def save_sent_alerts(sent_alerts):
  with open(SENT_LOG_FILE, "w") as f:
    json.dump(sent_alerts, f, indent=2)


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

  btts_and_home = sum(
      prob for (h, a), prob in ft_matrix.items() if h > a and h > 0 and a > 0
  )
  btts_and_away = sum(
      prob for (h, a), prob in ft_matrix.items() if a > h and h > 0 and a > 0
  )

  home_hcap_minus_1_5 = sum(
      prob for (h, a), prob in ft_matrix.items() if (h - 1.5) > a
  )
  away_hcap_minus_1_5 = sum(
      prob for (h, a), prob in ft_matrix.items() if (a - 1.5) > h
  )

  penalty_prob = min(round((home_exp + away_exp) * 0.09 * 100, 1), 35.0)
  header_goal_prob = min(round((home_exp + away_exp) * 0.18 * 100, 1), 65.0)

  value_picks = []
  if dc_1x * 100 >= 75.0:
    value_picks.append(f"Double Chance 1X ({round(dc_1x * 100, 1)}%)")
  if dc_x2 * 100 >= 75.0:
    value_picks.append(f"Double Chance X2 ({round(dc_x2 * 100, 1)}%)")
  if ft_over_1_5 * 100 >= 70.0:
    value_picks.append(f"Over 1.5 Goals ({round(ft_over_1_5 * 100, 1)}%)")

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
      "btts_and_home": round(btts_and_home * 100, 1),
      "btts_and_away": round(btts_and_away * 100, 1),
      "home_hcap_1_5": round(home_hcap_minus_1_5 * 100, 1),
      "away_hcap_1_5": round(away_hcap_minus_1_5 * 100, 1),
      "penalty_prob": penalty_prob,
      "header_goal_prob": header_goal_prob,
      "value_picks": high_conf_str,
  }


def send_telegram_alert(message):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }
  requests.post(url, json=payload)


def main():
  sent_alerts = load_and_clean_sent_alerts()

  url = "https://api.football-data.org/v4/matches"
  headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

  response = requests.get(url, headers=headers)
  if response.status_code != 200:
    print(f"API Error: {response.status_code}")
    return

  matches = response.json().get("matches", [])
  new_alerts_count = 0

  HOME_EXP_GOALS = 1.45
  AWAY_EXP_GOALS = 1.15
  HT_HOME_EXP_GOALS = 0.65
  HT_AWAY_EXP_GOALS = 0.50

  for match in matches:
    match_id = str(match.get("id"))
    comp_code = match.get("competition", {}).get("code")

    # Filter out matches not in selected leagues
    if TARGET_COMPETITIONS and comp_code not in TARGET_COMPETITIONS:
      continue

    if match_id in sent_alerts:
      continue

    home_team = match.get("homeTeam", {}).get("name", "Home")
    away_team = match.get("awayTeam", {}).get("name", "Away")
    competition = match.get("competition", {}).get("name", "League")
    match_date = match.get("utcDate", "")[:10]

    res = calculate_comprehensive_predictions(
        HOME_EXP_GOALS, AWAY_EXP_GOALS, HT_HOME_EXP_GOALS, HT_AWAY_EXP_GOALS
    )

    alert_msg = (
        f"⚽ *ALL-MARKETS MATCH PREDICTION* ⚽\n\n"
        f"🏆 *League:* {competition}\n"
        f"⚔️ *Match:* {home_team} vs {away_team}\n"
        f"📅 *Date:* {match_date}\n\n"
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
    sent_alerts[match_id] = datetime.now(timezone.utc).isoformat()
    new_alerts_count += 1

  save_sent_alerts(sent_alerts)
  print(f"Done. Sent {new_alerts_count} multi-market prediction(s).")


if __name__ == "__main__":
  main()
