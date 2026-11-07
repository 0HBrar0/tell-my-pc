# Manual end-to-end check (Phase 1 done criterion)

Imitates the iOS Shortcut with curl against a real `claude` CLI and a sample repo.

```bash
# 1. a sample repo with a failing test
mkdir sample && cd sample && git init -b main
printf 'def add(a, b):\n    return a - b\n' > calc.py
printf 'from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n' > test_calc.py
git add -A && git commit -m init && cd ..

# 2. a throwaway config (loopback is fine for this test)
export TELL_MY_PC_CONFIG_DIR=$PWD/cfg TELL_MY_PC_DATA_DIR=$PWD/data
uv run tell-my-pc init --project ./sample --name calculator --bind 127.0.0.1
uv run tell-my-pc check
uv run tell-my-pc serve &

# 3. what the Shortcut sends
curl -s -X POST http://127.0.0.1:8787/task \
  -H "Authorization: Bearer <token printed by init>" \
  -H "X-TMP-Timestamp: $(date +%s)" -H "X-TMP-Nonce: $RANDOM$RANDOM$RANDOM" \
  -H "Content-Type: application/json" \
  -d '{"text": "Run the tests in the calculator project and fix the failures."}'
```

Expected: the reply speaks "Got it. Task … Starting now."; within a minute an ntfy
notification `calculator: done (<id>)` arrives; `data/reports/<id>.md` lists `M calc.py`
under "Files changed (from git)"; `git -C sample branch` shows `tmp/task-<id>` and `main` is
unchanged.

## Result, 2026-11-07 (Windows 11, claude 2.1.310, sonnet)

- Task `3fcc8998`: Claude ran pytest, found `add()` subtracting, fixed it, re-ran: 2 passed.
  Report: `M calc.py`, branch `tmp/task-3fcc8998`, 14 s. ntfy notification received.
- `tell-my-pc send --resume "...add a sub function..."`: resumed the same session, new
  branch `tmp/task-7a806f12` started from the previous task branch, 3 tests pass.
