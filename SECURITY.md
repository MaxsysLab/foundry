# Security Policy

Foundry starts AI workers that write files and run commands. A weakness in it can
become a weakness on the machine it runs on. We want to hear about one before anyone
else does.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues or pull
requests.**

Report privately through
[GitHub Private Vulnerability Reporting](https://github.com/MaxsysLab/maxsys-foundry/security/advisories/new)
(preferred), or by email to **security@maxsys.org**.

Please include a description of the issue, steps to reproduce or a proof of concept,
the affected version or commit, and any fix you would suggest. If you would like
credit, tell us how to name you.

## What counts as a vulnerability here

- A way out of `foundry sandbox`: anything running inside that reads your files or
  keys, writes outside the workspace and the sandbox's own folders, gains a
  privilege, reaches your terminal, or makes the command outside do something for
  it. So is a line of `foundry sandbox proof` that says `closed` for a door that is
  open.
- A way to make Foundry start a build that it says it refuses: unattended on a bare
  host, on a filesystem that ignores case, or without an API sign-in.
- A way to have a build reported as done when a task's command failed, or when the
  verify step or the review was asked for and failed or did not run.
- A way for Foundry itself, apart from its workers, to write outside the workspace, or
  to lose or overwrite work in the workspace that it did not create.
- A way for text in a brief, a session file, a worker's report or a skill library to
  make Foundry itself run a command other than the commands of the plan, or to forge
  or hide lines in what it prints.

## What does not need a private report

What a worker does inside the workspace is by design. Outside the sandbox, workers are
Claude Code sessions with permission checks switched off, acting with the operator's
account, files and network, and the README says so. Inside the sandbox, what
[docs/SANDBOX.md](docs/SANDBOX.md) lists as left open is known as well: the machine's
network, the API key, and the build's own verdict. Ideas and help for closing those
are welcome in the open. The same holds for the plan's own commands: Foundry runs
them through a shell in the workspace, and the plan is something you can read and
approve first (`-i`).

A weakness in an app that Foundry built is a finding about the review step. Open an
issue with the brief and what the review missed. If you would rather not publish it
yet, report it privately and we will treat it with the same care.

## What to expect

- An **acknowledgement** within **7 days** of your report.
- An initial **severity assessment** and a triage decision shortly after.
- We work on a fix under a coordinated **embargo**. The standard disclosure window is
  **90 days** from your report, or sooner once a fix is released, whichever comes first.
- When the fix is out we publish a **GitHub Security Advisory**, request a CVE where
  warranted, and credit reporters who want it.

## Supported versions

| Version | Supported |
|---|---|
| the `main` branch | yes |
| anything older | best effort |

## Safe harbor

We will not pursue or support legal action against researchers who act in good faith,
follow this policy, avoid privacy violations and service disruption, and give us
reasonable time to respond before any public disclosure.
