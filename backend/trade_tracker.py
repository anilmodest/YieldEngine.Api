"""
Position tracking, P&L calculation, and MTM updates.
"""

import json
from datetime import datetime, date
from models import (create_trade, close_trade, get_open_trades, create_position,
                    get_active_positions, update_position_mtm, close_position,
                    create_notification, upsert_daily_summary, generate_id, now_iso)
from fee_calculator import calculate_fees, calculate_trade_fees


def execute_trade(recommendation, kite_svc):
    """
    Execute a trade from a recommendation and track it.

    Args:
        recommendation: strategy recommendation dict
        kite_svc: KiteService instance

    Returns:
        dict with trade result
    """
    # Place orders for each leg
    order_results = []
    for leg in recommendation["legs"]:
        result = kite_svc.place_order({
            "tradingsymbol": f"{recommendation['symbol']}{leg['strike']}{leg['type']}",
            "action": leg["action"],
            "qty": leg["qty"],
            "price": leg["premium"],
            "exchange": "NFO",
            "product": "NRML",
            "order_type": "LIMIT",
        })
        order_results.append(result)
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "Order failed"),
                    "partial_orders": order_results}

    # Record the trade
    trade_id = create_trade({
        "rec_id": recommendation["id"],
        "strategy_type": recommendation["type"],
        "symbol": recommendation["symbol"],
        "direction": recommendation["direction"],
        "legs": json.dumps(recommendation["legs"]),
        "entry_premium": recommendation.get("premium", 0),
        "margin_used": recommendation.get("margin_required", 0),
    })

    # Create position
    pos_id = create_position({
        "trade_id": trade_id,
        "symbol": recommendation["symbol"],
        "strategy_type": recommendation["type"],
        "legs": json.dumps(recommendation["legs"]),
        "entry_premium": recommendation.get("premium", 0),
        "current_premium": recommendation.get("premium", 0),
        "unrealized_pnl": 0,
        "expiry_date": recommendation.get("expiry_date"),
        "margin_blocked": recommendation.get("margin_required", 0),
    })

    # Notification
    create_notification(
        "TRADE_EXECUTED",
        f"Trade Executed: {recommendation['type']}",
        f"{recommendation['direction']} {recommendation['symbol']} "
        f"{recommendation.get('strike', '')} @ Rs {recommendation.get('premium', 0):.2f}",
        "INFO", "/positions"
    )

    # Update daily summary
    _update_daily_summary_on_trade(recommendation)

    return {
        "success": True,
        "trade_id": trade_id,
        "position_id": pos_id,
        "order_results": order_results,
    }


def close_position_manual(position, exit_premium, kite_svc):
    """
    Manually close a position.

    Args:
        position: position dict from DB
        exit_premium: exit premium per unit
        kite_svc: KiteService instance

    Returns:
        dict with close result
    """
    legs = json.loads(position["legs"])
    lot_size = legs[0]["qty"] if legs else 0

    # Calculate P&L
    entry_premium = position["entry_premium"]
    direction = "SELL"  # Most income strategies are sell

    # For sold options: PnL = (entry - exit) * qty
    # For bought options: PnL = (exit - entry) * qty
    if direction == "SELL":
        gross_pnl = (entry_premium - exit_premium) * lot_size
    else:
        gross_pnl = (exit_premium - entry_premium) * lot_size

    # Calculate fees
    entry_fees = calculate_fees("SELL", entry_premium, lot_size)
    exit_fees = calculate_fees("BUY", exit_premium, lot_size)
    total_fees = entry_fees["total"] + exit_fees["total"]
    net_pnl = gross_pnl - total_fees

    # Close in DB
    close_trade(position["trade_id"], exit_premium, "MANUAL", net_pnl, total_fees)
    close_position(position["id"])

    # Notification
    pnl_emoji = "Profit" if net_pnl > 0 else "Loss"
    create_notification(
        "TRADE_CLOSED",
        f"Position Closed: {position['symbol']}",
        f"{pnl_emoji}: Rs {net_pnl:.2f} (Gross: Rs {gross_pnl:.2f}, Fees: Rs {total_fees:.2f})",
        "INFO", "/trades"
    )

    return {
        "success": True,
        "gross_pnl": round(gross_pnl, 2),
        "fees": round(total_fees, 2),
        "net_pnl": round(net_pnl, 2),
        "fee_breakdown": {"entry": entry_fees, "exit": exit_fees},
    }


def update_all_mtm(kite_svc):
    """
    Update MTM for all active positions.
    Called every 5 minutes during market hours.
    """
    positions = get_active_positions()
    results = []

    for pos in positions:
        legs = json.loads(pos["legs"])
        symbol = pos["symbol"]

        # Get current premium for each leg
        total_current = 0
        for leg in legs:
            ltp = kite_svc.get_ltp(
                f"{symbol}{leg['strike']}{leg['type']}"
            )
            if ltp is None:
                ltp = leg.get("premium", 0)
            total_current += ltp if leg["action"] == "BUY" else -ltp

        # For sold positions, unrealized PnL = entry - current
        entry = pos["entry_premium"]
        unrealized = (entry - abs(total_current)) * (legs[0]["qty"] if legs else 1)

        update_position_mtm(pos["id"], abs(total_current), unrealized)
        results.append({
            "position_id": pos["id"],
            "symbol": symbol,
            "entry_premium": entry,
            "current_premium": abs(total_current),
            "unrealized_pnl": round(unrealized, 2),
        })

    return results


def get_analytics_summary():
    """Get overall P&L summary."""
    from models import get_db
    conn = get_db()

    # Closed trades summary
    closed = conn.execute("""
        SELECT COUNT(*) as total_trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as total_income,
               SUM(CASE WHEN pnl <= 0 THEN pnl ELSE 0 END) as total_loss,
               SUM(pnl) as net_pnl,
               SUM(fees) as total_fees
        FROM trades WHERE status = 'CLOSED'
    """).fetchone()

    # Open positions
    open_positions = conn.execute("""
        SELECT COUNT(*) as count, SUM(unrealized_pnl) as total_unrealized
        FROM positions WHERE status = 'ACTIVE'
    """).fetchone()

    conn.close()

    total_trades = closed["total_trades"] or 0
    wins = closed["wins"] or 0

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": closed["losses"] or 0,
        "win_rate": round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
        "total_income": round(closed["total_income"] or 0, 2),
        "total_loss": round(closed["total_loss"] or 0, 2),
        "net_pnl": round(closed["net_pnl"] or 0, 2),
        "total_fees": round(closed["total_fees"] or 0, 2),
        "open_positions": open_positions["count"] or 0,
        "total_unrealized": round(open_positions["total_unrealized"] or 0, 2),
    }


def get_strategy_breakdown():
    """Get performance breakdown by strategy type."""
    from models import get_db
    conn = get_db()

    rows = conn.execute("""
        SELECT strategy_type,
               COUNT(*) as trade_count,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(pnl) as total_pnl,
               SUM(fees) as total_fees,
               AVG(pnl) as avg_pnl,
               MAX(pnl) as best_trade,
               MIN(pnl) as worst_trade
        FROM trades WHERE status = 'CLOSED'
        GROUP BY strategy_type
    """).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_monthly_pnl():
    """Get monthly P&L data for charts."""
    from models import get_db
    conn = get_db()

    rows = conn.execute("""
        SELECT strftime('%Y-%m', exit_time) as month,
               SUM(pnl) as net_pnl,
               SUM(pnl + fees) as gross_pnl,
               SUM(fees) as fees,
               COUNT(*) as trades
        FROM trades WHERE status = 'CLOSED' AND exit_time IS NOT NULL
        GROUP BY strftime('%Y-%m', exit_time)
        ORDER BY month DESC
        LIMIT 12
    """).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def _update_daily_summary_on_trade(recommendation):
    """Update daily summary after a trade."""
    today = str(date.today())
    from models import get_daily_summary
    existing = get_daily_summary(today)

    data = {
        "open_positions": (existing["open_positions"] if existing else 0) + 1,
        "trades_executed": (existing["trades_executed"] if existing else 0) + 1,
        "premium_collected": (existing["premium_collected"] if existing else 0) +
                            (recommendation.get("total_premium", 0) if recommendation.get("direction") == "SELL" else 0),
        "premium_paid": (existing["premium_paid"] if existing else 0) +
                       (recommendation.get("total_premium", 0) if recommendation.get("direction") == "BUY" else 0),
        "margin_used": (existing["margin_used"] if existing else 0) +
                      recommendation.get("margin_required", 0),
    }

    upsert_daily_summary(today, data)
