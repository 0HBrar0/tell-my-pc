# Security policy

tell-my-pc runs an AI agent on your computer on request from your phone, so security bugs
matter more than usual. Thank you for reporting them.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/0HBrar0/tell-my-pc/security/advisories/new).

Please include the version or commit, your OS, the config needed (with secrets removed), and
the smallest steps that show the problem. For a policy bypass, the tool call and input that
got through are the most useful part.

We aim to acknowledge reports within 7 days and to fix confirmed high-severity issues within
30 days. We credit reporters in the release notes unless you ask us not to.

## In scope

- Anything that lets a tool call do what its tier or a Never rule forbids
- Ways to give the daemon tasks or answer approvals without the token, secret or one-time link
- Leaks of tokens, Claude credentials or other secrets the Never rules cover
- Ways to make the daemon touch files outside the project or switch branches in the wrong repo

## Out of scope

See "Known limits" in [docs/threat-model.md](docs/threat-model.md). For example: the Work tier
running the project's own tests (that is by design), plugins you installed doing what plugins
can do, or an attacker who already runs code as your user.

## Supported versions

Only the latest release and `main` get security fixes until v1.0.
