# Velox Governance Operating System

## Purpose

This document defines how Velox should be governed while remaining human-in-the-loop.

The goal is not for the human operator to understand every line of code or every quant concept.
The goal is to make sure capital is governed with discipline, changes are controlled, and strong books earn more risk while weak books lose it.

Velox should not be governed as:

- a generic "improve the bot" project
- a stream of reactive fixes based only on recent red/green
- an AI free-for-all where cleverness outruns proof

Velox should be governed as:

- a capital allocation machine
- with explicit risk boundaries
- with measurable promotion and demotion rules
- with structured committee roles
- with evidence required before live risk increases

Platform artifacts for this governance layer live in:

- [config/governance_committee.json](../../config/governance_committee.json)
- [docs/governance/roles/README.md](roles/README.md)

## Mission

Velox is optimizing for:

- positive net expectancy after real execution friction
- controlled drawdown and smaller tail losses
- truthful attribution by book, regime, session, and exit path
- disciplined capital concentration only where proof exists
- operational reliability that becomes boring

Velox is **not** optimizing for:

- more trades for their own sake
- higher activity without better expectancy
- smarter-sounding analysis
- more complex architecture
- live changes without a rollout path

## Human Role

The human operator is the chairman of the governance committee.

The operator is **not** required to:

- read code deeply
- inspect every prompt
- understand every subsystem in implementation detail

The operator **is** responsible for:

- defining the mission and constraints
- selecting the primary question for the current review cycle
- approving or rejecting material live-risk changes
- deciding which books earn more capital
- deciding which books go on probation or get retired
- enforcing kill switches and rollback discipline

## Committee Structure

Velox governance should use explicit agent roles.

### 1. Builder

Responsible for:

- implementation clarity
- operational safety
- identifying failure modes
- recommending the safest rollout path

Questions Builder must answer:

- What changed?
- What system layer does it touch?
- What can break operationally?
- How do we roll it back?

### 2. Strategy / PM / Quant Reviewer

Responsible for:

- strategy logic
- expected benefit
- book and regime impact
- measurable success criteria

Questions this role must answer:

- Why should this improve expectancy?
- Which books or regimes should benefit?
- What metric should improve?
- What are the likely second-order effects?

### 3. Risk Committee

Responsible for:

- protecting capital
- identifying downside scenarios
- preventing recent-pain overfitting
- challenging changes that increase hidden fragility

Questions Risk must answer:

- What gets worse if this change is wrong?
- Could this increase churn, tail loss, or concentration?
- Should this be capped, shadowed, or rejected?

### 4. Outside Critic

Responsible for:

- pressure-testing the narrative
- calling out when architecture outruns proof
- simplifying priorities
- identifying the highest-leverage next move

Questions this role must answer:

- Are we adding proof or just more explanation?
- Are we fixing the right bottleneck?
- Should we prune instead of add?

### 5. Human Chairman

The human makes the final call on:

- rollout mode
- capital/risk increases
- book promotion/demotion
- whether a proposed change is approved, rejected, or needs more evidence

## Rollout States

Every meaningful change must be labeled.

### Logging / Reporting Only

Use for:

- dashboards
- attribution
- error handling
- observability improvements

No trading behavior changes.

### Shadow

Use for:

- candidate scoring
- new book logic
- new exit logic
- new allocator ideas

The system records what it **would** have done without risking capital.

### Paper

Use when:

- execution sequencing matters
- you need order and position lifecycle realism
- shadow is insufficient

Still no real capital risk.

### Capped Live

Use for:

- new trading behavior with small real risk
- size-reduced tests
- constrained deployment after shadow/paper success

Capital must be intentionally limited.

### Scaled Live

Use only when:

- enough sample exists
- expectancy is positive after friction
- drawdown is acceptable
- operational behavior is clean

## Book Lifecycle

Every strategy book must always be in one lifecycle state.

### Incubation

Used for:

- new books
- low-sample books
- unclear edge

Default behavior:

- shadow or capped live only
- no meaningful capital concentration

### Active

Used for books with:

- enough evidence to justify live trading
- positive rolling expectancy
- acceptable drawdown

Default behavior:

- allowed normal budget
- closely monitored

### Scaled

Used only for books with:

- strong trade count
- positive expectancy after friction
- tolerable drawdown and tail-loss profile
- evidence across more than one condition

Default behavior:

- eligible for larger allocation
- still subject to risk shell and portfolio constraints

### Probation

Used for books with:

- deteriorating rolling expectancy
- worsening tail-loss behavior
- unresolved operational issues
- evidence that size should be reduced before retirement

Default behavior:

- size reduction
- tighter review
- potential retirement if it does not recover

### Retired

Used for books that:

- failed to prove durable edge
- rely on excuses instead of evidence
- repeatedly damage expectancy

Default behavior:

- no new live risk
- can remain available for research/shadow only

## Decision Rights

### Allowed Without Human Approval

- logging improvements
- dashboard/reporting improvements
- attribution field additions
- better error handling
- paper/shadow-only analysis improvements
- non-economic operational cleanup

### Requires Review Before Activation

- entry threshold changes
- exit logic changes
- stop logic changes
- sizing changes
- allocator logic changes
- extended-hours behavior changes
- book enable/disable changes
- options behavior changes

### Requires Explicit Human Approval

- increased portfolio risk
- increased leverage
- larger hard-stop ranges
- removal of kill switches
- changes to broker-canonical reconciliation
- promotion from capped live to scaled live

## Required Proposal Format

Every material proposal must use this format.

### Issue

What is the actual problem in plain English?

### Why It Matters

How does it hurt expectancy, drawdown, reliability, or allocation quality?

### System Layer Affected

Choose one or more:

- observability
- candidate generation
- setup classification
- execution timing
- stop logic
- exit logic
- sizing
- allocator behavior
- risk controls
- reconciliation

### Proposed Change

What exactly should change?

### Expected Upside

What measurable result should improve?

### Main Risks

What could get worse?

### Rollout Mode

Choose one:

- logging/reporting only
- shadow
- paper
- capped live
- scaled live

### Success Criteria

What result counts as success, over what sample?

### Rollback Trigger

What result forces reversal?

### Recommendation

Choose one:

- approve
- reject
- shadow first
- capped live only
- needs more evidence

## Daily Safety Checks

The operator should always be able to see:

- current equity
- current open exposure
- biggest concentration
- whether reconciliation is clean
- whether any book is on probation
- whether any new changes are in shadow, capped live, or scaled live
- biggest current live risk

## Weekly Committee Memo

Once per week, the system should produce a memo that answers:

- what made money
- what lost money
- which books improved
- which books deteriorated
- which books should be scaled
- which books should go to probation
- which books should be retired
- what meaningful changes were made
- what is still experimental
- what the single biggest current risk is

## Kill Switches

These are non-negotiable.

Examples:

- if reconciliation integrity breaks materially, reduce to safe mode
- if daily drawdown exceeds the approved threshold, stop adding risk
- if a book has negative rolling expectancy over the review window, probation or de-allocation
- if large-loss frequency spikes, size down globally or by affected book
- if a new change underperforms its control case, roll it back

## Core Doctrine

The system must be judged by the following sentence:

> Velox should identify which books have durable net expectancy, verify where they work, constrain where they do not, and preserve that edge in live trading through disciplined execution and capital allocation.

That is the standard. Not activity. Not novelty. Not elegance.
