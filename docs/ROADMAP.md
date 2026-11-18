# Roadmap

This is the work that is open, in the order we think it matters. If something here
interests you, open an issue and say so before you start, so that two people do not
build the same thing. Small steps are welcome; none of these has to arrive in one pull
request.

## 1. A sandbox of its own

Today a worker is a Claude Code session with permission checks switched off. It acts
with the operator's account, files and network, and the only protection is the
container or virtual machine the operator puts around Foundry. That is why Foundry
refuses to run unattended on a bare host, and it is the first thing to change.

The design we have chosen is to use what Claude Code already provides, through the
Claude Agent SDK, and not to write a sandbox of our own:

- the SDK's sandbox settings for the shell: a list of paths a worker may write, a list
  of hosts it may reach, and credentials kept out of its reach;
- the set of tools a worker has, limited by role, in place of every tool with every
  permission;
- a hook before each tool call that can refuse it and that writes a record.

A first step that stands alone: a tested container recipe in this repository, so that
"run it in a container" is one command.

Two things belong to the same work. The session file a build resumes from sits today
where the workers can write, and a resumed build trusts what it says; it should sit
where only the operator can write. And the live build of November 2026 ran inside a
wrapper written for one machine of ours. A recipe that anyone can run is still to be
written.

## 2. A live build through the command, on an API key

The first live build with this copy was made in November 2026, and the README says
how it came out. It was started from Python with our own sign-in, on a machine of
ours. What is still owed is a build started the way the README tells a stranger to
start one: `foundry build`, with an API key, inside a container. Its result goes into
the README as it comes out: what passed, what failed, how long it took and what it
cost.

## 3. Heavier stacks and real parallel work

A stack such as Next.js has not been tried. Neither has a build whose plan has many
independent tasks running at once, which is what the worktree for each task is for.

One known gap belongs here. A virtual environment or a `node_modules` folder is not
committed, so it does not travel from one task's worktree to the next, and a task
whose command needs what the task before it installed will fail. Until the build
environment lives outside the worktrees, such builds need `--isolation none`.

The November build showed a second gap. Two tasks ran side by side, and the worker of
one changed files that belonged to the other's work. The second merge conflicted.
Foundry refused it and failed the task, which is the safe answer, but the build then
stopped until it was run again. Two ways to do better: tell each task which files are
its own, or run a conflicted task once more on the new base before giving up.

## 4. More kinds of project

There is one project type today, the web app. When a brief reads as one, Foundry adds
a security task to a plan that lacks it and asks the planner for a final end-to-end
check. A command-line tool, a library and a data pipeline each want their own short
list of tasks that must not be missing.

## 5. A command for every task

A task is proven by its command, and the planner may leave a task without one. The
security task that Foundry adds has none either, because Foundry does not know the
project's test tooling at that point. Such a task is done when its worker says so,
and rests on the verify step and the review that follow. Every task should end with a
command Foundry can run, or the plan should be sent back.

## 6. A check of the plan against the spec

Before any worker starts, compare the plan with the acceptance criteria: a criterion
that no task covers, or two tasks that contradict each other, is cheapest to fix at
that moment. A small "argue against this plan" step belongs with it.

The same moment is the time to look at the machine. A plan's commands call the tools
of the app's stack, such as `python -m pytest`. If one of them is missing, the first
gate fails, and the fault is in the machine and not in the work. Foundry should check
that the programs the plan names are there, and say so before a worker starts.

## 7. Skills that are vetted before they are used

`foundry vet` scans a skill library, but `foundry build --skills` does not ask whether
the library was scanned. It should either refuse a library that was not, or scan it
itself.

## 8. A record of the run that can be queried

A build prints its events and keeps a session file. A long build wants more: a
structured record of every worker call, gate and decision, so that a failed build can
be understood afterwards without reading the whole log.

## 9. Improving a project by a number

A second way of working, apart from building: given a goal and a number that measures
it (test coverage, a response time), change, measure, keep the change only if the
number improved, and repeat.

## 10. Smaller things the November build showed

Each of these is a small piece of work, and a good first one.

- **An error that says why.** When the planner's `claude` call fails, the message is
  empty, because the reason is on the command's standard output and Foundry shows
  only its error stream. With an expired sign-in the build goes on to fail one call
  later, still without a reason.
- **SkillWarden pointed at the app.** The check after a build scans the whole
  workspace, with the worktrees Foundry kept and the test caches in it. It should
  scan the tree as committed.
- **A command to clear what a build kept.** After one small build there were ten kept
  worktrees and their branches. Foundry never removes them by itself, and it should
  not; a command the operator runs to clear them is wanted.
- **A verifier that tidies up.** The verify step leaves its scratch folder in `/tmp`.

## What we decided not to do

- **An agent framework underneath.** The pipeline is plain code that one person can
  read, and it stays that way.
- **Other model providers, for now.** Foundry drives Claude Code. Supporting a second
  kind of worker is possible behind the `Worker` interface, but it is not on this
  list.
- **Deleting anything it did not create.** Foundry never removes a worktree, a branch
  or a file of yours to make room. It reports what it left and lets you decide.
