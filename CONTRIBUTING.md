# Contributing to tell-my-pc

Thanks for helping. A few rules keep this project safe to run on people's computers.

## Ground rules

- **No servers run by maintainers.** Every feature must work with the user's own machine,
  Claude login, Tailscale and ntfy (or a self-hosted relay they run).
- **Never touch Claude credentials.** Don't read, store, copy or forward Claude login or
  OAuth tokens. We only launch the user's own `claude` CLI.
- **Never `--dangerously-skip-permissions`.** Containment is tiers, the policy hook, task
  branches and approvals.
- **Every deny rule needs a test that proves it blocks**, through `policy.decide` and, for new
  mechanisms, through the real hook process (`tests/test_policy.py` shows how).
- **Fail closed.** When something is unclear, broken or times out, deny.
- **No secrets in the repo.** Config lives in the user's config dir; the repo has
  `config/example.yaml` and the generated `config/schema.json`.

## Setup

```bash
git clone https://github.com/0HBrar0/tell-my-pc && cd tell-my-pc
uv sync                                   # Python 3.12+
uv run pytest -q                          # FakeExecutor tests, no Claude needed
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

Tests that need a real, logged-in `claude`: `TMP_RUN_CLAUDE=1 uv run pytest -q -m claude`.
They are skipped in CI.

If you change config models, regenerate the schema: `uv run python -m tell_my_pc.schema`
(a test fails if it is stale). If your code drives git, run the suite once with
`GIT_CONFIG_GLOBAL=/dev/null`; a global gitignore can hide bugs that CI catches.

## Making a change

1. Open or pick an issue. For anything security-related, see [SECURITY.md](SECURITY.md) first.
2. Branch from `main` (`feat/…`, `fix/…`, `docs/…`).
3. Keep PRs focused. Add tests. Update docs and `CLAUDE.md` if behaviour changes.
4. Significant design decisions get a short ADR in `docs/decisions/NNNN-title.md`.
5. PR titles use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`,
   `fix:`, `docs:`, `chore:`). PRs are squash-merged with that title.
6. CI must pass on Windows, macOS and Linux.

## Plugins

New notifiers, transports, executors and delivery methods usually belong in a separate
package, not in core. See [docs/writing-a-plugin.md](docs/writing-a-plugin.md) and the sample
in `examples/plugins/file-notifier`.

## Code of conduct

Be kind, assume good faith, and keep discussion about the work.
