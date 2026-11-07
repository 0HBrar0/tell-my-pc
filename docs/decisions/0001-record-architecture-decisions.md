# 0001 · Record architecture decisions

Status: accepted · 2026-11-07

## Context
The project is built largely by autonomous Claude sessions that start cold. Decisions must be
discoverable without reading the whole history.

## Decision
Record each significant decision as a short ADR in `docs/decisions/NNNN-title.md` with Status,
Context, Decision and Consequences. `CLAUDE.md` links here and carries the current phase.

## Consequences
New sessions read `CLAUDE.md` and the ADRs to pick up where the last one stopped.
