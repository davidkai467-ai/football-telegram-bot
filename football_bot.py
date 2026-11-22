"""
Football prediction bot (v3)

What changed compared with the old version:
  1. Picks are now based on VALUE: model probability vs the bookmaker's no-vig
     probability (needs ODDS_API_KEY). No odds = no value pick.
  2. EVERY upcoming match is logged to predictions_log.csv before kickoff
     (not only matches that pass a threshold), so calibration can be measured.
  3. Results are written to results_log.csv, and `--report` shows live
     log loss / Brier vs the market.
  4. `--backtest` replays the model on past matches using only earlier data.
  5. Correlated combo picks removed: at most one result-type pick and one
     goals-type pick per match.
  6. Rest days now use matches from ALL target competitions (league + Europe).
  7. Teams with too little data are logged but never alerted.
  8. Telegram messages use HTML mode (team names with _ or * no longer break).

Usage:
  python football_bot.py              # normal run (log, alert, verify)
  python football_bot.py --backtest   # replay model on recent past matches
  python football_bot.py --report     # live results from the logs

Environment variables:
  FOOTBALL_DATA_API_KEY   required (football-data.org)
  TELEGRAM_BOT_TOKEN      required for alerts
  TELEGRAM_CHAT_ID        required for alerts
  ODDS_API_KEY            optional but strongly recommended (the-odds-api.com)
"""

import csv
import difflib
import html
import json
import math
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

try:
    import numpy as np
    from scipy.optimize import minimize
except ImportError:
    raise SystemExit("Missing packages. In the workflow use: pip install requests numpy scipy")

# =============================================================================
# SETTINGS
# =============================================================================

MODEL_VERSION = "v4-2026-11"

TARGET_COMPETITIONS = [
    "PL",   # Premier League
    "ELC",  # Championship
    "PD",   # La Liga
    "SA",   # Serie A
    "BL1",  # Bundesliga
    "FL1",  # Ligue 1
    "CL",   # Champions League
    "BSA",  # Brasileirao Serie A
]

# The Odds API sport keys for the competitions above
ODDS_SPORT_KEYS = {
    "PL": "soccer_epl",
    "ELC": "soccer_efl_champ",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
    "BSA": "soccer_brazil_campeonato",
}

API_BASE = "https://api.football-data.org/v4"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "eu,uk"

# --- Model parameters (check them with --backtest) ---
HISTORY_DAYS = 365
HALF_LIFE_DAYS = 180      # how fast old matches lose weight
RIDGE = 3.0               # shrinks teams with little data toward the league average
MAX_GOALS = 7
DISPERSION = 0.2
RHO = -0.11

# --- Data quality gates: below these the match is logged but never alerted ---
MIN_LEAGUE_MATCHES = 20   # finished matches in the competition before kickoff
MIN_TEAM_MATCHES = 8      # matches per team in that competition's history

# --- Value rules ---
W_MODEL = 0.35            # weight on the model when blended with the market (--report suggests a value)
MIN_EDGE = 0.02           # blended probability minus market no-vig probability
MIN_EV = 0.03             # expected value per 1 unit staked (when we have a price)
MAX_ODDS = 5.0            # long shots are where model errors hide
SUSPECT_EDGE = 0.12       # raw model-vs-market gaps above this are flagged

# --- Fallback when no odds are available (informational only, NOT value) ---
ALERT_WITHOUT_ODDS = False
THRESHOLD_PICKS = {"1X": 0.68, "X2": 0.68, "OVER_1.5": 0.65, "UNDER_3.5": 0.82}

# --- Reporting / backtest ---
ROLLING_WINDOW = 20
BACKTEST_DAYS = 150
BACKTEST_GRID_HALF_LIFE = [90, 180, 365]
BACKTEST_GRID_RIDGE = [1.0, 3.0, 8.0]

# Format: "Team Name": {"att_mult": 0.85, "def_mult": 1.10}
INJURY_OVERRIDES = {}

# --- Files ---
SENT_ALERTS_FILE = "sent_alerts.json"
PREDICTIONS_LOG = "predictions_log.csv"
RESULTS_LOG = "results_log.csv"

PREDICTION_FIELDS = [
    "logged_at", "match_id", "league", "kickoff_utc", "home", "away",
    "model_version", "method", "reliable", "home_xg", "away_xg",
    "p_home", "p_draw", "p_away", "p_over15", "p_over25", "p_under35", "p_btts",
    "mkt_home", "mkt_draw", "mkt_away", "mkt_over25",
    "best_home", "best_draw", "best_away",
]
RESULT_FIELDS = ["match_id", "home_goals", "away_goals", "recorded_at"]

# --- Credentials ---
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_ERRORS = []  # reasons odds could not be fetched; shown in the Telegram summary

session = requests.Session()
if FOOTBALL_DATA_API_KEY:
    session.headers.update({"X-Auth-Token": FOOTBALL_DATA_API_KEY})


# =============================================================================
# SMALL HELPERS
# =============================================================================

def parse_utc_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def esc(text):
    """Escape text for Telegram HTML mode."""
    return html.escape(str(text), quote=False)


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


def read_csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_csv_row(path, fields, row):
    """Append only. Never rewrites old rows."""
    is_new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing; skipping message.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
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


# =============================================================================
# FOOTBALL DATA (fixtures and history)
# =============================================================================

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


def merge_histories(histories):
    """One combined list of finished matches from all competitions (for rest days)."""
    by_id = {}
    for matches in histories.values():
        for m in matches:
            by_id[m["id"]] = m
    return sorted(by_id.values(), key=lambda m: m["_kickoff"])


# =============================================================================
# MODEL: goal rates -> score matrix -> market probabilities
# =============================================================================

_REST_CACHE = {}


def calculate_rest_days(all_history, team_id, kickoff):
    key = (team_id, kickoff)
    if key in _REST_CACHE:
        return _REST_CACHE[key]
    prev_games = [
        m["_kickoff"]
        for m in all_history
        if m["_kickoff"] < kickoff
        and (
            m.get("homeTeam", {}).get("id") == team_id
            or m.get("awayTeam", {}).get("id") == team_id
        )
    ]
    if not prev_games:
        _REST_CACHE[key] = 7.0
        return 7.0
    days = (kickoff - max(prev_games)).total_seconds() / 86400.0
    _REST_CACHE[key] = max(1.0, days)
    return _REST_CACHE[key]


def get_fatigue_multiplier(rest_days):
    if rest_days < 4.0:
        return 0.88, 1.08
    if rest_days < 5.0:
        return 0.94, 1.04
    return 1.00, 1.00


def fit_strengths(history, as_of, half_life=None, ridge=None):
    """
    Fit a whole league at once (Poisson regression with time decay):
        log(home goals) = base + home_advantage + attack[home] + defence[away]
        log(away goals) = base + attack[away] + defence[home]
    Only matches played before `as_of` are used. The ridge penalty pulls teams
    with little evidence back toward the league average.
    Returns None when there is not enough data.
    """
    half_life = HALF_LIFE_DAYS if half_life is None else half_life
    ridge = RIDGE if ridge is None else ridge

    usable = [m for m in history if m["_kickoff"] < as_of]
    if len(usable) < MIN_LEAGUE_MATCHES:
        return None

    team_ids = sorted(
        {m["homeTeam"]["id"] for m in usable} | {m["awayTeam"]["id"] for m in usable}
    )
    index = {team_id: i for i, team_id in enumerate(team_ids)}
    n = len(team_ids)

    home_idx = np.array([index[m["homeTeam"]["id"]] for m in usable])
    away_idx = np.array([index[m["awayTeam"]["id"]] for m in usable])
    home_goals = np.array([m["_home_goals"] for m in usable], dtype=float)
    away_goals = np.array([m["_away_goals"] for m in usable], dtype=float)
    age_days = np.array([(as_of - m["_kickoff"]).total_seconds() / 86400.0 for m in usable])
    weights = 0.5 ** (np.maximum(age_days, 0.0) / half_life)

    def negative_log_likelihood(theta):
        base, home_adv = theta[0], theta[1]
        attack = theta[2:2 + n]
        defence = theta[2 + n:]
        eta_home = base + home_adv + attack[home_idx] + defence[away_idx]
        eta_away = base + attack[away_idx] + defence[home_idx]
        lam_home = np.exp(eta_home)
        lam_away = np.exp(eta_away)

        log_lik = np.sum(weights * (home_goals * eta_home - lam_home + away_goals * eta_away - lam_away))
        log_lik -= ridge * (np.sum(attack ** 2) + np.sum(defence ** 2))

        res_home = weights * (home_goals - lam_home)
        res_away = weights * (away_goals - lam_away)
        grad = np.zeros_like(theta)
        grad[0] = np.sum(res_home) + np.sum(res_away)
        grad[1] = np.sum(res_home)
        grad[2:2 + n] = (
            np.bincount(home_idx, res_home, n) + np.bincount(away_idx, res_away, n)
            - 2.0 * ridge * attack
        )
        grad[2 + n:] = (
            np.bincount(away_idx, res_home, n) + np.bincount(home_idx, res_away, n)
            - 2.0 * ridge * defence
        )
        return -log_lik, -grad

    theta0 = np.zeros(2 + 2 * n)
    theta0[0] = math.log(max(0.5, (home_goals.mean() + away_goals.mean()) / 2.0))
    result = minimize(negative_log_likelihood, theta0, jac=True, method="L-BFGS-B", options={"maxiter": 300})
    if not np.all(np.isfinite(result.x)):
        return None

    counts = {}
    for m in usable:
        for team_id in (m["homeTeam"]["id"], m["awayTeam"]["id"]):
            counts[team_id] = counts.get(team_id, 0) + 1

    theta = result.x
    return {
        "base": float(theta[0]),
        "home_adv": float(theta[1]),
        "attack": {tid: float(theta[2 + i]) for tid, i in index.items()},
        "defence": {tid: float(theta[2 + n + i]) for tid, i in index.items()},
        "counts": counts,
    }


def estimate_goal_rates(fit, home_name, away_name, home_id, away_id, kickoff, all_history):
    """Expected goals for both teams from the fitted strengths, plus rest-day and injury tweaks."""
    home_rest = calculate_rest_days(all_history, home_id, kickoff)
    away_rest = calculate_rest_days(all_history, away_id, kickoff)

    result = {
        "home_xg": 1.48, "away_xg": 1.18, "method": "fallback",
        "home_rest": home_rest, "away_rest": away_rest,
        "home_n": 0, "away_n": 0,
    }
    if fit is None or home_id not in fit["attack"] or away_id not in fit["attack"]:
        return result

    lam_home = math.exp(fit["base"] + fit["home_adv"] + fit["attack"][home_id] + fit["defence"][away_id])
    lam_away = math.exp(fit["base"] + fit["attack"][away_id] + fit["defence"][home_id])

    home_att_mult, home_def_mult = get_fatigue_multiplier(home_rest)
    away_att_mult, away_def_mult = get_fatigue_multiplier(away_rest)
    home_inj = INJURY_OVERRIDES.get(home_name, {})
    away_inj = INJURY_OVERRIDES.get(away_name, {})

    lam_home *= home_att_mult * home_inj.get("att_mult", 1.0) * away_def_mult * away_inj.get("def_mult", 1.0)
    lam_away *= away_att_mult * away_inj.get("att_mult", 1.0) * home_def_mult * home_inj.get("def_mult", 1.0)

    result["home_xg"] = round(min(3.5, max(0.3, lam_home)), 2)
    result["away_xg"] = round(min(3.5, max(0.3, lam_away)), 2)
    result["method"] = "poisson-fit+rest"
    result["home_n"] = fit["counts"].get(home_id, 0)
    result["away_n"] = fit["counts"].get(away_id, 0)
    return result


def negative_binomial_prob(mu, k, dispersion=DISPERSION):
    """Negative binomial probability of k goals given mean mu (log-space for stability)."""
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    r = 1.0 / dispersion
    log_p = (
        math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
        + r * math.log(r / (r + mu))
        + k * math.log(mu / (r + mu))
    )
    return math.exp(log_p)


def dixon_coles_tau(x, y, home_xg, away_xg, rho=RHO):
    if x == 0 and y == 0:
        return max(0.0, 1.0 - (home_xg * away_xg * rho))
    if x == 1 and y == 0:
        return max(0.0, 1.0 + (away_xg * rho))
    if x == 0 and y == 1:
        return max(0.0, 1.0 + (home_xg * rho))
    if x == 1 and y == 1:
        return max(0.0, 1.0 - rho)
    return 1.0


def build_score_matrix(home_xg, away_xg, dispersion=DISPERSION, rho=RHO):
    matrix = {}
    total = 0.0
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            p = negative_binomial_prob(home_xg, h, dispersion) * negative_binomial_prob(away_xg, a, dispersion)
            p *= dixon_coles_tau(h, a, home_xg, away_xg, rho)
            p = max(0.0, p)
            matrix[(h, a)] = p
            total += p
    if total > 0:
        for key in matrix:
            matrix[key] /= total
    return matrix


def market_probabilities(matrix):
    """Model probability for every market we can compare against bookmaker prices."""
    def s(cond):
        return sum(p for (h, a), p in matrix.items() if cond(h, a))

    return {
        "HOME": s(lambda h, a: h > a),
        "DRAW": s(lambda h, a: h == a),
        "AWAY": s(lambda h, a: a > h),
        "1X": s(lambda h, a: h >= a),
        "X2": s(lambda h, a: a >= h),
        "OVER_1.5": s(lambda h, a: h + a >= 2),
        "UNDER_1.5": s(lambda h, a: h + a <= 1),
        "OVER_2.5": s(lambda h, a: h + a >= 3),
        "UNDER_2.5": s(lambda h, a: h + a <= 2),
        "OVER_3.5": s(lambda h, a: h + a >= 4),
        "UNDER_3.5": s(lambda h, a: h + a <= 3),
        "BTTS_YES": s(lambda h, a: h > 0 and a > 0),
    }


def predict_match(fit, all_history, home_name, away_name, home_id, away_id, kickoff):
    rates = estimate_goal_rates(fit, home_name, away_name, home_id, away_id, kickoff, all_history)
    matrix = build_score_matrix(rates["home_xg"], rates["away_xg"])
    rates["probs"] = market_probabilities(matrix)
    rates["reliable"] = (
        rates["method"] != "fallback"
        and rates["home_n"] >= MIN_TEAM_MATCHES
        and rates["away_n"] >= MIN_TEAM_MATCHES
    )
    return rates


# =============================================================================
# ODDS (The Odds API): market probabilities with the bookmaker margin removed
# =============================================================================

NAME_NOISE = {"fc", "afc", "cf", "sc", "ac", "as", "ssc", "ud", "rcd", "cd", "fk", "sk", "the", "club"}


def normalize_name(name):
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = "".join(ch if ch.isalnum() or ch == " " else " " for ch in text)
    words = [w for w in text.split() if w not in NAME_NOISE and not w.isdigit()]
    return " ".join(words)


def name_similarity(a, b):
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def remove_vig(decimal_odds):
    inverse = [1.0 / o for o in decimal_odds]
    total = sum(inverse)
    return [x / total for x in inverse]


def fetch_odds_events(code):
    sport = ODDS_SPORT_KEYS.get(code)
    if not ODDS_API_KEY:
        ODDS_ERRORS.append("ODDS_API_KEY is empty (secret missing or misnamed)")
        return []
    if not sport:
        return []
    try:
        res = requests.get(
            f"{ODDS_API_BASE}/sports/{sport}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": ODDS_REGIONS,
                "markets": "h2h,totals",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            timeout=25,
        )
        if res.status_code != 200:
            print(f"Odds API error for {code}: HTTP {res.status_code}")
            ODDS_ERRORS.append(f"{code}: HTTP {res.status_code} {res.text[:60].strip()}")
            return []
        print(f"Odds API requests remaining: {res.headers.get('x-requests-remaining', '?')}")
        return res.json()
    except Exception as exc:
        print(f"Odds API error for {code}: {exc}")
        ODDS_ERRORS.append(f"{code}: {str(exc)[:60]}")
        return []


def find_odds_event(events, home, away, kickoff):
    best_event, best_score = None, 0.0
    for event in events:
        commence = parse_utc_date(event.get("commence_time"))
        if commence and abs((commence - kickoff).total_seconds()) > 18 * 3600:
            continue
        score = min(
            name_similarity(home, event.get("home_team", "")),
            name_similarity(away, event.get("away_team", "")),
        )
        if score > best_score:
            best_event, best_score = event, score
    return best_event if best_score >= 0.6 else None


def extract_market_prices(event):
    """
    Returns {market_key: {"mkt_prob": average no-vig prob across bookmakers,
                          "best_odds": best decimal price or None}}.
    1X and X2 come from the no-vig 1X2 probabilities (no price, so best_odds is None).
    """
    h2h_probs = []
    best = {}
    totals = {}

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") == "h2h":
                prices = {o.get("name"): o.get("price") for o in market.get("outcomes", [])}
                hp = prices.get(event.get("home_team"))
                ap = prices.get(event.get("away_team"))
                dp = prices.get("Draw")
                if hp and ap and dp:
                    h2h_probs.append(remove_vig([hp, dp, ap]))
                    for key, price in (("HOME", hp), ("DRAW", dp), ("AWAY", ap)):
                        best[key] = max(best.get(key, 0.0), price)
            elif market.get("key") == "totals":
                by_point = {}
                for outcome in market.get("outcomes", []):
                    by_point.setdefault(outcome.get("point"), {})[outcome.get("name")] = outcome.get("price")
                for point, pair in by_point.items():
                    if point in (1.5, 2.5, 3.5) and pair.get("Over") and pair.get("Under"):
                        over_p, under_p = remove_vig([pair["Over"], pair["Under"]])
                        slot = totals.setdefault(point, {"over": [], "under": [], "best_over": 0.0, "best_under": 0.0})
                        slot["over"].append(over_p)
                        slot["under"].append(under_p)
                        slot["best_over"] = max(slot["best_over"], pair["Over"])
                        slot["best_under"] = max(slot["best_under"], pair["Under"])

    prices = {}
    if h2h_probs:
        n = len(h2h_probs)
        mh = sum(p[0] for p in h2h_probs) / n
        md = sum(p[1] for p in h2h_probs) / n
        ma = sum(p[2] for p in h2h_probs) / n
        prices["HOME"] = {"mkt_prob": mh, "best_odds": best.get("HOME")}
        prices["DRAW"] = {"mkt_prob": md, "best_odds": best.get("DRAW")}
        prices["AWAY"] = {"mkt_prob": ma, "best_odds": best.get("AWAY")}
        prices["1X"] = {"mkt_prob": mh + md, "best_odds": None}
        prices["X2"] = {"mkt_prob": md + ma, "best_odds": None}

    for point, slot in totals.items():
        tag = f"{point:.1f}"
        prices[f"OVER_{tag}"] = {
            "mkt_prob": sum(slot["over"]) / len(slot["over"]),
            "best_odds": slot["best_over"] or None,
        }
        prices[f"UNDER_{tag}"] = {
            "mkt_prob": sum(slot["under"]) / len(slot["under"]),
            "best_odds": slot["best_under"] or None,
        }
    return prices


# =============================================================================
# PICK SELECTION
# =============================================================================

def pick_label(key, home, away):
    labels = {
        "HOME": f"{home} to win",
        "DRAW": "Draw",
        "AWAY": f"{away} to win",
        "1X": f"{home} or Draw (1X)",
        "X2": f"Draw or {away} (X2)",
    }
    if key in labels:
        return labels[key]
    side, line = key.split("_")
    return f"{side.title()} {line} Goals"


def pick_family(key):
    """Picks in the same family are correlated, so we keep only the best one."""
    return "result" if key in {"HOME", "DRAW", "AWAY", "1X", "X2"} else "goals"


def select_value_picks(probs, prices):
    """
    The model is blended with the market before picking:
        blended = W_MODEL * model + (1 - W_MODEL) * market
    so only a model view that is strong AND consistent moves the number enough to pass.
    """
    candidates = []
    for key, info in prices.items():
        if key not in probs:
            continue
        raw_p = probs[key]
        market_p = info["mkt_prob"]
        blended = W_MODEL * raw_p + (1.0 - W_MODEL) * market_p
        odds = info.get("best_odds")
        edge = blended - market_p
        if edge < MIN_EDGE:
            continue
        ev = None
        if odds is not None:
            ev = blended * odds - 1.0
            if ev < MIN_EV or odds > MAX_ODDS:
                continue
        candidates.append({
            "key": key,
            "raw_prob": raw_p,
            "prob": blended,
            "mkt_prob": market_p,
            "odds": odds,
            "edge": edge,
            "raw_edge": raw_p - market_p,
            "ev": ev,
            "min_odds": (1.0 + MIN_EV) / blended,
        })

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    chosen, used = [], set()
    for c in candidates:
        family = pick_family(c["key"])
        if family in used:
            continue
        used.add(family)
        chosen.append(c)
    return chosen


def select_threshold_picks(probs):
    """Only used when ALERT_WITHOUT_ODDS is True. This is NOT value betting."""
    chosen = []
    for key, threshold in THRESHOLD_PICKS.items():
        if probs.get(key, 0.0) >= threshold:
            chosen.append({
                "key": key, "prob": probs[key], "mkt_prob": None, "odds": None,
                "edge": None, "ev": None, "min_odds": (1.0 + MIN_EV) / probs[key],
            })
    chosen.sort(key=lambda c: c["prob"], reverse=True)
    return chosen[:1]


def generate_prediction_message(match, pred, picks, has_odds):
    league = match.get("competition", {}).get("name", "Unknown League")
    home = match.get("homeTeam", {}).get("name", "Home")
    away = match.get("awayTeam", {}).get("name", "Away")
    kickoff = parse_utc_date(match.get("utcDate")) or datetime.now(timezone.utc)

    title = "VALUE PICKS" if has_odds else "PROBABILITY PICKS (no odds, NOT value)"
    lines = [
        f"⚽ <b>{title}</b> [{MODEL_VERSION}]",
        "",
        f"🏆 <b>League:</b> {esc(league)}",
        f"⚔️ <b>Match:</b> {esc(home)} vs {esc(away)}",
        f"🕒 <b>Kick-off:</b> {kickoff.strftime('%Y-%m-%d %H:%M UTC')}",
        f"📊 <b>Model xG:</b> {pred['home_xg']:.2f} - {pred['away_xg']:.2f}",
    ]
    if pred["home_rest"] < 5.0 or pred["away_rest"] < 5.0:
        lines.append(f"⚡ <b>Rest:</b> {pred['home_rest']:.1f}d vs {pred['away_rest']:.1f}d (fatigue applied)")
    if home in INJURY_OVERRIDES or away in INJURY_OVERRIDES:
        lines.append("🚑 <b>Injury adjustments active</b>")
    lines.append("")

    for i, p in enumerate(picks):
        label = esc(pick_label(p["key"], home, away))
        prefix = "🎯" if i == 0 else "👉"
        if p["mkt_prob"] is None:
            lines.append(f"{prefix} <b>{label}</b>: model {p['prob'] * 100:.1f}% | need odds ≥ {p['min_odds']:.2f}")
            continue
        text = (
            f"{prefix} <b>{label}</b>: model {p['raw_prob'] * 100:.1f}% | blended {p['prob'] * 100:.1f}% "
            f"vs market {p['mkt_prob'] * 100:.1f}% (edge {p['edge'] * 100:+.1f}%)"
        )
        if p["odds"] is not None:
            text += f" | best odds {p['odds']:.2f} | EV {p['ev'] * 100:+.0f}%"
        else:
            text += f" | check your bookie's price, need odds ≥ {p['min_odds']:.2f}"
        if p.get("raw_edge", 0.0) > SUSPECT_EDGE:
            text += " ⚠️ large gap, probable model error"
        lines.append(text)

    lines.append("")
    lines.append("<i>Unvalidated model. Paper trade first.</i>")


# =============================================================================
# VERIFICATION
# =============================================================================

def evaluate_single_pick(key, hg, ag):
    total = hg + ag
    rules = {
        "HOME": hg > ag, "DRAW": hg == ag, "AWAY": ag > hg,
        "1X": hg >= ag, "X2": ag >= hg,
        "OVER_1.5": total >= 2, "UNDER_1.5": total <= 1,
        "OVER_2.5": total >= 3, "UNDER_2.5": total <= 2,
        "OVER_3.5": total >= 4, "UNDER_3.5": total <= 3,
        # Old v2 keys, so earlier alerts still get verified
        "Over 1.5 Goals": total >= 2,
        "Under 3.5 Goals": total <= 3,
        "BTTS & X2": hg > 0 and ag > 0 and ag >= hg,
        "BTTS & 12": hg > 0 and ag > 0 and hg != ag,
        "12 & Over 1.5 Goals": hg != ag and total >= 2,
        "X2 & Over 0.5 Goals": ag >= hg and total >= 1,
        "X2 & Over 1.5 Goals": ag >= hg and total >= 2,
        "BTTS & Over 2.5 Goals": hg > 0 and ag > 0 and total >= 3,
    }
    return rules.get(key, False)


def normalized_picks(info):
    """Old alerts stored picks as plain strings; new ones store dicts."""
    picks = []
    outcomes = info.get("pick_outcomes", {}) if isinstance(info.get("pick_outcomes"), dict) else {}
    for p in info.get("picks", []):
        if isinstance(p, dict):
            picks.append(p)
        else:
            picks.append({"key": p, "odds": None, "won": outcomes.get(p)})
    return picks


def compute_track_record(sent_alerts, window=ROLLING_WINDOW):
    verified = [i for i in sent_alerts.values() if i.get("status") == "VERIFIED"]
    verified.sort(key=lambda x: x.get("verified_at", x.get("timestamp", "")))

    def calc(entries):
        n = wins = 0
        staked = profit = 0.0
        top_n = top_w = 0
        for entry in entries:
            picks = normalized_picks(entry)
            for p in picks:
                won = p.get("won")
                if not isinstance(won, bool):
                    continue
                n += 1
                wins += int(won)
                if p.get("odds"):
                    staked += 1.0
                    profit += (p["odds"] - 1.0) if won else -1.0
            top = next((p for p in picks if p.get("key") == entry.get("best_pick")), picks[0] if picks else None)
            if top and isinstance(top.get("won"), bool):
                top_n += 1
                top_w += int(top["won"])
        hit = wins / n * 100 if n else 0.0
        top_hit = top_w / top_n * 100 if top_n else 0.0
        roi = profit / staked * 100 if staked else None
        return n, wins, hit, top_n, top_w, top_hit, staked, profit, roi

    def fmt(stats):
        n, wins, hit, top_n, top_w, top_hit, staked, profit, roi = stats
        roi_text = f"{roi:+.1f}% on {int(staked)} priced picks ({profit:+.1f}u)" if roi is not None else "no priced picks yet"
        return f"• Hit rate: <b>{hit:.1f}%</b> ({wins}/{n}) | Top pick: {top_hit:.1f}% ({top_w}/{top_n})\n• Flat ROI: {roi_text}"

    recent = verified[-window:] if window > 0 else verified
    return (
        f"📈 <b>TRACK RECORD (Overall)</b>\n{fmt(calc(verified))}\n\n"
        f"📉 <b>Last {len(recent)} alerts</b>\n{fmt(calc(recent))}"
    )


def process_match_results(sent_alerts, results_by_id):
    updated = False
    for match_id, info in list(sent_alerts.items()):
        if info.get("status") != "SENT":
            continue

        goals = results_by_id.get(str(match_id))
        league = info.get("league", "League")
        home_team, away_team = info.get("home", "Home"), info.get("away", "Away")

        if goals is None:
            details = fetch_match_details(match_id)
            if not details or details.get("status") != "FINISHED":
                continue
            full_time = details.get("score", {}).get("fullTime", {})
            if full_time.get("home") is None or full_time.get("away") is None:
                continue
            goals = (int(full_time["home"]), int(full_time["away"]))

        h_goals, a_goals = goals
        picks = normalized_picks(info)
        lines = []
        for p in picks:
            p["won"] = evaluate_single_pick(p["key"], h_goals, a_goals)
            badge = "✅ WON" if p["won"] else "❌ LOST"
            label = pick_label(p["key"], home_team, away_team) if "_" in p["key"] or p["key"] in {"HOME", "DRAW", "AWAY", "1X", "X2"} else p["key"]
            lines.append(f"• {esc(label)}: {badge}")

        previous = {k: info.get(k) for k in ("status", "score", "picks", "verified_at")}
        info["status"] = "VERIFIED"
        info["score"] = f"{h_goals}-{a_goals}"
        info["picks"] = picks
        info["verified_at"] = datetime.now(timezone.utc).isoformat()

        msg = (
            f"🏁 <b>MATCH RESULT & PICK VERIFICATION</b>\n\n"
            f"🏆 <b>League:</b> {esc(league)}\n"
            f"⚔️ <b>Match:</b> {esc(home_team)} {h_goals} - {a_goals} {esc(away_team)}\n\n"
            + "\n".join(lines) + "\n\n"
            + compute_track_record(sent_alerts) + "\n"
        )
        if send_telegram_message(msg):
            updated = True
        else:
            # Roll back so it retries on the next run
            for k, v in previous.items():
                if v is None:
                    info.pop(k, None)
                else:
                    info[k] = v
    return updated


# =============================================================================
# LOGGING (every prediction, every result)
# =============================================================================

def log_prediction(match, pred, prices):
    home = match.get("homeTeam", {}).get("name", "")
    away = match.get("awayTeam", {}).get("name", "")
    probs = pred["probs"]

    def price(key, field):
        info = prices.get(key)
        if not info or info.get(field) is None:
            return ""
        return round(info[field], 4)

    row = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "match_id": match.get("id"),
        "league": match.get("competition", {}).get("name", ""),
        "kickoff_utc": match.get("utcDate", ""),
        "home": home,
        "away": away,
        "model_version": MODEL_VERSION,
        "method": pred["method"],
        "reliable": int(pred["reliable"]),
        "home_xg": pred["home_xg"],
        "away_xg": pred["away_xg"],
        "p_home": round(probs["HOME"], 4),
        "p_draw": round(probs["DRAW"], 4),
        "p_away": round(probs["AWAY"], 4),
        "p_over15": round(probs["OVER_1.5"], 4),
        "p_over25": round(probs["OVER_2.5"], 4),
        "p_under35": round(probs["UNDER_3.5"], 4),
        "p_btts": round(probs["BTTS_YES"], 4),
        "mkt_home": price("HOME", "mkt_prob"),
        "mkt_draw": price("DRAW", "mkt_prob"),
        "mkt_away": price("AWAY", "mkt_prob"),
        "mkt_over25": price("OVER_2.5", "mkt_prob"),
        "best_home": price("HOME", "best_odds"),
        "best_draw": price("DRAW", "best_odds"),
        "best_away": price("AWAY", "best_odds"),
    }
    append_csv_row(PREDICTIONS_LOG, PREDICTION_FIELDS, row)


def update_results_log(results_by_id):
    logged_ids = {r["match_id"] for r in read_csv_rows(PREDICTIONS_LOG)}
    done_ids = {r["match_id"] for r in read_csv_rows(RESULTS_LOG)}
    added = 0
    for match_id in logged_ids - done_ids:
        goals = results_by_id.get(str(match_id))
        if goals is None:
            continue
        append_csv_row(RESULTS_LOG, RESULT_FIELDS, {
            "match_id": match_id,
            "home_goals": goals[0],
            "away_goals": goals[1],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
        added += 1
    if added:
        print(f"Recorded {added} new result(s).")


# =============================================================================
# EVALUATION METRICS (used by --backtest and --report)
# =============================================================================

EPS = 1e-9


def log_loss_multiclass(probs, outcome_index):
    return -math.log(max(EPS, probs[outcome_index]))


def brier_multiclass(probs, outcome_index):
    return sum((p - (1.0 if i == outcome_index else 0.0)) ** 2 for i, p in enumerate(probs))


def log_loss_binary(p, happened):
    p = min(1 - EPS, max(EPS, p))
    return -math.log(p if happened else 1 - p)


def calibration_table(pairs, title):
    """pairs: list of (predicted_probability, happened_bool)."""
    print(f"\nCalibration: {title}")
    print(f"{'bin':<10}{'n':>6}{'avg pred':>11}{'actual':>9}")
    for low in [i / 10 for i in range(10)]:
        bucket = [(p, y) for p, y in pairs if low <= p < low + 0.1 or (low == 0.9 and p == 1.0)]
        if len(bucket) < 5:
            continue
        avg_pred = sum(p for p, _ in bucket) / len(bucket)
        actual = sum(1 for _, y in bucket if y) / len(bucket)
        print(f"{low:.1f}-{low + 0.1:.1f}  {len(bucket):>6}{avg_pred:>10.1%}{actual:>9.1%}")


def outcome_index(hg, ag):
    return 0 if hg > ag else (1 if hg == ag else 2)


def evaluate_config(histories, all_history, cutoff, half_life, ridge):
    """Replay one parameter setting. Strengths are refitted weekly using only earlier matches."""
    stats = {
        "n": 0, "ll": 0.0, "br": 0.0, "base_ll": 0.0, "base_br": 0.0,
        "ou_ll": 0.0, "ou_base_ll": 0.0, "correct": 0,
        "home_pairs": [], "x2_pairs": [], "over_pairs": [],
    }
    for code, history in histories.items():
        train = [m for m in history if m["_kickoff"] < cutoff]
        test = [m for m in history if m["_kickoff"] >= cutoff]
        if len(train) < 50 or not test:
            continue

        total = len(train)
        base = [
            sum(1 for m in train if m["_home_goals"] > m["_away_goals"]) / total,
            sum(1 for m in train if m["_home_goals"] == m["_away_goals"]) / total,
            sum(1 for m in train if m["_home_goals"] < m["_away_goals"]) / total,
        ]
        base_over = sum(1 for m in train if m["_home_goals"] + m["_away_goals"] >= 3) / total

        fit_cache = {}
        for m in test:
            week = int((m["_kickoff"] - cutoff).total_seconds() // (7 * 86400))
            if week not in fit_cache:
                as_of = cutoff + timedelta(days=7 * week)
                fit_cache[week] = fit_strengths(history, as_of, half_life, ridge)
            pred = predict_match(
                fit_cache[week], all_history,
                m["homeTeam"].get("name", ""), m["awayTeam"].get("name", ""),
                m["homeTeam"]["id"], m["awayTeam"]["id"], m["_kickoff"],
            )
            if not pred["reliable"]:
                continue

            probs = pred["probs"]
            vec = [probs["HOME"], probs["DRAW"], probs["AWAY"]]
            hg, ag = m["_home_goals"], m["_away_goals"]
            idx = outcome_index(hg, ag)
            over = hg + ag >= 3

            stats["ll"] += log_loss_multiclass(vec, idx)
            stats["base_ll"] += log_loss_multiclass(base, idx)
            stats["br"] += brier_multiclass(vec, idx)
            stats["base_br"] += brier_multiclass(base, idx)
            stats["correct"] += int(vec.index(max(vec)) == idx)
            stats["ou_ll"] += log_loss_binary(probs["OVER_2.5"], over)
            stats["ou_base_ll"] += log_loss_binary(base_over, over)
            stats["home_pairs"].append((probs["HOME"], idx == 0))
            stats["x2_pairs"].append((probs["X2"], hg <= ag))
            stats["over_pairs"].append((probs["OVER_2.5"], over))
            stats["n"] += 1
    return stats


def run_backtest(histories, all_history):
    """Try a small grid of settings on recent past matches (no look-ahead, no odds)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
    print(f"\nBACKTEST ({MODEL_VERSION}), last {BACKTEST_DAYS} days, strengths refitted weekly")
    print(f"{'half-life':>10}{'ridge':>8}{'n':>6}{'1X2 LL':>10}{'Brier':>9}{'O2.5 LL':>10}")

    results = []
    for half_life in BACKTEST_GRID_HALF_LIFE:
        for ridge in BACKTEST_GRID_RIDGE:
            s = evaluate_config(histories, all_history, cutoff, half_life, ridge)
            if s["n"] == 0:
                continue
            results.append((half_life, ridge, s))
            print(f"{half_life:>10}{ridge:>8}{s['n']:>6}{s['ll'] / s['n']:>10.4f}{s['br'] / s['n']:>9.4f}{s['ou_ll'] / s['n']:>10.4f}")

    if not results:
        print("No reliable matches to backtest.")
        return

    best_half, best_ridge, best = min(results, key=lambda r: r[2]["ll"])
    n = best["n"]
    print(f"\nBest 1X2 log loss: half-life {best_half}, ridge {best_ridge} (current: {HALF_LIFE_DAYS}, {RIDGE})")
    print(f"{'':<22}{'model':>10}{'baseline':>12}")
    print(f"{'1X2 log loss':<22}{best['ll'] / n:>10.4f}{best['base_ll'] / n:>12.4f}")
    print(f"{'1X2 Brier':<22}{best['br'] / n:>10.4f}{best['base_br'] / n:>12.4f}")
    print(f"{'Over 2.5 log loss':<22}{best['ou_ll'] / n:>10.4f}{best['ou_base_ll'] / n:>12.4f}")
    print(f"{'1X2 accuracy':<22}{best['correct'] / n:>10.1%}")
    calibration_table(best["home_pairs"], "home win")
    calibration_table(best["x2_pairs"], "X2 (draw or away)")
    calibration_table(best["over_pairs"], "over 2.5 goals")
    print(
        "\nBaseline = league home/draw/away rates from before the test window.\n"
        "Bookmaker 1X2 log loss is usually about 0.96 to 0.99.\n"
        "The best row is picked on the same data it is scored on, so only change the\n"
        "settings if it beats the current ones clearly (about 0.005 log loss or more)."
    )


def report_for_rows(label, rows):
    n = 0
    model_ll = model_br = 0.0
    correct = 0
    market_rows = []
    over_pairs = []

    for p, r in rows:
        hg, ag = int(r["home_goals"]), int(r["away_goals"])
        idx = outcome_index(hg, ag)
        vec = [float(p["p_home"]), float(p["p_draw"]), float(p["p_away"])]
        model_ll += log_loss_multiclass(vec, idx)
        model_br += brier_multiclass(vec, idx)
        correct += int(vec.index(max(vec)) == idx)
        over_pairs.append((float(p["p_over25"]), hg + ag >= 3))
        n += 1
        if p.get("mkt_home") and p.get("mkt_draw") and p.get("mkt_away"):
            mvec = [float(p["mkt_home"]), float(p["mkt_draw"]), float(p["mkt_away"])]
            market_rows.append((vec, mvec, idx))

    print(f"\n=== {label}: {n} finished predictions ===")
    print(f"1X2 log loss: {model_ll / n:.4f} | Brier: {model_br / n:.4f} | accuracy: {correct / n:.1%}")

    if market_rows:
        m = len(market_rows)
        print(f"On the {m} matches with market odds logged (lower is better):")
        print(f"  model only  : {sum(log_loss_multiclass(v, i) for v, _, i in market_rows) / m:.4f}")
        print(f"  market only : {sum(log_loss_multiclass(mv, i) for _, mv, i in market_rows) / m:.4f}")
        print("  Blend (w = weight on the model):")
        best_w, best_ll = None, None
        for w in (0.0, 0.25, 0.35, 0.5, 0.75, 1.0):
            ll = sum(
                log_loss_multiclass([w * a + (1 - w) * b for a, b in zip(v, mv)], i)
                for v, mv, i in market_rows
            ) / m
            if best_ll is None or ll < best_ll:
                best_w, best_ll = w, ll
            print(f"    w = {w:.2f}: {ll:.4f}")
        print(f"  Best weight on the model so far: {best_w:.2f} (current W_MODEL = {W_MODEL})")
        if best_w == 0.0:
            print("  The market alone beat every blend: the model adds nothing yet.")
    else:
        print("No matches with market odds logged yet.")
    calibration_table(over_pairs, "over 2.5 goals")


def run_report():
    predictions = {r["match_id"]: r for r in read_csv_rows(PREDICTIONS_LOG)}
    results = {r["match_id"]: r for r in read_csv_rows(RESULTS_LOG)}
    joined = [(predictions[mid], results[mid]) for mid in predictions if mid in results]
    joined = [(p, r) for p, r in joined if p.get("reliable") == "1"]

    if not joined:
        print("No finished, reliable predictions logged yet.")
        return

    versions = sorted({p.get("model_version", "?") for p, _ in joined})
    for version in versions:
        rows = [(p, r) for p, r in joined if p.get("model_version", "?") == version]
        report_for_rows(f"model {version}", rows)


# =============================================================================
# MAIN
# =============================================================================

def build_histories():
    print("Fetching competition histories...")
    histories = {code: fetch_competition_history(code) for code in TARGET_COMPETITIONS}
    return histories, merge_histories(histories)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "--report":
        run_report()
        return

    if not FOOTBALL_DATA_API_KEY:
        print("ERROR: Set FOOTBALL_DATA_API_KEY.")
        return

    if mode == "--backtest":
        histories, all_history = build_histories()
        run_backtest(histories, all_history)
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return
    if not ODDS_API_KEY:
        print("WARNING: ODDS_API_KEY not set. Predictions are logged but no value picks can be made.")

    histories, all_history = build_histories()
    results_by_id = {
        str(m["id"]): (m["_home_goals"], m["_away_goals"]) for m in all_history
    }

    sent_alerts = load_sent_alerts()
    if process_match_results(sent_alerts, results_by_id):
        save_sent_alerts(sent_alerts)
    update_results_log(results_by_id)

    matches = fetch_upcoming_matches()
    now = datetime.now(timezone.utc)
    upcoming_codes = {m.get("competition", {}).get("code") for m in matches}

    # One odds request and one strength fit per competition that has upcoming matches
    odds_events = {code: fetch_odds_events(code) for code in upcoming_codes}
    fits = {code: fit_strengths(histories.get(code, []), now) for code in upcoming_codes}

    logged_ids = {r["match_id"] for r in read_csv_rows(PREDICTIONS_LOG)}
    sent_count = 0
    reliable_count = 0
    checked_count = 0

    for match in matches:
        match_id = str(match.get("id"))
        code = match.get("competition", {}).get("code")
        home_data, away_data = match.get("homeTeam", {}), match.get("awayTeam", {})
        kickoff = parse_utc_date(match.get("utcDate"))
        if kickoff is None or kickoff <= now:
            continue

        pred = predict_match(
            fits.get(code), all_history,
            home_data.get("name", ""), away_data.get("name", ""),
            home_data.get("id"), away_data.get("id"), kickoff,
        )
        checked_count += 1
        reliable_count += int(pred["reliable"])

        event = find_odds_event(odds_events.get(code, []), home_data.get("name", ""), away_data.get("name", ""), kickoff)
        prices = extract_market_prices(event) if event else {}

        # Log every match once, before kickoff, whether or not it becomes a pick
        if match_id not in logged_ids:
            log_prediction(match, pred, prices)
            logged_ids.add(match_id)

        if match_id in sent_alerts or not pred["reliable"]:
            continue

        if prices:
            picks = select_value_picks(pred["probs"], prices)
            has_odds = True
        elif ALERT_WITHOUT_ODDS:
            picks = select_threshold_picks(pred["probs"])
            has_odds = False
        else:
            picks = []
            has_odds = False

        if not picks:
            continue

        msg = generate_prediction_message(match, pred, picks, has_odds)
        if send_telegram_message(msg):
            sent_alerts[match_id] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "SENT",
                "home": home_data.get("name", ""),
                "away": away_data.get("name", ""),
                "league": match.get("competition", {}).get("name", ""),
                "model_version": MODEL_VERSION,
                "picks": picks,
                "best_pick": picks[0]["key"],
            }
            save_sent_alerts(sent_alerts)
            sent_count += 1

    summary = (
        f"🤖 Bot run finished [{MODEL_VERSION}]\n"
        f"Upcoming matches: {len(matches)}\n"
        f"Reliable predictions: {reliable_count}/{checked_count}\n"
        f"Odds events found: {sum(len(v) for v in odds_events.values())}\n"
        f"New picks sent: {sent_count}"
    )
    if ODDS_ERRORS:
        summary += "\nOdds problems: " + "; ".join(sorted(set(ODDS_ERRORS))[:4])
    print(summary)
    send_telegram_message(summary)


if __name__ == "__main__":
    main()
