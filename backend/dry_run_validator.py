"""
Mandatory pre-order validation. Every order MUST pass all 8 checks.
No bypass mechanism exists.
"""

from models import SAFETY_HARD_CAPS, get_db


def validate_order(order_legs, kite_svc):
    """
    Validate order against all safety hard caps.
    Returns: {"valid": bool, "errors": list}
    """
    errors = []

    for leg in order_legs:
        symbol = leg.get("tradingsymbol", "")

        # 1. Quantity cap
        if "NIFTY" in symbol and "BANK" not in symbol:
            max_lots = SAFETY_HARD_CAPS["MAX_LOTS_PER_ORDER_NIFTY"]
            lot_size = 25
        elif "BANKNIFTY" in symbol:
            max_lots = SAFETY_HARD_CAPS["MAX_LOTS_PER_ORDER_BANKNIFTY"]
            lot_size = 15
        else:
            max_lots = SAFETY_HARD_CAPS["MAX_LOTS_PER_ORDER_STOCK"]
            lot_size = kite_svc.get_lot_size(symbol.split(":")[0] if ":" in symbol else symbol[:10])

        if leg.get("qty", 0) > max_lots * lot_size:
            errors.append(
                f"QUANTITY EXCEEDED: {leg['qty']} > max {max_lots * lot_size} for {symbol}"
            )

        # 2. Order value cap
        order_value = leg.get("price", 0) * leg.get("qty", 0)
        if order_value > SAFETY_HARD_CAPS["MAX_ORDER_VALUE"]:
            errors.append(
                f"ORDER VALUE EXCEEDED: Rs {order_value} > max Rs {SAFETY_HARD_CAPS['MAX_ORDER_VALUE']}"
            )

        # 3. Price sanity check
        ltp = kite_svc.get_ltp(symbol)
        if ltp and leg.get("price"):
            deviation = abs(leg["price"] - ltp) / ltp
            if deviation > SAFETY_HARD_CAPS["PRICE_DEVIATION_LIMIT"]:
                errors.append(
                    f"PRICE DEVIATION: Rs {leg['price']} is >{SAFETY_HARD_CAPS['PRICE_DEVIATION_LIMIT']*100}% "
                    f"from LTP Rs {ltp}"
                )

        # 4. Exchange whitelist
        exchange = leg.get("exchange", "NFO")
        if exchange not in SAFETY_HARD_CAPS["ALLOWED_EXCHANGES"]:
            errors.append(f"EXCHANGE NOT ALLOWED: {exchange}")

        # 5. Product whitelist
        product = leg.get("product", "NRML")
        if product not in SAFETY_HARD_CAPS["ALLOWED_PRODUCTS"]:
            errors.append(f"PRODUCT NOT ALLOWED: {product}")

    # 6. Daily order count
    today_count = _get_today_order_count()
    if today_count >= SAFETY_HARD_CAPS["MAX_ORDERS_PER_DAY"]:
        errors.append(
            f"DAILY ORDER LIMIT: {today_count} orders placed (max {SAFETY_HARD_CAPS['MAX_ORDERS_PER_DAY']})"
        )

    # 7. Open position count
    open_count = _get_open_position_count()
    if open_count >= SAFETY_HARD_CAPS["MAX_OPEN_POSITIONS"]:
        errors.append(
            f"MAX OPEN POSITIONS: {open_count} open (max {SAFETY_HARD_CAPS['MAX_OPEN_POSITIONS']})"
        )

    # 8. F&O symbol validation
    for leg in order_legs:
        symbol = leg.get("tradingsymbol", "")
        if not _is_valid_fno_symbol(symbol):
            errors.append(f"INVALID SYMBOL: {symbol} not in F&O approved list")

    return {"valid": len(errors) == 0, "errors": errors}


def _get_today_order_count():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM order_audit WHERE date(timestamp) = date('now') AND status = 'EXECUTED'"
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def _get_open_position_count():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM positions WHERE status = 'ACTIVE'"
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def _is_valid_fno_symbol(symbol):
    """Check if symbol is a valid F&O instrument."""
    valid_prefixes = [
        "NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY",
        "BEL", "SBIN", "HAL", "ICICIBANK", "ITC", "LT", "TATAMOTORS",
        "AXISBANK", "KOTAKBANK", "MARUTI", "BAJFINANCE", "TITAN",
        "HINDUNILVR", "WIPRO", "TECHM", "ADANIENT", "ADANIPORTS",
    ]
    return any(symbol.startswith(p) for p in valid_prefixes)
