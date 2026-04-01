# Velox Event Ledger - 2026-03-14

Generated from raw VPS files in `/opt/velox-app/logs` (copied locally).

## bot_2026-03-12.log

- Total lines: 211,981
- gpt_api_429: 1,079
- jury_missing_gpt: 468
- jury_missing_grok: 0
- single_model_insufficient: 50
- two_model_no_consensus: 88
- pm_ai_emergency: 341
- trading_blind: 18
- risk_denied: 437
- wash_trade: 815
- recon_mismatch: 10,383
- canaries: 147,682
- after_hours: 39
- top_emergency_symbols: FLY:141, ONDS:101, ROMA:44, LWLG:33, BATL:13, SOC:2, POET:2, ATPC:2

Sample lines:
- `2026-03-12 01:42:57.924 | ERROR    | src.broker.alpaca_client:place_market_buy:282 - Market buy failed (LWLG): {"code":40310000,"existing_order_id":"78882f27-a8ce-4ea9-8f4b-429765c064be","message":"potential wash trade detected. use complex orders","reject_reason":"opposite side `
- `2026-03-12 01:43:32.243 | ERROR    | src.broker.alpaca_client:place_market_buy:282 - Market buy failed (SOC): {"code":40310000,"existing_order_id":"67f0fdc5-cb7d-45ba-b44a-7238df2251b9","message":"potential wash trade detected. use complex orders","reject_reason":"opposite side m`
- `2026-03-12 01:45:52.360 | ERROR    | src.broker.alpaca_client:place_market_buy:282 - Market buy failed (LWLG): {"code":40310000,"existing_order_id":"78882f27-a8ce-4ea9-8f4b-429765c064be","message":"potential wash trade detected. use complex orders","reject_reason":"opposite side `
- `2026-03-12 01:46:15.857 | ERROR    | src.broker.alpaca_client:place_market_buy:282 - Market buy failed (SOC): {"code":40310000,"existing_order_id":"67f0fdc5-cb7d-45ba-b44a-7238df2251b9","message":"potential wash trade detected. use complex orders","reject_reason":"opposite side m`
- `2026-03-12 01:48:38.904 | ERROR    | src.broker.alpaca_client:place_market_buy:282 - Market buy failed (LWLG): {"code":40310000,"existing_order_id":"78882f27-a8ce-4ea9-8f4b-429765c064be","message":"potential wash trade detected. use complex orders","reject_reason":"opposite side `

## bot_2026-03-13.log

- Total lines: 104,955
- gpt_api_429: 1,028
- jury_missing_gpt: 490
- jury_missing_grok: 0
- single_model_insufficient: 75
- two_model_no_consensus: 104
- pm_ai_emergency: 930
- trading_blind: 20
- risk_denied: 79
- wash_trade: 0
- recon_mismatch: 3,213
- canaries: 21,345
- after_hours: 276
- top_emergency_symbols: FLY:245, HIMX:185, ONDS:167, ROMA:166, LWLG:159, APEI:5, BATL:2, SVCO:1

Sample lines:
- `2026-03-13 01:42:34.671 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: ONDS — Infrastructure failure: 1,092 failed stops, 94% fill failure, position is dust remainder from broken execution, trapped 9+ hours`
- `2026-03-13 01:42:34.685 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: FLY — Infrastructure failure: 768 failed stops, broker sync broken (reloads after removal), real -5% loss trapped 8.5+ hours`
- `2026-03-13 01:42:34.698 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: ROMA — Infrastructure failure: Disaster entry at market close, exit pending 5.5+ hours with no fill, trapped overnight with gap risk`
- `2026-03-13 01:42:34.709 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: LWLG — Infrastructure failure: 99.9% fill failure, dust remainder (0.032 shares), exit stuck 5.5+ hours, scanner correct but execution catastrophic`
- `2026-03-13 01:45:13.677 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: ONDS — Dust position (0.9 shares) trapped in broken execution infrastructure for 9+ hours. 1,092 failed trailing stops = system catastrophically non-functional. Exit stuck s`

## bot_2026-03-14.log

- Total lines: 25,189
- gpt_api_429: 0
- jury_missing_gpt: 0
- jury_missing_grok: 0
- single_model_insufficient: 0
- two_model_no_consensus: 0
- pm_ai_emergency: 70
- trading_blind: 1
- risk_denied: 0
- wash_trade: 0
- recon_mismatch: 3,894
- canaries: 18,504
- after_hours: 139
- top_emergency_symbols: HIMX:70

Sample lines:
- `2026-03-14 01:43:38.377 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: HIMX — INFRASTRUCTURE CORRUPTION: Ghost position with 0.1 shares (99.5% fill failure), exit order stuck in 'pending_new' for 2+ hours, entry timestamp physically impossible `
- `2026-03-14 01:46:02.225 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: HIMX — Infrastructure corruption: 0.1 phantom shares, exit order stuck 2+ hours in 'pending_new' state after market close, physically impossible extended hours entry timesta`
- `2026-03-14 01:48:22.035 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: HIMX — INFRASTRUCTURE FAILURE: Position is corrupted state - 0.1 shares dust position with exit order stuck in 'pending_new' for 2+ hours after market close. Entry timestamp`
- `2026-03-14 01:50:42.790 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: HIMX — Infrastructure corruption: 0.1 share dust position, exit order stuck 2+ hours in 'pending_new' state, impossible extended hours entry timestamp. This is not a trading`
- `2026-03-14 01:53:04.739 | WARNING  | src.ai.position_manager:_execute_market_exit:404 - 🤖 PM AI_EMERGENCY: HIMX — INFRASTRUCTURE FAILURE: Phantom 0.1 share position with corrupted order state (exit pending 7+ hours, never filled). This is not a trade - it's broken system state. M`

## VPS Runtime Snapshot (Collection Window)

- Services of interest observed: `velox.service`, `velox-errors.service` both active.
- Python process of interest observed: single `python -m src.main` instance active.

## Notes

- This ledger is an evidence index for investigation and patch verification.
- Detailed diagnosis and remediation lives in `VELOX_SYSTEMATIC_DOSSIER_2026-03-14.md`.
