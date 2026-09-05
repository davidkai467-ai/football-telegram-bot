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
  """Calculates Poisson probability of k goals given average lambda."""
  return (math.pow(lambd, k) * math.exp(-lambd)) / math.factorial(k)


def calculate_match_prediction(home_goals_avg, away_goals_avg, max_goals=5):
  """Estimates Home Win, Draw, Away Win probabilities using Poisson distribution."""
  prob_home, prob_draw, prob_away = 0.0, 0.0, 0.0

  for h in range(max_goals + 1):
    for a in range(max_goals + 1):
      p = poisson_probability(h, home_goals_avg) * poisson_probability(
          a, away_goals_avg
      )
      if h > a:
        prob_home += p
      elif h == a:
        prob_draw += p
      else:
        prob_away += p

  total = prob_home + prob_draw + prob_away
  return (
      round((prob_home / total) * 100, 1),
      round((prob_draw / total) * 100, 1),
      round((prob_away / total) * 100, 1),
  )


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

  HOME_GOAL_AVG = 1.45
  AWAY_GOAL_AVG = 1.15

  for match in matches:
    match_id = str(match.get("id"))

    if match_id in sent_alerts:
      continue

    home_team = match.get("homeTeam", {}).get("name", "Home")
    away_team = match.get("awayTeam", {}).get("name", "Away")
    competition = match.get("competition", {}).get("name", "League")
    match_date = match.get("utcDate", "")[:10]

    p_home, p_draw, p_away = calculate_match_prediction(
        HOME_GOAL_AVG, AWAY_GOAL_AVG
    )

    if p_home >= p_away and p_home >= p_draw:
      tip = f"1 ({home_team})"
    elif p_away >= p_home and p_away >= p_draw:
      tip = f"2 ({away_team})"
    else:
      tip = "X (Draw)"

    alert_msg = (
        f"⚽ *MATCH PREDICTION ALERT* ⚽\n\n"
        f"🏆 *League:* {competition}\n"
        f"⚔️ *Match:* {home_team} vs {away_team}\n"
        f"📅 *Date:* {match_date}\n\n"
        f"📊 *Probabilities:*\n"
        f"• Home Win: *{p_home}%*\n"
        f"• Draw: *{p_draw}%*\n"
        f"• Away Win: *{p_away}%*\n\n"
        f"🎯 *Predicted Outcome:* *{tip}*"
    )

    send_telegram_alert(alert_msg)
    sent_alerts[match_id] = datetime.now(timezone.utc).isoformat()
    new_alerts_count += 1

  save_sent_alerts(sent_alerts)
  print(f"Done. Sent {new_alerts_count} new prediction(s).")


if __name__ == "__main__":
  main()
