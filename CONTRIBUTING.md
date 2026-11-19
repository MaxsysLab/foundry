# Contributing to Foundry

Welcome. This is a learning-friendly project published by Maxsys International
Incorporated through MaxsysLab. Students and interns are the intended contributors, so
questions are encouraged and small pull requests are great.

## New to open source? Start here

1. Read the code. It is about 5,700 lines, and `src/foundry/engine.py` is the place to
   begin: one function, `build`, runs the whole pipeline from top to bottom.
2. Run the tests on your own machine (`make dev`, then `make test`). They need no API
   key and call no model.
3. Pick an issue labelled **good first issue**, or an item on the
   [roadmap](docs/ROADMAP.md). Comment so others know you are working on it. If
   nothing fits, ask in a new issue and a maintainer will suggest one.
4. Read "The two rules that matter most" below.
5. Open a small pull request. A maintainer will review it and help you get it merged.

You do not have to write code to contribute. A build that went wrong, written up with
its brief and its session file, clearer documentation, and help answering other
people's issues all count.

## Ground rules

- Be kind and follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- **File an issue before a large pull request.** Small fixes (typos, obvious bugs) can
  go straight to a pull request. For anything substantial, open an issue first so we
  can agree on the approach.
- Security problems are never reported in a public issue. Follow [SECURITY.md](SECURITY.md).

## Prerequisites

- Linux, on a filesystem that tells `README.md` from `readme.md`
- Python 3.12 or 3.13
- [uv](https://docs.astral.sh/uv/) (`uv --version`) and git

You need Claude Code and an API key only to run a real build. Read "Before you run
it" in the README first, and run real builds inside the sandbox. For the sandbox and
its tests you need bubblewrap; without it those tests are skipped.

## Build, test, run

```bash
make dev          # install with the development extras
make test         # the test suite: offline, a scripted planner and a fake worker
make lint         # ruff
make typecheck    # mypy --strict
```

## How the code is laid out

```
src/foundry/cli.py           the commands: foundry build, foundry vet, foundry sandbox
src/foundry/engine.py        the build: discovery, plan, tasks, verify, review
src/foundry/trust.py         when Foundry refuses to run unattended
src/foundry/auth.py          the API sign-in a build needs
src/foundry/fsprobe.py       the check for a filesystem that ignores case
src/foundry/discovery.py     the brief turned into a spec with acceptance criteria
src/foundry/planner.py       the spec turned into tasks in dependency order
src/foundry/archetypes.py    tasks a kind of project must always have
src/foundry/constitution.py  the standard every worker is given
src/foundry/roles/           the role each worker takes
src/foundry/worker.py        the worker: Claude Code through the Agent SDK
src/foundry/gates.py         the command that proves a task
src/foundry/isolation.py     a git worktree for each parallel task, and the merge back
src/foundry/verify.py        the running app checked against the criteria
src/foundry/review.py        the code review, and what blocks
src/foundry/report.py        the report read out of a verifier's or reviewer's answer
src/foundry/security.py      the calls to SkillWarden
src/foundry/skills.py        the skill library
src/foundry/session.py       the session file a build resumes from
src/foundry/models.py        the plan, the tasks, the reports and the build's record
src/foundry/control.py       the approval of a plan before the build
src/foundry/providers/       the planner's model: the API, or the `claude` command
src/foundry/procenv.py       the environment every process Foundry starts is given
src/foundry/sandbox/         the sandbox: bubblewrap round a whole build, and its proof
tests/                       the tests
```

## The two rules that matter most

**A worker's word is not evidence.** A task with a command is done when that command
exits 0, run by Foundry. A build is done when the verify step and the review ran and
did not block.
When you add a step that can fail, make its failure visible in the result. Do not
catch an error and carry on as if the step had run, and do not treat a check that
could not run as a check that passed.

**The tool stays small enough to read.** Foundry has five dependencies and a few
thousand lines, and a person can read all of it. Prefer a few lines of our own to a
new dependency, and discuss first if you think one is needed.

## Changing what Foundry refuses, what counts as done, how work is merged, or the sandbox

These four are where a mistake does harm, so a change to any of them needs a little
more than a passing test:

1. **A test for the case that must fail**, beside the test for the case that works.
   If you change when a build is refused, test a run that must still be refused.
2. **No work lost.** `isolation.py` never deletes a worktree or a branch, and never
   merges over an edit it did not make. Its tests use real git repositories. A change
   there keeps every one of them passing and adds one for the new case.
3. **A second person who tries to break it.** Ask in the pull request for someone other
   than you to look for the input your change did not think of. In the sandbox, a
   check is made on the thing that was opened, never on the name it was asked for
   by, and every line of the proof has a test that shows it can say `REACHED`.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Make your change. Keep pull requests focused and small.
3. Add or update tests, and run `make lint typecheck test`.
4. Write commit messages in the [Conventional Commits](https://www.conventionalcommits.org/)
   style, for example `fix: report a retained worktree once`.
5. **Sign off every commit** (see below).
6. Open a pull request, fill in the template, and link the issue it closes.

## Developer Certificate of Origin (DCO)

This project uses the **DCO**, not a contributor licence agreement. By signing off, you
certify that you wrote the contribution or have the right to submit it under the
project's licence. The full text is at <https://developercertificate.org/>. Sign off by
adding a `Signed-off-by` line to each commit:

```bash
git commit -s -m "fix: report a retained worktree once"
```

This appends `Signed-off-by: Your Name <your@email>`, matching your git configuration.
A check on every pull request enforces it. Fix a missing sign-off with
`git commit --amend -s`, or `git rebase --signoff` for a series.

## Good first areas

- A second project type beside the web app, such as a command-line tool, with the
  tasks it must always have (`archetypes.py`).
- Run `foundry sandbox proof` on your own system and tell us what it printed. Other
  distributions and 64-bit ARM are untried.
- Tests for a part of the code that has few.
- Clearer wording in a message Foundry prints.

## Review and merge

We aim to give a first response within 3 business days. Reviews are about the code,
never the person. Once a change is approved and every required check passes, a
maintainer merges it.

## Growing here

Sustained, good contribution earns responsibility. [GOVERNANCE.md](GOVERNANCE.md)
describes the path from first contribution to maintainer. Foundry is looking for a
dedicated maintainer, so that path is real.
