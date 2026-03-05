# Transcript Fixtures

These JSON fixtures model captured Alpaca paper-trading payloads and are replayed by `tests/replay_harness.py`.

Supported event types in `events`:
- `scan`: candidate list fed into `TradingBot._process_candidates`
- `alpaca_rest`: snapshot of `positions` and `closed_orders`
- `alpaca_ws_trade_update`: raw `trade_updates` message payload
- `monitor_positions`: invokes `TradingBot._monitor_positions`

`alpaca_rest` semantics:
- Treated as a full-state snapshot (replace), not a delta/merge patch.
- If multiple `alpaca_rest` events are present, each one fully replaces the broker open-position view.

Intended workflow:
1. Capture real REST + WebSocket payloads during paper trading.
2. Convert/redact into fixture JSONs in this folder.
3. Replay with `tests/test_transcript_replay.py` to validate lifecycle invariants.
