## What and why

<!-- One or two sentences. Link issues with "Closes #123". -->

## Checklist

- [ ] Title follows Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`)
- [ ] Tests added or updated; `uv run pytest -q` passes
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` pass
- [ ] Any new or changed deny rule has a test that proves the action is blocked
- [ ] Fails closed on errors and timeouts
- [ ] No secrets, real config or personal files committed
- [ ] Docs, `config/schema.json` and `CLAUDE.md` updated if behaviour changed
- [ ] ADR added in `docs/decisions/` for a significant design decision
