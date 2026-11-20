# Foundry

Give Foundry a short brief, such as "a bookmark manager with tags and a REST API". It
writes down what the finished app must do, plans the work, and hands each task to a
Claude worker with a role: architect, backend, frontend, tester, security. Most tasks
carry a command that proves them, such as the build, the type check or the tests, and
such a task is not done until Foundry has run the command and seen it pass. When
every task has passed, a separate verifier starts the app and checks it against
what was written down at the start, and a separate reviewer reads the code. What you
get is a repository with the app, its tests, and the plan it was built to.

Foundry comes from our own production work. We use it in everyday development, and we
have built production applications and live sites with it. We share it here so that
other developers can use it too, and improve it with us if they choose.

Two things about it are deliberate.

**A worker's word is not evidence.** When a task has a command that proves it, Foundry
runs that command itself and reads the exit code. The finished app is then exercised
by a verifier and read by a reviewer, both on the strongest model. The reviewer is
told to treat any security hole as blocking, and a blocking finding keeps the build
from being called done. So does a verify step or a review that fails to run. One
limit is on the roadmap: a task whose plan gave it no command still rests on the
worker's report and on the checks that come after.

**It is small enough to read.** About 5,700 lines of Python and five dependencies, with
no agent framework underneath. One person can read all of it in a day or two, which is
a good way to learn how such a system is put together.

## How a build goes

1. **Discovery.** The brief becomes a short spec with acceptance criteria that can be
   tested against a running app.
2. **Plan.** A stack is chosen from what the brief needs, and the work is split into
   tasks in dependency order, most with the command that proves them. A brief that
   reads as a web app gets a security task, added by Foundry if the plan left it out.
   With `-i` you read the plan and can ask for another before anything is built.
3. **Build.** Each task goes to a worker with a role. Foundry runs the task's command.
   If it fails, the output goes back to the worker, up to three attempts. Tasks that do
   not depend on each other run side by side, each in its own git worktree, and are
   merged back only when the merge is clean.
4. **Verify.** A verifier starts the app and checks each acceptance criterion, in a
   browser when one is available and over HTTP when not.
5. **Review.** A reviewer reads the code for correctness, readability, architecture,
   security and performance. Injection, broken authentication, path traversal, exposed
   secrets and the like are to be rated as blocking. If
   [SkillWarden](https://github.com/MaxsysLab/skillwarden) is installed, its critical
   findings are added.

Every worker is given the same short constitution: the standard for quality, testing,
security and honest reporting. You can replace it with your own (`--constitution`).

A build can be stopped and picked up again from its session file.

## Before you run it

Foundry's workers are Claude Code sessions with permission checks switched off, so
that a build can run without someone approving every step. Left to themselves they
act with your account, your files and your network. So Foundry has a sandbox of its
own, and a build belongs inside it:

```bash
uv run foundry sandbox build "your brief" -w ./out
```

Inside, a build can write in its workspace and in a home and a `/tmp` of its own. It
can read the system's programs, Foundry and Claude Code. Your keys, your sign-ins and
the rest of your files are not there. It needs Linux and
[bubblewrap](https://github.com/containers/bubblewrap), and no root. You do not have
to take our word for it: `uv run foundry sandbox proof` tries each door from the
inside, on your machine, and prints what it found.

Two things it does not close, and you should know them before you start. The network
inside is your machine's own, because a build needs the internet for the model and
for packages; with it a build can also reach your local network and the services that
listen on the machine itself. And the API key is passed in, because the workers call
the model with it. Use a key made for this, with a spending limit.
[The sandbox page](docs/SANDBOX.md) has the whole account: what is closed, what is
left open, and how to shut the local network out when you have root. For a brief or a
skill library from someone you do not trust, a virtual machine around it is still the
right choice, since the sandbox shares your machine's kernel.

Foundry would rather refuse than run half-protected. It exits with code 2, before any
model is called, in these cases:

- **A sandbox that cannot be made as promised.** Without bubblewrap, or where
  bubblewrap cannot start, `foundry sandbox` stops there. It does not fall back to
  running the build unprotected. It also refuses a workspace that is your home, a
  folder where keys are kept, or a path that leads through a link.
- **No terminal, and no sandbox.** Outside the sandbox, a run from cron, CI or a pipe
  is refused, unless a container or VM wrapper of your own names itself with
  `--sandbox NAME`. Foundry records that name and cannot check it. At a terminal,
  without a sandbox, it runs as you and says so.
- **A filesystem that ignores case** (the default on macOS and Windows). Its checks
  treat `README.md` and `readme.md` as two files, so it does not build where they are
  one. To look, it creates the workspace folder and a small probe inside it, and
  removes the probe again.
- **No API sign-in.** See the next section.

## Start

You need Linux, Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), git,
bubblewrap, [Claude Code](https://code.claude.com/docs/en/setup) on your `PATH`, and an
Anthropic API key.

Foundry runs each task's command with the tools on your machine, so the machine also
needs what the app's own stack needs. For a Python app that is `python` and `pip`. In
the sandbox, `--venv` gives the build a virtual environment of its own for that.

Foundry is built on the Claude Agent SDK, and Anthropic asks that tools built on it use
an API key and not a claude.ai sign-in
([their note](https://code.claude.com/docs/en/agent-sdk/overview)). So Foundry starts a
build only when `ANTHROPIC_API_KEY` is set, or one of the cloud providers the SDK
supports is switched on by setting its variable to `1` (`CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_ANTHROPIC_AWS`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`).
The last of these names Microsoft Foundry, which has nothing to do with this tool.

1. Get the code and install it:

   ```bash
   git clone https://github.com/MaxsysLab/foundry.git
   cd foundry
   uv sync
   ```

2. Set the key and give it a small brief, inside the sandbox:

   ```bash
   export ANTHROPIC_API_KEY=your-key
   uv run foundry sandbox --venv build \
     "a bookmark manager with tags, SQLite and a REST API" \
     -w ./out -i --session ./out/.foundry/session.json
   ```

   The sandbox's own options come before the word `build`. What comes after it is
   what you would give `foundry build` without a sandbox, and the table below lists
   it. In the sandbox the session file has to be inside the workspace, since the
   build can write nowhere else.

3. If it stops, run the same command again without the brief. It picks up from the
   session file:

   ```bash
   uv run foundry sandbox --venv build -w ./out --session ./out/.foundry/session.json
   ```

A build makes many model calls, some of them on the strongest model. Start with a
small brief and watch the cost in your Anthropic console.

| Option | What it does |
|---|---|
| `-w DIR` | The workspace the app is built in. Give it a folder of its own |
| `-i` | Show the plan and wait for your approval |
| `--session FILE` | Save progress, and resume from it |
| `-g TEXT` | Guidance for the plan: stack, scope, conventions |
| `--skills DIR` | A skill library the workers may consult (see below) |
| `--constitution FILE` | Your own standard in place of the built-in one |
| `--isolation none` | One shared workspace instead of a worktree per task |
| `--no-verify`, `--no-review` | Skip that step. The build then says so |

The exit code is `0` when the build is done, `1` when it is not, and `2` when Foundry
refused to start or to go on.

## Skills

A skill is a folder with a `SKILL.md` that tells a worker how to do one kind of work
well. Point `--skills` at a library of them and Foundry gives each task the few that
fit its role. A small starter library is in [`skill-library/`](skill-library), taken
from the skills Anthropic publishes.

Before you adopt a library from somewhere else, scan it:

```bash
uv run foundry vet ./some-skill-library
```

This needs [SkillWarden](https://github.com/MaxsysLab/skillwarden), our scanner for
agent skills, installed beside Foundry.

## Status

We have used Foundry for almost a year in our own development. This repository is its
public version, 0.1.0, and no release has been tagged from it yet.

Two things here are newer than the rest. A build in this version starts with an API
key, and the `foundry sandbox` command is new. Both are covered by the automated tests,
and the sandbox passes its own proof. Our own builds have run on our in-house setup, so
we would like to hear how a build goes when it is started the way the Start section
describes.

**Tested on.** Linux on x86-64: Debian 13 and Ubuntu 26.04. Every change runs 508
automated tests on Python 3.12 and 3.13, with strict type checking and lint.

**What a build costs.** As one measure, a small JSON API with FastAPI and SQLite,
planned as nine tasks, took 19 minutes in all. It used about two million input tokens,
most of them read from cache, and about one hundred thousand output tokens, on Claude
Opus 5.5 and Claude Sonnet 5.5.

**Known limits.**

- Foundry works with Claude, and it builds web apps.
- Two tasks that run side by side can change the same file. Foundry refuses a merge
  that conflicts, marks the task failed and leaves the repository clean. Run the build
  again from its session file, as step 3 of the Start section describes, and it picks
  up there.
- When tasks depend on each other and share one virtual environment, use
  `--isolation none`.
- A verify step that could not start the app at all is reported as inconclusive and
  does not block.
- The sandbox leaves the network open unless you pass `--offline` or put a filter
  around it. [docs/SANDBOX.md](docs/SANDBOX.md) says what it closes and what it leaves
  open.
- When the `claude` call fails, the error message can be empty, and the worktrees
  Foundry keeps pile up with no command to clear them. Both are on the
  [roadmap](docs/ROADMAP.md).

## Where you can help

Foundry is in use and still growing, and it gets better when more people run it and
read it. These are the places where help counts most today.

**Run the sandbox's proof on your system.** `uv run foundry sandbox proof` calls no
model and costs nothing. It passes on Debian 13 and on Ubuntu 26.04. If you have
another distribution, a 64-bit ARM machine, or a system that restricts user namespaces,
tell us in [issue 2](https://github.com/MaxsysLab/foundry/issues/2) what it printed,
whether it passed or not.

**Try to get out of the sandbox.** If you find a way out, or a line of the proof that
says closed for a door that is open, please tell us privately first, as
[SECURITY.md](SECURITY.md) describes.

**Build something small and tell us how it went.** Use a key made for this, and a
small brief. What passed, where it stopped, how long it took and what it cost are all
useful to us, and the failures most of all.
[Issue 3](https://github.com/MaxsysLab/foundry/issues/3) is the place for it.

**Pick something from the roadmap.** The [roadmap](docs/ROADMAP.md) lists the open
work, and its tenth item has small pieces that make a good first change.

## How it compares

There are larger and better-known open-source tools that build software with AI
agents, among them OpenHands, MetaGPT, ChatDev and gpt-pilot. They cover more ground
than Foundry does.

Foundry is lean on purpose. It works with Claude, it builds web apps, and its Python
source is about 250 kB, against 850 kB to 3,200 kB for the Python tools among those
four (theirs measured on GitHub on 16 November 2026). What it gives for that size is
the checking: a task with a proving command is not done until Foundry has run the
command itself. The finished app is then exercised by a separate verifier and read by
a separate reviewer, and a blocking finding keeps the build from being called done.

## Where the ideas came from

Foundry's own code is ours. Its ideas are not all ours, and we are glad to say where
they came from: the constitution from GitHub's
[spec-kit](https://github.com/github/spec-kit), a git worktree for each parallel task
from [superpowers](https://github.com/obra/superpowers), the review method from Addy
Osmani's [agent-skills](https://github.com/addyosmani/agent-skills), project types and
the evidence-first verifier from
[agency-agents](https://github.com/msitarzewski/agency-agents), and browser
verification from [gstack](https://github.com/garrytan/gstack). The bundled skills are
Anthropic's, under their own licence; see
[`skill-library/README.md`](skill-library/README.md).

## Development

```bash
make dev          # install with the development extras
make test         # the tests: offline, with a scripted planner and a fake worker
make lint         # ruff
make typecheck    # mypy --strict
```

Pull requests are welcome, small ones especially. The [roadmap](docs/ROADMAP.md) lists
the work that is open, and [CONTRIBUTING.md](CONTRIBUTING.md) explains how a change is
tested. If you find a security problem in Foundry itself, please tell us privately
first, as [SECURITY.md](SECURITY.md) describes.

## Who stands behind it

Published as open source by Maxsys International Incorporated, a nonprofit
(maxsys.org), through [MaxsysLab](https://github.com/MaxsysLab). Built by CSEAM
Technology Corporation for its own development work, and released for anyone to use.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Copyright 2026 Maxsys International Incorporated.
