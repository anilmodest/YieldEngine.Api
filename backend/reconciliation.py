"""
Post-order reconciliation. Verifies every placed order matches intent.
On mismatch: execution auto-locked to READONLY.
"""

import time
import logging
from models import create_notification

logger = logging.getLogger(__name__)


def reconcile_order(expected_params, kite_order_id, kite_svc):
    """
    Verify placed order matches intended parameters.
    Called immediately after kite.place_order() succeeds.

    Returns:
        dict with status, alert flag, and message
    """
    # Simulated orders always pass
    if str(kite_order_id).startswith("SIM-"):
        return {
            "status": "VERIFIED",
            "alert": False,
            "message": f"Simulated order {kite_order_id} - no reconciliation needed",
        }

    # Brief delay for order to appear in book
    time.sleep(1)

    orders = kite_svc.get_orders()
    placed_order = next(
        (o for o in orders if str(o.get("order_id")) == str(kite_order_id)),
        None
    )

    if not placed_order:
        return {
            "status": "ORDER_NOT_FOUND",
            "alert": True,
            "message": f"Order {kite_order_id} not found in order book",
        }

    mismatches = []

    # Check symbol
    if placed_order.get("tradingsymbol") != expected_params.get("tradingsymbol"):
        mismatches.append(
            f"Symbol: expected {expected_params.get('tradingsymbol')}, "
            f"got {placed_order.get('tradingsymbol')}"
        )

    # Check quantity
    if placed_order.get("quantity") != expected_params.get("qty"):
        mismatches.append(
            f"Qty: expected {expected_params.get('qty')}, "
            f"got {placed_order.get('quantity')}"
        )

    # Check transaction type
    if placed_order.get("transaction_type") != expected_params.get("action"):
        mismatches.append(
            f"Action: expected {expected_params.get('action')}, "
            f"got {placed_order.get('transaction_type')}"
        )

    # Check if rejected by Kite
    if placed_order.get("status") == "REJECTED":
        return {
            "status": "KITE_REJECTED",
            "alert": True,
            "message": f"Kite rejected order: {placed_order.get('status_message', 'unknown')}",
        }

    if mismatches:
        # CRITICAL: Lock execution immediately
        kite_svc.lock_execution()
        mismatch_msg = ", ".join(mismatches)
        logger.critical(f"Order reconciliation MISMATCH: {mismatch_msg}")
        return {
            "status": "MISMATCH",
            "alert": True,
            "mismatches": mismatches,
            "message": f"EXECUTION LOCKED - order parameters don't match. "
                      f"Mismatches: {mismatch_msg}. Check Kite order book manually.",
        }

    return {
        "status": "VERIFIED",
        "alert": False,
        "message": f"Order {kite_order_id} verified: {placed_order.get('status')}",
    }
