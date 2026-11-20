# Roadmap

This is the work that is open, in the order we think it matters. If something here
interests you, open an issue and say so before you start, so that two people do not
build the same thing. Small steps are welcome; none of these has to arrive in one pull
request.

## 1. The sandbox: what is there, and what comes next

A worker is a Claude Code session with permission checks switched off. Our first plan
was to use Claude Code's own sandbox around each worker's shell. Reading it closely
changed the order. That sandbox covers a worker's shell commands only, and Foundry
itself runs what the workers wrote: the plan's commands, the app under test, and git
in a repository whose hooks a worker can write. So the boundary had to go round the
whole build first.

That first step is in the repository: `foundry sandbox`, a whole build inside
bubblewrap without root, with a proof anyone can run ([SANDBOX.md](SANDBOX.md)). What
is still open, in the order we think it matters:

- **The network without root.** Today the inside has the machine's own network unless
  `--offline` is given, and shutting out the local network takes root. The plan: no
  network inside at all, and a small proxy outside that lets through the hosts a
  build needs. The same proxy could add the API key on the way out, so that the key
  never enters the sandbox.
- **Limits inside the boundary.** Each worker's tools limited by its role, the
  operator's Claude settings kept out of the workers' sessions, and a hook before
  each tool call that writes a record and can refuse.
- **The build's verdict.** Inside the sandbox the workers and Foundry are the same
  user, so a worker that set out to cheat could change the session file a build
  resumes from. Foundry's own records should sit where the workers cannot write.
- **More systems.** The proof passes on Debian 13 and on Ubuntu 26.04, both x86-64.
  Other distributions, 64-bit ARM and systems that restrict user namespaces are
  untried.
- **More eyes.** A sandbox is the kind of code that a second and a third reader make
  better. [SECURITY.md](../SECURITY.md) says how to tell us about a way out.
- **A live build inside it,** started with `foundry sandbox build` and an API key;
  see the next item.

## 2. A live build through the command, on an API key

Our own builds have run on our in-house setup. What this public version still owes is
a build started the way the README describes: `foundry sandbox build`, with an API
key. Its result goes into the README as it comes out: what passed, what failed, how
long it took and what it cost.

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

## 10. Smaller things

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
