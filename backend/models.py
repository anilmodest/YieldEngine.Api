import os
import sqlite3
import uuid
from datetime import datetime

DB_PATH = os.getenv("SQLITE_DB_PATH", "data/yield_engine.db")


def get_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else "data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def generate_id():
    return str(uuid.uuid4())


def now_iso():
    return datetime.utcnow().isoformat()


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            rec_id TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            legs TEXT NOT NULL,
            entry_premium REAL NOT NULL,
            entry_time TEXT NOT NULL,
            exit_premium REAL,
            exit_time TEXT,
            exit_reason TEXT,
            pnl REAL,
            fees REAL DEFAULT 0,
            margin_used REAL DEFAULT 0,
            status TEXT DEFAULT 'OPEN',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY,
            trade_id TEXT REFERENCES trades(id),
            symbol TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            legs TEXT NOT NULL,
            entry_premium REAL NOT NULL,
            current_premium REAL,
            unrealized_pnl REAL,
            days_held INTEGER DEFAULT 0,
            expiry_date TEXT,
            margin_blocked REAL DEFAULT 0,
            last_updated TEXT,
            status TEXT DEFAULT 'ACTIVE'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            severity TEXT DEFAULT 'INFO',
            read INTEGER DEFAULT 0,
            action_url TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            holdings TEXT NOT NULL,
            cash_balance REAL DEFAULT 0,
            total_value REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT PRIMARY KEY,
            open_positions INTEGER DEFAULT 0,
            trades_executed INTEGER DEFAULT 0,
            premium_collected REAL DEFAULT 0,
            premium_paid REAL DEFAULT 0,
            realized_pnl REAL DEFAULT 0,
            unrealized_pnl REAL DEFAULT 0,
            margin_used REAL DEFAULT 0,
            collateral_value REAL DEFAULT 0,
            notes TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS order_audit (
            id TEXT PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            action TEXT NOT NULL,
            rec_id TEXT,
            trade_id TEXT,
            legs TEXT NOT NULL,
            dry_run_result TEXT NOT NULL,
            kite_response TEXT,
            reconciliation TEXT,
            user_confirmed INTEGER DEFAULT 0,
            status TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS gtt_orders (
            id TEXT PRIMARY KEY,
            trade_id TEXT REFERENCES trades(id),
            kite_gtt_id INTEGER,
            symbol TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            trigger_price REAL NOT NULL,
            order_type TEXT NOT NULL,
            limit_price REAL,
            quantity INTEGER NOT NULL,
            exchange TEXT NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            created_at TEXT DEFAULT (datetime('now')),
            triggered_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS adjustments (
            id TEXT PRIMARY KEY,
            trade_id TEXT REFERENCES trades(id),
            adjustment_type TEXT NOT NULL,
            old_legs TEXT NOT NULL,
            new_legs TEXT,
            cost REAL NOT NULL,
            reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Insert default settings if not exist
    default_settings = {
        "max_loss_per_trade": "10000",
        "min_prob_otm": "0.75",
        "max_margin_util": "0.6",
        "preferred_dte": "7",
        "notify_scan_complete": "true",
        "notify_expiry_reminder": "true",
        "notify_token_expired": "true",
        "notify_margin_warning": "true",
        "notify_daily_summary": "true",
        "notify_pnl_threshold": "5000",
        "kite_auto_login": "false",
        "kite_user_id": "",
        "kite_totp_secret": "",
        "risk_profile": "moderate",
        "strike_selection_mode": "auto",
        "manual_min_otm_pct": "2",
        "manual_max_otm_pct": "10",
        "manual_target_delta_puts": "0.20",
        "manual_target_delta_calls": "0.15",
        "skip_if_iv_rank_above": "80",
        "skip_before_events": "true",
        "stop_loss_multiplier": "2.0",
        "delta_alert_threshold": "0.50",
        "daily_loss_limit": "25000",
        "circuit_breaker_enabled": "false",
        "auto_stop_loss_enabled": "false",
        "auto_gtt_on_entry": "true",
        "intraday_drop_alert_pct": "1.5",
        "close_itm_before_expiry": "true",
        "allowed_strategies": "COVERED_CALL,CASH_SECURED_PUT,PUT_CREDIT_SPREAD,COLLAR,CASH_FUTURES_ARB",
    }

    for key, value in default_settings.items():
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    conn.commit()
    conn.close()


# --- Helper functions for DB operations ---

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, str(value), now_iso()),
    )
    conn.commit()
    conn.close()


def get_all_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}


def create_trade(trade_data):
    conn = get_db()
    trade_id = generate_id()
    conn.execute(
        """INSERT INTO trades (id, rec_id, strategy_type, symbol, direction, legs,
           entry_premium, entry_time, margin_used, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
        (
            trade_id,
            trade_data["rec_id"],
            trade_data["strategy_type"],
            trade_data["symbol"],
            trade_data["direction"],
            trade_data["legs"],
            trade_data["entry_premium"],
            now_iso(),
            trade_data.get("margin_used", 0),
        ),
    )
    conn.commit()
    conn.close()
    return trade_id


def close_trade(trade_id, exit_premium, exit_reason, pnl, fees):
    conn = get_db()
    conn.execute(
        """UPDATE trades SET exit_premium = ?, exit_time = ?, exit_reason = ?,
           pnl = ?, fees = ?, status = 'CLOSED' WHERE id = ?""",
        (exit_premium, now_iso(), exit_reason, pnl, fees, trade_id),
    )
    conn.commit()
    conn.close()


def get_open_trades():
    conn = get_db()
    rows = conn.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY entry_time DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_trades(limit=100, offset=0, filters=None):
    conn = get_db()
    query = "SELECT * FROM trades"
    params = []
    conditions = []

    if filters:
        if filters.get("strategy_type"):
            conditions.append("strategy_type = ?")
            params.append(filters["strategy_type"])
        if filters.get("symbol"):
            conditions.append("symbol = ?")
            params.append(filters["symbol"])
        if filters.get("status"):
            conditions.append("status = ?")
            params.append(filters["status"])
        if filters.get("date_from"):
            conditions.append("entry_time >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            conditions.append("entry_time <= ?")
            params.append(filters["date_to"])

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY entry_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_position(position_data):
    conn = get_db()
    pos_id = generate_id()
    conn.execute(
        """INSERT INTO positions (id, trade_id, symbol, strategy_type, legs,
           entry_premium, current_premium, unrealized_pnl, expiry_date,
           margin_blocked, last_updated, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')""",
        (
            pos_id,
            position_data["trade_id"],
            position_data["symbol"],
            position_data["strategy_type"],
            position_data["legs"],
            position_data["entry_premium"],
            position_data.get("current_premium"),
            position_data.get("unrealized_pnl", 0),
            position_data.get("expiry_date"),
            position_data.get("margin_blocked", 0),
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return pos_id


def get_active_positions():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'ACTIVE' ORDER BY last_updated DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_position_mtm(pos_id, current_premium, unrealized_pnl):
    conn = get_db()
    conn.execute(
        """UPDATE positions SET current_premium = ?, unrealized_pnl = ?,
           last_updated = ?, days_held = CAST(
               (julianday('now') - julianday(
                   (SELECT entry_time FROM trades WHERE id = positions.trade_id)
               )) AS INTEGER
           ) WHERE id = ?""",
        (current_premium, unrealized_pnl, now_iso(), pos_id),
    )
    conn.commit()
    conn.close()


def close_position(pos_id):
    conn = get_db()
    conn.execute("UPDATE positions SET status = 'CLOSED', last_updated = ? WHERE id = ?", (now_iso(), pos_id))
    conn.commit()
    conn.close()


def create_notification(ntype, title, message, severity="INFO", action_url=None):
    conn = get_db()
    nid = generate_id()
    conn.execute(
        """INSERT INTO notifications (id, type, title, message, severity, action_url)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (nid, ntype, title, message, severity, action_url),
    )
    conn.commit()
    conn.close()
    return nid


def get_notifications(limit=50, offset=0, unread_only=False):
    conn = get_db()
    query = "SELECT * FROM notifications"
    if unread_only:
        query += " WHERE read = 0"
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    rows = conn.execute(query, (limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unread_count():
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) as count FROM notifications WHERE read = 0").fetchone()
    conn.close()
    return row["count"]


def mark_notification_read(nid):
    conn = get_db()
    conn.execute("UPDATE notifications SET read = 1 WHERE id = ?", (nid,))
    conn.commit()
    conn.close()


def mark_all_notifications_read():
    conn = get_db()
    conn.execute("UPDATE notifications SET read = 1")
    conn.commit()
    conn.close()


def delete_notification(nid):
    conn = get_db()
    conn.execute("DELETE FROM notifications WHERE id = ?", (nid,))
    conn.commit()
    conn.close()


def save_portfolio_snapshot(name, holdings, cash_balance, total_value):
    conn = get_db()
    sid = generate_id()
    conn.execute(
        """INSERT INTO portfolio_snapshots (id, name, holdings, cash_balance, total_value)
           VALUES (?, ?, ?, ?, ?)""",
        (sid, name, holdings, cash_balance, total_value),
    )
    conn.commit()
    conn.close()
    return sid


def get_portfolio_snapshots():
    conn = get_db()
    rows = conn.execute("SELECT * FROM portfolio_snapshots ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_portfolio_snapshot(sid):
    conn = get_db()
    conn.execute("DELETE FROM portfolio_snapshots WHERE id = ?", (sid,))
    conn.commit()
    conn.close()


def upsert_daily_summary(date_str, data):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO daily_summary
           (date, open_positions, trades_executed, premium_collected, premium_paid,
            realized_pnl, unrealized_pnl, margin_used, collateral_value, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            date_str,
            data.get("open_positions", 0),
            data.get("trades_executed", 0),
            data.get("premium_collected", 0),
            data.get("premium_paid", 0),
            data.get("realized_pnl", 0),
            data.get("unrealized_pnl", 0),
            data.get("margin_used", 0),
            data.get("collateral_value", 0),
            data.get("notes"),
        ),
    )
    conn.commit()
    conn.close()


def get_daily_summary(date_str=None):
    conn = get_db()
    if date_str:
        row = conn.execute("SELECT * FROM daily_summary WHERE date = ?", (date_str,)).fetchone()
        conn.close()
        return dict(row) if row else None
    rows = conn.execute("SELECT * FROM daily_summary ORDER BY date DESC LIMIT 30").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_order_audit(action, legs, dry_run_result, status, rec_id=None, trade_id=None,
                    kite_response=None, reconciliation=None, user_confirmed=0):
    conn = get_db()
    aid = generate_id()
    conn.execute(
        """INSERT INTO order_audit (id, action, rec_id, trade_id, legs, dry_run_result,
           kite_response, reconciliation, user_confirmed, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (aid, action, rec_id, trade_id, legs, dry_run_result,
         kite_response, reconciliation, user_confirmed, status),
    )
    conn.commit()
    conn.close()
    return aid


def create_gtt_order(gtt_data):
    conn = get_db()
    gid = generate_id()
    conn.execute(
        """INSERT INTO gtt_orders (id, trade_id, kite_gtt_id, symbol, trigger_type,
           trigger_price, order_type, limit_price, quantity, exchange, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')""",
        (
            gid,
            gtt_data["trade_id"],
            gtt_data.get("kite_gtt_id"),
            gtt_data["symbol"],
            gtt_data["trigger_type"],
            gtt_data["trigger_price"],
            gtt_data["order_type"],
            gtt_data.get("limit_price"),
            gtt_data["quantity"],
            gtt_data["exchange"],
        ),
    )
    conn.commit()
    conn.close()
    return gid


def get_active_gtt_orders():
    conn = get_db()
    rows = conn.execute("SELECT * FROM gtt_orders WHERE status = 'ACTIVE'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_adjustment(trade_id, adjustment_type, old_legs, new_legs, cost, reason=None):
    conn = get_db()
    aid = generate_id()
    conn.execute(
        """INSERT INTO adjustments (id, trade_id, adjustment_type, old_legs, new_legs, cost, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (aid, trade_id, adjustment_type, old_legs, new_legs, cost, reason),
    )
    conn.commit()
    conn.close()
    return aid


def get_adjustments_for_trade(trade_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM adjustments WHERE trade_id = ? ORDER BY created_at DESC", (trade_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_order_audit_log(limit=100, offset=0):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM order_audit ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Safety hard caps - NOT configurable via UI, only via env vars
SAFETY_HARD_CAPS = {
    "MAX_LOTS_PER_ORDER_NIFTY": int(os.getenv("MAX_LOTS_NIFTY", "2")),
    "MAX_LOTS_PER_ORDER_BANKNIFTY": int(os.getenv("MAX_LOTS_BANKNIFTY", "1")),
    "MAX_LOTS_PER_ORDER_STOCK": int(os.getenv("MAX_LOTS_STOCK", "2")),
    "MAX_ORDER_VALUE": int(os.getenv("MAX_ORDER_VALUE", "500000")),
    "MAX_ORDERS_PER_DAY": int(os.getenv("MAX_ORDERS_PER_DAY", "20")),
    "MAX_OPEN_POSITIONS": int(os.getenv("MAX_OPEN_POSITIONS", "10")),
    "PRICE_DEVIATION_LIMIT": float(os.getenv("PRICE_DEVIATION_LIMIT", "0.20")),
    "ALLOWED_EXCHANGES": ["NFO", "NSE"],
    "ALLOWED_PRODUCTS": ["NRML", "CNC"],
    "PASSWORD_RETENTION_MINUTES": 0,
}
