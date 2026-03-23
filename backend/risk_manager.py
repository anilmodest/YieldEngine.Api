"""
Position monitoring, stop-loss management, adjustment calculations, GTT orders.
Runs every 5 minutes during market hours (9:15 AM - 3:30 PM IST).
"""

import json
import math
from models import (get_active_positions, get_setting, create_notification,
                    create_gtt_order, get_active_gtt_orders, log_adjustment,
                    generate_id, SAFETY_HARD_CAPS)
from black_scholes import delta as calc_delta, probability_otm, option_price
from fee_calculator import calculate_fees, estimate_slippage


def monitor_positions(kite_svc):
    """
    Run risk monitoring on all active positions.
    Checks: stop-loss, delta breach, daily loss limit, margin squeeze, expiry ITM.

    Returns:
        list of risk alerts generated
    """
    positions = get_active_positions()
    alerts = []

    stop_loss_mult = float(get_setting("stop_loss_multiplier", "2.0"))
    delta_threshold = float(get_setting("delta_alert_threshold", "0.50"))
    daily_loss_limit = float(get_setting("daily_loss_limit", "25000"))
    intraday_drop_pct = float(get_setting("intraday_drop_alert_pct", "1.5"))
    circuit_breaker = get_setting("circuit_breaker_enabled", "false") == "true"
    auto_stop_loss = get_setting("auto_stop_loss_enabled", "false") == "true"

    total_unrealized_loss = 0

    for pos in positions:
        legs = json.loads(pos["legs"])
        symbol = pos["symbol"]
        entry_premium = pos["entry_premium"]
        current_premium = pos.get("current_premium", entry_premium)

        # Get current LTP for the position
        current_ltp = current_premium  # Will be updated by MTM
        spot = kite_svc.get_ltp(symbol)
        if not spot:
            continue

        # --- Rule 1: 2x Stop-Loss ---
        if current_premium >= entry_premium * stop_loss_mult:
            alert = {
                "type": "STOP_LOSS_HIT",
                "position_id": pos["id"],
                "symbol": symbol,
                "severity": "URGENT",
                "message": f"Premium reached {stop_loss_mult}x entry "
                          f"(Rs {current_premium:.2f} vs entry Rs {entry_premium:.2f}). "
                          f"EXIT recommended.",
            }
            alerts.append(alert)
            create_notification("STOP_LOSS_HIT", f"Stop-Loss Hit: {symbol}",
                              alert["message"], "URGENT", "/positions")

            if auto_stop_loss:
                # Auto-close position
                from trade_tracker import close_position_manual
                close_position_manual(pos, current_premium, kite_svc)

        # --- Rule 2: Delta Breach ---
        if legs:
            leg = legs[0]
            T = max(1, pos.get("days_held", 7)) / 365.0  # approximate
            strike = leg.get("strike", spot)
            opt_type = leg.get("type", "PE")
            pos_delta = abs(calc_delta(spot, strike, T, sigma=0.2, option_type=opt_type))

            if pos_delta > delta_threshold:
                alert = {
                    "type": "DELTA_BREACH",
                    "position_id": pos["id"],
                    "symbol": symbol,
                    "severity": "WARNING",
                    "message": f"Delta {pos_delta:.2f} crossed threshold {delta_threshold}. "
                              f"Position becoming ATM/ITM. Consider adjustments.",
                    "delta": pos_delta,
                }
                alerts.append(alert)
                create_notification("DELTA_BREACH", f"Delta Breach: {symbol}",
                                  alert["message"], "WARNING", f"/positions")

        # --- Rule 3: Underlying Drop ---
        # (In real mode, compare to day open)
        if spot and entry_premium > 0:
            implied_drop = (current_premium - entry_premium) / entry_premium * 100
            if implied_drop > intraday_drop_pct * 100:
                unrealized = -(current_premium - entry_premium) * (legs[0]["qty"] if legs else 1)
                alert = {
                    "type": "UNDERLYING_DROP",
                    "position_id": pos["id"],
                    "symbol": symbol,
                    "severity": "WARNING",
                    "message": f"Significant adverse move. Unrealized loss: Rs {unrealized:.0f}",
                }
                alerts.append(alert)

        # Track total unrealized loss
        if pos.get("unrealized_pnl", 0) < 0:
            total_unrealized_loss += abs(pos["unrealized_pnl"])

    # --- Rule 5: Daily Loss Limit ---
    if total_unrealized_loss > daily_loss_limit:
        alert = {
            "type": "DAILY_LOSS_LIMIT",
            "severity": "URGENT",
            "message": f"Total unrealized loss Rs {total_unrealized_loss:.0f} exceeds "
                      f"daily limit Rs {daily_loss_limit:.0f}.",
        }
        alerts.append(alert)
        create_notification("DAILY_LOSS_LIMIT", "Daily Loss Limit Breached",
                          alert["message"], "URGENT", "/risk")

        if circuit_breaker:
            _trigger_circuit_breaker(kite_svc)

    return alerts


def compute_adjustments(position, kite_svc):
    """
    Compute all viable adjustments for an at-risk position.
    Returns 4 options: Exit, Roll, Convert to Spread, Do Nothing.
    """
    legs = json.loads(position["legs"])
    symbol = position["symbol"]
    entry_premium = position["entry_premium"]
    current_premium = position.get("current_premium", entry_premium)
    spot = kite_svc.get_ltp(symbol) or 0
    lot_size = legs[0]["qty"] if legs else kite_svc.get_lot_size(symbol)

    adjustments = []

    # --- Adjustment 1: EXIT NOW ---
    exit_cost = current_premium * lot_size
    exit_loss = (current_premium - entry_premium) * lot_size
    exit_fees = calculate_fees("BUY", current_premium, lot_size)
    slippage = estimate_slippage(current_premium, symbol)

    adjustments.append({
        "type": "EXIT_NOW",
        "description": "Buy back the sold option at current premium",
        "cost": round(exit_cost + slippage * lot_size, 2),
        "realized_loss": round(-exit_loss, 2),
        "fees": exit_fees,
        "slippage": slippage,
        "margin_freed": position.get("margin_blocked", 0),
        "recommendation": "Best if you think underlying will keep moving against you",
        "legs": [{"action": "BUY", "type": legs[0].get("type", "PE"),
                  "strike": legs[0].get("strike"), "qty": lot_size,
                  "premium": current_premium}],
    })

    # --- Adjustment 2: ROLL DOWN + OUT ---
    if legs:
        leg = legs[0]
        original_strike = leg.get("strike", spot)
        opt_type = leg.get("type", "PE")

        # Find new strike at next week's expiry with similar delta
        chain = kite_svc.get_option_chain(symbol)
        type_chain = [c for c in chain if c.get("option_type") == opt_type]

        if type_chain:
            # Roll to next expiry, lower strike (for puts)
            new_strike = original_strike - (spot * 0.02) if opt_type == "PE" else original_strike + (spot * 0.02)
            new_leg = min(type_chain, key=lambda x: abs(x["strike"] - new_strike), default=None)

            if new_leg:
                roll_credit = new_leg.get("ltp", 0)
                roll_cost = current_premium - roll_credit
                roll_fees = calculate_fees("BUY", current_premium, lot_size)["total"] + \
                           calculate_fees("SELL", roll_credit, lot_size)["total"]

                is_good_roll = roll_cost < 0.5 * entry_premium

                adjustments.append({
                    "type": "ROLL_DOWN_OUT",
                    "description": f"Buy back current + Sell {new_leg['strike']} {opt_type} next expiry",
                    "cost": round(roll_cost * lot_size + roll_fees, 2),
                    "new_strike": new_leg["strike"],
                    "new_premium": roll_credit,
                    "roll_cost_per_unit": round(roll_cost, 2),
                    "fees": round(roll_fees, 2),
                    "slippage": estimate_slippage(current_premium, symbol),
                    "is_good_roll": is_good_roll,
                    "recommendation": "Good roll" if is_good_roll else "Bad roll - consider exiting instead",
                    "legs": [
                        {"action": "BUY", "type": opt_type, "strike": original_strike,
                         "qty": lot_size, "premium": current_premium},
                        {"action": "SELL", "type": opt_type, "strike": new_leg["strike"],
                         "qty": lot_size, "premium": roll_credit},
                    ],
                })

    # --- Adjustment 3: CONVERT TO SPREAD ---
    if legs:
        leg = legs[0]
        opt_type = leg.get("type", "PE")
        original_strike = leg.get("strike", spot)

        # Buy protective option below current strike
        protection_strike = original_strike - (spot * 0.03) if opt_type == "PE" else original_strike + (spot * 0.03)
        chain = kite_svc.get_option_chain(symbol)
        type_chain = [c for c in chain if c.get("option_type") == opt_type]
        protect_leg = min(type_chain, key=lambda x: abs(x["strike"] - protection_strike), default=None)

        if protect_leg:
            protect_cost = protect_leg.get("ltp", 0)
            max_loss_after = abs(original_strike - protect_leg["strike"]) * lot_size - entry_premium * lot_size
            protect_fees = calculate_fees("BUY", protect_cost, lot_size)

            adjustments.append({
                "type": "CONVERT_TO_SPREAD",
                "description": f"Buy {protect_leg['strike']} {opt_type} to cap downside",
                "cost": round(protect_cost * lot_size + protect_fees["total"], 2),
                "protection_strike": protect_leg["strike"],
                "protection_premium": protect_cost,
                "max_loss_after": round(max_loss_after, 2),
                "fees": protect_fees,
                "recommendation": "Best if unsure about direction, want to cap max loss",
                "legs": [
                    {"action": "BUY", "type": opt_type, "strike": protect_leg["strike"],
                     "qty": lot_size, "premium": protect_cost},
                ],
            })

    # --- Adjustment 4: DO NOTHING ---
    if legs and spot:
        leg = legs[0]
        strike = leg.get("strike", spot)
        opt_type = leg.get("type", "PE")
        T = 7 / 365.0

        prob_recovery = probability_otm(spot, strike, T, sigma=0.2, option_type=opt_type)

        best_case = entry_premium * lot_size  # Full premium kept
        base_case = -(current_premium - entry_premium) * lot_size * 0.5
        worst_case = -(spot * 0.05) * lot_size  # 5% further move

        adjustments.append({
            "type": "DO_NOTHING",
            "description": "Hold position and wait for recovery",
            "cost": 0,
            "probability_recovery": round(prob_recovery * 100, 1),
            "scenarios": {
                "best_case": {"description": "Underlying recovers above strike",
                             "pnl": round(best_case, 2), "probability": f"{prob_recovery*100:.0f}%"},
                "base_case": {"description": "Stays near current level",
                             "pnl": round(base_case, 2)},
                "worst_case": {"description": "Drops further 5%",
                              "pnl": round(worst_case, 2)},
            },
            "recommendation": "Best if DTE > 3 days AND delta < 0.55",
        })

    return adjustments


def place_gtt_stop_loss(trade_id, position, kite_svc):
    """Place GTT stop-loss order after trade execution."""
    stop_loss_mult = float(get_setting("stop_loss_multiplier", "2.0"))
    entry_premium = position["entry_premium"]
    trigger_price = entry_premium * stop_loss_mult

    legs = json.loads(position["legs"])
    if not legs:
        return None

    leg = legs[0]
    symbol = f"{position['symbol']}{leg['strike']}{leg['type']}"

    result = kite_svc.place_gtt({
        "tradingsymbol": symbol,
        "exchange": "NFO",
        "trigger_price": trigger_price,
        "last_price": entry_premium,
        "quantity": leg["qty"],
    })

    if result.get("success"):
        gtt_id = create_gtt_order({
            "trade_id": trade_id,
            "kite_gtt_id": result.get("gtt_id"),
            "symbol": symbol,
            "trigger_type": "STOP_LOSS",
            "trigger_price": trigger_price,
            "order_type": "MARKET",
            "quantity": leg["qty"],
            "exchange": "NFO",
        })

        create_notification("GTT_PLACED", f"GTT Stop-Loss Placed",
                          f"Stop-loss at Rs {trigger_price:.2f} for {symbol}",
                          "INFO", "/positions")
        return gtt_id

    create_notification("GTT_PLACED", "GTT Placement Failed",
                      f"Failed to place GTT for {symbol}: {result.get('error')}",
                      "URGENT", "/positions")
    return None


def check_expiry_itm(kite_svc):
    """Check for ITM positions on expiry day. Called at 2:00 PM and 3:00 PM."""
    from fee_calculator import calculate_exercise_stt
    positions = get_active_positions()
    alerts = []

    for pos in positions:
        # Check if expiring today (simplified - in prod check actual expiry)
        legs = json.loads(pos["legs"])
        symbol = pos["symbol"]
        spot = kite_svc.get_ltp(symbol)
        if not spot:
            continue

        for leg in legs:
            strike = leg.get("strike", 0)
            opt_type = leg.get("type", "PE")
            qty = leg.get("qty", 0)

            # Check if ITM
            is_itm = (opt_type == "CE" and spot > strike) or (opt_type == "PE" and spot < strike)

            if is_itm:
                intrinsic = abs(spot - strike)
                exercise_stt = calculate_exercise_stt(intrinsic, qty)
                manual_close_fees = calculate_fees("BUY", leg.get("premium", 0), qty)["total"]
                savings = exercise_stt - manual_close_fees

                alert = {
                    "type": "EXPIRY_ITM_STT",
                    "position_id": pos["id"],
                    "symbol": symbol,
                    "strike": strike,
                    "intrinsic": intrinsic,
                    "exercise_stt": round(exercise_stt, 2),
                    "manual_close_cost": round(manual_close_fees, 2),
                    "savings": round(savings, 2),
                }
                alerts.append(alert)

                if savings > 0:
                    create_notification(
                        "EXPIRY_ITM_STT",
                        f"Close {symbol} {strike} {opt_type} before 3:25 PM",
                        f"Exercise STT: Rs {exercise_stt:.0f} vs manual close: Rs {manual_close_fees:.0f}. "
                        f"Save Rs {savings:.0f} by closing manually.",
                        "URGENT", "/positions"
                    )

    return alerts


def get_risk_summary(kite_svc):
    """Get current risk status summary."""
    positions = get_active_positions()
    total_delta = 0
    total_margin = 0
    total_unrealized = 0
    at_risk_count = 0

    for pos in positions:
        total_margin += pos.get("margin_blocked", 0)
        total_unrealized += pos.get("unrealized_pnl", 0)

        legs = json.loads(pos["legs"])
        if legs:
            leg = legs[0]
            spot = kite_svc.get_ltp(pos["symbol"]) or 0
            if spot:
                T = 7 / 365.0
                d = calc_delta(spot, leg.get("strike", spot), T,
                              sigma=0.2, option_type=leg.get("type", "PE"))
                total_delta += d
                if abs(d) > 0.30:
                    at_risk_count += 1

    daily_loss_limit = float(get_setting("daily_loss_limit", "25000"))

    return {
        "portfolio_net_delta": round(total_delta, 3),
        "total_margin_used": round(total_margin, 2),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "open_positions": len(positions),
        "at_risk_count": at_risk_count,
        "daily_loss_limit": daily_loss_limit,
        "daily_loss_pct": round(abs(total_unrealized) / daily_loss_limit * 100, 1) if daily_loss_limit > 0 else 0,
        "circuit_breaker_enabled": get_setting("circuit_breaker_enabled", "false") == "true",
    }


def _trigger_circuit_breaker(kite_svc):
    """Circuit breaker: auto-close ALL positions."""
    from trade_tracker import close_position_manual
    positions = get_active_positions()

    for pos in positions:
        current = pos.get("current_premium", pos["entry_premium"])
        close_position_manual(pos, current, kite_svc)

    create_notification(
        "CIRCUIT_BREAKER",
        "CIRCUIT BREAKER TRIGGERED",
        f"All {len(positions)} positions auto-closed due to daily loss limit breach.",
        "URGENT", "/risk"
    )
