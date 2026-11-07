# Writing a plugin

tell-my-pc has four plugin kinds. A plugin is an ordinary Python package that registers a
factory under an entry-point group. Core never needs to change for a new plugin.

| Kind | Entry-point group | Interface (`tell_my_pc.plugins.api`) | Configured with |
|---|---|---|---|
| Notifier | `tell_my_pc.notifiers` | `Notifier` | `notifier:` or `also_notify:` |
| Transport | `tell_my_pc.transports` | `Transport` | `transports:` |
| Executor | `tell_my_pc.executors` | `Executor` | `executor: {type: ...}` |
| Delivery | `tell_my_pc.delivery` | `Delivery` | Phase 4 |

The reference is [`examples/plugins/file-notifier`](../examples/plugins/file-notifier).

## 1. The factory

```python
def create(options: dict, ctx) -> MyNotifier:
    ...
```

- `options` is the `options:` mapping from the user's config, unchanged.
- `ctx` has `kind`, `name`, `api_version` (what the host speaks), `logger`, and `data_dir`:
  a private folder for your plugin (`<data dir>/plugins/<kind>/<name>`). Don't read
  tell-my-pc's config file or state yourself.

## 2. The object

It needs two class attributes and the methods of its interface:

```python
from typing import ClassVar

class MyNotifier:
    api_version: ClassVar[str] = "0.2"   # the plugin API you wrote against
    name: ClassVar[str] = "mine"

    async def send(self, note) -> None:  # note: tell_my_pc.plugins.api.Notification
        ...
```

You may import `tell_my_pc.plugins.api` for the dataclasses and protocols, or not import
tell-my-pc at all (the sample doesn't). The host checks at load time that:

- `api_version` is compatible with its own `PLUGIN_API_VERSION`. Before 1.0 the major and
  minor versions must match; from 1.0 on, the major version must match.
- the object implements the interface (method names match the protocol).
- the plugin's name does not shadow a built-in (`ntfy`, `claude_code`, `fake`,
  `tailscale_http`, `ntfy_inbox`). Shadowing plugins are ignored with a warning.

## 3. Register it

```toml
# pyproject.toml
[project.entry-points."tell_my_pc.notifiers"]
mine = "my_package:create"
```

Install it into the same environment as tell-my-pc, then check:

```bash
uv pip install ./my-plugin        # or: pipx inject tell-my-pc my-plugin
tell-my-pc plugins                # notifier  mine  my-plugin (my_package:create)
tell-my-pc check                  # loads configured plugins and reports errors
```

## Interface notes

### Notifier
`send(note)` gets a `Notification` with `title`, `message`, `priority` (1–5), `tags`,
`actions` (buttons: `label`, `url`, `method`), `click` and `kind` (`report`, `question`,
`approval`, `info`). Raise on failure. Approval requests only go to the **main** `notifier`,
because their buttons carry one-time links. If you can't show buttons, don't take the main
slot: approvals sent there would be unanswerable and would time out as denies.

### Transport
`start(sink)` and `close()`. Call `sink.submit(IncomingTask(...))`, `sink.status()`,
`sink.stop()`, `sink.answer_approval()` or `sink.lock()`; each returns a `SinkReply` with a
short `speak` string. **You are responsible for authenticating every request.** Follow the
built-in transports: a strong secret, a timestamp window, a nonce (`store.use_nonce` is not
part of the API, so keep your own replay cache), and rate limits. Never accept requests from
all interfaces by default.

### Executor
`run(req, on_event) -> ExecResult` and `cancel(task_id)`. **This is the most sensitive kind.**
The tiers only hold if your executor makes every tool call pass the policy at
`req.policy_path`: call `tell_my_pc.hook.evaluate(event, PolicySpec.load(req.policy_path))`
before each action, as `FakeExecutor` does, or register the hook as the Claude Code executor
does. Never report `files_changed`; the daemon gets those from git. `tell-my-pc check` warns
whenever a non-built-in executor is configured.

## Plugin API changelog

- **0.2**: `TaskSink` gained `answer_approval()` and `lock()`.
- **0.1**: first version.
