"""
Notification service for in-app notifications and reminders.
"""

from models import (create_notification, get_notifications, get_unread_count,
                    mark_notification_read, mark_all_notifications_read,
                    delete_notification)


# Notification types and their default severity
NOTIFICATION_TYPES = {
    "SCAN_COMPLETE": {"severity": "INFO", "title": "Scan Complete"},
    "TRADE_EXECUTED": {"severity": "INFO", "title": "Trade Executed"},
    "TRADE_CLOSED": {"severity": "INFO", "title": "Trade Closed"},
    "EXPIRY_REMINDER": {"severity": "WARNING", "title": "Expiry Reminder"},
    "TOKEN_EXPIRED": {"severity": "URGENT", "title": "Kite Token Expired"},
    "AUTO_LOGIN_SUCCESS": {"severity": "INFO", "title": "Auto-Login Success"},
    "AUTO_LOGIN_FAILED": {"severity": "URGENT", "title": "Auto-Login Failed"},
    "MARGIN_WARNING": {"severity": "WARNING", "title": "Margin Warning"},
    "POSITION_ALERT": {"severity": "WARNING", "title": "Position Alert"},
    "DAILY_SUMMARY": {"severity": "INFO", "title": "Daily Summary"},
    "PNL_MILESTONE": {"severity": "INFO", "title": "P&L Milestone"},
    "NO_SCAN_REMINDER": {"severity": "WARNING", "title": "Scan Reminder"},
    "STOP_LOSS_HIT": {"severity": "URGENT", "title": "Stop-Loss Hit"},
    "DELTA_BREACH": {"severity": "WARNING", "title": "Delta Breach"},
    "MARKET_DROP": {"severity": "WARNING", "title": "Market Drop"},
    "EXPIRY_ITM_STT": {"severity": "URGENT", "title": "ITM Expiry Warning"},
    "DAILY_LOSS_LIMIT": {"severity": "URGENT", "title": "Daily Loss Limit"},
    "CIRCUIT_BREAKER": {"severity": "URGENT", "title": "Circuit Breaker"},
    "ADJUSTMENT_SUGGESTED": {"severity": "WARNING", "title": "Adjustment Suggested"},
    "GTT_PLACED": {"severity": "INFO", "title": "GTT Order Placed"},
    "GTT_TRIGGERED": {"severity": "URGENT", "title": "GTT Triggered"},
    "GTT_CANCELLED": {"severity": "INFO", "title": "GTT Cancelled"},
    "RECONCILIATION_MISMATCH": {"severity": "URGENT", "title": "Order Mismatch"},
}


def notify(ntype, message, severity=None, action_url=None):
    """Create a notification with type defaults."""
    defaults = NOTIFICATION_TYPES.get(ntype, {"severity": "INFO", "title": ntype})
    return create_notification(
        ntype=ntype,
        title=defaults["title"],
        message=message,
        severity=severity or defaults["severity"],
        action_url=action_url,
    )


def is_notification_enabled(ntype):
    """Check if a notification type is enabled in settings."""
    from models import get_setting
    type_to_setting = {
        "SCAN_COMPLETE": "notify_scan_complete",
        "EXPIRY_REMINDER": "notify_expiry_reminder",
        "TOKEN_EXPIRED": "notify_token_expired",
        "MARGIN_WARNING": "notify_margin_warning",
        "DAILY_SUMMARY": "notify_daily_summary",
    }
    setting_key = type_to_setting.get(ntype)
    if not setting_key:
        return True  # Always enable critical notifications
    return get_setting(setting_key, "true") == "true"


def generate_daily_summary(kite_svc):
    """Generate morning daily summary notification."""
    from trade_tracker import get_analytics_summary
    from models import get_active_positions

    summary = get_analytics_summary()
    positions = get_active_positions()

    expiring_today = [p for p in positions if p.get("status") == "EXPIRING_TODAY"]

    message = (
        f"Open positions: {summary['open_positions']} | "
        f"Unrealized P&L: Rs {summary['total_unrealized']:.0f} | "
        f"Net realized: Rs {summary['net_pnl']:.0f} | "
        f"Win rate: {summary['win_rate']}%"
    )

    if expiring_today:
        message += f" | {len(expiring_today)} position(s) expiring today!"

    if is_notification_enabled("DAILY_SUMMARY"):
        notify("DAILY_SUMMARY", message, action_url="/dashboard")
