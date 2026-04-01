# Velox Technical Architecture 3.0

## Status

This document describes the architecture of the **current** Velox system as implemented in the codebase, not the original simplified v1 momentum-bot concept in [README.md](README.md) or the older multi-AI consensus framing in [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md).

Velox 3.0 is best understood as:

- an event-driven autonomous trading engine
- with deterministic execution and state control
- AI-assisted reasoning and governance
- persistent journaling and replay
- book-level capital allocation
- broker-canonical reconciliation

It is no longer just a scanner with a model bolted onto it.

Related governance docs:

- [docs/governance/VELOX_GOVERNANCE_OS.md](docs/governance/VELOX_GOVERNANCE_OS.md)
- [docs/governance/AGENT_GOVERNANCE_PROMPT.md](docs/governance/AGENT_GOVERNANCE_PROMPT.md)
- [docs/governance/CHANGE_PROPOSAL_TEMPLATE.md](docs/governance/CHANGE_PROPOSAL_TEMPLATE.md)
- [docs/governance/WEEKLY_COMMITTEE_MEMO_TEMPLATE.md](docs/governance/WEEKLY_COMMITTEE_MEMO_TEMPLATE.md)
- [docs/governance/roles/README.md](docs/governance/roles/README.md)
- [config/governance_committee.json](config/governance_committee.json)

## 1. Design Goals

Velox is trying to solve a specific class of problem:

- transform noisy, heterogeneous market data into structured trade opportunities
- convert opportunities into executable trades rather than endless analysis
- manage live positions under real broker constraints
- preserve rich context about why a trade was taken
- route capital toward strategies and regimes that are empirically working
- continuously improve from outcomes without allowing unbounded self-corruption

The architectural thesis is:

- deterministic code should own classification, constraints, state, sizing boundaries, order placement, reconciliation, and exits
- AI should own context, disagreement, thesis quality, nuance, and post-trade interpretation

This separation is the single most important design decision in the current system.

## 2. System Definition

Velox is a long-running orchestration process centered in [src/main.py](src/main.py).

It boots and coordinates:

- market/broker clients
- scanners and enrichment modules
- live streams
- AI specialist agents
- council/jury decision layers
- entry and exit managers
- risk manager and allocator
- reconciliation and persistence
- dashboard and analytics surfaces

At runtime it behaves more like a small autonomous trading operation than a script.

## 3. Top-Level Dataflow

```text
External Feeds / Broker / Streams
    -> Scanner + Candidate Enrichment
    -> Signal Attribution + Strategy Identity
    -> Deterministic Setup Classification
    -> Deterministic Play Resolution
    -> AI Governance (jury / council / classifier auto-path)
    -> Playbook + Risk Gates
    -> Book Allocator
    -> Entry Manager / Options Overlay / Broker Execution
    -> Exit Stack (hard stop / ratchet / PM / after-hours)
    -> Reconciliation
    -> Trade History / Dashboard / Game Film / Replay / Tuning
```

The core difference versus older versions is that the pipeline is no longer:

```text
scan -> ask models -> maybe trade
```

It is now:

```text
observe -> classify -> resolve state -> govern -> constrain -> allocate -> execute -> reconcile -> learn
```

## 4. Source Tree by Responsibility

Major code domains:

- control plane: [src/main.py](src/main.py)
- specialist agents: [src/agents](src/agents)
- AI review/analytics layers: [src/ai](src/ai)
- broker integration: [src/broker](src/broker)
- scanner/candidate construction: [src/scanner](src/scanner)
- deterministic setup grammar: [src/signals/mode_classifier.py](src/signals/mode_classifier.py), [src/signals/play_resolver.py](src/signals/play_resolver.py)
- trade context + identity: [src/data](src/data)
- risk + allocation: [src/risk](src/risk)
- execution: [src/entry](src/entry), [src/exit](src/exit)
- options: [src/options](src/options)
- streams: [src/streams](src/streams)
- reconciliation: [src/reconciliation](src/reconciliation)
- dashboard: [src/dashboard/dashboard.py](src/dashboard/dashboard.py)

## 5. Control Plane and Runtime Lifecycle

`TradingBot` in [src/main.py](src/main.py) is the root coordinator.

Its responsibilities include:

- constructing subsystem objects
- loading persisted state
- reconciling startup state with broker truth
- starting asynchronous streams and service loops
- scanning and candidate evaluation
- pending-setup refresh
- position monitoring
- exit/order maintenance
- overnight and after-hours handling
- dashboard publication

The process is effectively a cooperative async state machine.

### Runtime Loop Families

Velox does not have a single loop; it has several:

- scan loop
- stream event loop
- pending setup refresh loop
- position monitor loop
- profit ratchet loop
- strategic/PM exit loop
- observer/advisor/tuner/game-film intervals
- dashboard/API surface updates

That division is important because many failure modes in trading systems come from assuming one cadence fits all tasks.

## 6. External Inputs and Data Sources

Velox consumes multiple signal classes, each implemented as a separate module.

### Core Market/Broker

- Alpaca broker/account/order execution in [src/broker/alpaca_client.py](src/broker/alpaca_client.py)
- Polygon price/quote/bar data in [src/data/polygon_client.py](src/data/polygon_client.py)
- market stream in [src/streams/market_stream.py](src/streams/market_stream.py)
- trade stream in [src/streams/trade_stream.py](src/streams/trade_stream.py)

### Social / Narrative / Discretionary

- StockTwits in [src/signals/stocktwits.py](src/signals/stocktwits.py)
- X/Twitter sentiment/trending in [src/signals/twitter.py](src/signals/twitter.py) and [src/signals/grok_x_trending.py](src/signals/grok_x_trending.py)
- copy trader in [src/signals/copy_trader.py](src/signals/copy_trader.py)
- human intel in [src/signals/human_intel.py](src/signals/human_intel.py)
- watchlist in [src/signals/watchlist.py](src/signals/watchlist.py)

### Catalyst / Event / Fundamental

- earnings in [src/signals/earnings.py](src/signals/earnings.py)
- pharma/FDA in [src/signals/pharma_catalyst.py](src/signals/pharma_catalyst.py)
- EDGAR/SEC in [src/signals/edgar.py](src/signals/edgar.py)
- congress in [src/signals/congress.py](src/signals/congress.py)
- insider/ARK related contextual inputs in [src/signals/ark_trades.py](src/signals/ark_trades.py)

### Macro / Regime

- FRED in [src/signals/fred.py](src/signals/fred.py)
- Finnhub calendar context in [src/signals/finnhub.py](src/signals/finnhub.py)
- sector rotation in [src/signals/sector_rotation.py](src/signals/sector_rotation.py)
- overnight ETF bias in [src/signals/overnight_context.py](src/signals/overnight_context.py)

### Specialty / Strategy-Specific

- fade runner in [src/signals/fade_runner.py](src/signals/fade_runner.py)
- short interest in [src/signals/short_interest.py](src/signals/short_interest.py)
- live indicator bridge in [src/signals/live_indicators.py](src/signals/live_indicators.py)

### Unusual Whales

- REST client in [src/signals/unusual_whales.py](src/signals/unusual_whales.py)
- websocket stream in [src/streams/unusual_whales_stream.py](src/streams/unusual_whales_stream.py)
- unusual options scanner in [src/signals/unusual_options.py](src/signals/unusual_options.py)

Technically, Velox is a data-fusion engine before it is a decision engine.

## 7. Candidate Construction and Enrichment

The scanner in [src/scanner/scanner.py](src/scanner/scanner.py) is the candidate factory.

It does three broad things:

- discovers potentially tradable symbols from multiple feeds
- enriches them with context
- emits normalized candidate dictionaries for downstream evaluation

Candidate enrichment currently includes:

- price / volume / spread / range position
- market tide bias
- news summary
- options-chain / flow confirmation
- catalyst markers
- watchlist and copy-trader context
- social sentiment context
- session label
- regime hints

The candidate is Velox's main internal unit of work. Nearly every later stage either adds structure to it or constrains it.

## 8. Signal Attribution and Strategy Identity

Signal attribution is handled by:

- [src/data/signal_attribution.py](src/data/signal_attribution.py)
- [src/data/trade_schema.py](src/data/trade_schema.py)
- [src/data/strategy_tags.py](src/data/strategy_tags.py)

Velox explicitly normalizes:

- `signal_sources`
- `strategy_tag`
- `setup_mode`
- `best_play`
- `direction_constraint`
- `timing_state`
- `entry_path`
- `entry_reason_code`

This lets the system answer questions like:

- Which book is losing money?
- Which setup family is profitable?
- Which signals are contributing to good trades?
- Which positions are broker-restored artifacts rather than genuine live entries?

Without this layer, review and self-improvement would be noisy and misleading.

## 9. Strategy Books

Velox uses strategy-book identity rather than treating all trades as generic.

Books currently include forms such as:

- `momentum_long`
- `momentum_short`
- `social_momentum_long`
- `social_momentum_short`
- `uw_flow_long`
- `uw_flow_short`
- `fade_short`
- `copy_trader_long`
- `copy_trader_short`
- `watchlist_long`
- `watchlist_short`
- `pharma_catalyst`
- `congress_follow`

This matters for:

- analytics
- playbook gating
- size caps
- disable/probation logic
- allocator control
- attribution

Architecturally, books are the bridge from symbol-centric trading to portfolio management.

## 10. Deterministic Setup Grammar

Two modules define the structured trade grammar:

- classifier: [src/signals/mode_classifier.py](src/signals/mode_classifier.py)
- resolver: [src/signals/play_resolver.py](src/signals/play_resolver.py)

### Setup Modes

Current core setup families:

- `continuation_long`
- `continuation_short`
- `exhaustion_fade_short`
- `swing_catalyst_long`
- `general_momentum_long`
- `general_momentum_short`

### Execution States

Current resolver states:

- `enter_now`
- `wait_for_trigger`
- `shadow_only`
- `broker_blocked`
- `capital_blocked`
- `mode_conflict`
- `data_insufficient`

The important architectural choice is that **setup family** and **execution state** are separate axes.

That allows the bot to represent:

- valid play, not live yet
- valid play, broker blocked
- valid play, book disabled
- valid play, capital exhausted

rather than collapsing everything into `skip`.

## 11. Candidate State Machine

```text
candidate discovered
    -> setup classified
    -> play resolved
        -> enter_now
        -> wait_for_trigger
        -> shadow_only
        -> broker_blocked
        -> capital_blocked
        -> mode_conflict
        -> data_insufficient
```

If `wait_for_trigger`, the setup can be persisted and reevaluated later. This statefulness is what turns Velox from a screener into an execution engine.

## 12. Specialist-Agent Layer

The multi-agent layer in [src/agents](src/agents) provides structured opinionated briefs.

Specialist briefs:

- technical agent in [technical_agent.py](src/agents/technical_agent.py)
- sentiment agent in [sentiment_agent.py](src/agents/sentiment_agent.py)
- catalyst agent in [catalyst_agent.py](src/agents/catalyst_agent.py)
- macro agent in [macro_agent.py](src/agents/macro_agent.py)
- deterministic risk brief in [risk_agent.py](src/agents/risk_agent.py)

These are coordinated by [orchestrator.py](src/agents/orchestrator.py).

The orchestrator:

- enriches signals with missing macro context
- runs specialists in parallel
- handles brief failures with fallbacks
- caches verdicts and skip cooldowns
- feeds the downstream governance layer
- updates the exit agent with latest contextual briefs

## 13. Governance Models

Velox contains two governance styles.

### Jury Model

Implemented in [jury.py](src/agents/jury.py).

This is the older synthesis layer:

- consumes specialist briefs
- emits `BUY`, `SHORT`, or `SKIP`
- includes confidence, trigger, invalidation, hold style, size posture

It is entry-focused and assumes exits are mechanical.

### Council Model

Implemented through:

- [advocate.py](src/agents/advocate.py)
- [adversary.py](src/agents/adversary.py)
- orchestrator integration in [orchestrator.py](src/agents/orchestrator.py)

This is the newer, structurally better pattern:

- Advocate must find the best executable play
- Adversary must produce a specific kill reason to veto
- deterministic math and risk limits still cap authority

This is a meaningful shift from “consensus” toward “governance with internal tension.”

## 14. Risk System

Core files:

- [src/risk/risk_manager.py](src/risk/risk_manager.py)
- [src/agents/risk_agent.py](src/agents/risk_agent.py)
- [src/data/strategy_playbook.py](src/data/strategy_playbook.py)
- [src/data/strategy_controls.py](src/data/strategy_controls.py)

### Risk Manager Responsibilities

- dynamic risk tiers by equity
- daily/weekly circuit breakers
- heat tracking
- streak adjustments
- position sizing
- wash-sale tracking
- day-trade / PDT logic
- options premium exposure

### Risk Agent Responsibilities

- strategy-level hard disables
- per-book size caps
- sector concentration limits
- spread/execution safety
- extended-hours trade rules
- entry-quality size multipliers

### Playbook and Controls

The playbook/control layer decides:

- what strategies are enabled
- what strategies are shadow only
- what size reductions or probation rules apply
- whether recent performance justifies disabling or suppressing a book

## 15. Book Allocator

The allocator in [src/risk/book_allocator.py](src/risk/book_allocator.py) is the newest portfolio-control layer.

It consumes:

- market regime
- open positions / current exposure
- equity
- realized analytics by strategy book

It emits a snapshot describing:

- current book exposure
- realized book performance
- allocator state for each book
- whether a book should be pressed, trimmed, or blocked

This is the point where Velox stops being “trade all valid ideas equally” and starts becoming “deploy capital according to regime and live expectancy.”

## 16. Entry System

The entry engine is centered in [src/entry/entry_manager.py](src/entry/entry_manager.py).

Its responsibilities include:

- final eligibility checks
- long vs short execution path
- sizing from notional and share constraints
- extended-hours order compatibility
- high-confidence notional floors
- whole-share floors where necessary
- shortability/execution safety handling
- position registration

Entry is also influenced by:

- governance output from jury/council
- deterministic play-state gates in [src/main.py](src/main.py)
- risk agent constraints
- book allocator budget
- options overlay budget consumption

The system is explicitly designed so that upstream AI can recommend a trade, but the execution boundary can still deny it on broker, risk, or capital grounds.

## 17. Options Subsystem

Core files:

- [src/options/options_engine.py](src/options/options_engine.py)
- [src/options/options_monitor.py](src/options/options_monitor.py)

The options layer is not currently the primary trading engine. It is an overlay.

Current behavior:

- only enabled for narrow high-confidence cases
- regular-hours only
- liquid whitelist
- capped portfolio premium at risk
- contract selection based on practical DTE/liquidity heuristics
- stock notional is reduced if options already consume budget

This is architecturally correct. Options are treated as convexity on top of proven equity-side logic, not as a replacement for core trade discipline.

## 18. Exit System

The exit stack is one of the most important and most complex parts of Velox.

Core files:

- [src/exit/exit_manager.py](src/exit/exit_manager.py)
- [src/exit/profit_ratchet.py](src/exit/profit_ratchet.py)
- [src/exit/extended_hours_guard.py](src/exit/extended_hours_guard.py)
- [src/exit/order_conflicts.py](src/exit/order_conflicts.py)
- [src/agents/exit_agent.py](src/agents/exit_agent.py)
- [src/ai/position_manager.py](src/ai/position_manager.py)

### Exit Mechanisms

- hard stops
- profit ratchets
- dead-money exits
- strategic/PM exits
- after-hours limit exits
- stale extended-hours repricing
- deferred next-session exits when markets are closed
- conflict resolution between multiple protective orders

### Important Invariant

Once ratchet protection becomes active, it should own protection rather than compete with legacy hard-stop logic on the same shares. Recent changes reinforced that invariant.

## 19. Position Lifecycle State Machine

```text
entry approved
    -> order submitted
    -> fill confirmed / restored / reconciled
    -> live_position
    -> hard stop active
    -> ratchet activates once sufficiently green
    -> hold / tighten / reprice / strategic exit
    -> exit filled or deferred to next session
    -> trade recorded
    -> analytics updated
```

Because the system supports restarts, every stage must survive process death and state rebuild.

## 20. After-Hours and Overnight Model

Velox treats session type as first-class runtime context:

- pre-market
- regular hours
- after-hours
- overnight/closed

Relevant files:

- [src/signals/overnight_context.py](src/signals/overnight_context.py)
- [src/exit/extended_hours_guard.py](src/exit/extended_hours_guard.py)
- session logic inside [src/main.py](src/main.py)

The system adjusts for session in several ways:

- extended-hours entry restrictions
- extended-hours spread gating
- limit-only behavior where appropriate
- close/overnight carry review
- deferred exit if the market is fully closed
- overnight index bias summaries for AI layers

After-hours is not bolted on. It is part of the core state model.

## 21. Persistence and Replay

Key files:

- [src/persistence.py](src/persistence.py)
- [src/data/pending_setups.py](src/data/pending_setups.py)
- [src/data/setup_snapshots.py](src/data/setup_snapshots.py)
- [src/data/setup_identity.py](src/data/setup_identity.py)
- [src/ai/setup_replay.py](src/ai/setup_replay.py)

Persisted state includes:

- open equity positions
- open options positions
- pending setups
- setup snapshots
- trade history
- strategy controls
- reconciliation snapshots
- shadow trades
- human intel

This is what allows:

- waiting setups to survive scans
- restarts not to erase context
- historical replay and review
- controls to persist across sessions

## 22. Broker Truth and Reconciliation

The reconciler in [src/reconciliation/reconciler.py](src/reconciliation/reconciler.py) exists to enforce a hard rule:

**the broker is canonical**

At startup and throughout runtime, Velox:

- compares internal state with broker positions and equity
- backfills reconstructed trades from broker fills
- deduplicates reconstructed artifacts
- syncs internal positions with broker reality
- classifies mismatch severity
- emits trust flags and canaries

This is one of the strongest engineering properties in the system. It prevents local state from becoming a self-consistent hallucination.

## 23. Analytics Surface

The analytics layer in [src/ai/trade_history.py](src/ai/trade_history.py) is much richer than a plain P&L ledger.

It computes:

- by-symbol performance
- by-hour performance
- by-exit-reason performance
- by-strategy performance
- by-setup-mode performance
- by-signal-source performance
- by-asset-type performance
- hold-duration buckets
- recent-20 / recent-50 performance
- Sharpe ratios
- MFE / MAE
- slippage
- first 1m / 3m / 5m green rates
- anomaly counts

It now also exposes UW-specific attribution buckets:

- overall UW-involved trades
- flow-book trades
- stream-assisted trades
- REST-assisted trades
- congress-follow trades

This is essential for deciding whether costly data sources are truly producing edge.

## 24. Dashboard and Operations Surface

The dashboard backend in [src/dashboard/dashboard.py](src/dashboard/dashboard.py) is not cosmetic. It is the operational control plane surface.

It exposes:

- status
- positions
- P&L
- intelligence source status
- stream health
- book scoreboard
- pending setups
- shadow trades
- reconciliation state
- manual human-intel injection
- pause/resume/stop controls

The dashboard is effectively the operator console for the system.

## 25. Self-Improvement Layers

Velox has four higher-level review/adaptation modules:

- Observer in [src/ai/observer.py](src/ai/observer.py)
- Advisor in [src/ai/advisor.py](src/ai/advisor.py)
- Game Film in [src/ai/game_film.py](src/ai/game_film.py)
- Tuner in [src/ai/tuner.py](src/ai/tuner.py)

### Observer

- sees account, risk, open positions, scanner state, recent trades
- emits market/portfolio observations

### Advisor

- consumes observer output, trade analytics, game film
- emits strategic guidance and position-level recommendations

### Game Film

- aggregates historical outcomes
- identifies strong/weak symbols, books, hours, hold patterns
- can update strategy controls

### Tuner

- can mutate only bounded parameters
- gated behind trade-count minimums
- designed to avoid uncontrolled config drift

The system already learned an important lesson here:

- journaling and review are good
- unconstrained automatic mutation is dangerous

## 26. Invariants

Velox depends on a few hard invariants.

1. Broker truth is canonical.  
2. Setup family and execution state are separate.  
3. Every trade should retain structured context.  
4. Risk constraints are deterministic.  
5. AI does not own order-state truth.  
6. Books are first-class capital containers.  
7. After-hours and overnight are first-class runtime modes.  
8. Restarts must preserve valid state but kill ghosts.  
9. Attribution must survive reconciliation.  
10. Idle analysis is failure if executable plays exist.

These invariants are more important than any specific model or feed.

## 27. Strengths of the Current Architecture

The current system has several genuine strengths:

- much cleaner separation of deterministic and AI responsibilities
- explicit trade grammar instead of vague buy/skip logic
- rich context persistence
- broker-canonical reconciliation
- live and after-hours position lifecycle support
- strategy books and allocator layer
- very strong analytics and attribution surface
- options integrated as bounded overlay rather than reckless leverage

These are the traits of a serious trading system, not a novelty bot.

## 28. Known Weaknesses and Active Failure Modes

Velox is still incomplete. Current weaknesses include:

- hard-stop loss asymmetry remains too large relative to average ratchet win
- some classifier and gate thresholds were loosened to recover activity
- repeat-symbol churn can still outpace symbol lockouts
- live service lifecycle on the VPS can hang in `systemd` deactivation states
- UW is integrated but not yet transformed into positive realized edge
- options are not yet a proven alpha engine
- allocator is live but not yet fully validated across multiple market sessions

This means Velox is now in the right problem space, but not yet solved.

## 29. Why 3.0 Is Different from Prior Versions

Velox 3.0 differs from earlier versions in five foundational ways:

- it uses explicit setup grammar
- it preserves operational state for waiting/blocked/shadowed plays
- it has a governance model with structural tension, not just consensus polling
- it treats books as allocatable units of capital
- it treats reconciliation and broker truth as central architecture, not cleanup

Those are first-order changes, not parameter tweaks.

## 30. What “Solved” Would Mean

From an architecture perspective, “solved” does not mean “never loses.”

It would mean:

- each major book has measurable positive expectancy in its intended regime
- allocator presses hot/aligned books and suppresses cold/misaligned books reliably
- hard-stop loss magnitude is controlled relative to average winner capture
- after-hours and overnight carry decisions are thesis-driven and operationally robust
- broker reconciliation becomes boring
- UW and options are either promoted into real edge or constrained to confirmation layers
- the system compounds by concentrating capital where the data justifies it

That is the finish line for the architecture, even if the exact strategies continue to evolve.

## 31. Summary

Velox 3.0 is a hybrid autonomous trading architecture composed of:

- multi-source data fusion
- deterministic setup classification
- deterministic play-state resolution
- AI specialist briefs
- council/jury governance
- book-aware risk and playbook constraints
- allocator-driven capital deployment
- broker-canonical execution and reconciliation
- layered exit management
- persistent replay, analytics, and review

The important fact is not that it uses AI.

The important fact is that it now uses AI inside a coherent execution architecture.

That is what gives it a plausible path from “interesting bot” to “profitable trading system.”
