from datetime import datetime, timedelta, timezone
import json
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

  for match in matches:
    match_id = str(match.get("id"))

    if match_id in sent_alerts:
      continue

    home_team = match.get("homeTeam", {}).get("name", "Home")
    away_team = match.get("awayTeam", {}).get("name", "Away")
    competition = match.get("competition", {}).get("name", "League")
    match_date = match.get("utcDate", "")[:10]

    alert_msg = (
        f"🚨 *UPCOMING MATCH ALERT* 🚨\n\n"
        f"🏆 *League:* {competition}\n"
        f"⚽ *Match:* {home_team} vs {away_team}\n"
        f"📅 *Date:* {match_date}\n"
    )

    send_telegram_alert(alert_msg)
    sent_alerts[match_id] = datetime.now(timezone.utc).isoformat()
    new_alerts_count += 1

  save_sent_alerts(sent_alerts)
  print(f"Done. Sent {new_alerts_count} new alert(s).")


if __name__ == "__main__":
  main()
