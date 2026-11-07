# 0002 · Enforce tiers with our own PreToolUse hook

Status: accepted · 2026-11-07

## Context
Native Windows has no Claude Code shell sandbox. Claude's permission rules alone are a
pattern language we do not control, and a user's or project's settings can widen them. We need
an enforcement point we own and can test.

## Decision
Every task runs `claude -p` with:

- `--permission-mode dontAsk`: anything not explicitly allowed is refused, nobody is prompted.
- `--tools <tier list>`: the tool set itself is cut down to the tier.
- `--settings <per-task settings.json>` registering one PreToolUse hook for every tool:
  `"<daemon python>" -m tell_my_pc.hook --policy <task policy.json>`.
- `--setting-sources user` and `--strict-mcp-config`: project settings and MCP servers in the
  repo cannot add hooks or permissions.

The hook (`tell_my_pc.policy.decide`) returns allow, ask or deny. Allow is a JSON
`permissionDecision: allow`. Deny and unresolved ask exit with code 2, which Claude Code
treats as a block. Any hook failure is a block (fail closed); this was verified with a real
`claude -p` run where the hook could not start and every tool call was blocked.

On Windows Claude Code runs hooks through Git Bash. If Git Bash is not in a standard place the
hook cannot start, so the executor sets `CLAUDE_CODE_GIT_BASH_PATH` from the `git` it finds
(or `executor.git_bash_path`).

Shell commands are parsed into a deliberately tiny subset (words, quotes, `&&`, `2>&1`,
`| head`/`| tail`). Everything else is "ask". Nested shells and interpreters are Never programs
so they cannot hide commands from the parser.

## Consequences
- Tests in `tests/test_policy.py` prove each Never rule and tier boundary, including through
  the real hook process.
- Work tier still means "run the project's own tests", which runs code the task may have
  edited. The tiers are tool-level containment, not an OS sandbox; the threat model says so.
- The hook adds one Python start-up per tool call (~100 ms). Acceptable for phone tasks.
