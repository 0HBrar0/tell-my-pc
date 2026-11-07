# 0003 · Task branches, switched in place

Status: accepted · 2026-11-07

## Context
Every task must run on its own `tmp/task-<id>` branch, and "continue" must resume the same
Claude session, which Claude keys by working directory.

## Decision
- The project folder must be the top of its own git work tree. A folder that only sits inside
  another repo (for example a home folder that is itself a repo) is treated as not a repo, so
  we never switch branches in an outer repo by accident.
- Work tier: the tracked tree must be clean, else the task is refused with a spoken reason.
  The daemon runs `git switch -c tmp/task-<id>` in the project folder itself (no worktree), so
  the cwd, and therefore `--resume`, stays stable.
- Read tier: same, but on a dirty tree it runs in place without a branch (it cannot write).
- After the run the daemon commits leftovers on the task branch (`--no-verify`; files that
  were already untracked before the task are left alone), diffs `start..task-branch`, and
  switches back to where the user was. A branch with no commits is deleted.
- `continue` starts the new task branch from the previous task's branch.
- `files_changed` is the `git diff --name-status` of that range, never the model's claims.

## Consequences
- While a task runs, the user's editor sees the branch switch. Documented in setup docs.
- One task per project at a time is required anyway; the scheduler enforces it.
