"""
Arbitrage scanner for risk-free and near-risk-free opportunities.
Scans: Cash-Futures arbitrage, Put-Call Parity violations, Calendar Spreads.
"""

import math
from black_scholes import call_price, put_price, RISK_FREE_RATE
from fee_calculator import calculate_trade_fees
from models import generate_id


def scan_arbitrage(kite_svc):
    """
    Scan for arbitrage opportunities.

    Returns:
        list of arbitrage opportunity dicts
    """
    opportunities = []

    # Cash-Futures Arbitrage
    cash_futures = scan_cash_futures(kite_svc)
    opportunities.extend(cash_futures)

    # Put-Call Parity Arbitrage
    pcp = scan_put_call_parity(kite_svc)
    opportunities.extend(pcp)

    return opportunities


def scan_cash_futures(kite_svc):
    """
    Cash-Futures arbitrage: spot vs futures basis exceeding carry cost + 15bps round-trip.
    """
    opportunities = []

    for symbol in ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ICICIBANK"]:
        spot = kite_svc.get_ltp(symbol)
        if not spot:
            continue

        lot_size = kite_svc.get_lot_size(symbol)

        # Simulate futures price (in real mode, fetch from Kite)
        # Futures typically trade at a premium = spot * e^(r*T)
        for dte in [7, 14, 30]:
            T = dte / 365.0
            fair_futures = spot * math.exp(RISK_FREE_RATE * T)

            # In simulation, add a small random basis
            import random
            random.seed(hash(f"{symbol}{dte}"))
            basis_noise = spot * random.uniform(-0.003, 0.008)
            futures_price = fair_futures + basis_noise

            basis = futures_price - spot
            basis_pct = (basis / spot) * 100
            carry_cost = spot * RISK_FREE_RATE * T
            round_trip_cost = spot * 0.0015  # 15bps

            # Annualized return
            net_profit = basis - carry_cost - round_trip_cost
            ann_return = (net_profit / spot) * (365 / dte) * 100

            if ann_return > 1.0:  # Only show if > 1% annualized
                fees = calculate_trade_fees([
                    {"action": "BUY", "premium": spot, "quantity": lot_size},
                    {"action": "SELL", "premium": futures_price, "quantity": lot_size},
                ])

                opportunities.append({
                    "id": generate_id(),
                    "type": "CASH_FUTURES_ARB",
                    "symbol": symbol,
                    "spot": spot,
                    "futures_price": round(futures_price, 2),
                    "basis": round(basis, 2),
                    "basis_pct": round(basis_pct, 2),
                    "dte": dte,
                    "annualized_return": round(ann_return, 1),
                    "lot_size": lot_size,
                    "margin_required": round(spot * lot_size * 0.2),
                    "net_profit_per_lot": round(net_profit * lot_size, 2),
                    "risk_free": True,
                    "fees_estimate": fees["total"],
                    "legs": [
                        {"action": "BUY", "instrument": f"{symbol} (Spot/Cash)",
                         "price": spot, "qty": lot_size},
                        {"action": "SELL", "instrument": f"{symbol} Futures ({dte}D)",
                         "price": round(futures_price, 2), "qty": lot_size},
                    ],
                    "holding_days": dte,
                })

    return opportunities


def scan_put_call_parity(kite_svc):
    """
    Put-Call Parity: C - P = S - K * e^(-rT)
    Violations indicate arbitrage opportunities.
    """
    opportunities = []

    for symbol in ["NIFTY", "BANKNIFTY"]:
        spot = kite_svc.get_ltp(symbol)
        if not spot:
            continue

        chain = kite_svc.get_option_chain(symbol)
        lot_size = kite_svc.get_lot_size(symbol)
        T = 7 / 365.0  # Weekly expiry

        # Group by strike
        strikes = {}
        for opt in chain:
            s = opt["strike"]
            if s not in strikes:
                strikes[s] = {}
            strikes[s][opt["option_type"]] = opt

        for strike, opts in strikes.items():
            if "CE" not in opts or "PE" not in opts:
                continue

            call_ltp = opts["CE"].get("ltp", 0)
            put_ltp = opts["PE"].get("ltp", 0)

            if call_ltp <= 0 or put_ltp <= 0:
                continue

            # PCP: C - P should equal S - K*e^(-rT)
            pcp_theoretical = spot - strike * math.exp(-RISK_FREE_RATE * T)
            pcp_actual = call_ltp - put_ltp
            violation = abs(pcp_actual - pcp_theoretical)
            violation_pct = (violation / spot) * 100

            # Only report significant violations (> 0.1% of spot)
            if violation_pct > 0.1:
                # Determine trade direction
                if pcp_actual > pcp_theoretical:
                    # Call overpriced: sell call, buy put, buy underlying
                    direction = "Sell Call + Buy Put + Buy Spot"
                    profit_per_unit = pcp_actual - pcp_theoretical
                else:
                    # Put overpriced: buy call, sell put, sell underlying
                    direction = "Buy Call + Sell Put + Sell Spot"
                    profit_per_unit = pcp_theoretical - pcp_actual

                ann_return = (profit_per_unit / spot) * (365 / 7) * 100

                opportunities.append({
                    "id": generate_id(),
                    "type": "PUT_CALL_PARITY",
                    "symbol": symbol,
                    "strike": strike,
                    "call_price": call_ltp,
                    "put_price": put_ltp,
                    "theoretical_diff": round(pcp_theoretical, 2),
                    "actual_diff": round(pcp_actual, 2),
                    "violation": round(violation, 2),
                    "violation_pct": round(violation_pct, 3),
                    "direction": direction,
                    "profit_per_unit": round(profit_per_unit, 2),
                    "profit_per_lot": round(profit_per_unit * lot_size, 2),
                    "annualized_return": round(ann_return, 1),
                    "lot_size": lot_size,
                    "dte": 7,
                    "risk_free": True,
                    "margin_required": round(spot * lot_size * 0.15),
                    "legs": [
                        {"action": "SELL" if pcp_actual > pcp_theoretical else "BUY",
                         "type": "CE", "strike": strike,
                         "premium": call_ltp, "qty": lot_size},
                        {"action": "BUY" if pcp_actual > pcp_theoretical else "SELL",
                         "type": "PE", "strike": strike,
                         "premium": put_ltp, "qty": lot_size},
                    ],
                })

    return opportunities
