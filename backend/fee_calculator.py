"""
Exact Indian market fee computation for F&O trades.
Covers: brokerage, STT, exchange charges, SEBI charges, stamp duty, GST, IPFT.
"""


def calculate_fees(transaction_type, premium, quantity, is_exercise=False):
    """
    Calculate exact trading fees for an F&O order.

    Args:
        transaction_type: "BUY" or "SELL"
        premium: option premium per unit
        quantity: total quantity (lot_size * lots)
        is_exercise: True if this is an exercise/assignment (different STT rate)

    Returns:
        dict with individual fee components and total
    """
    turnover = premium * quantity

    fees = {
        "brokerage": min(20, turnover * 0.0003),       # Rs 20 or 0.03%, whichever is lower
        "stt": 0.0,
        "exchange_txn": turnover * 0.000495,            # 0.0495% of premium turnover
        "sebi_charges": turnover * 0.000001,            # Rs 10 per crore
        "stamp_duty": 0.0,
        "gst": 0.0,
        "ipft": turnover * 0.0000001,                   # Rs 0.01 per crore
    }

    # STT: only on sell side for F&O
    if transaction_type == "SELL":
        if is_exercise:
            # STT on exercise = 0.125% of intrinsic value (not premium)
            fees["stt"] = turnover * 0.00125
        else:
            fees["stt"] = turnover * 0.000625            # 0.0625% of premium

    # Stamp duty: only on buy side
    if transaction_type == "BUY":
        fees["stamp_duty"] = turnover * 0.00003          # 0.003%

    # GST: 18% of (brokerage + exchange_txn + sebi)
    fees["gst"] = (fees["brokerage"] + fees["exchange_txn"] + fees["sebi_charges"]) * 0.18

    fees["total"] = sum(fees.values())

    return fees


def calculate_trade_fees(legs):
    """
    Calculate total fees for a multi-leg trade.

    Args:
        legs: list of dicts with keys: action (BUY/SELL), premium, quantity

    Returns:
        dict with per-leg fees and total
    """
    total_fees = {
        "brokerage": 0.0,
        "stt": 0.0,
        "exchange_txn": 0.0,
        "sebi_charges": 0.0,
        "stamp_duty": 0.0,
        "gst": 0.0,
        "ipft": 0.0,
        "total": 0.0,
    }
    leg_fees = []

    for leg in legs:
        lf = calculate_fees(
            transaction_type=leg["action"],
            premium=leg["premium"],
            quantity=leg["quantity"],
            is_exercise=leg.get("is_exercise", False),
        )
        leg_fees.append(lf)
        for key in total_fees:
            total_fees[key] += lf[key]

    return {"total": total_fees, "per_leg": leg_fees}


def calculate_exercise_stt(intrinsic_value, quantity):
    """
    Calculate STT cost if option is exercised at expiry.
    Used to compare exercise STT vs manual close cost.

    Args:
        intrinsic_value: how much the option is ITM per unit
        quantity: total quantity

    Returns:
        Exercise STT amount in Rs
    """
    return intrinsic_value * quantity * 0.00125  # 0.125% of intrinsic value


def estimate_slippage(current_premium, symbol, is_volatile=False):
    """
    Estimate slippage for an order.

    Args:
        current_premium: current option premium
        symbol: trading symbol
        is_volatile: True during volatile market conditions

    Returns:
        Estimated slippage per unit in Rs
    """
    # NIFTY/BANKNIFTY options are very liquid
    if "NIFTY" in symbol.upper():
        base_slippage = max(1.0, current_premium * 0.005)  # Rs 1 or 0.5%
    else:
        # Stock options are less liquid
        base_slippage = max(2.0, current_premium * 0.01)   # Rs 2 or 1%

    if is_volatile:
        base_slippage *= 2

    return round(base_slippage, 2)


def format_fee_breakdown(fees):
    """Format fee breakdown for display."""
    return (
        f"Brokerage: Rs {fees['brokerage']:.2f} | "
        f"STT: Rs {fees['stt']:.2f} | "
        f"Exchange: Rs {fees['exchange_txn']:.2f} | "
        f"GST: Rs {fees['gst']:.2f} | "
        f"Stamp: Rs {fees['stamp_duty']:.2f} | "
        f"SEBI: Rs {fees['sebi_charges']:.2f} | "
        f"Total: Rs {fees['total']:.2f}"
    )


def net_pnl(gross_pnl, entry_fees, exit_fees):
    """Calculate net P&L after fees."""
    return gross_pnl - entry_fees["total"] - exit_fees["total"]
