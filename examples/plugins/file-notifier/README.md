# tell-my-pc-file-notifier

A sample notifier plugin for [tell-my-pc](../../../README.md). It appends every report,
question and info notification to a JSONL file. It is the reference for
[docs/writing-a-plugin.md](../../../docs/writing-a-plugin.md).

```bash
# install next to tell-my-pc (same environment)
uv pip install ./examples/plugins/file-notifier
tell-my-pc plugins          # lists: notifier  file  tell-my-pc-file-notifier
```

```yaml
also_notify:
  - type: file
    options:
      path: ~/tell-my-pc-notifications.jsonl
```
