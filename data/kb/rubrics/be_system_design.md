---
type: rubric
visibility: internal
role: Backend Engineer
title: Rubric — Rate Limiter Design
question: How would you design a rate limiter for an API?
---

## Competency
Evaluates systems reasoning: clarifying requirements, choosing an algorithm with explicit
trade-offs, and handling distribution, failure, and abuse.

## Signals We Want
- Clarifies limits (per user/IP/key), window semantics, and burst behavior first.
- Names concrete algorithms (token bucket, sliding window log/counter) and their trade-offs.
- Addresses distributed state (shared store vs local), atomicity, and clock skew.
- Considers failure modes: fail-open vs fail-closed, storage outage, hot keys.

## Score Anchors (0–5)
- 0–1: Jumps to code with no requirements; single-node only; no trade-offs.
- 2: One algorithm, no distribution or failure discussion.
- 3: Reasonable algorithm choice with basic distributed storage awareness.
- 4: Clear trade-offs, distributed correctness (atomic ops), and failure handling.
- 5: All of 4 plus abuse handling, hot-key mitigation, and observability of limits.

## Model Answer (abridged)
Clarify the limit dimension and window, pick token bucket for smooth bursts, keep counters in a
shared low-latency store with atomic increments, decide fail-open for availability, and add
metrics on rejects and hot keys.
