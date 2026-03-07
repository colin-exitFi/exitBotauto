"""Shared helpers for canceling conflicting open orders before exits."""

import asyncio


async def cancel_conflicting_exit_orders(broker, symbol: str, exit_side: str) -> int:
    """
    Cancel open orders that reserve the same position quantity needed for an exit.

    Prefer broker-level symbol helpers when available, then fall back to scanning
    open orders for simpler test doubles.
    """
    if not broker:
        return 0

    target_side = str(exit_side or "").lower()
    cancel_fn = None
    if target_side == "buy":
        cancel_fn = getattr(broker, "cancel_open_buys_for_symbol", None)
    elif target_side == "sell":
        cancel_fn = getattr(broker, "cancel_open_sells_for_symbol", None)

    if cancel_fn:
        try:
            canceled = await asyncio.get_event_loop().run_in_executor(None, cancel_fn, symbol)
        except Exception:
            canceled = 0
        if canceled:
            await asyncio.sleep(0.25)
        return int(canceled or 0)

    if not hasattr(broker, "get_orders") or not hasattr(broker, "cancel_order"):
        return 0

    try:
        open_orders = await asyncio.get_event_loop().run_in_executor(
            None, broker.get_orders, "open"
        )
    except Exception:
        return 0

    target_symbol = str(symbol or "").upper()
    cancel_ids = []
    for order in open_orders or []:
        if str(order.get("symbol", "")).upper() != target_symbol:
            continue
        if str(order.get("side", "")).lower() != target_side:
            continue
        order_id = str(order.get("id", "") or "").strip()
        if order_id:
            cancel_ids.append(order_id)

    canceled = 0
    for order_id in sorted(set(cancel_ids)):
        ok = await asyncio.get_event_loop().run_in_executor(
            None, broker.cancel_order, order_id
        )
        if ok:
            canceled += 1

    if canceled:
        await asyncio.sleep(0.25)
    return canceled
