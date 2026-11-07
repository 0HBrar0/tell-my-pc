# 0007: Plugin API 1.0 and what "stable" means

Status: accepted (2026-11-07)

## Context

Phases 3–5 changed the plugin API twice (0.1 → 0.2) while approvals, files and fallback
landed. For v1.0, plugin authors need to know what they can rely on.

## Decision

`PLUGIN_API_VERSION` is `"1.0"`. The interfaces are the same as 0.2.

- The host loads a plugin when its major version matches (`api_compatible`).
- Minor versions (1.1, 1.2, ...) only add things: new optional fields on dataclasses with
  defaults, new optional methods the host checks for before calling, new `Notification.kind`
  values that notifiers may treat as `info`.
- Removing or renaming a method, field or entry-point group, or changing a method's meaning,
  needs 2.0.
- 0.x plugins are refused (pre-1.0 minors had to match exactly); they only need
  `api_version = "1.0"`.

The package version (`tell_my_pc.__version__`) follows SemVer separately from the plugin API.

## Consequences

Security fixes may still tighten what an executor is allowed to do (the policy is not part of
the plugin API). Anything else that would break a 1.x plugin waits for 2.0 and gets an entry
in the plugin API changelog in `docs/writing-a-plugin.md`.
