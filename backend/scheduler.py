"""
Scheduled jobs using APScheduler.
Runs: auto-login, daily scan, risk monitoring, expiry checks, cleanup.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_scheduler = None


def init_scheduler(app, kite_svc):
    """Initialize APScheduler with all cron jobs."""
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning("APScheduler not installed, scheduler disabled")
        return

    _scheduler = BackgroundScheduler()

    # 6:30 AM IST - TOTP auto-login
    _scheduler.add_job(
        _job_auto_login, CronTrigger(hour=1, minute=0),  # UTC = IST - 5:30
        args=[kite_svc], id="auto_login", name="TOTP Auto-Login"
    )

    # 9:15 AM IST - Expiry check
    _scheduler.add_job(
        _job_expiry_check, CronTrigger(hour=3, minute=45),
        args=[kite_svc], id="expiry_check", name="Expiry Day Check"
    )

    # 9:20 AM IST - Morning scan
    _scheduler.add_job(
        _job_morning_scan, CronTrigger(hour=3, minute=50),
        args=[kite_svc], id="morning_scan", name="Morning Strategy Scan"
    )

    # Every 5 min (9:15-3:30 IST = 3:45-10:00 UTC) - Risk monitor + MTM
    _scheduler.add_job(
        _job_risk_monitor, IntervalTrigger(minutes=5),
        args=[kite_svc], id="risk_monitor", name="Risk Monitor"
    )

    # 2:00 PM IST - Expiry day ITM check
    _scheduler.add_job(
        _job_expiry_itm_check, CronTrigger(hour=8, minute=30),
        args=[kite_svc], id="expiry_itm_2pm", name="Expiry ITM Check (2 PM)"
    )

    # 3:00 PM IST - Pre-close warning
    _scheduler.add_job(
        _job_pre_close_warning, CronTrigger(hour=9, minute=30),
        args=[kite_svc], id="pre_close_warning", name="Pre-Close Warning"
    )

    # 3:35 PM IST - Daily summary
    _scheduler.add_job(
        _job_daily_summary, CronTrigger(hour=10, minute=5),
        args=[kite_svc], id="daily_summary", name="Daily Summary"
    )

    # 11:59 PM IST - Cleanup
    _scheduler.add_job(
        _job_cleanup, CronTrigger(hour=18, minute=29),
        id="cleanup", name="Nightly Cleanup"
    )

    _scheduler.start()
    logger.info("Scheduler initialized with all jobs")


def _is_market_hours():
    """Check if within Indian market hours (9:15 AM - 3:30 PM IST)."""
    now = datetime.utcnow()
    # IST = UTC + 5:30
    ist_hour = (now.hour + 5) % 24 + (1 if now.minute >= 30 else 0)
    ist_minute = (now.minute + 30) % 60
    ist_time = ist_hour * 60 + ist_minute
    return 9 * 60 + 15 <= ist_time <= 15 * 60 + 30


def _job_auto_login(kite_svc):
    """6:30 AM IST - Attempt TOTP auto-login."""
    logger.info("Running auto-login job")
    from models import get_setting
    if get_setting("kite_auto_login", "false") != "true":
        return
    result = kite_svc.auto_login()
    logger.info(f"Auto-login result: {result}")


def _job_expiry_check(kite_svc):
    """9:15 AM IST - Check for expiring positions."""
    logger.info("Running expiry check")
    from models import get_active_positions, create_notification
    positions = get_active_positions()

    # In production, check actual expiry dates
    # For now, create reminders for all positions
    if positions:
        create_notification(
            "EXPIRY_REMINDER",
            "Position Expiry Check",
            f"You have {len(positions)} open positions. Check for expiring positions.",
            "WARNING", "/positions"
        )


def _job_morning_scan(kite_svc):
    """9:20 AM IST - Auto-run strategy scan."""
    logger.info("Running morning scan")
    from models import get_setting, create_notification
    from strategy_engine import scan_strategies

    risk_profile = get_setting("risk_profile", "moderate")
    recommendations = scan_strategies(kite_svc, risk_profile)

    create_notification(
        "SCAN_COMPLETE",
        "Morning Scan Complete",
        f"Found {len(recommendations)} opportunities ({risk_profile} profile)",
        "INFO", "/scanner"
    )


def _job_risk_monitor(kite_svc):
    """Every 5 min during market hours - Monitor all positions."""
    if not _is_market_hours():
        return

    logger.info("Running risk monitor")
    from trade_tracker import update_all_mtm
    from risk_manager import monitor_positions

    # Update MTM
    update_all_mtm(kite_svc)

    # Check risk thresholds
    alerts = monitor_positions(kite_svc)
    if alerts:
        logger.warning(f"Risk monitor generated {len(alerts)} alerts")


def _job_expiry_itm_check(kite_svc):
    """2:00 PM IST - Check ITM positions on expiry day."""
    logger.info("Running expiry ITM check")
    from risk_manager import check_expiry_itm
    check_expiry_itm(kite_svc)


def _job_pre_close_warning(kite_svc):
    """3:00 PM IST - Urgent warning for remaining ITM positions."""
    logger.info("Running pre-close warning")
    from risk_manager import check_expiry_itm
    from models import get_setting, get_active_positions

    alerts = check_expiry_itm(kite_svc)
    close_itm = get_setting("close_itm_before_expiry", "true") == "true"

    if alerts and close_itm:
        from trade_tracker import close_position_manual
        positions = get_active_positions()
        for alert in alerts:
            pos = next((p for p in positions if p["id"] == alert.get("position_id")), None)
            if pos:
                close_position_manual(pos, pos.get("current_premium", pos["entry_premium"]), kite_svc)


def _job_daily_summary(kite_svc):
    """3:35 PM IST - Generate daily P&L summary."""
    logger.info("Running daily summary")
    from notification_service import generate_daily_summary
    generate_daily_summary(kite_svc)


def _job_cleanup():
    """11:59 PM IST - Clear old notifications and archive data."""
    logger.info("Running nightly cleanup")
    from models import get_db
    conn = get_db()
    # Delete notifications older than 30 days
    conn.execute("DELETE FROM notifications WHERE created_at < datetime('now', '-30 days')")
    conn.commit()
    conn.close()
