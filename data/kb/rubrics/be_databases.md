---
type: rubric
visibility: internal
role: Backend Engineer
title: Rubric — Database Schema Design
question: How do you approach database schema design?
---

## Competency
Evaluates whether the candidate designs relational schemas that stay correct and performant as
requirements change, and whether they reason about trade-offs rather than reciting rules.

## Signals We Want
- Starts from access patterns and invariants, not tables in isolation.
- Normalizes for correctness, then denormalizes deliberately for known read patterns.
- Discusses indexing, foreign keys, and constraints as correctness tools, not afterthoughts.
- Considers evolution: migrations, backfills, and avoiding destructive changes.

## Score Anchors (0–5)
- 0–1: Vague or incorrect; treats schema as an afterthought; no trade-offs.
- 2: Names normalization but cannot connect it to access patterns.
- 3: Solid, correct default approach; some indexing and constraint awareness.
- 4: Clear reasoning from access patterns to schema with deliberate denormalization and evolution.
- 5: All of 4 plus concrete production experience: partitioning, hotspots, migration safety.

## Model Answer (abridged)
A strong answer starts with the queries and invariants, models entities and relationships, adds
constraints and indexes to enforce and serve them, and only denormalizes for measured read paths,
while planning safe, reversible migrations.
