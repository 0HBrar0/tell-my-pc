# When Claude is at its usage limit

## Default: wait, then run automatically

If Claude reports a usage limit, tell-my-pc **holds** the task and runs it by itself once the
limit resets (it reads the reset time from Claude's message, or retries in an hour if there
is none). You get a notification like:

> demo: held (3fcc8998) · This task needs Claude, and Claude's usage limit is reached. I'll
> run it automatically at 15:01.

While Claude is limited, new tasks don't spend an attempt on Claude. Siri tells you straight
away: "Claude is at its usage limit until 15:00. It will run then." A held task resumes its
own Claude session and branch. `tell-my-pc stop` (or the Stop Shortcut) cancels held tasks.

## Optional: a fallback model for questions

Read-tier tasks (questions, reviews, summaries) can go to another model instead of waiting.
Any OpenAI-compatible endpoint works. A local one keeps your code on your computer:

```yaml
fallback:
  read_tier: true                 # default
  models:
    - name: local
      base_url: http://127.0.0.1:11434/v1   # Ollama
      model: qwen2.5-coder:7b
      max_steps: 12
```

With [Ollama](https://ollama.com): install it, run `ollama pull qwen2.5-coder:7b` (pick a
model that supports tool calling), and use the config above. For a hosted service, add
`api_key: {env: OPENAI_API_KEY}` and an `https://` `base_url`. `tell-my-pc check` warns
when a fallback service is not on your machine or tailnet, because tasks then send it your
project's files.

What the fallback model can and can't do:

- It gets `read_file`, `list_files` and `search`. Every call goes through the same tiers
  and Never rules as Claude's.
- No web search, and it **never** asks you for approvals: anything that would need a tap on
  the phone is simply refused.
- The report and the notification say plainly that a fallback model did the work, and
  which one. Siri starts with "Claude was at its usage limit, so a backup model answered."

## Optional: Work tasks on a fallback model

Off by default. Turn it on per project:

```yaml
projects:
  - name: website
    path: ~/code/website
    fallback_work: true
```

Then Work tasks there may also `write_file` and `run_command` (Work allowlist only) on a
separate `fallback/task-<id>` branch, so it is obvious which changes came from the fallback
model. Projects without `fallback_work` keep waiting for Claude.
