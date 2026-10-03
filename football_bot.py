# Expanded target competitions list
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

def calculate_comprehensive_predictions(match_data):
    # ... (existing Poisson matrix calculations above) ...

    # CUSTOMIZABLE CONFIDENCE THRESHOLDS
    # Adjust these values (in decimal format, e.g., 0.70 = 70%) to control pick selectivity
    DC_THRESHOLD = 0.70        # Double Chance threshold (e.g., lowered from 75% to 70%)
    OVER_15_THRESHOLD = 0.65   # Over 1.5 goals threshold (e.g., lowered from 70% to 65%)
    HANDICAP_THRESHOLD = 0.75  # Asian Handicap threshold (e.g., lowered from 80% to 75%)

    high_confidence_picks = []

    if prob_home_or_draw >= DC_THRESHOLD:
        high_confidence_picks.append(f"1X ({prob_home_or_draw*100:.1f}%)")
    if prob_draw_or_away >= DC_THRESHOLD:
        high_confidence_picks.append(f"X2 ({prob_draw_or_away*100:.1f}%)")
    if prob_over_15 >= OVER_15_THRESHOLD:
        high_confidence_picks.append(f"Over 1.5 Goals ({prob_over_15*100:.1f}%)")
    if prob_home_plus_15 >= HANDICAP_THRESHOLD:
        high_confidence_picks.append(f"Home +1.5 ({prob_home_plus_15*100:.1f}%)")
    if prob_away_plus_15 >= HANDICAP_THRESHOLD:
        high_confidence_picks.append(f"Away +1.5 ({prob_away_plus_15*100:.1f}%)")

    # ... (rest of your formatting and send logic) ...
