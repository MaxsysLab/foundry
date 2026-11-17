# Foundry skill library

A small starter library of skills that workers may consult during a build
(`foundry build --skills ./skill-library`). A library is a folder of
`<category>/<skill>/SKILL.md` bundles. For each task, Foundry picks the skills that
fit the task's role and copies their `SKILL.md` into the workspace.

## Categories

- `frontend/`: the look and quality of a user interface (for the `frontend` role)
- `testing/`: checking a running web app (for the `tester` role)

## Where these skills come from

They are taken from the public [`anthropics/skills`](https://github.com/anthropics/skills)
repository and are licensed under the Apache License 2.0. Each skill keeps the
`LICENSE.txt` it came with. The copyright is Anthropic's; see each skill's licence
file.

| Skill | Source | Licence | State |
|---|---|---|---|
| `frontend/frontend-design` | anthropics/skills | Apache-2.0 | as published |
| `frontend/theme-factory` | anthropics/skills | Apache-2.0 | as published |
| `testing/webapp-testing` | anthropics/skills | Apache-2.0 | changed by us (see below) |

## What we changed

- **`webapp-testing`**: `scripts/with_server.py` as published started servers with
  `subprocess.Popen(..., shell=True)`. A command passed through a shell can be
  injected into, so we changed the script to run without one: a leading `cd <dir> &&`
  is read as a working directory, and the rest of the command is split with `shlex`.
  The documented uses behave as before. The change is marked in the file.

Each skill was scanned with [SkillWarden](https://github.com/MaxsysLab/skillwarden)
before it was added (`foundry vet ./skill-library`).

Anthropic's document skills (`docx`, `pdf`, `pptx`, `xlsx`) are not included. They are
published under a different licence that does not allow it.
