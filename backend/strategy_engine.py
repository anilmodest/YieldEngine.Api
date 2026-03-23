"""
Strategy scanner for option income strategies.
Scans for: Covered Calls, Cash-Secured Puts, Put Credit Spreads, Collars.
"""

import json
from black_scholes import option_price, greeks, probability_otm, RISK_FREE_RATE
from strike_selector import select_strike, RISK_PROFILES
from fee_calculator import calculate_trade_fees, estimate_slippage
from models import generate_id, get_setting


def get_allowed_strategies():
    allowed = get_setting("allowed_strategies", "COVERED_CALL,CASH_SECURED_PUT,PUT_CREDIT_SPREAD,COLLAR,CASH_FUTURES_ARB")
    return [s.strip() for s in allowed.split(",")]


def scan_strategies(kite_svc, risk_profile="moderate", cash_balance=0):
    """
    Scan all available strategies and return ranked recommendations.

    Args:
        kite_svc: KiteService instance
        risk_profile: conservative/moderate/aggressive
        cash_balance: available cash for margin

    Returns:
        list of recommendation dicts, ranked by score
    """
    holdings = kite_svc.get_holdings()
    allowed = get_allowed_strategies()
    recommendations = []
    rank = 1

    # Covered Calls - on F&O eligible holdings
    if "COVERED_CALL" in allowed:
        for h in holdings:
            symbol = h.get("tradingsymbol", h.get("symbol", ""))
            qty = h.get("quantity", 0)
            lot_size = h.get("lot_size") or kite_svc.get_lot_size(symbol)
            if qty < lot_size:
                continue

            spot = h.get("last_price") or kite_svc.get_ltp(symbol) or 0
            if spot <= 0:
                continue

            chain = kite_svc.get_option_chain(symbol)
            ce_chain = [c for c in chain if c.get("option_type") == "CE"]
            if not ce_chain:
                continue

            result = select_strike(symbol, spot, ce_chain, "CE", risk_profile,
                                  iv_rank=50, dte=7, sigma=_get_iv(symbol, kite_svc))

            if "error" in result or result.get("strategy") == "SPREAD_ONLY":
                continue

            lots = qty // lot_size
            premium_per_lot = result["premium"] * lot_size
            total_premium = premium_per_lot * lots
            ann_return = (result["premium"] / spot) * (365 / 7) * 100

            fees = calculate_trade_fees([{
                "action": "SELL", "premium": result["premium"],
                "quantity": lot_size * lots
            }])

            rec = {
                "id": generate_id(),
                "rank": rank,
                "type": "COVERED_CALL",
                "symbol": symbol,
                "direction": "SELL",
                "strike": result["strike"],
                "option_type": "CE",
                "premium": result["premium"],
                "total_premium": total_premium,
                "lots": lots,
                "lot_size": lot_size,
                "margin_required": 0,  # covered by holdings
                "annualized_return": round(ann_return, 1),
                "prob_otm": result["prob_otm"],
                "delta": result["actual_delta"],
                "theta": result["greeks"]["theta"],
                "safety": result["safety"],
                "risk_profile": risk_profile,
                "rationale": result["rationale"],
                "alternatives": result.get("alternatives", {}),
                "fees_estimate": fees["total"],
                "max_loss": "Opportunity cost only (shares called away above strike)",
                "legs": [
                    {"action": "SELL", "type": "CE", "strike": result["strike"],
                     "qty": lot_size * lots, "premium": result["premium"]}
                ],
            }
            recommendations.append(rec)
            rank += 1

    # Cash-Secured Puts - on indices
    if "CASH_SECURED_PUT" in allowed:
        for symbol in ["NIFTY", "BANKNIFTY"]:
            spot = kite_svc.get_ltp(symbol)
            if not spot:
                continue

            chain = kite_svc.get_option_chain(symbol)
            pe_chain = [c for c in chain if c.get("option_type") == "PE"]
            if not pe_chain:
                continue

            result = select_strike(symbol, spot, pe_chain, "PE", risk_profile,
                                  iv_rank=50, dte=7, sigma=_get_iv(symbol, kite_svc))

            if "error" in result:
                continue
            if result.get("strategy") == "SPREAD_ONLY":
                continue

            lot_size = kite_svc.get_lot_size(symbol)
            premium_total = result["premium"] * lot_size
            margin_required = spot * lot_size * 0.15  # ~15% margin
            ann_return = (result["premium"] / spot) * (365 / 7) * 100

            fees = calculate_trade_fees([{
                "action": "SELL", "premium": result["premium"],
                "quantity": lot_size
            }])

            rec = {
                "id": generate_id(),
                "rank": rank,
                "type": "CASH_SECURED_PUT",
                "symbol": symbol,
                "direction": "SELL",
                "strike": result["strike"],
                "option_type": "PE",
                "premium": result["premium"],
                "total_premium": premium_total,
                "lots": 1,
                "lot_size": lot_size,
                "margin_required": round(margin_required),
                "annualized_return": round(ann_return, 1),
                "prob_otm": result["prob_otm"],
                "delta": result["actual_delta"],
                "theta": result["greeks"]["theta"],
                "safety": result["safety"],
                "risk_profile": risk_profile,
                "rationale": result["rationale"],
                "alternatives": result.get("alternatives", {}),
                "fees_estimate": fees["total"],
                "max_loss": f"Unlimited (naked put on {symbol})",
                "legs": [
                    {"action": "SELL", "type": "PE", "strike": result["strike"],
                     "qty": lot_size, "premium": result["premium"]}
                ],
            }
            recommendations.append(rec)
            rank += 1

    # Put Credit Spreads - defined risk
    if "PUT_CREDIT_SPREAD" in allowed:
        for symbol in ["NIFTY", "BANKNIFTY"]:
            spot = kite_svc.get_ltp(symbol)
            if not spot:
                continue

            chain = kite_svc.get_option_chain(symbol)
            pe_chain = sorted([c for c in chain if c.get("option_type") == "PE"],
                            key=lambda x: x["strike"], reverse=True)
            if len(pe_chain) < 2:
                continue

            result = select_strike(symbol, spot, pe_chain, "PE", risk_profile,
                                  iv_rank=50, dte=7, sigma=_get_iv(symbol, kite_svc))

            if "error" in result:
                continue

            sell_strike = result["strike"]
            lot_size = kite_svc.get_lot_size(symbol)

            # Buy strike: 200-300 points below for NIFTY, 400-600 for BANKNIFTY
            width = 200 if symbol == "NIFTY" else 500
            buy_strike = sell_strike - width

            # Find buy leg premium
            buy_leg = next((c for c in pe_chain if c["strike"] == buy_strike), None)
            if not buy_leg:
                # Find closest
                buy_leg = min(pe_chain, key=lambda x: abs(x["strike"] - buy_strike), default=None)
                if not buy_leg:
                    continue
                buy_strike = buy_leg["strike"]

            buy_premium = buy_leg.get("ltp", 0)
            net_credit = result["premium"] - buy_premium
            if net_credit <= 0:
                continue

            max_loss = (sell_strike - buy_strike) * lot_size - net_credit * lot_size
            ann_return = (net_credit / (sell_strike - buy_strike)) * (365 / 7) * 100

            fees = calculate_trade_fees([
                {"action": "SELL", "premium": result["premium"], "quantity": lot_size},
                {"action": "BUY", "premium": buy_premium, "quantity": lot_size},
            ])

            rec = {
                "id": generate_id(),
                "rank": rank,
                "type": "PUT_CREDIT_SPREAD",
                "symbol": symbol,
                "direction": "SELL",
                "strike": sell_strike,
                "buy_strike": buy_strike,
                "option_type": "PE",
                "premium": net_credit,
                "total_premium": net_credit * lot_size,
                "lots": 1,
                "lot_size": lot_size,
                "margin_required": round(max_loss * 0.5),
                "annualized_return": round(ann_return, 1),
                "prob_otm": result["prob_otm"],
                "delta": result["actual_delta"],
                "theta": result["greeks"]["theta"],
                "safety": result["safety"],
                "risk_profile": risk_profile,
                "rationale": result["rationale"],
                "alternatives": result.get("alternatives", {}),
                "fees_estimate": fees["total"],
                "max_loss": f"Capped at Rs {max_loss:.0f}",
                "legs": [
                    {"action": "SELL", "type": "PE", "strike": sell_strike,
                     "qty": lot_size, "premium": result["premium"]},
                    {"action": "BUY", "type": "PE", "strike": buy_strike,
                     "qty": lot_size, "premium": buy_premium},
                ],
            }
            recommendations.append(rec)
            rank += 1

    # Collars - on profitable positions (>8% gain)
    if "COLLAR" in allowed:
        for h in holdings:
            symbol = h.get("tradingsymbol", h.get("symbol", ""))
            avg_price = h.get("average_price", 0)
            spot = h.get("last_price") or kite_svc.get_ltp(symbol) or 0
            qty = h.get("quantity", 0)
            lot_size = h.get("lot_size") or kite_svc.get_lot_size(symbol)

            if qty < lot_size or avg_price <= 0 or spot <= 0:
                continue

            gain_pct = (spot - avg_price) / avg_price
            if gain_pct < 0.08:
                continue

            chain = kite_svc.get_option_chain(symbol)
            ce_chain = [c for c in chain if c.get("option_type") == "CE"]
            pe_chain = [c for c in chain if c.get("option_type") == "PE"]

            if not ce_chain or not pe_chain:
                continue

            ce_result = select_strike(symbol, spot, ce_chain, "CE", risk_profile,
                                    iv_rank=50, dte=7, sigma=_get_iv(symbol, kite_svc))
            pe_result = select_strike(symbol, spot, pe_chain, "PE", risk_profile,
                                    iv_rank=50, dte=7, sigma=_get_iv(symbol, kite_svc))

            if "error" in ce_result or "error" in pe_result:
                continue

            net_cost = pe_result["premium"] - ce_result["premium"]
            lots = qty // lot_size

            rec = {
                "id": generate_id(),
                "rank": rank,
                "type": "COLLAR",
                "symbol": symbol,
                "direction": "HEDGE",
                "strike": ce_result["strike"],
                "buy_strike": pe_result["strike"],
                "option_type": "COLLAR",
                "premium": -net_cost,
                "total_premium": -net_cost * lot_size * lots,
                "lots": lots,
                "lot_size": lot_size,
                "margin_required": 0,
                "annualized_return": round((-net_cost / spot) * (365 / 7) * 100, 1),
                "prob_otm": ce_result["prob_otm"],
                "delta": 0,
                "theta": ce_result["greeks"]["theta"] - pe_result["greeks"]["theta"],
                "safety": "VERY_SAFE",
                "risk_profile": risk_profile,
                "rationale": f"Collar on {symbol} with {gain_pct*100:.0f}% unrealized gain. "
                            f"Sell {ce_result['strike']} CE, Buy {pe_result['strike']} PE. "
                            f"Net cost: Rs {net_cost:.2f}/unit",
                "alternatives": {},
                "fees_estimate": {"total": 0},
                "max_loss": f"Downside protected below {pe_result['strike']}",
                "legs": [
                    {"action": "SELL", "type": "CE", "strike": ce_result["strike"],
                     "qty": lot_size * lots, "premium": ce_result["premium"]},
                    {"action": "BUY", "type": "PE", "strike": pe_result["strike"],
                     "qty": lot_size * lots, "premium": pe_result["premium"]},
                ],
            }
            recommendations.append(rec)
            rank += 1

    # Sort by annualized return descending, with safety as tiebreaker
    safety_order = {"VERY_SAFE": 0, "SAFE": 1, "MODERATE": 2, "AGGRESSIVE": 3}
    recommendations.sort(key=lambda r: (-r.get("annualized_return", 0),
                                         safety_order.get(r.get("safety", "MODERATE"), 2)))

    # Re-rank after sort
    for i, rec in enumerate(recommendations):
        rec["rank"] = i + 1

    return recommendations


def _get_iv(symbol, kite_svc):
    """Get IV for a symbol from simulation data or Kite."""
    from kite_service import SIMULATION_STOCKS
    stock = SIMULATION_STOCKS.get(symbol)
    return stock["iv"] if stock else 0.2
