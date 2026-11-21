import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
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

API_BASE = "https://api.football-data.org/v4"
HISTORY_DAYS = 365
HALF_LIFE_DAYS = 120
PRIOR_MATCHES = 6.0
MAX_GOALS = 7
ROLLING_WINDOW = 20

# Format: "Team Name": {"att_mult": 0.85, "def_mult": 1.10, "notes": "Missing key player"}
INJURY_OVERRIDES = {}

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SENT_ALERTS_FILE = "sent_alerts.json"

session = requests.Session()
if FOOTBALL_DATA_API_KEY:
    session.headers.update({"X-Auth-Token": FOOTBALL_DATA_API_KEY})


def parse_utc_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_sent_alerts():
    if os.path.exists(SENT_ALERTS_FILE):
        try:
            with open(SENT_ALERTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_sent_alerts(alerts):
    tmp_path = f"{SENT_ALERTS_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, SENT_ALERTS_FILE)


def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing; skipping message.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=20)
        return res.status_code == 200 and res.json().get("ok", False)
    except Exception as exc:
        print(f"Telegram send error: {exc}")
        return False


def api_get(path, params=None):
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    for attempt in range(3):
        try:
            res = session.get(url, params=params, timeout=25)
            if res.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"Rate limit hit; sleeping {wait}s...")
                time.sleep(wait)
                continue
            res.raise_for_status()
            return res.json()
        except requests.RequestException as exc:
            print(f"API request failed ({attempt + 1}/3): {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def fetch_upcoming_matches(days_ahead=5):
    today = datetime.now(timezone.utc).date()
    data = api_get(
        "/matches",
        params={
            "dateFrom": today.isoformat(),
            "dateTo": (today + timedelta(days=days_ahead)).isoformat(),
        },
    )
    if not data:
        return []
    return [
        m
        for m in data.get("matches", [])
        if m.get("competition", {}).get("code") in TARGET_COMPETITIONS
        and m.get("status") in {"SCHEDULED", "TIMED"}
    ]


def fetch_match_details(match_id):
    return api_get(f"/matches/{match_id}")


def fetch_competition_history(code):
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=HISTORY_DAYS)
    data = api_get(
        f"/competitions/{code}/matches",
        params={
            "dateFrom": start.isoformat(),
            "dateTo": today.isoformat(),
            "status": "FINISHED",
        },
    )
    if not data:
        return []

    matches = []
    for match in data.get("matches", []):
        score = match.get("score", {}).get("fullTime", {})
        h_goals = score.get("home")
        a_goals = score.get("away")
        kickoff = parse_utc_date(match.get("utcDate"))

        if (
            h_goals is None
            or a_goals is None
            or kickoff is None
            or not match.get("homeTeam", {}).get("id")
            or not match.get("awayTeam", {}).get("id")
        ):
            continue

        match["_kickoff"] = kickoff
        match["_home_goals"] = int(h_goals)
        match["_away_goals"] = int(a_goals)
        matches.append(match)

    matches.sort(key=lambda m: m["_kickoff"])
    return matches


def calculate_rest_days(history, team_id, kickoff):
    prev_games = [
        m["_kickoff"]
        for m in history
        if m["_kickoff"] < kickoff
        and (
            m.get("homeTeam", {}).get("id") == team_id
            or m.get("awayTeam", {}).get("id") == team_id
        )
    ]
    if not prev_games:
        return 7.0
    days = (kickoff - max(prev_games)).total_seconds() / 86400.0
    return max(1.0, days)


def get_fatigue_multiplier(rest_days):
    if rest_days < 4.0:
        return 0.88, 1.08
    if rest_days < 5.0:
        return 0.94, 1.04
    return 1.00, 1.00


def weighted_team_rate(matches, team_id, venue, metric, league_average, as_of):
    weighted_goals = 0.0
    total_weight = 0.0

    for match in matches:
        kickoff = match["_kickoff"]
        if kickoff >= as_of:
            continue

        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")

        if venue == "home" and home_id == team_id:
            goals = match["_home_goals"] if metric == "scored" else match["_away_goals"]
        elif venue == "away" and away_id == team_id:
            goals = match["_away_goals"] if metric == "scored" else match["_home_goals"]
        else:
            continue

        age_days = max(0.0, (as_of - kickoff).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        weighted_goals += goals * weight
        total_weight += weight

    return (weighted_goals + PRIOR_MATCHES * league_average) / (total_weight + PRIOR_MATCHES)


def estimate_goal_rates(history, home_name, away_name, home_id, away_id, kickoff):
    usable = [m for m in history if m["_kickoff"] < kickoff]
    if len(usable) < 20:
        return 1.48, 1.18, "fallback", len(usable), 7.0, 7.0

    total_weight = 0.0
    home_g_w = 0.0
    away_g_w = 0.0

    for match in usable:
        age_days = max(0.0, (kickoff - match["_kickoff"]).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        total_weight += weight
        home_g_w += match["_home_goals"] * weight
        away_g_w += match["_away_goals"] * weight

    if total_weight <= 0:
        return 1.48, 1.18, "fallback", len(usable), 7.0, 7.0

    league_home_avg = home_g_w / total_weight
    league_away_avg = away_g_w / total_weight

    home_attack = weighted_team_rate(usable, home_id, "home", "scored", league_home_avg, kickoff)
    home_defence = weighted_team_rate(usable, home_id, "home", "conceded", league_away_avg, kickoff)
    away_attack = weighted_team_rate(usable, away_id, "away", "scored", league_away_avg, kickoff)
    away_defence = weighted_team_rate(usable, away_id, "away", "conceded", league_home_avg, kickoff)

    home_rest = calculate_rest_days(usable, home_id, kickoff)
    away_rest = calculate_rest_days(usable, away_id, kickoff)
    home_att_mult, home_def_mult = get_fatigue_multiplier(home_rest)
    away_att_mult, away_def_mult = get_fatigue_multiplier(away_rest)

    home_inj = INJURY_OVERRIDES.get(home_name, {"att_mult": 1.0, "def_mult": 1.0})
    away_inj = INJURY_OVERRIDES.get(away_name, {"att_mult": 1.0, "def_mult": 1.0})

    eff_home_att = home_attack * home_att_mult * home_inj.get("att_mult", 1.0)
    eff_home_def = home_defence * home_def_mult * home_inj.get("def_mult", 1.0)
    eff_away_att = away_attack * away_att_mult * away_inj.get("att_mult", 1.0)
    eff_away_def = away_defence * away_def_mult * away_inj.get("def_mult", 1.0)

    expected_home = (eff_home_att * eff_away_def) / max(league_home_avg, 0.2)
    expected_away = (eff_away_att * eff_home_def) / max(league_away_avg, 0.2)

    expected_home = min(3.5, max(0.3, expected_home))
    expected_away = min(3.5, max(0.3, expected_away))

    return round(expected_home, 2), round(expected_away, 2), "attack-defense+rest", len(usable), home_rest, away_rest


def negative_binomial_prob(mu, k, dispersion=0.2):
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    r = 1.0 / dispersion
    try:
        coef = math.gamma(k + r) / (math.factorial(k) * math.gamma(r))
        return max(0.0, coef * ((r / (r + mu)) ** r) * ((mu / (r + mu)) ** k))
    except (ValueError, OverflowError):
        return (mu ** k) * math.exp(-mu) / math.factorial(k)


def dixon_coles_tau(x, y, home_xg, away_xg, rho=-0.11):
    if x == 0 and y == 0:
        return max(0.0, 1.0 - (home_xg * away_xg * rho))
    if x == 1 and y == 0:
        return max(0.0, 1.0 + (away_xg * rho))
    if x == 0 and y == 1:
        return max(0.0, 1.0 + (home_xg * rho))
    if x == 1 and y == 1:
        return max(0.0, 1.0 - rho)
    return 1.0


def build_score_matrix(home_xg, away_xg):
    matrix = {}
    total = 0.0
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            p = negative_binomial_prob(home_xg, h) * negative_binomial_prob(away_xg, a)
            p *= dixon_coles_tau(h, a, home_xg, away_xg)
            p = max(0.0, p)
            matrix[(h, a)] = p
            total += p

    if total > 0:
        for key in matrix:
            matrix[key] /= total
    return matrix


def generate_prediction_message(match, history):
    league = match.get("competition", {}).get("name", "Unknown League")
    home_data = match.get("homeTeam", {})
    away_data = match.get("awayTeam", {})
    home = home_data.get("name", "Home")
    away = away_data.get("name", "Away")

    kickoff = parse_utc_date(match.get("utcDate")) or datetime.now(timezone.utc)
    kickoff_str = kickoff.strftime("%Y-%m-%d %H:%M UTC")

    home_xg, away_xg, _, _, home_rest, away_rest = estimate_goal_rates(
        history, home, away, home_data.get("id"), away_data.get("id"), kickoff
    )

    prob_matrix = build_score_matrix(home_xg, away_xg)

    prob_1x = sum(p for (h, a), p in prob_matrix.items() if h >= a)
    prob_x2 = sum(p for (h, a), p in prob_matrix.items() if a >= h)
    prob_over_15 = sum(p for (h, a), p in prob_matrix.items() if (h + a) >= 2)
    prob_under_35 = sum(p for (h, a), p in prob_matrix.items() if (h + a) <= 3)

    combo_12_over15 = sum(p for (h, a), p in prob_matrix.items() if (h != a) and ((h + a) >= 2))
    combo_x2_over05 = sum(p for (h, a), p in prob_matrix.items() if (a >= h) and ((h + a) >= 1))
    combo_x2_over15 = sum(p for (h, a), p in prob_matrix.items() if (a >= h) and ((h + a) >= 2))
    combo_btts_x2 = sum(p for (h, a), p in prob_matrix.items() if (h > 0 and a > 0) and (a >= h))
    combo_btts_12 = sum(p for (h, a), p in prob_matrix.items() if (h > 0 and a > 0) and (h != a))
    combo_btts_over25 = sum(p for (h, a), p in prob_matrix.items() if (h > 0 and a > 0) and ((h + a) >= 3))

    candidates = []
    if prob_1x >= 0.68:
        candidates.append(("1X", prob_1x, f"1X ({prob_1x * 100:.1f}%)"))
    if prob_x2 >= 0.68:
        candidates.append(("X2", prob_x2, f"X2 ({prob_x2 * 100:.1f}%)"))
    if prob_over_15 >= 0.65:
        candidates.append(("Over 1.5 Goals", prob_over_15, f"Over 1.5 Goals ({prob_over_15 * 100:.1f}%)"))
    if prob_under_35 >= 0.82:
        candidates.append(("Under 3.5 Goals", prob_under_35, f"Under 3.5 Goals ({prob_under_35 * 100:.1f}%)"))
    if combo_12_over15 >= 0.52:
        candidates.append(("12 & Over 1.5 Goals", combo_12_over15, f"{home}/{away} & Over 1.5 Goals ({combo_12_over15 * 100:.1f}%)"))
    if combo_x2_over05 >= 0.52:
        candidates.append(("X2 & Over 0.5 Goals", combo_x2_over05, f"Draw/{away} & Over 0.5 Goals ({combo_x2_over05 * 100:.1f}%)"))
    if combo_x2_over15 >= 0.52:
        candidates.append(("X2 & Over 1.5 Goals", combo_x2_over15, f"Draw/{away} & Over 1.5 Goals ({combo_x2_over15 * 100:.1f}%)"))
    if combo_btts_x2 >= 0.52:
        candidates.append(("BTTS & X2", combo_btts_x2, f"BTTS Yes & Draw/{away} ({combo_btts_x2 * 100:.1f}%)"))
    if combo_btts_12 >= 0.52:
        candidates.append(("BTTS & 12", combo_btts_12, f"BTTS Yes & {home}/{away} ({combo_btts_12 * 100:.1f}%)"))
    if combo_btts_over25 >= 0.52:
        candidates.append(("BTTS & Over 2.5 Goals", combo_btts_over25, f"BTTS Yes & Over 2.5 Goals ({combo_btts_over25 * 100:.1f}%)"))

    verification_keys = [c[0] for c in candidates]
    picks_str = ", ".join(c[2] for c in candidates) if candidates else "None"

    best_pick_key = None
    if candidates:
        top_pick = max(candidates, key=lambda x: x[1])
        best_pick_key = top_pick[0]
        best_pick_str = f"🎯 *BEST VALUE PICK:* {top_pick[2]}"
    else:
        best_pick_str = "🎯 *BEST VALUE PICK:* No high-confidence pick found"

    extra_lines = ""
    if home in INJURY_OVERRIDES or away in INJURY_OVERRIDES:
        extra_lines += "🚑 *Injury/Absence Adjustments Active*\n"
    if home_rest < 5.0 or away_rest < 5.0:
        extra_lines += f"⚡ *Rest:* {home_rest:.1f}d vs {away_rest:.1f}d (Fatigue Applied)\n"

    msg = (
        f"⚽ *MATCH ANALYSIS & VALUE PICKS [Step 2]*\n\n"
        f"🏆 *League:* {league}\n"
        f"⚔️ *Match:* {home} vs {away}\n"
        f"🕒 *Kick-off:* {kickoff_str}\n"
        f"{extra_lines}"
        f"📊 *xG [Attack-Defense]:* {home_xg:.2f} - {away_xg:.2f}\n\n"
        f"🔥 *HIGH-CONFIDENCE VALUE PICKS:*\n"
        f"👉 {picks_str}\n\n"
        f"{best_pick_str}\n"
    )
    return msg, verification_keys, best_pick_key, home, away, league


def evaluate_single_pick(pick, home_goals, away_goals):
    total = home_goals + away_goals
    rules = {
        "1X": home_goals >= away_goals,
        "X2": away_goals >= home_goals,
        "Over 1.5 Goals": total >= 2,
        "Under 3.5 Goals": total <= 3,
        "BTTS & X2": (home_goals > 0 and away_goals > 0) and (away_goals >= home_goals),
        "BTTS & 12": (home_goals > 0 and away_goals > 0) and (home_goals != away_goals),
        "12 & Over 1.5 Goals": (home_goals != away_goals) and (total >= 2),
        "X2 & Over 0.5 Goals": (away_goals >= home_goals) and (total >= 1),
        "X2 & Over 1.5 Goals": (away_goals >= home_goals) and (total >= 2),
        "BTTS & Over 2.5 Goals": (home_goals > 0 and away_goals > 0) and (total >= 3),
    }
    return rules.get(pick, False)


def verify_predictions(picks, best_pick, home_goals, away_goals):
    results = []
    pick_outcomes = {}
    best_line = None
    best_pick_won = None

    for pick in picks:
        won = evaluate_single_pick(pick, home_goals, away_goals)
        pick_outcomes[pick] = won
        badge = "✅ WON" if won else "❌ LOST"
        if best_pick and pick == best_pick:
            best_pick_won = won
            best_line = f"🎯 *BEST VALUE PICK ({pick}):* {badge}"
            results.append(f"• *{pick} (Best Pick)*: {badge}")
        else:
            results.append(f"• {pick}: {badge}")

    body = "\n".join(results) if results else "• No active high-confidence picks."
    header = f"{best_line}\n\n📋 *All High-Confidence Picks:*\n" if best_line else "📋 *High-Confidence Pick Verification:*\n"
    return header + body, pick_outcomes, best_pick_won


def compute_hit_rate_summary(sent_alerts, window=ROLLING_WINDOW):
    """Calculate overall and rolling hit rates for Best Picks and All Picks."""
    verified_entries = [
        info for info in sent_alerts.values()
        if info.get("status") == "VERIFIED" and isinstance(info.get("pick_outcomes"), dict)
    ]
    verified_entries.sort(key=lambda x: x.get("verified_at", x.get("timestamp", "")))

    def _calc_stats(entries):
        all_wins = 0
        all_total = 0
        best_wins = 0
        best_total = 0

        for item in entries:
            outcomes = item.get("pick_outcomes", {})
            for won in outcomes.values():
                all_total += 1
                if won is True:
                    all_wins += 1

            bp = item.get("best_pick")
            bp_won = item.get("best_pick_won")
            if bp and isinstance(bp_won, bool):
                best_total += 1
                if bp_won:
                    best_wins += 1

        all_pct = (all_wins / all_total * 100.0) if all_total > 0 else 0.0
        best_pct = (best_wins / best_total * 100.0) if best_total > 0 else 0.0
        return best_wins, best_total, best_pct, all_wins, all_total, all_pct

    ov_bw, ov_bt, ov_bpct, ov_aw, ov_at, ov_apct = _calc_stats(verified_entries)
    recent_entries = verified_entries[-window:] if window > 0 else verified_entries
    rc_bw, rc_bt, rc_bpct, rc_aw, rc_at, rc_apct = _calc_stats(recent_entries)

    return (
        f"📈 *TRACK RECORD (Overall / Last {len(recent_entries)}):*\n"
        f"• Best Picks: *{ov_bpct:.1f}%* ({ov_bw}/{ov_bt}) | Rolling: *{rc_bpct:.1f}%* ({rc_bw}/{rc_bt})\n"
        f"• All Picks: *{ov_apct:.1f}%* ({ov_aw}/{ov_at}) | Rolling: *{rc_apct:.1f}%* ({rc_aw}/{rc_at})"
    )


def process_match_results(sent_alerts):
    updated = False
    for match_id, info in list(sent_alerts.items()):
        if info.get("status") != "SENT":
            continue
        details = fetch_match_details(match_id)
        if not details or details.get("status") != "FINISHED":
            continue

        full_time = details.get("score", {}).get("fullTime", {})
        h_goals = full_time.get("home")
        a_goals = full_time.get("away")
        if h_goals is None or a_goals is None:
            continue

        h_goals, a_goals = int(h_goals), int(a_goals)
        home_team = details.get("homeTeam", {}).get("name", "Home")
        away_team = details.get("awayTeam", {}).get("name", "Away")
        league = details.get("competition", {}).get("name", "League")

        summary, pick_outcomes, best_pick_won = verify_predictions(
            info.get("picks", []), info.get("best_pick"), h_goals, a_goals
        )

        # Temporarily store outcomes so compute_hit_rate_summary includes this match
        sent_alerts[match_id]["status"] = "VERIFIED"
        sent_alerts[match_id]["score"] = f"{h_goals}-{a_goals}"
        sent_alerts[match_id]["pick_outcomes"] = pick_outcomes
        sent_alerts[match_id]["best_pick_won"] = best_pick_won
        sent_alerts[match_id]["verified_at"] = datetime.now(timezone.utc).isoformat()

        stats_block = compute_hit_rate_summary(sent_alerts, window=ROLLING_WINDOW)

        msg = (
            f"🏁 *MATCH RESULT & PICK VERIFICATION*\n\n"
            f"🏆 *League:* {league}\n"
            f"⚔️ *Match:* {home_team} {h_goals} - {a_goals} {away_team}\n"
            f"📌 *Status:* FINISHED\n\n"
            f"{summary}\n\n"
            f"{stats_block}\n"
        )
        if send_telegram_message(msg):
            updated = True
        else:
            # Revert status if notification failed so it can retry cleanly
            sent_alerts[match_id]["status"] = "SENT"
            sent_alerts[match_id].pop("score", None)
            sent_alerts[match_id].pop("pick_outcomes", None)
            sent_alerts[match_id].pop("best_pick_won", None)
            sent_alerts[match_id].pop("verified_at", None)
    return updated


def main():
    if not FOOTBALL_DATA_API_KEY:
        print("ERROR: Set FOOTBALL_DATA_API_KEY.")
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return

    sent_alerts = load_sent_alerts()
    if process_match_results(sent_alerts):
        save_sent_alerts(sent_alerts)

    print("Fetching competition histories...")
    histories = {code: fetch_competition_history(code) for code in TARGET_COMPETITIONS}

    matches = fetch_upcoming_matches()
    sent_count = 0

    for match in matches:
        match_id = str(match.get("id"))
        if match_id in sent_alerts:
            continue

        code = match.get("competition", {}).get("code")
        msg, picks, best_pick, home_name, away_name, league_name = generate_prediction_message(
            match, histories.get(code, [])
        )
        if picks and send_telegram_message(msg):
            sent_alerts[match_id] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "SENT",
                "home": home_name,
                "away": away_name,
                "league": league_name,
                "picks": picks,
                "best_pick": best_pick,
            }
            save_sent_alerts(sent_alerts)
            sent_count += 1

    print(f"Done. Sent {sent_count} new prediction(s).")


if __name__ == "__main__":
    main()
