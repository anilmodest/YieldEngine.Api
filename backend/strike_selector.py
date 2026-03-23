"""
Adaptive OTM strike selection based on delta, IV rank, DTE, market trend,
and portfolio delta. Supports Conservative, Moderate, and Aggressive profiles.
"""

from black_scholes import delta as calc_delta, probability_otm, greeks

# Risk profile delta targets
RISK_PROFILES = {
    "conservative": {
        "PE": {"min": 0.10, "max": 0.15},
        "CE": {"min": 0.10, "max": 0.15},
        "label": "Low income, very safe",
        "effective_otm": "7-10% OTM",
    },
    "moderate": {
        "PE": {"min": 0.15, "max": 0.25},
        "CE": {"min": 0.15, "max": 0.20},
        "label": "Balanced",
        "effective_otm": "4-7% OTM",
    },
    "aggressive": {
        "PE": {"min": 0.25, "max": 0.35},
        "CE": {"min": 0.20, "max": 0.30},
        "label": "High income, more risk",
        "effective_otm": "2-4% OTM",
    },
}


def _clamp(value, min_val, max_val):
    return max(min_val, min(max_val, value))


def compute_adjustments(iv_rank, dte, market_trend, consecutive_red_days, portfolio_net_delta, opt_type):
    """
    Compute delta adjustments based on market conditions.
    Returns list of adjustments applied with reasons.
    """
    adjustments = []
    total_adj = 0.0

    # 1. IV Environment Adjustment
    if iv_rank < 20:
        total_adj += 0.05
        adjustments.append({"type": "IV_LOW", "delta": +0.05,
                           "reason": f"IV Rank {iv_rank}% < 20% - premiums thin, moving closer to ATM"})
    elif 50 < iv_rank <= 80:
        total_adj -= 0.05
        adjustments.append({"type": "IV_HIGH", "delta": -0.05,
                           "reason": f"IV Rank {iv_rank}% > 50% - rich premiums, moving further OTM"})
    elif iv_rank > 80:
        adjustments.append({"type": "IV_EXTREME", "delta": 0,
                           "reason": f"IV Rank {iv_rank}% > 80% - spreads only for naked puts"})

    # 2. Days to Expiry Adjustment
    if 3 <= dte <= 4:
        total_adj -= 0.03
        adjustments.append({"type": "DTE_SHORT", "delta": -0.03,
                           "reason": f"{dte} DTE - mid-week entry, moving further OTM"})
    elif dte >= 14:
        total_adj += 0.03
        adjustments.append({"type": "DTE_LONG", "delta": +0.03,
                           "reason": f"{dte} DTE - monthly expiry, allowing closer to ATM"})

    # 3. Market Structure Adjustment
    if market_trend == "bearish" and opt_type == "PE":
        total_adj -= 0.05
        adjustments.append({"type": "BEARISH", "delta": -0.05,
                           "reason": "Below 200 DMA - widening OTM for put selling"})
    elif market_trend == "bullish" and opt_type == "PE":
        adjustments.append({"type": "BULLISH", "delta": 0,
                           "reason": "Above 200 DMA - bullish bias, puts favored"})

    if consecutive_red_days >= 3:
        total_adj -= 0.05
        adjustments.append({"type": "RED_DAYS", "delta": -0.05,
                           "reason": f"{consecutive_red_days} consecutive red days - widening OTM"})

    # 4. Portfolio Delta Adjustment
    if portfolio_net_delta > 0.3 and opt_type == "PE":
        adjustments.append({"type": "PORTFOLIO_LONG", "delta": 0,
                           "reason": f"Portfolio delta +{portfolio_net_delta:.2f} too long - prefer covered calls"})
    elif portfolio_net_delta < -0.1 and opt_type == "CE":
        adjustments.append({"type": "PORTFOLIO_SHORT", "delta": 0,
                           "reason": f"Portfolio delta {portfolio_net_delta:.2f} too short - prefer put selling"})

    return total_adj, adjustments


def select_strike(symbol, spot, option_chain, opt_type, risk_profile,
                  iv_rank, dte, market_trend="neutral",
                  consecutive_red_days=0, portfolio_net_delta=0.0,
                  manual_override=None, sigma=0.2):
    """
    Select optimal strike based on adaptive delta targeting.

    Args:
        symbol: e.g., "NIFTY", "RELIANCE"
        spot: current spot price
        option_chain: list of dicts with keys: strike, ltp, iv, delta (if available)
        opt_type: "PE" or "CE"
        risk_profile: "conservative", "moderate", "aggressive"
        iv_rank: 0-100 percentile
        dte: days to expiry
        market_trend: "bullish", "bearish", "neutral"
        consecutive_red_days: number of consecutive red candles
        portfolio_net_delta: current portfolio net delta
        manual_override: dict with manual delta/OTM targets (if manual mode)
        sigma: annualized IV for the underlying

    Returns:
        dict with selected strike details, rationale, and alternatives
    """
    T = dte / 365.0
    profile = RISK_PROFILES.get(risk_profile, RISK_PROFILES["moderate"])

    # Manual override mode
    if manual_override:
        target_delta = manual_override.get(
            f"target_delta_{'puts' if opt_type == 'PE' else 'calls'}",
            profile[opt_type]["min"]
        )
        total_adjustment = 0
        applied_adjustments = [{"type": "MANUAL", "delta": 0,
                               "reason": "Manual strike selection mode"}]
    else:
        # Base delta from risk profile (midpoint of range)
        base_delta = (profile[opt_type]["min"] + profile[opt_type]["max"]) / 2
        total_adjustment, applied_adjustments = compute_adjustments(
            iv_rank, dte, market_trend, consecutive_red_days, portfolio_net_delta, opt_type
        )
        target_delta = base_delta + total_adjustment

    # Force spreads only if IV rank > 80% and naked puts
    if iv_rank > 80 and opt_type == "PE" and not manual_override:
        return {
            "strategy": "SPREAD_ONLY",
            "reason": f"IV Rank {iv_rank}% too high for naked puts - recommend defined-risk spreads",
            "target_delta": target_delta,
            "adjustments_applied": applied_adjustments,
        }

    # Clamp to sane range
    target_delta = _clamp(target_delta, 0.05, 0.40)

    # Find strike closest to target delta
    best_strike = None
    best_delta_diff = float("inf")

    for strike_data in option_chain:
        strike = strike_data["strike"]
        # Calculate delta if not provided
        if "delta" in strike_data and strike_data["delta"] is not None:
            strike_delta = abs(strike_data["delta"])
        else:
            strike_iv = strike_data.get("iv", sigma)
            strike_delta = abs(calc_delta(spot, strike, T, sigma=strike_iv, option_type=opt_type))

        delta_diff = abs(strike_delta - target_delta)
        if delta_diff < best_delta_diff:
            best_delta_diff = delta_diff
            best_strike = {
                "strike": strike,
                "delta": strike_delta,
                "ltp": strike_data.get("ltp", 0),
                "iv": strike_data.get("iv", sigma),
            }

    if not best_strike:
        return {"error": "No suitable strike found in option chain"}

    # Calculate prob OTM
    prob_otm = probability_otm(spot, best_strike["strike"], T, sigma=best_strike["iv"],
                               option_type=opt_type)
    otm_pct = abs(best_strike["strike"] - spot) / spot * 100

    # Get all greeks
    strike_greeks = greeks(spot, best_strike["strike"], T, sigma=best_strike["iv"],
                          option_type=opt_type)

    # Build alternatives for all risk profiles
    alternatives = {}
    for alt_profile_name, alt_profile in RISK_PROFILES.items():
        alt_target = (alt_profile[opt_type]["min"] + alt_profile[opt_type]["max"]) / 2
        alt_target = _clamp(alt_target + total_adjustment, 0.05, 0.40)
        alt_best = None
        alt_best_diff = float("inf")
        for sd in option_chain:
            s_delta = abs(calc_delta(spot, sd["strike"], T, sigma=sd.get("iv", sigma),
                                    option_type=opt_type))
            if abs(s_delta - alt_target) < alt_best_diff:
                alt_best_diff = abs(s_delta - alt_target)
                alt_best = sd
        if alt_best:
            alt_prob = probability_otm(spot, alt_best["strike"], T,
                                      sigma=alt_best.get("iv", sigma), option_type=opt_type)
            alternatives[alt_profile_name] = {
                "strike": alt_best["strike"],
                "premium": alt_best.get("ltp", 0),
                "prob_otm": round(alt_prob * 100, 1),
                "target_delta": round(alt_target, 2),
            }

    # Safety scoring
    if prob_otm > 0.90 and otm_pct > 5:
        safety = "VERY_SAFE"
    elif prob_otm > 0.85:
        safety = "SAFE"
    elif prob_otm > 0.75:
        safety = "MODERATE"
    else:
        safety = "AGGRESSIVE"

    return {
        "strike": best_strike["strike"],
        "actual_delta": round(best_strike["delta"], 4),
        "target_delta": round(target_delta, 4),
        "premium": best_strike["ltp"],
        "iv": round(best_strike["iv"] * 100, 1) if best_strike["iv"] < 1 else round(best_strike["iv"], 1),
        "prob_otm": round(prob_otm * 100, 1),
        "otm_pct": round(otm_pct, 1),
        "greeks": {k: round(v, 4) for k, v in strike_greeks.items()},
        "safety": safety,
        "risk_profile": risk_profile,
        "adjustments_applied": applied_adjustments,
        "alternatives": alternatives,
        "rationale": _build_rationale(risk_profile, target_delta, iv_rank, dte,
                                     market_trend, best_strike, prob_otm, otm_pct,
                                     opt_type, applied_adjustments),
    }


def _build_rationale(profile, target_delta, iv_rank, dte, market_trend,
                     strike_data, prob_otm, otm_pct, opt_type, adjustments):
    """Build human-readable rationale for strike selection."""
    profile_range = RISK_PROFILES[profile][opt_type]
    lines = [
        f"Delta {strike_data['delta']:.2f} (within {profile} range {profile_range['min']:.2f}-{profile_range['max']:.2f})",
    ]

    for adj in adjustments:
        if adj["delta"] != 0:
            direction = "closer to ATM" if adj["delta"] > 0 else "further OTM"
            lines.append(f"{adj['reason']}")

    if not any(a["delta"] != 0 for a in adjustments):
        lines.append(f"IV Rank {iv_rank}% - no adjustment needed")

    lines.append(f"{dte} DTE")

    if market_trend == "bullish" and opt_type == "PE":
        lines.append("Above 200 DMA - bullish bias, puts favored")
    elif market_trend == "bearish":
        lines.append("Below 200 DMA - bearish caution applied")

    lines.append(
        f"Result: {strike_data['strike']} {opt_type}, {otm_pct:.1f}% OTM, "
        f"{prob_otm * 100:.1f}% prob OTM"
    )

    return " | ".join(lines)
