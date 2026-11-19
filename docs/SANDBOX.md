# The sandbox

A Foundry worker is a Claude Code session with permission checks switched off. That
is what lets a build run without someone approving every step, and it is also why a
build should not run loose on a machine you care about. `foundry sandbox` puts the
whole build inside [bubblewrap](https://github.com/containers/bubblewrap), the small
program that Flatpak uses for the same purpose. It needs no root and no container
runtime.

The boundary goes round the whole build, and not only round each worker's shell,
because Foundry itself runs what the workers wrote: the plan's commands, the app
under test, and git in a repository whose hooks and settings a worker can change.

This page says how to use it, what it closes, what it leaves open, and how to check
all of that on your own machine. It is new. It has been tried on one machine of ours
(Debian 13 under WSL 2 on x86-64, bubblewrap 0.12.0), and no live build has run inside
it yet.

## Use it

You need Linux on x86-64, bubblewrap 0.8.0 or later (the package is called
`bubblewrap` on Debian, Ubuntu and Fedora), and an ordinary user. As root it
refuses. A system-call filter for 64-bit ARM is written too, and has never run on
such a machine; if you have one, the proof below is the way to find out.

Put the word `sandbox` in front of the build you would have run:

```bash
export ANTHROPIC_API_KEY=your-key
uv run foundry sandbox --venv build "a bookmark manager with tags, SQLite and a REST API" \
  -w ./out -i --session ./out/.foundry/session.json
```

Everything after the word `build` goes to `foundry build` as it is. The sandbox's own
options come before it. If the build stops, run the same line again without the
brief, as you would outside.

Start it from outside the workspace, as in the line above. Python and uv read the
folder a command is started in before Foundry has a say, and a build can write its
workspace; so when you stand inside the workspace the sandbox says so and stops.

Two things are different inside. The session file has to be in the workspace, since
the build can write nowhere else on your machine; `./out/.foundry/session.json` is a
good place, because Foundry keeps that folder out of the app's own git history. And
the machine's Python is not set up for installing packages on most systems, so
`--venv` gives the build a virtual environment of its own, and `python` and `pip`
inside lead to it.

To look at what a build left, run your commands inside as well:

```bash
uv run foundry sandbox run -w ./out -- git log --oneline
uv run foundry sandbox --venv run -w ./out -- python -m pytest -q
```

The reason is given under "What a build leaves" below.

The sandbox follows no link on the way to a path you name, because a build can
leave a link behind, or put one there while it is still running. It opens the folder
first, asks the kernel where the opened folder lies, and stops unless that is the
path as you wrote it. The refusal names the real path, so you can type that one if
it is the one you mean. On a system whose `/home` is itself a link, that is the path
to use.

| Option | What it does |
|---|---|
| `--offline` | No network at all inside |
| `--venv` | A Python virtual environment of the sandbox's own, which `python` and `pip` inside lead to |
| `--name NAME` | Keep the sandbox's home and `/tmp` under this name. The default is made from the workspace, so a resumed build finds its own |
| `--ro PATH` | Also show this file or folder inside, read-only, at the same path. Refused for your home, for the folders keys and sign-ins are kept in, and for a folder that holds a socket |
| `--path DIR` | Put this folder on the `PATH` inside. It must be under `/usr` or under a `--ro` folder |
| `--tool FILE` | Put this one program on the `PATH` inside, under its own name |
| `--env NAME` | Pass this variable of your environment to the inside |
| `--env-file FILE` | Pass the `NAME=VALUE` lines of this file. Only you may be able to read the file (`chmod 600`) |
| `--memory SIZE` | A ceiling on memory, such as `3G`. It needs systemd |
| `--dns ADDRESS` | Name these resolvers inside, in place of the machine's own |

Node from nvm, for example, lives under your home, which the sandbox does not show.
Give it by name: `--ro ~/.nvm/versions/node/v24.22.1 --path ~/.nvm/versions/node/v24.22.1/bin`.

## What it closes

Before it starts, the command prints what the inside can write, what it can read and
which variables were passed in. Here is the same in full.

**Your files.** The inside sees `/usr`, a short list of harmless files from `/etc`,
Foundry with its Python, Claude Code, and what you add with `--ro`. It gets a home
and a `/tmp` of its own. Your home folder is not there: no SSH keys, no GitHub or
cloud sign-ins, no Claude Code sign-in, no shell history, no other projects. The
only folder of yours it can write is the workspace, and the workspace has to be a
folder of its own: your home, a folder where keys are kept, a system folder and a
folder that holds Foundry itself are refused. Everything else it is shown is
read-only, and so is every folder on its `PATH`, with one exception that you ask
for: the virtual environment of `--venv` is the build's own to write. Its folder
comes last on the `PATH`, so that nothing a build puts there can stand in for `git`
or `claude`. Before anything starts, the sandbox looks from the inside and stops if
any other folder on the `PATH` can be written.

A read-only folder keeps the inside from changing its files. It does not keep the
inside from connecting to a socket that lies in it, even with `--offline`. So the
sandbox refuses to show `/run`, and it looks through a folder you name with `--ro`
and refuses it when it finds a socket there. It looks at no more than 100,000
entries, so a very large folder is not searched to its end. It does not look through
the workspace in that way: keep the sockets of your own programs out of it.

**Your environment.** The inside starts with an environment built from nothing. The
API sign-in is passed (`ANTHROPIC_API_KEY`, or the switch of a cloud provider),
with any `FOUNDRY_*` settings, and what you name with `--env` or `--env-file`.
Nothing else: no tokens that happen to sit in your shell, no SSH agent. bubblewrap
itself is started with an empty environment, because its first process can be looked
at from inside.

**Your privileges.** It runs as you, with no capabilities, with no way to gain any
through `sudo` or another setuid program, and without user namespaces of its own. It
has its own process list and cannot see or signal yours. The list of your kernel
keyrings is covered.

**Your terminal.** There is no terminal inside. What the build prints comes out
through a pipe, and control codes in it are shown as text (`\x1b`), never obeyed. What
you type is passed in, so approving a plan with `-i` works.

**A few system calls** that the rest does not cover: the socket family that leads
from a virtual machine or a WSL system to its host, the kernel keyring, and io_uring.

When the sandbox cannot be made as described (no bubblewrap, an older one, a machine
type it has no system-call filter for, or a system that does not let bubblewrap
start), the command stops with exit code 2 and says why. It never falls back to
running the build unprotected.

## What it leaves open

Please read this part before you rely on it.

**The network.** Unless you give `--offline`, the network inside is your machine's
own. A build needs the internet for the model and for packages, and with it comes
everything else your machine can reach: your local network, services that listen on
the machine's own loopback, and, on a rented machine, the provider's metadata
address, which hands out the machine's credentials. If a program on your desktop
listens on an abstract unix socket, a build can connect to it too. `foundry sandbox
proof` lists what it finds of these on your machine.

If you have root, you can shut the local network out today with a filter that
systemd puts around the sandbox. This is how we run it on our own machine:

```bash
sudo systemd-run --scope --quiet --collect \
  -p "IPAddressDeny=0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4 fc00::/7 fe80::/10 ff00::/8" \
  -- setpriv --reuid="$(id -u)" --regid="$(id -g)" --clear-groups \
  -- env HOME="$HOME" PATH="$PATH" "$PWD/.venv/bin/foundry" sandbox \
     --env-file ./key.env --dns 1.1.1.1 --venv build "your brief" -w ./out
```

The `setpriv` line matters: it makes the build run as you again, without the groups
root would leave it. The key comes from a file because `sudo` does not carry your
environment across, and `--dns` names a public resolver because the filter also
refuses a resolver on your own network. Run `proof` in place of `build` on the same
line and it will say that the local network is refused. The loopback stays open
either way.

Doing this without root is the next step on the [roadmap](ROADMAP.md): no network
inside at all, and a small proxy outside that lets through the hosts a build needs.

**The key.** The workers call the model, so they can read the key you pass in. Use a
key made for this, with a spending limit, and take it away when you are done.

**The build's verdict.** Inside the sandbox the workers and Foundry are the same
user. A worker that set out to cheat could change the session file or the
repository's settings and so forge what Foundry records. The sandbox protects your
machine. It does not make a build's "done" more trustworthy than it was.

**What a build leaves.** The workspace comes back to you with whatever the workers put
in it. A link in it can point anywhere on your machine, and git runs the hooks and
settings of the repository it is in. So until you have read what is there, look at it
through `foundry sandbox run`, and do not run git, npm, pip or the app's own tests on
it outside. Inside, the mount table shows the real paths of the folders the sandbox
was given, so a build can learn where your workspace lies on your machine. The same
care holds for the sandbox's home and `/tmp`, which are kept under
`~/.local/state/foundry/sandbox/` (or `$XDG_STATE_HOME`) so that a resumed build finds
its caches. A resumed build also finds whatever the earlier workers left in that home.
When a build is finished and you have what you want from it, that folder can go.

**Your groups.** Without root, a process cannot give up the groups you belong to. The
inside is shown so little that this should not matter, and the proof looks for any
file inside that only a group of yours may read.

**Disk and memory.** There is no limit on disk space. `--memory` sets a ceiling on
memory; at the ceiling the kernel stops the process that passed it, and the build
goes on or fails from there. Without it a build can use all the memory the machine
has, through its programs or through `/dev/shm`.

**On WSL.** Windows registers its program files with the Linux kernel, so a Windows
program file started inside the sandbox is handed to WSL's own launcher, which then
runs inside the sandbox. Its two known roads to Windows are closed: the socket
family named above is refused, and the folder with WSL's own sockets is not shown.
We have not proven that there is no third.

**The kernel.** A sandbox of this kind shares your machine's kernel with what runs
inside. A virtual machine is a stronger wall, and still the right choice for a brief
or a skill library from someone you do not trust.

## Check it yourself

```bash
uv run foundry sandbox proof
uv run foundry sandbox --offline proof
```

The proof makes a sandbox new, goes inside, and tries each door: your keys and
sign-ins by their paths, a file it put in your home and a variable it put in your
environment as bait, writes outside its own folders, `sudo`, the system calls named
above, and the network. Give it the options you mean to build with (`--venv`, `--ro`)
and it tries the sandbox as you will use it. Each line is one attempt:

```
closed    your /home/you/.ssh
closed    a file made as bait in your home folder
closed    a variable set as bait in your environment
closed    sudo
closed    writing in /usr
open      writing in /home/foundry
left open the internet (api.anthropic.com, port 443)
```

`closed` is a door that was tried and refused. `open` is one that works and is meant
to. `left open` is one this sandbox does not close, as described above. `REACHED`
means something that must be closed was reached, and the proof then fails with exit
code 1. Please tell us privately if you ever see that line; [SECURITY.md](../SECURITY.md)
says how.

A pass covers your machine as it is set up today and the doors the proof knows, tried
from inside. It cannot look at the command outside that makes the sandbox, which
decides which paths it accepts; that part is covered by the tests and by review. And
it is not a promise about doors nobody has thought of. A reviewer set to break the
sandbox found doors that the proof of that day had missed, and they are in it now. If
you can think of another, that is the most useful contribution this part of Foundry
can get.

## How it is made

Three files, under `src/foundry/sandbox/`:

- `__init__.py` decides what the inside is given, refuses what it cannot make safe,
  writes bubblewrap's arguments and relays the output.
- `seccomp.py` assembles the system-call filter by hand, so that Foundry needs no
  compiler and no extra library for it.
- `proof.py` is the proof. It runs inside.

bubblewrap gets its arguments through a file descriptor and not on its command line,
so that neither the layout nor the key shows in the machine's process list.

A path you name is never trusted by its name. It is opened, the kernel is asked
where the opened folder lies, and every check is made on that answer. When the
sandbox starts, the folder is opened once more, compared with what was checked, and
handed to bubblewrap as an open descriptor.

The command outside runs nothing a build can write. The programs it runs itself
(bubblewrap, and with `--memory` systemd-run and env) are taken from the system's own
folders and never from your `PATH`. A workspace is refused when it shares a folder
with Foundry's own installation, with Claude Code's, or with a folder on your `PATH`.
Entries of your `PATH` that are not full paths, such as `.`, are not looked at, and
your own shell still follows them when you step into a workspace.
