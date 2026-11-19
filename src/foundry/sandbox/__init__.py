"""Foundry's own sandbox: a whole build inside bubblewrap, without root.

A worker is a Claude Code session with permission checks switched off, and Foundry
itself runs what the workers wrote: the plan's commands, the app under test, and git
in a repository whose hooks and settings a worker can change. So the boundary goes
round the whole build, not round each worker's shell.

What the inside is given, and nothing else:

* `/usr` and a short list of files from `/etc`, read-only; a `passwd`, `group`,
  `hosts`, `nsswitch.conf` and `resolv.conf` written for it;
* a home and a `/tmp` of its own, kept per name under the state folder;
* the workspace, at the path it has outside, writable;
* Foundry, its Python and Claude Code, read-only, and what `--ro`, `--path` and
  `--tool` add;
* an environment built from nothing: the API sign-in and the `FOUNDRY_*` settings
  are passed, and what `--env` and `--env-file` name.

It runs as the same user with no privileges, no user namespaces of its own, its own
process list, no terminal (output comes through a pipe and control characters are
shown, never obeyed), and a system-call filter (`seccomp.py`).

What stays open is the network: unless `--offline` is given it is the machine's own,
with the internet, the local network, the machine's loopback and any abstract unix
socket listening on it. `proof.py` prints what is closed and what is left open on
the machine it runs on. `docs/SANDBOX.md` has the whole account.
"""

from __future__ import annotations

import codecs
import contextlib
import dataclasses
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from foundry import auth, trust
from foundry.sandbox import seccomp

NAME = "bubblewrap"  # what FOUNDRY_SANDBOX says inside
USER = "foundry"
HOME = "/home/foundry"
VENV = f"{HOME}/venv"
TOOLS = "/opt/foundry/bin"
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
MIN_BWRAP = (0, 8, 0)  # the first with --disable-userns

# Top-level folders that hold the system's programs and libraries beside /usr.
SYSTEM_DIRS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# The only things shown from /etc. Not shown: anything that can hold a secret or
# name the operator's network (fstab, environment, hosts, the system's git, npm and
# pip settings, sudoers, ssh). passwd, group, hosts, nsswitch.conf and resolv.conf
# are written for the sandbox, see `Sandbox.files`.
ETC_SHOWN = (
    "/etc/alternatives",
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/crypto-policies/back-ends",
    "/etc/debian_version",
    "/etc/fonts",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/localtime",
    "/etc/mime.types",
    "/etc/os-release",
    "/etc/pki/ca-trust/extracted",
    "/etc/pki/tls/cert.pem",
    "/etc/pki/tls/certs",
    "/etc/pki/tls/openssl.cnf",
    "/etc/protocols",
    "/etc/services",
    "/etc/ssl/certs",
    "/etc/ssl/openssl.cnf",
    "/etc/terminfo",
)
# A workspace under one of these is refused: they are shown read-only or made new,
# or (/run) hold the sockets of the machine's own services.
NOT_A_WORKSPACE = (
    "/usr", "/etc", "/proc", "/sys", "/dev", "/boot", "/run", "/var/run", *SYSTEM_DIRS
)  # fmt: skip
# Not to be shown with --ro: whole, they hold what the sandbox leaves out on purpose.
# A read-only folder still lets the inside connect to a socket that lies in it.
NOT_SHOWN_WHOLE = ("/etc", "/var", "/tmp", "/var/tmp")
NOT_SHOWN_AT_ALL = ("/run", "/var/run", "/proc", "/sys", "/dev")
# Where a person's keys and sign-ins usually are, under their home.
YOURS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc",
    ".gitconfig", ".git-credentials", ".config/gh", ".config/git", ".config/gcloud",
    ".claude", ".claude.json", ".bash_history", ".zsh_history", ".password-store",
    ".local/share/keyrings", ".mozilla", ".config/google-chrome",
)  # fmt: skip

_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")
_VAR_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIZE_OK = re.compile(r"^[1-9][0-9]*[KMGT]?$")
# What the relay shows as an escape: the set `cli.visible` uses, without the tab and
# the newline (a test keeps the two in step).
UNSAFE_OUTPUT = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏  ‪-‮⁦-⁩]"
)


class SandboxRefused(RuntimeError):
    """The sandbox could not be made as promised; nothing was started inside it."""


@dataclass(frozen=True)
class Options:
    """What the operator asked for on the command line."""

    name: str | None = None
    offline: bool = False
    ro: tuple[str, ...] = ()
    path: tuple[str, ...] = ()
    tool: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    env_file: str | None = None
    venv: bool = False
    memory: str | None = None
    dns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sandbox:
    """One sandbox, decided: every path checked, every refusal already made."""

    name: str
    state: Path  # holds this name's `home` and `tmp`
    workspace: Path
    shown: tuple[tuple[str, str], ...]  # (path inside, where that really is): read-only
    links: tuple[tuple[str, str], ...]  # (where, what it says): links remade inside
    tools: tuple[tuple[str, str], ...]  # (name on the PATH inside, program outside)
    env: dict[str, str]  # all of the inside's environment
    passed: tuple[str, ...]  # the names in it that came from the operator
    offline: bool
    venv: bool
    memory: str | None
    resolv: str
    bwrap: str
    version: str
    machine: str

    @property
    def home(self) -> Path:
        return self.state / "home"

    @property
    def tmp(self) -> Path:
        return self.state / "tmp"

    def files(self) -> dict[str, bytes]:
        """The files written for the inside, by the path they get there."""
        uid, gid = os.getuid(), os.getgid()
        passwd = (
            "root:x:0:0:root:/root:/usr/sbin/nologin\n"
            f"{USER}:x:{uid}:{gid}:Foundry sandbox:{HOME}:/bin/sh\n"
            "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
        )
        group = f"root:x:0:\n{USER}:x:{gid}:\nnogroup:x:65534:\n"
        hosts = "127.0.0.1 localhost foundry-sandbox\n::1 localhost ip6-localhost ip6-loopback\n"
        nsswitch = "passwd: files\ngroup: files\nhosts: files dns\n"
        files = {
            "/etc/passwd": passwd.encode(),
            "/etc/group": group.encode(),
            "/etc/hosts": hosts.encode(),
            "/etc/nsswitch.conf": nsswitch.encode(),
            "/etc/resolv.conf": self.resolv.encode(),
        }
        if self.venv:
            # The virtual environment is the build's own and it can write there, so
            # its folder is last on the PATH, where nothing in it can stand in for
            # git or claude. These four, read-only, lead to it by name.
            for name in ("python", "python3", "pip", "pip3"):
                script = f'#!/bin/sh\nexec {VENV}/bin/{name.rstrip("3")} "$@"\n'
                files[f"{TOOLS}/{name}"] = script.encode()
        return files

    def prefix(self) -> list[str]:
        """What stands in front of bubblewrap: a systemd user scope when a memory
        ceiling was asked for, and nothing otherwise."""
        if self.memory is None:
            return []
        runner, clean = _system_program("systemd-run"), _system_program("env")
        if runner is None or clean is None:
            raise SandboxRefused(
                "--memory needs systemd (the `systemd-run` program) and `env` as "
                "programs of the system's own, and one of them is not there. Nothing "
                "was started."
            )
        line = [
            runner, "--user", "--scope", "--quiet", "--collect",
            "-p", f"MemoryMax={self.memory}", "-p", "MemorySwapMax=0",
        ]  # fmt: skip
        # At the ceiling the kernel stops the process that passed it. From systemd
        # 253 a scope would be stopped whole unless it is told to go on.
        if _systemd_version(runner) >= 253:
            line += ["-p", "OOMPolicy=continue"]
        # systemd-run is handed two variables to find the user's systemd (see
        # `_start_environment`); `env -i` drops them again before bubblewrap.
        return [*line, "--", clean, "-i"]


def _systemd_version(runner: str) -> int:
    try:
        said = subprocess.run(
            [runner, "--version"], capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.search(r"systemd (\d+)", said)
    return int(match.group(1)) if match else 0


# --- deciding what the sandbox is


def state_root(environ: Mapping[str, str] | None = None) -> Path:
    """Where the sandboxes' homes are kept: `$XDG_STATE_HOME/foundry/sandbox`."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "foundry" / "sandbox"


def default_name(workspace: Path) -> str:
    """The same workspace gets the same home, so a resumed build finds its own."""
    digest = hashlib.sha256(os.fsencode(str(workspace))).hexdigest()[:8]
    stem = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-")[:40] or "workspace"
    return f"{stem}-{digest}"


def _inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _open_named(
    given: str, option: str, *, expand: bool = True, flags: int = os.O_PATH
) -> tuple[int, str]:
    """Open the path the operator named with `option`, and say where it is.

    The path is opened first, and the kernel is asked where the opened thing lies
    (/proc/self/fd). Unless that is the path as it was written, made absolute, the
    sandbox stops: a link was on the way, one that a build left behind or one that a
    build still running put there a moment ago. Every other check is made on the
    kernel's answer and never on a second reading of the name, so there is nothing
    to win a race against. Returns the open descriptor and the path."""
    raw = os.path.expanduser(given) if expand else given
    raw = raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)
    written = os.path.abspath(raw)
    try:
        descriptor = os.open(raw, flags | os.O_CLOEXEC)
    except OSError as exc:
        why = "a link that leads nowhere" if os.path.islink(written) else exc.strerror
        raise SandboxRefused(f"{option} {given}: {why}. Nothing was started.") from exc
    try:
        real = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as exc:
        os.close(descriptor)
        raise SandboxRefused(f"{option} {given}: {exc.strerror}. Nothing was started.") from exc
    if real != written:
        os.close(descriptor)
        raise SandboxRefused(
            f"{option} {given}: that path leads to {real}, through a link on the way. "
            "A build can leave a link behind, so the sandbox follows none. Name "
            f"{real} itself if that is the one you mean. Nothing was started."
        )
    return descriptor, real


def _where(given: str, option: str, *, expand: bool = True) -> str:
    """Where the path the operator named really is (see `_open_named`)."""
    descriptor, real = _open_named(given, option, expand=expand)
    os.close(descriptor)
    return real


def require_session_inside(workspace: str, session: str) -> None:
    """The sandbox can write nowhere on the machine but in the workspace and in its
    own home, so a session file has to be inside the workspace. This reads the two
    paths as written and touches nothing: the build opens the file itself, inside,
    where a link that leads out of the workspace leads nowhere."""
    ws = os.path.abspath(workspace)
    place = os.path.abspath(session)
    if place == ws or not _inside(place, ws):
        raise SandboxRefused(
            f"--session {session}: inside the sandbox a build can write only in its "
            "workspace, so keep the session file there, for example "
            f"--session {os.path.join(workspace, '.foundry', 'session.json')}. "
            "Nothing was started."
        )


SYSTEM_PROGRAMS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin")


def _system_program(name: str) -> str | None:
    """`name` as the system has it, or None. The command outside runs bubblewrap,
    systemd-run and env; it takes them from the system's own folders and only when
    they are root's and nobody else can change them. It never looks along the PATH,
    where a folder a build can write may come first."""
    for folder in SYSTEM_PROGRAMS:
        path = os.path.join(folder, name)
        try:
            info = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
            return path
    return None


def _bwrap() -> tuple[str, str]:
    program = _system_program("bwrap")
    if program is None:
        raise SandboxRefused(
            "the sandbox needs bubblewrap (the `bwrap` program) as a program of the "
            f"system's own (looked in {', '.join(SYSTEM_PROGRAMS)}). Install the package "
            "`bubblewrap`. Nothing was started."
        )
    try:
        said = subprocess.run(
            [program, "--version"], capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxRefused(f"bubblewrap did not answer ({exc}). Nothing was started.") from exc
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", said)
    if match is None or tuple(int(g) for g in match.groups()) < MIN_BWRAP:
        need = ".".join(str(n) for n in MIN_BWRAP)
        raise SandboxRefused(
            f"the sandbox needs bubblewrap {need} or later; this machine has "
            f"{said.strip() or 'a version it does not name'}. Nothing was started."
        )
    return program, match.group()


def _keys_and_sign_ins() -> list[str]:
    """The folders and files of `YOURS`, as written and as they really lie."""
    home = Path.home()
    places = [str(home / name) for name in YOURS]
    return list(dict.fromkeys(places + [os.path.realpath(place) for place in places]))


def _make_folders(written: str) -> None:
    """Make the folders of `written` that are not there yet. The deepest one that is
    there is opened and checked like any named path, and each new one is made inside
    the one above it as it was opened, so no link on the way is followed."""
    base, missing = written, []
    while not os.path.lexists(base):
        base, name = os.path.split(base)
        missing.append(name)
    if not missing:
        return
    flags = os.O_RDONLY | os.O_DIRECTORY
    descriptor, _real = _open_named(base, "the workspace", expand=False, flags=flags)
    try:
        for name in reversed(missing):
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, dir_fd=descriptor)
            below = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = below
    except OSError as exc:
        raise SandboxRefused(
            f"the workspace {written} could not be made ({exc.strerror}). Nothing was started."
        ) from exc
    finally:
        os.close(descriptor)


def _run_from(environ: Mapping[str, str]) -> list[str]:
    """Folders whose programs the operator runs outside the sandbox: Foundry's own
    installation, Claude Code's, and every folder on the PATH that is not the
    system's. A build that could write there could put its own program in the place
    of one that is later started outside."""
    places = []
    for entry in environ.get("PATH", "").split(":"):
        if os.path.isabs(entry) and not any(_inside(entry, top) for top in NOT_A_WORKSPACE):
            places += [os.path.normpath(entry), os.path.realpath(entry)]
    found = shutil.which("claude", path=environ.get("PATH"))
    if found is not None:
        places.append(os.path.dirname(os.path.realpath(found)))
    return list(dict.fromkeys(places))


def _workspace(
    given: str, root: Path, install: Sequence[str], environ: Mapping[str, str]
) -> Path:
    """The workspace, where it really is. The refusals read the path as written;
    `_open_named` then has the kernel confirm that the folder is at that path."""
    raw = given if os.path.isabs(given) else os.path.join(os.getcwd(), given)
    text = os.path.abspath(raw)
    home = os.path.realpath(Path.home())
    own = "Give the build a folder of its own. Nothing was started."
    if _inside(home, text):
        raise SandboxRefused(
            f"the workspace {text} holds your home folder, so the sandbox would be "
            f"given all of it to write. {own}"
        )
    for yours in _keys_and_sign_ins():
        if _inside(text, yours) or _inside(yours, text):
            raise SandboxRefused(
                f"the workspace {text} holds or lies in {yours}, where keys and "
                f"sign-ins are kept. {own}"
            )
    sockets = ["/mnt/wslg"]
    if environ.get("XDG_RUNTIME_DIR", "").strip():
        sockets.append(os.path.realpath(environ["XDG_RUNTIME_DIR"]))
    if any(_inside(text, top) for top in (*NOT_A_WORKSPACE, *sockets)):
        raise SandboxRefused(
            f"the workspace {text} is inside a system folder, which the sandbox does "
            f"not hand over. {own}"
        )
    if _inside(text, str(root)) or _inside(str(root), text):
        raise SandboxRefused(
            f"the workspace {text} and the folder the sandboxes keep their homes in "
            f"({root}) lie inside one another. {own}"
        )
    # What a build can write, the command outside must never run: a build could put
    # its own code where Foundry is started from.
    for place in install:
        if _inside(text, place) or _inside(place, text):
            raise SandboxRefused(
                f"the workspace {text} holds or lies in {place}, which is part of "
                f"Foundry's own installation. {own}"
            )
    for place in _run_from(environ):
        if _inside(text, place) or _inside(place, text):
            raise SandboxRefused(
                f"the workspace {text} holds or lies in {place}, a folder whose "
                f"programs you run outside the sandbox. {own}"
            )
    # Python and uv look for their settings and modules in the folder they are
    # started in, before any of this runs. A build can write its workspace.
    if _inside(os.getcwd(), text):
        raise SandboxRefused(
            f"you are standing inside the workspace {text}. Start the sandbox from "
            "outside it: the folder a command is started in is read by Python and by "
            "uv before Foundry has a say, and a build can write there. Nothing was "
            "started."
        )
    _make_folders(text)
    return Path(_where(given, "the workspace", expand=False))


def _state(root: Path, name: str) -> Path:
    state = root / name
    for folder in (state / "home", state / "tmp"):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.is_symlink():
            raise SandboxRefused(
                f"{folder} is a link; the sandbox's home and /tmp must be real "
                "folders. Nothing was started."
            )
    return state


def _links_on_the_way(path: str) -> list[tuple[str, str]]:
    """Every symbolic link met while following `path`, as (where it is, what it
    says), in the order met. A virtual environment's `python` is a link, and the
    path it names may pass through another."""
    links: list[tuple[str, str]] = []
    current = Path("/")
    parts = list(Path(path).parts[1:])
    while parts:
        part = parts.pop(0)
        if part == "..":
            current = current.parent
            continue
        step = current / part
        if step.is_symlink():
            if len(links) >= 40:
                raise SandboxRefused(f"{path} goes through too many links. Nothing was started.")
            try:
                says = os.readlink(step)
            except OSError as exc:
                raise SandboxRefused(
                    f"{step} changed while it was read ({exc.strerror}). Nothing was started."
                ) from exc
            links.append((str(step), says))
            if says.startswith("/"):
                current = Path("/")
            parts = [p for p in Path(says).parts if p != "/"] + parts
        else:
            current = step
    return links


def _install() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """What Foundry itself needs shown: its environment, the Python under it and
    the package, and the links on the way to the interpreter. Paths under /usr are
    shown already."""
    import foundry

    places = [sys.prefix, sys.base_prefix, str(Path(foundry.__file__).resolve().parent)]
    places = [p for p in dict.fromkeys(places) if not _inside(p, "/usr")]
    links = [
        (where, says)
        for where, says in _links_on_the_way(sys.executable)
        if not _inside(where, "/usr") and not any(_inside(where, p) for p in places)
    ]
    return [(place, os.path.realpath(place)) for place in places], links


def _a_socket_in(folder: str, limit: int = 100_000) -> str | None:
    """The first socket found in `folder`, or None. It looks at no more than `limit`
    entries; a larger tree is not looked through to its end."""
    seen = 0
    for top, folders, files in os.walk(folder):
        for name in (*folders, *files):
            seen += 1
            path = os.path.join(top, name)
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    return path
        if seen > limit:
            return None
    return None


def _is_program(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def _find_claude() -> str | None:
    """Claude Code as one program file (how its own installer puts it), or None."""
    found = shutil.which("claude")
    if found is None:
        return None
    real = os.path.realpath(found)
    return real if _is_program(real) else None


def _env_file(path: str) -> dict[str, str]:
    descriptor, _real = _open_named(path, "--env-file", flags=os.O_RDONLY | os.O_NONBLOCK)
    try:
        with open(descriptor, encoding="utf-8") as handle:
            info = os.fstat(descriptor)
            text = handle.read() if stat.S_ISREG(info.st_mode) else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise SandboxRefused(
            f"--env-file {path} cannot be read ({exc}). Nothing was started."
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SandboxRefused(
            f"--env-file {path} must be a file of your own that nobody else can read "
            f"(chmod 600 {path}). Nothing was started."
        )
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, equals, value = line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if not equals or not _VAR_OK.match(name):
            raise SandboxRefused(
                f"--env-file {path}, line {number}: expected NAME=VALUE. Nothing was started."
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def _environment(
    options: Options, environ: Mapping[str, str], path: Sequence[str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    own = {
        "HOME": HOME,
        "USER": USER,
        "LOGNAME": USER,
        "SHELL": "/bin/sh",
        "PATH": ":".join(path),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp",
        "PYTHONNOUSERSITE": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        trust.SANDBOX_ENV: NAME,
    }
    if options.venv:
        own["VIRTUAL_ENV"] = VENV
    passed: dict[str, str] = {}
    for name in auth.API_AUTH_VARS:
        if environ.get(name, "").strip():
            passed[name] = environ[name]
    for name, value in environ.items():
        if name.startswith("FOUNDRY_") and name != trust.SANDBOX_ENV:
            passed[name] = value
    for name in options.env:
        if not _VAR_OK.match(name):
            raise SandboxRefused(f"--env {name}: not a variable's name. Nothing was started.")
        if name not in environ:
            raise SandboxRefused(
                f"--env {name}: that variable is not set here. Nothing was started."
            )
        passed[name] = environ[name]
    if options.env_file is not None:
        passed.update(_env_file(options.env_file))
    for name, value in passed.items():
        if name in own:
            raise SandboxRefused(
                f"{name} is set by the sandbox itself and cannot be passed in. Nothing "
                "was started."
            )
        if "\0" in value:
            raise SandboxRefused(f"{name} holds a NUL character. Nothing was started.")
    return {**own, **passed}, tuple(sorted(passed))


def _resolv(options: Options) -> str:
    if options.dns:
        try:
            servers = [str(ipaddress.ip_address(address)) for address in options.dns]
        except ValueError as exc:
            raise SandboxRefused(f"--dns: {exc}. Nothing was started.") from exc
        return "".join(f"nameserver {server}\n" for server in servers)
    try:
        lines = Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    # The machine's resolvers, without the search list that names its own network.
    kept = [line.strip() for line in lines if line.split()[:1] in (["nameserver"], ["options"])]
    return "".join(f"{line}\n" for line in kept[:8])


def prepare(
    options: Options,
    workspace: str,
    *,
    environ: Mapping[str, str] | None = None,
    need_claude: bool = False,
) -> Sandbox:
    """Decide one sandbox for `workspace`, or raise `SandboxRefused`. Creates the
    workspace and the name's home and `/tmp` when they are not there, and nothing
    else."""
    environ = os.environ if environ is None else environ
    if os.getuid() == 0:
        raise SandboxRefused(
            "the sandbox is made for an ordinary user and does not run as root. "
            "Nothing was started."
        )
    machine = platform.machine()
    try:
        seccomp.assemble(machine)
    except seccomp.UnsupportedMachineError as exc:
        raise SandboxRefused(str(exc)) from exc
    program, version = _bwrap()
    root = state_root(environ)
    shown, links = _install()
    ws = _workspace(workspace, root, [p for pair in shown for p in pair], environ)
    name = options.name if options.name is not None else default_name(ws)
    if not _NAME_OK.match(name):
        raise SandboxRefused(
            f"--name {name}: use small letters, digits, dots and dashes. Nothing was started."
        )
    if options.memory is not None and not _SIZE_OK.match(options.memory):
        raise SandboxRefused(
            f"--memory {options.memory}: give a size such as 3G or 800M. Nothing was started."
        )

    home = os.path.realpath(Path.home())
    for given in options.ro:
        real = _where(given, "--ro")
        only = "Show the one folder that is needed. Nothing was started."
        if _inside(home, real):
            raise SandboxRefused(
                f"--ro {given}: that would show the inside your whole home folder, with "
                f"your keys and sign-ins in it. {only}"
            )
        for yours in _keys_and_sign_ins():
            if _inside(real, yours) or _inside(yours, real):
                raise SandboxRefused(
                    f"--ro {given}: that holds or lies in {yours}, where keys and "
                    "sign-ins are kept, and those are what the sandbox is there to keep "
                    "out. Nothing was started."
                )
        if real in NOT_SHOWN_WHOLE or any(_inside(real, top) for top in NOT_SHOWN_AT_ALL):
            raise SandboxRefused(
                f"--ro {given}: the sandbox leaves that folder out on purpose. {only}"
            )
        socket_found = _a_socket_in(real)
        if socket_found is not None:
            raise SandboxRefused(
                f"--ro {given}: {socket_found} is a socket. A read-only folder does not "
                "keep the inside from connecting to a socket in it, so a folder that "
                "holds one is not shown. Nothing was started."
            )
        shown.append((real, real))

    tools: dict[str, str] = {}
    claude = _find_claude()
    if claude is not None:
        tools["claude"] = claude
    for given in options.tool:
        descriptor, real = _open_named(given, "--tool")
        info = os.fstat(descriptor)
        os.close(descriptor)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise SandboxRefused(f"--tool {given}: not a program file. Nothing was started.")
        tools[os.path.basename(real)] = real

    extra: list[str] = []
    for given in options.path:
        # No link on the way: inside, a link could lead to a folder a build can write.
        place = _where(given, "--path")
        # Of the folders it lies in, the innermost decides: that is the one it is
        # part of inside.
        over = [(str(ws), True), (HOME, True), ("/tmp", True), ("/var/tmp", True), ("/usr", False)]
        over += [(shown_place, False) for shown_place, _real in shown]
        covering = [(len(top), writable) for top, writable in over if _inside(place, top)]
        if not covering:
            raise SandboxRefused(
                f"--path {given}: the folder is not shown inside. Show it, or a folder "
                "above it, with --ro. Nothing was started."
            )
        # A folder in the workspace is a build's own work, even when it is shown
        # read-only for this one run.
        if max(covering)[1] or _inside(place, str(ws)):
            raise SandboxRefused(
                f"--path {given}: the inside can write there, or has written it, and "
                "nothing a build writes goes on its PATH. Nothing was started."
            )
        extra.append(place)
    if need_claude and "claude" not in tools and not any(
        os.access(os.path.join(folder, "claude"), os.X_OK) for folder in extra
    ):
        raise SandboxRefused(
            "Claude Code was not found as one program file on your PATH. Show the "
            "folder of your installation with --ro and put the folder that holds "
            "`claude` on the PATH inside with --path. Nothing was started."
        )

    path = [TOOLS, *extra, *SYSTEM_PATH, *([f"{VENV}/bin"] if options.venv else [])]
    env, passed = _environment(options, environ, path)
    return Sandbox(
        name=name,
        state=_state(root, name),
        workspace=ws,
        shown=tuple(dict.fromkeys(shown)),
        links=tuple(links),
        tools=tuple(sorted(tools.items())),
        env=env,
        passed=passed,
        offline=options.offline,
        venv=options.venv,
        memory=options.memory,
        resolv=_resolv(options),
        bwrap=program,
        version=version,
        machine=machine,
    )


# --- bubblewrap's arguments


def arguments(
    sandbox: Sandbox, cwd: str, files: Mapping[str, int], held: Mapping[str, int]
) -> list[str]:
    """Everything bubblewrap is told, in order. `files` gives, for each path of
    `Sandbox.files`, the descriptor its text can be read from; `held` gives, for the
    workspace and each path shown, the descriptor it was opened on (`_hold`). The
    list goes to bubblewrap through a descriptor of its own, so that neither the
    layout nor a passed value shows in a process list."""
    out = [
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--unshare-cgroup", "--die-with-parent", "--new-session", "--disable-userns",
        "--hostname", "foundry-sandbox", "--clearenv",
    ]  # fmt: skip
    if sandbox.offline:
        out.append("--unshare-net")
    for name, value in sandbox.env.items():
        out += ["--setenv", name, value]

    # The system, read-only.
    out += ["--ro-bind", "/usr", "/usr"]
    for folder in SYSTEM_DIRS:
        if os.path.islink(folder):
            out += ["--symlink", os.readlink(folder), folder]
        elif os.path.isdir(folder):
            out += ["--ro-bind", folder, folder]
    if os.path.isdir("/usr/lib/wsl"):  # WSL mounts Windows' driver folders here
        out += ["--tmpfs", "/usr/lib/wsl", "--remount-ro", "/usr/lib/wsl"]
    out += ["--dir", "/etc"]
    for place in ETC_SHOWN:
        if os.path.exists(place):
            out += ["--ro-bind", place, place]
    out += ["--proc", "/proc", "--dev", "/dev", "--dir", "/run", "--dir", "/var", "--dir", TOOLS]
    for place, descriptor in files.items():
        mode = "0555" if _inside(place, TOOLS) else "0444"
        out += ["--perms", mode, "--ro-bind-data", str(descriptor), place]
    # The list of the operator's kernel keyrings is the machine's, not the inside's.
    for listing in ("/proc/keys", "/proc/key-users"):
        if os.path.exists(listing):
            out += ["--ro-bind", "/dev/null", listing]

    # Its own folders and what it is shown. Shallow paths first, so that a path
    # inside another always lands on top of it, whichever of the two is writable.
    workspace = str(sandbox.workspace)
    binds = [
        ("--bind", str(sandbox.tmp), "/tmp"),
        ("--bind", str(sandbox.tmp), "/var/tmp"),
        ("--bind", str(sandbox.home), HOME),
        ("--bind-fd", str(held[workspace]), workspace),
        *[("--ro-bind-fd", str(held[place]), place) for place, _real in sandbox.shown],
        *[
            ("--ro-bind-fd", str(held[f"{TOOLS}/{name}"]), f"{TOOLS}/{name}")
            for name, _program in sandbox.tools
        ],
    ]
    for kind, source, place in sorted(binds, key=lambda b: len(Path(b[2]).parts)):
        out += [kind, source, place]
    for where, says in sandbox.links:
        out += ["--symlink", says, where]
    covered = [place for _kind, _source, place in binds]
    if not any(_inside(cwd, place) for place in covered):
        out += ["--dir", cwd]
    # Nothing above is left writable but the binds made writable on purpose.
    out += ["--remount-ro", "/", "--chdir", cwd]
    return out


# --- starting it


def _memfd(label: str, content: bytes) -> int:
    descriptor = os.memfd_create(f"foundry-sandbox-{label}")
    os.write(descriptor, content)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor


def shown_safely(text: str) -> str:
    """`text` with control and direction characters shown as escapes; the tab and
    the newline are kept."""

    def shown(match: re.Match[str]) -> str:
        code = ord(match.group())
        return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"

    return UNSAFE_OUTPUT.sub(shown, text)


def _relay(source: IO[bytes], target: IO[str]) -> None:
    """Copy what the inside prints, as it comes, with `shown_safely` applied."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    descriptor = source.fileno()
    while True:
        chunk = os.read(descriptor, 65536)
        text = decoder.decode(chunk, final=not chunk)
        if text:
            with contextlib.suppress(OSError, ValueError):
                target.write(shown_safely(text))
                target.flush()
        if not chunk:
            return


def _feed(target: IO[bytes]) -> None:
    """Pass what the operator types to the inside (the plan's approval)."""
    try:
        descriptor = sys.stdin.fileno()
        while chunk := os.read(descriptor, 65536):
            target.write(chunk)
            target.flush()
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            target.close()


def _hold(path: str, real: str) -> int:
    """Open `path` without reading it, and see that what was opened is where the
    checks found it. bubblewrap is handed the descriptor, never the path again, so a
    link put in the way after the checks is either caught here or comes too late."""
    descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    opened = os.readlink(f"/proc/self/fd/{descriptor}")
    if opened != real:
        os.close(descriptor)
        raise SandboxRefused(
            f"{path} was {real} when it was checked and is {opened} now. Nothing was started."
        )
    return descriptor


def _start_environment(sandbox: Sandbox) -> dict[str, str]:
    """What the process that starts bubblewrap is given. bubblewrap's own first
    process is pid 1 inside, and what it was started with can be read there
    (/proc/1/environ). So it is started with nothing, and the operator's
    environment never comes near it. With a memory ceiling, systemd-run needs these
    two to find the user's systemd; `Sandbox.prefix` drops them before bubblewrap."""
    if sandbox.memory is None:
        return {}
    wanted = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    return {name: os.environ[name] for name in wanted if name in os.environ}


def _command(sandbox: Sandbox, command: Sequence[str], cwd: str) -> tuple[list[str], list[int]]:
    """The command line that starts `command` inside, and the descriptors it needs
    kept open. bubblewrap reads its own arguments from one of them."""
    descriptors: list[int] = []
    try:
        files, held = {}, {}
        for place, content in sandbox.files().items():
            files[place] = _memfd("file", content)
            descriptors.append(files[place])
        workspace = str(sandbox.workspace)
        named = [(workspace, workspace, workspace), *[(p, p, real) for p, real in sandbox.shown]]
        named += [(f"{TOOLS}/{name}", program, program) for name, program in sandbox.tools]
        for place, path, real in named:
            held[place] = _hold(path, real)
            descriptors.append(held[place])
        descriptors.append(_memfd("filter", seccomp.assemble(sandbox.machine)))
        told = [*arguments(sandbox, cwd, files, held), "--seccomp", str(descriptors[-1])]
        descriptors.append(_memfd("args", b"".join(os.fsencode(a) + b"\0" for a in told)))
        line = [*sandbox.prefix(), sandbox.bwrap, "--args", str(descriptors[-1]), "--", *command]
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    return line, descriptors


def launch(
    sandbox: Sandbox, command: Sequence[str], *, cwd: str | None = None, feed: bool = False
) -> int:
    """Run `command` inside and return its exit code. Its output is relayed; with
    `feed`, what is typed here is passed to it. Stopping this process stops
    everything inside."""
    line, descriptors = _command(sandbox, command, cwd or str(sandbox.workspace))
    try:
        process = subprocess.Popen(
            line,
            stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=descriptors,
            env=_start_environment(sandbox),
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    assert process.stdout is not None and process.stderr is not None
    relays = [
        threading.Thread(target=_relay, args=(process.stdout, sys.stdout), daemon=True),
        threading.Thread(target=_relay, args=(process.stderr, sys.stderr), daemon=True),
    ]
    if feed:
        assert process.stdin is not None
        threading.Thread(target=_feed, args=(process.stdin,), daemon=True).start()
    for relay in relays:
        relay.start()
    try:
        code = process.wait()
    except KeyboardInterrupt:
        process.kill()  # bubblewrap takes everything inside with it
        process.wait()
        code = 130
    for relay in relays:
        relay.join(timeout=10)
    for pipe in (process.stdout, process.stderr, process.stdin):
        if pipe is not None:
            with contextlib.suppress(OSError, ValueError):
                pipe.close()
    if code < 0:  # ended by a signal: say so, and answer as a shell would
        print(f"sandbox: stopped by signal {-code}", file=sys.stderr)
        code = 128 - code
    return code


def capture(
    sandbox: Sandbox, command: Sequence[str], *, cwd: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Run `command` inside and return what it printed, for the sandbox's own checks."""
    line, descriptors = _command(sandbox, command, cwd or str(sandbox.workspace))
    try:
        return subprocess.run(
            line,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            pass_fds=descriptors,
            env=_start_environment(sandbox),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxRefused(
            f"the sandbox's own check did not finish in {timeout:.0f} seconds. "
            "Nothing was started."
        ) from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


_PREFLIGHT = (
    "import os, shutil, sys\n"
    "import foundry.cli\n"
    "path = [d for d in os.environ['PATH'].split(':') if d]\n"
    "writable = [d for d in path if os.access(d, os.W_OK) and d != sys.argv[1]]\n"
    "missing = [tool for tool in sys.argv[2:] if shutil.which(tool) is None]\n"
    "if writable: print('writable: ' + ' '.join(writable))\n"
    "elif missing: print('missing: ' + ' '.join(missing))\n"
    "else: print('ready')\n"
)


def check(sandbox: Sandbox, tools: Sequence[str] = (), *, cwd: str = "") -> None:
    """Start the sandbox once with nothing to do. Raises `SandboxRefused` when
    bubblewrap cannot make it on this machine, when Foundry does not start inside,
    or when one of `tools` is not on the PATH inside."""
    venv = f"{VENV}/bin" if sandbox.venv else "-"  # the one folder it may write
    command = [sys.executable, "-P", "-c", _PREFLIGHT, venv, *tools]
    done = capture(sandbox, command, cwd=cwd or None)
    said = done.stdout.strip()
    if said.startswith("writable: "):
        raise SandboxRefused(
            f"the inside can write {said.removeprefix('writable: ')}, which is on its "
            "PATH, and nothing it can write goes there. Nothing was started."
        )
    if done.returncode != 0 or not said.startswith(("ready", "missing: ")):
        why = shown_safely((done.stderr or done.stdout).strip()[-600:])
        raise SandboxRefused(
            "the sandbox could not be started on this machine, or Foundry does not "
            f"start inside it (install Foundry in a virtual environment):\n{why}\n"
            "Nothing was started."
        )
    if said.startswith("missing: "):
        raise SandboxRefused(
            f"not on the PATH inside the sandbox: {said.removeprefix('missing: ')}. "
            "Programs under /usr are there already; show others with --ro and --path, "
            "or one program with --tool. Nothing was started."
        )


def make_venv(sandbox: Sandbox) -> None:
    """With `--venv`: a Python virtual environment in the sandbox's home, made
    inside the sandbox by the first `python3` on its PATH, once."""
    if os.path.lexists(sandbox.home / "venv" / "bin" / "python"):
        return
    script = f'command -v python3 >/dev/null || exit 127; exec python3 -m venv "{VENV}"'
    plain = dataclasses.replace(sandbox, venv=False)  # python3 is still the system's
    done = capture(plain, ["/bin/sh", "-c", script], cwd=HOME, timeout=300)
    if done.returncode != 0:
        why = shown_safely((done.stderr or done.stdout).strip()[-600:])
        raise SandboxRefused(
            "--venv: the virtual environment could not be made inside the sandbox "
            "(it needs python3 with its venv module; on Debian and Ubuntu that is the "
            f"package python3-venv):\n{why}\nNothing was started."
        )


# --- telling the operator what was made, and proving it


def describe(sandbox: Sandbox) -> list[str]:
    """What this sandbox is, in the lines the command prints before it starts."""
    shown = ["/usr", "a short list from /etc", *[place for place, _real in sandbox.shown]]
    tools = ", ".join(name for name, _program in sandbox.tools) or "none"
    if sandbox.offline:
        network = "none (--offline)"
    else:
        network = (
            "this machine's own: the internet, your local network and its loopback "
            "(--offline for none)"
        )
    lines = [
        f"sandbox: bubblewrap {sandbox.version}, name {sandbox.name}",
        f"  can write   {sandbox.workspace}, and a home and /tmp of its own in {sandbox.state}",
        f"  can read    {', '.join(shown)}",
        f"  programs    {tools} (and what is under /usr)",
        f"  passed in   {', '.join(sandbox.passed) or 'nothing'} (what runs inside can read these)",
        f"  network     {network}",
    ]
    if sandbox.venv:
        lines.append(
            f"  python      a virtual environment of its own in {VENV}, which it can "
            "write; its folder is last on the PATH"
        )
    if sandbox.memory is not None:
        lines.append(f"  memory      at most {sandbox.memory}")
    return lines


def _has_vsock() -> bool:
    """Whether this machine has the AF_VSOCK family at all, asked outside."""
    try:
        socket.socket(seccomp.AF_VSOCK, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


def prove(options: Options, environ: Mapping[str, str] | None = None) -> int:
    """Make a sandbox new, run `proof.py` inside it and return its exit code: 0
    when everything that must be closed is closed. What it made is removed again."""
    environ = dict(os.environ if environ is None else environ)
    token = secrets.token_hex(4)
    # The bait is put where the operator's own things are: in this process's real
    # environment, which is what a careless start would hand on, and in the home.
    bait_env = f"PROOF_BAIT_{token.upper()}"
    environ[bait_env] = os.environ[bait_env] = "set outside the sandbox"
    home = Path.home()
    bait_file = home / f".foundry-proof-bait-{token}"
    workspace = os.path.realpath(tempfile.mkdtemp(prefix="foundry-proof-"))
    state: Path | None = None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        os.close(os.open(bait_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        chosen = dataclasses.replace(options, name=f"proof-{token}")
        sandbox = prepare(chosen, workspace, environ=environ)
        state = sandbox.state
        check(sandbox)
        if sandbox.venv:
            make_venv(sandbox)
        yours, absent = [], []
        for name in YOURS:
            try:
                info = os.lstat(home / name)
                yours.append([str(home / name), info.st_dev, info.st_ino])
            except OSError:
                absent.append(f"~/{name}")
        facts = {
            "uid": os.getuid(),
            "yours": yours,
            "absent": absent,
            "bait_file": str(bait_file),
            "bait_env": bait_env,
            "read_only": [
                *[place for place, _real in sandbox.shown],
                TOOLS,
                *[f"{TOOLS}/{n}" for n, _p in sandbox.tools],
            ],
            "writable": [HOME, "/tmp", str(sandbox.workspace)],
            "venv_bin": f"{VENV}/bin" if sandbox.venv else "",
            "offline": sandbox.offline,
            "listener": listener.getsockname()[1],
            "wsl": any(Path("/proc/sys/fs/binfmt_misc").glob("WSLInterop*")),
            "vsock": _has_vsock(),
        }
        command = [sys.executable, "-P", "-m", "foundry.sandbox.proof", json.dumps(facts)]
        return launch(sandbox, command, feed=True)
    finally:
        listener.close()
        os.environ.pop(bait_env, None)
        with contextlib.suppress(OSError):
            bait_file.unlink()
        # Only what this call made: its own workspace, and the home and /tmp of its
        # own name, in which nothing but the proof has run.
        shutil.rmtree(workspace, ignore_errors=True)
        if state is not None:
            shutil.rmtree(state, ignore_errors=True)
