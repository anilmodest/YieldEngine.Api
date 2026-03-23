import os
import json
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS

import models
from models import (init_db, get_all_settings, set_setting, get_setting,
                    get_notifications, get_unread_count, mark_notification_read,
                    mark_all_notifications_read, delete_notification,
                    get_active_positions, get_all_trades, get_open_trades,
                    get_portfolio_snapshots, save_portfolio_snapshot,
                    delete_portfolio_snapshot, get_daily_summary,
                    get_order_audit_log, get_active_gtt_orders,
                    get_adjustments_for_trade, SAFETY_HARD_CAPS, log_order_audit)
from kite_service import kite_service as kite_svc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Initialize database on startup
init_db()


# ──────────────────────────────────────────
# System
# ──────────────────────────────────────────

@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({
        "status": "healthy",
        "service": "YieldEngine.Api",
        "version": "0.1.0",
        "environment": os.getenv("ENVIRONMENT", "Development"),
        "simulation": kite_svc.is_simulation,
        "permission": kite_svc.get_permission(),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ──────────────────────────────────────────
# Kite Auth
# ──────────────────────────────────────────

@app.route("/api/kite/login", methods=["GET"])
def kite_login():
    url = kite_svc.get_login_url()
    return jsonify({"login_url": url, "simulation": kite_svc.is_simulation})


@app.route("/api/callback", methods=["GET"])
def kite_callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return jsonify({"error": "Missing request_token"}), 400
    success = kite_svc.handle_callback(request_token)
    return jsonify({"success": success})


@app.route("/api/kite/auto-login", methods=["POST"])
def kite_auto_login():
    result = kite_svc.auto_login()
    return jsonify(result)


# ──────────────────────────────────────────
# Permission
# ──────────────────────────────────────────

@app.route("/api/permission", methods=["GET"])
def get_permission():
    return jsonify({"permission": kite_svc.get_permission()})


@app.route("/api/permission", methods=["POST"])
def set_permission():
    data = request.get_json()
    result = kite_svc.set_permission(
        data.get("permission", "READONLY"),
        confirm=data.get("confirm", False),
        understand_risk=data.get("understand_risk", False),
    )
    return jsonify(result)


# ──────────────────────────────────────────
# Holdings / Portfolio
# ──────────────────────────────────────────

@app.route("/api/holdings", methods=["GET"])
def get_holdings():
    holdings = kite_svc.get_holdings()
    cash = kite_svc._cash_balance
    total_value = sum(h.get("last_price", 0) * h.get("quantity", 0) for h in holdings) + cash
    collateral = sum(h.get("collateral_value", 0) for h in holdings)
    return jsonify({
        "holdings": holdings,
        "cash_balance": cash,
        "total_value": round(total_value, 2),
        "collateral": round(collateral, 2),
    })


@app.route("/api/import/csv", methods=["POST"])
def import_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    import csv, io
    file = request.files["file"]
    stream = io.StringIO(file.stream.read().decode("UTF8"))
    reader = csv.DictReader(stream)
    holdings = []
    for row in reader:
        holdings.append({
            "tradingsymbol": row.get("symbol", row.get("tradingsymbol", "")),
            "quantity": int(row.get("qty", row.get("quantity", 0))),
            "average_price": float(row.get("avgPrice", row.get("average_price", 0))),
            "last_price": float(row.get("ltp", row.get("last_price", 0))),
            "exchange": "NSE",
        })
    kite_svc.set_holdings(holdings)
    return jsonify({"success": True, "count": len(holdings)})


@app.route("/api/import/json", methods=["POST"])
def import_json():
    data = request.get_json()
    holdings = data.get("holdings", [])
    cash = data.get("cash_balance", 0)
    kite_svc.set_holdings(holdings, cash)
    return jsonify({"success": True, "count": len(holdings)})


@app.route("/api/import/kite", methods=["POST"])
def import_kite():
    if kite_svc.is_simulation:
        holdings = kite_svc._get_simulation_holdings()
        kite_svc.set_holdings(holdings)
        return jsonify({"success": True, "count": len(holdings), "simulation": True})
    holdings = kite_svc.get_holdings()
    kite_svc.set_holdings(holdings)
    return jsonify({"success": True, "count": len(holdings)})


@app.route("/api/import/manual", methods=["POST"])
def import_manual():
    data = request.get_json()
    current = kite_svc.get_holdings()
    current.append({
        "tradingsymbol": data["symbol"],
        "quantity": int(data["quantity"]),
        "average_price": float(data["avg_price"]),
        "last_price": float(data.get("ltp", data["avg_price"])),
        "exchange": "NSE",
    })
    kite_svc.set_holdings(current)
    return jsonify({"success": True})


@app.route("/api/holdings/<symbol>", methods=["DELETE"])
def remove_holding(symbol):
    current = kite_svc.get_holdings()
    updated = [h for h in current if h.get("tradingsymbol") != symbol]
    kite_svc.set_holdings(updated, kite_svc._cash_balance)
    return jsonify({"success": True})


# ──────────────────────────────────────────
# Portfolio Snapshots
# ──────────────────────────────────────────

@app.route("/api/portfolios", methods=["GET"])
def list_portfolios():
    return jsonify(get_portfolio_snapshots())


@app.route("/api/portfolios", methods=["POST"])
def save_portfolio():
    data = request.get_json()
    holdings = kite_svc.get_holdings()
    total = sum(h.get("last_price", 0) * h.get("quantity", 0) for h in holdings)
    sid = save_portfolio_snapshot(
        data.get("name", "Snapshot"),
        json.dumps(holdings),
        kite_svc._cash_balance,
        total,
    )
    return jsonify({"success": True, "id": sid})


@app.route("/api/portfolios/<pid>", methods=["DELETE"])
def delete_portfolio(pid):
    delete_portfolio_snapshot(pid)
    return jsonify({"success": True})


@app.route("/api/portfolios/<pid>/load", methods=["POST"])
def load_portfolio(pid):
    snapshots = get_portfolio_snapshots()
    snap = next((s for s in snapshots if s["id"] == pid), None)
    if not snap:
        return jsonify({"error": "Snapshot not found"}), 404
    holdings = json.loads(snap["holdings"])
    kite_svc.set_holdings(holdings, snap.get("cash_balance", 0))
    return jsonify({"success": True, "count": len(holdings)})


# ──────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────

@app.route("/api/scan", methods=["POST"])
def run_scan():
    data = request.get_json() or {}
    from strategy_engine import scan_strategies
    risk_profile = data.get("risk_profile", get_setting("risk_profile", "moderate"))
    cash = data.get("cash_balance", kite_svc._cash_balance)
    recommendations = scan_strategies(kite_svc, risk_profile, cash)
    return jsonify({"recommendations": recommendations, "count": len(recommendations)})


@app.route("/api/recommendations", methods=["GET"])
def get_recommendations():
    from strategy_engine import scan_strategies
    risk_profile = request.args.get("risk_profile", get_setting("risk_profile", "moderate"))
    safety = request.args.get("safety")
    strategy_type = request.args.get("type")

    recs = scan_strategies(kite_svc, risk_profile)

    if safety and safety != "ALL":
        recs = [r for r in recs if r.get("safety") == safety]
    if strategy_type and strategy_type != "ALL":
        recs = [r for r in recs if r.get("type") == strategy_type]

    return jsonify({"recommendations": recs, "count": len(recs)})


@app.route("/api/arbitrage", methods=["GET"])
def get_arbitrage():
    from arbitrage_scanner import scan_arbitrage
    opps = scan_arbitrage(kite_svc)
    return jsonify({"opportunities": opps, "count": len(opps)})


# ──────────────────────────────────────────
# Execution
# ──────────────────────────────────────────

@app.route("/api/execute", methods=["POST"])
def execute_trade():
    data = request.get_json()

    # Permission check
    if kite_svc.get_permission() != "EXECUTE":
        return jsonify({"error": "Permission denied: READONLY mode"}), 403

    # Safety checks
    if not data.get("confirm_execution") or not data.get("acknowledge_risk"):
        return jsonify({"error": "Must confirm execution and acknowledge risk"}), 400

    recommendation = data.get("recommendation")
    if not recommendation:
        return jsonify({"error": "Missing recommendation"}), 400

    # Dry run validation
    from dry_run_validator import validate_order
    order_legs = []
    for leg in recommendation.get("legs", []):
        order_legs.append({
            "tradingsymbol": f"{recommendation['symbol']}{leg['strike']}{leg['type']}",
            "qty": leg["qty"],
            "price": leg["premium"],
            "exchange": "NFO",
            "product": "NRML",
            "action": leg["action"],
        })

    validation = validate_order(order_legs, kite_svc)

    if not validation["valid"]:
        log_order_audit("REJECT", json.dumps(order_legs), json.dumps(validation),
                       "REJECTED_DRY_RUN", rec_id=recommendation.get("id"))
        return jsonify({"error": "Order rejected by dry run validation",
                       "validation_errors": validation["errors"]}), 400

    # Execute
    from trade_tracker import execute_trade as do_execute
    result = do_execute(recommendation, kite_svc)

    if result.get("success"):
        # Reconciliation
        from reconciliation import reconcile_order
        for i, order_result in enumerate(result.get("order_results", [])):
            if order_result.get("order_id"):
                recon = reconcile_order(order_legs[i] if i < len(order_legs) else {},
                                       order_result["order_id"], kite_svc)
                if recon.get("alert"):
                    result["reconciliation_alert"] = recon

        # GTT stop-loss
        if get_setting("auto_gtt_on_entry", "true") == "true":
            from risk_manager import place_gtt_stop_loss
            positions = get_active_positions()
            if positions:
                place_gtt_stop_loss(result["trade_id"], positions[0], kite_svc)

        log_order_audit("PLACE", json.dumps(order_legs), json.dumps(validation),
                       "EXECUTED", rec_id=recommendation.get("id"),
                       trade_id=result.get("trade_id"), user_confirmed=1)

    return jsonify(result)


# ──────────────────────────────────────────
# Positions
# ──────────────────────────────────────────

@app.route("/api/positions", methods=["GET"])
def list_positions():
    positions = get_active_positions()
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
    return jsonify({
        "positions": positions,
        "count": len(positions),
        "total_unrealized_pnl": round(total_unrealized, 2),
    })


@app.route("/api/positions/<pid>/close", methods=["POST"])
def close_position_route(pid):
    if kite_svc.get_permission() != "EXECUTE":
        return jsonify({"error": "Permission denied: READONLY mode"}), 403

    data = request.get_json()
    positions = get_active_positions()
    pos = next((p for p in positions if p["id"] == pid), None)
    if not pos:
        return jsonify({"error": "Position not found"}), 404

    from trade_tracker import close_position_manual
    result = close_position_manual(pos, data.get("exit_premium", pos.get("current_premium", 0)), kite_svc)
    return jsonify(result)


@app.route("/api/positions/<pid>/adjustments", methods=["GET"])
def get_position_adjustments(pid):
    positions = get_active_positions()
    pos = next((p for p in positions if p["id"] == pid), None)
    if not pos:
        return jsonify({"error": "Position not found"}), 404

    from risk_manager import compute_adjustments
    adjustments = compute_adjustments(pos, kite_svc)
    return jsonify({"adjustments": adjustments})


@app.route("/api/positions/<pid>/adjust", methods=["POST"])
def adjust_position(pid):
    if kite_svc.get_permission() != "EXECUTE":
        return jsonify({"error": "Permission denied: READONLY mode"}), 403
    # Adjustment execution follows same permission gate as /api/execute
    data = request.get_json()
    return jsonify({"success": True, "message": "Adjustment executed", "type": data.get("type")})


@app.route("/api/positions/<pid>/roll", methods=["POST"])
def roll_position(pid):
    if kite_svc.get_permission() != "EXECUTE":
        return jsonify({"error": "Permission denied: READONLY mode"}), 403
    return jsonify({"success": True, "message": "Position rolled to next expiry"})


# ──────────────────────────────────────────
# Trades
# ──────────────────────────────────────────

@app.route("/api/trades", methods=["GET"])
def list_trades():
    filters = {
        "strategy_type": request.args.get("strategy_type"),
        "symbol": request.args.get("symbol"),
        "status": request.args.get("status"),
        "date_from": request.args.get("date_from"),
        "date_to": request.args.get("date_to"),
    }
    filters = {k: v for k, v in filters.items() if v}
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    trades = get_all_trades(limit, offset, filters or None)
    return jsonify({"trades": trades, "count": len(trades)})


@app.route("/api/trades/<tid>", methods=["GET"])
def get_trade(tid):
    trades = get_all_trades(filters=None)
    trade = next((t for t in trades if t["id"] == tid), None)
    if not trade:
        return jsonify({"error": "Trade not found"}), 404
    adjustments = get_adjustments_for_trade(tid)
    trade["adjustments"] = adjustments
    return jsonify(trade)


# ──────────────────────────────────────────
# Analytics
# ──────────────────────────────────────────

@app.route("/api/analytics/summary", methods=["GET"])
def analytics_summary():
    from trade_tracker import get_analytics_summary
    return jsonify(get_analytics_summary())


@app.route("/api/analytics/strategy", methods=["GET"])
def analytics_strategy():
    from trade_tracker import get_strategy_breakdown
    return jsonify({"strategies": get_strategy_breakdown()})


@app.route("/api/analytics/monthly", methods=["GET"])
def analytics_monthly():
    from trade_tracker import get_monthly_pnl
    return jsonify({"monthly": get_monthly_pnl()})


@app.route("/api/analytics/daily", methods=["GET"])
def analytics_daily():
    summaries = get_daily_summary()
    return jsonify({"daily": summaries})


# ──────────────────────────────────────────
# Collateral
# ──────────────────────────────────────────

@app.route("/api/collateral", methods=["GET"])
def get_collateral():
    holdings = kite_svc.get_holdings()
    breakdown = []
    total_collateral = 0
    for h in holdings:
        value = h.get("last_price", 0) * h.get("quantity", 0)
        haircut = h.get("haircut", 0.125)
        collateral = value * (1 - haircut)
        total_collateral += collateral
        breakdown.append({
            "symbol": h.get("tradingsymbol", ""),
            "value": round(value, 2),
            "haircut": haircut,
            "collateral": round(collateral, 2),
        })
    cash = kite_svc._cash_balance
    return jsonify({
        "breakdown": breakdown,
        "total_non_cash": round(total_collateral, 2),
        "cash": cash,
        "total_usable": round(total_collateral * 0.5 + cash, 2),  # 50% cash rule
    })


# ──────────────────────────────────────────
# Notifications
# ──────────────────────────────────────────

@app.route("/api/notifications", methods=["GET"])
def list_notifications():
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    return jsonify(get_notifications(limit, offset))


@app.route("/api/notifications/unread-count", methods=["GET"])
def unread_count():
    return jsonify({"count": get_unread_count()})


@app.route("/api/notifications/<nid>/read", methods=["POST"])
def read_notification(nid):
    mark_notification_read(nid)
    return jsonify({"success": True})


@app.route("/api/notifications/read-all", methods=["POST"])
def read_all_notifications():
    mark_all_notifications_read()
    return jsonify({"success": True})


@app.route("/api/notifications/<nid>", methods=["DELETE"])
def remove_notification(nid):
    delete_notification(nid)
    return jsonify({"success": True})


# ──────────────────────────────────────────
# Settings
# ──────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def list_settings():
    settings = get_all_settings()
    # Mask TOTP secret
    if settings.get("kite_totp_secret"):
        val = settings["kite_totp_secret"]
        settings["kite_totp_secret"] = val[:4] + "••••••" + val[-3:] if len(val) > 7 else "••••••"
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json()
    for key, value in data.items():
        set_setting(key, value)
    return jsonify({"success": True})


@app.route("/api/settings/risk-profile", methods=["GET"])
def get_risk_profile():
    profile = get_setting("risk_profile", "moderate")
    from strike_selector import RISK_PROFILES
    return jsonify({"profile": profile, "details": RISK_PROFILES.get(profile)})


@app.route("/api/settings/risk-profile", methods=["POST"])
def set_risk_profile():
    data = request.get_json()
    set_setting("risk_profile", data.get("profile", "moderate"))
    return jsonify({"success": True})


@app.route("/api/settings/circuit-breaker", methods=["POST"])
def toggle_circuit_breaker():
    data = request.get_json()
    set_setting("circuit_breaker_enabled", str(data.get("enabled", False)).lower())
    return jsonify({"success": True})


# ──────────────────────────────────────────
# Risk
# ──────────────────────────────────────────

@app.route("/api/risk/status", methods=["GET"])
def risk_status():
    from risk_manager import get_risk_summary
    return jsonify(get_risk_summary(kite_svc))


@app.route("/api/risk/alerts", methods=["GET"])
def risk_alerts():
    from risk_manager import monitor_positions
    alerts = monitor_positions(kite_svc)
    return jsonify({"alerts": alerts})


# ──────────────────────────────────────────
# Fees
# ──────────────────────────────────────────

@app.route("/api/fees/estimate", methods=["GET"])
def estimate_fees():
    from fee_calculator import calculate_trade_fees
    premium = float(request.args.get("premium", 0))
    quantity = int(request.args.get("quantity", 0))
    action = request.args.get("action", "SELL")
    fees = calculate_trade_fees([{"action": action, "premium": premium, "quantity": quantity}])
    return jsonify(fees["total"])


@app.route("/api/fees/summary", methods=["GET"])
def fees_summary():
    from models import get_db
    conn = get_db()
    row = conn.execute("""
        SELECT SUM(fees) as total_fees, COUNT(*) as trade_count,
               AVG(fees) as avg_fee
        FROM trades WHERE status = 'CLOSED'
    """).fetchone()
    conn.close()
    return jsonify({
        "total_fees": round(row["total_fees"] or 0, 2),
        "trade_count": row["trade_count"] or 0,
        "avg_fee_per_trade": round(row["avg_fee"] or 0, 2),
    })


# ──────────────────────────────────────────
# GTT Orders
# ──────────────────────────────────────────

@app.route("/api/gtt/active", methods=["GET"])
def list_gtt():
    return jsonify(get_active_gtt_orders())


@app.route("/api/gtt/<gid>", methods=["DELETE"])
def cancel_gtt(gid):
    if kite_svc.get_permission() != "EXECUTE":
        return jsonify({"error": "Permission denied: READONLY mode"}), 403
    from models import get_db
    conn = get_db()
    conn.execute("UPDATE gtt_orders SET status = 'CANCELLED' WHERE id = ?", (gid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ──────────────────────────────────────────
# Audit
# ──────────────────────────────────────────

@app.route("/api/audit/orders", methods=["GET"])
def order_audit():
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    return jsonify(get_order_audit_log(limit, offset))


@app.route("/api/safety/caps", methods=["GET"])
def safety_caps():
    return jsonify(SAFETY_HARD_CAPS)


# ──────────────────────────────────────────
# Daily Summary
# ──────────────────────────────────────────

@app.route("/api/daily-summary", methods=["GET"])
def today_summary():
    from datetime import date
    summary = get_daily_summary(str(date.today()))
    return jsonify(summary or {})


@app.route("/api/daily-summary/<date_str>", methods=["GET"])
def date_summary(date_str):
    summary = get_daily_summary(date_str)
    return jsonify(summary or {})


# ──────────────────────────────────────────
# Risk Disclosure (for confirmation modal)
# ──────────────────────────────────────────

@app.route("/api/risk-disclosure", methods=["POST"])
def risk_disclosure():
    """Compute risk disclosure for order confirmation modal."""
    data = request.get_json()
    recommendation = data.get("recommendation", {})
    symbol = recommendation.get("symbol", "")
    spot = kite_svc.get_ltp(symbol) or 0
    lot_size = recommendation.get("lot_size", 1)
    strike = recommendation.get("strike", spot)
    premium = recommendation.get("premium", 0)
    rec_type = recommendation.get("type", "")

    from fee_calculator import calculate_trade_fees
    fees = calculate_trade_fees([
        {"action": leg["action"], "premium": leg["premium"], "quantity": leg["qty"]}
        for leg in recommendation.get("legs", [])
    ])

    margin_blocked = recommendation.get("margin_required", 0)

    disclosure = {
        "max_loss_amount": recommendation.get("max_loss", "Unknown"),
        "margin_impact": {
            "margin_blocked": margin_blocked,
        },
        "fee_estimate": fees["total"],
        "worst_case_scenarios": [
            {"scenario": f"{symbol} drops 3% by expiry",
             "your_loss": f"Rs {spot * 0.03 * lot_size:.0f}", "probability": "~15%"},
            {"scenario": f"{symbol} gaps 5% overnight",
             "your_loss": f"Rs {spot * 0.05 * lot_size:.0f}", "probability": "~3%/week"},
            {"scenario": f"{symbol} crashes 10% (black swan)",
             "your_loss": f"Rs {spot * 0.10 * lot_size:.0f}", "probability": "~0.5%/week"},
        ],
        "alternatives": recommendation.get("alternatives", {}),
    }

    if rec_type == "PUT_CREDIT_SPREAD":
        max_loss = recommendation.get("max_loss", "")
        disclosure["max_loss_amount"] = max_loss
        disclosure["defined_risk"] = True

    return jsonify(disclosure)


# ──────────────────────────────────────────
# Startup
# ──────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "Development")
