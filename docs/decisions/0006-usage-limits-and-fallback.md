# 0006 · Usage limits: hold by default, fallback models only where safe

Status: accepted · 2026-11-07

## Context
Claude Code subscriptions have usage limits. A phone task that hits one should not just fail,
and should not silently be done by a weaker model either.

## Decision
- **Detect**: the Claude Code executor parses limit messages from the result and stderr:
  `…|<epoch>`, "resets 3pm", "resets 4:30am (Area/City)"; unknown reset → 1 hour.
- **Hold by default**: the task becomes `held` with `hold_until = reset + 60 s`. The
  scheduler wakes at least every 15 s and re-queues held tasks whose time has passed. The
  task resumes its own session and its own `tmp/task-<id>` branch. While Claude is known to
  be limited, new tasks skip the Claude attempt, and the Siri reply says so.
- **Fallback models** are OpenAI-compatible profiles in `fallback.models` (the first is used).
  - Read tier: used when `fallback.read_tier` is true (default).
  - Work tier: only for projects with `fallback_work: true`, and the task branch is renamed
    `fallback/task-<id>`.
  - Approve tier: never. The fallback executor runs with approvals forced off, so anything
    outside the tier is denied rather than sent to the phone.
- The fallback executor is a small tool loop (`read_file`, `list_files`, `search`, plus
  `write_file` and `run_command` for opted-in Work). Every call is mapped to the equivalent
  Claude tool and checked with `tell_my_pc.hook.evaluate`, so tiers, Never rules, web taint
  and lock all apply.
- **Honesty**: reports carry `fallback_model` and a "Done by" line; the notification title says
  `via <model>`; the spoken reply starts with "Claude was at its usage limit, so a backup
  model answered."; held tasks say "This task needs Claude …".

## Consequences
- Proven by `tests/test_fallback.py` against a fake OpenAI-compatible server. No real local
  model was available on the maintainer machine when this landed, so the first real
  Ollama run is still to be checked.
- A non-local fallback service receives project file contents; `tell-my-pc check` warns, and
  plain `http://` to a non-local host is refused.
