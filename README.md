# Foundry

Give Foundry a short brief, such as "a bookmark manager with tags and a REST API". It
writes down what the finished app must do, plans the work, and hands each task to a
Claude worker with a role: architect, backend, frontend, tester, security. Most tasks
carry a command that proves them, such as the build, the type check or the tests, and
such a task is not done until Foundry has run the command and seen it pass. When
every task has passed, a separate verifier starts the app and checks it against
what was written down at the start, and a separate reviewer reads the code. What you
get is a repository with the app, its tests, and the plan it was built to.

We built it for our own software lab, to learn how far a team of AI workers can be held
to a standard when nobody watches each step. It is published here as a lab project. It
is early and small, and it is open to anyone who wants to read it, try it and help
build it.

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
   git clone https://github.com/MaxsysLab/maxsys-foundry.git
   cd maxsys-foundry
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

## Where it stands

This is version 0.1.0. It has not been released, and it has not built anything for a
customer.

In June 2026 we ran three live builds of one small app, a bookmark manager with
FastAPI, SQLite and plain JavaScript. The first built and passed its own 31 tests. The
second shipped a stored XSS that the review rated too low. That is why the reviewer
is now told to rate security findings as blocking, and why a web app gets a security
task. The third came out with that hole closed and no security finding. Those builds
ran on an earlier version.

In November 2026 we ran one live build with this copy, from the brief in the Start
section above. Foundry wrote down eight things the app must do, planned nine tasks,
each with a command that proves it, and built a JSON API with FastAPI and SQLite. The
brief did not ask for a web page, so it made none.

It did not go through in one run. Two tasks ran side by side, the README and the
security hardening. The worker writing the README also changed two code files, and
when the security work came back, its merge conflicted. Foundry refused the merge,
marked the task failed, left the repository clean and said the build was not done. We
ran the build again from its session file, as step 3 above describes. The security
task ran again on the new base and the build finished: nine of nine tasks, the
verifier's checks passed against the running app, a stop and a restart on the same
database among them, and the review approved it at 90 with three low findings.

We did not take its word for it. We ran the app's 273 tests ourselves and sent our
own requests for each of the eight points, and they hold. The two runs together took
19 minutes. They used about two million input tokens, most of them read from cache,
and about one hundred thousand output tokens, on Claude Opus 5.5 and Claude Sonnet 5.5.

Read that result for what it is: one small Python app, with two tasks in parallel at
the most. We ran it on a machine of our own, inside a wrapper we wrote for that
machine. We started it from Python, with our own Claude Code sign-in on our own
machine. The `foundry build` command with an API key, as the Start section describes
it, is covered by the automated tests (508 of them, with strict type checking and
lint, on Python 3.12 and 3.13) and has not yet been used for a live build.

Foundry's own sandbox was made from that wrapper afterwards, and it is new. It passes
its own proof on one machine of ours (Debian 13 under WSL 2 on x86-64, bubblewrap
0.12.0), with and without a network. Before we wrote this we set a reviewer to break
it. The first two passes found ways through, and those are closed: one let a build
read the environment of the person who started it, the other let a build lead a later
command out of its workspace through a link. The third pass found no way through and
four smaller things in the command outside, and the fourth confirmed their repair. No
live build has run inside the sandbox yet, and we have not tried it on other systems.
If you run the proof on yours, tell us what it printed.

The build also showed us things to fix, and they are on the
[roadmap](docs/ROADMAP.md). This page did not say that the machine needs the tools of
the app's own stack. An expired sign-in gives an empty error message. A worker can
step outside its task. And the worktrees Foundry keeps pile up, with no command to
clear them.

What we have not tried: heavier stacks such as Next.js, an app with a web page built
by this copy, a build with many tasks truly in parallel, and other kinds of project
than a web app. When tasks depend on each other and share one virtual environment,
use `--isolation none`. A verify step that could not start the app at all is reported
as inconclusive and does not block.

## How it compares

There are larger and better-known open-source tools that build software with AI
agents, among them OpenHands, MetaGPT, ChatDev and gpt-pilot. They do more than
Foundry does. Foundry works with Claude only and builds web apps only.

Foundry is the small one. Its Python source is about 250 kB, against 850 kB to
3,200 kB for the Python tools among those four (theirs measured on GitHub on 16
November 2026). We have not run them side by side on the same briefs, so we make no
claim about whose apps come out better.

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
Technology Corporation for its own software lab, and released for anyone to use.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Copyright 2026 Maxsys International Incorporated.
