"""The proof: run inside the sandbox, it tries each door and says what happened.

`foundry sandbox proof` starts it in a sandbox made new for the purpose and hands it
what it cannot know from inside: where your own files are outside, and a file and a
variable made as bait. Each line is one attempt:

    closed    tried, and refused: as promised
    open      works, and is meant to (the sandbox's own folders, an ordinary socket)
    left open works, and is not closed by this sandbox: said here and in the docs
    REACHED   something that must be closed was reached
    MISSING   something that must work does not

It ends with PROOF PASSED when no line says REACHED or MISSING. A pass covers this
machine, as it is set up today, and the doors tried here, from inside. It cannot look
at the command outside that makes the sandbox (which paths it accepts, and how), and
it is not a promise about doors nobody has thought of.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
from typing import Any

from foundry.sandbox import seccomp

CLONE_NEWUSER = 0x10000000


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def section(self, title: str) -> None:
        print(f"-- {title}")

    def closed(self, what: str, reached: bool, detail: str = "") -> None:
        if reached:
            self.failed += 1
        print(f"{'REACHED  ' if reached else 'closed   '} {what}{detail and f' ({detail})'}")

    def works(self, what: str, ok: bool, detail: str = "") -> None:
        if not ok:
            self.failed += 1
        print(f"{'open     ' if ok else 'MISSING  '} {what}{detail and f' ({detail})'}")

    def left_open(self, what: str) -> None:
        print(f"left open {what}")

    def note(self, what: str) -> None:
        print(f"          {what}")


def _status() -> dict[str, str]:
    with open("/proc/self/status", encoding="utf-8") as handle:
        return dict(line.rstrip("\n").split(":\t", 1) for line in handle if ":\t" in line)


def _can_create(folder: str) -> bool:
    path = os.path.join(folder, f".foundry-proof-{os.getpid()}")
    try:
        with open(path, "x", encoding="utf-8"):
            pass
    except OSError:
        return False
    os.unlink(path)
    return True


def _can_change(path: str) -> bool:
    if os.path.isdir(path):
        return _can_create(path)
    try:
        with open(path, "ab"):
            return True
    except OSError:
        return False


def _connect(host: str, port: int, timeout: float = 4.0) -> str:
    """'connected'; 'refused' (a machine answered, with a no); 'filtered' (stopped
    on this side, by a rule); or 'no answer'."""
    try:
        socket.create_connection((host, port), timeout).close()
        return "connected"
    except ConnectionRefusedError:
        return "refused"
    except PermissionError:
        return "filtered"
    except OSError:
        return "no answer"


def _filtered(host: str) -> bool:
    """Whether a rule on this side refuses packets to `host`. A filter on a systemd
    scope drops a connection's first packet without a word, but it refuses a
    datagram aloud; so one empty datagram goes to the discard port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.sendto(b"", (host, 9))
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _gateway() -> str | None:
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            for line in list(handle)[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
    except (OSError, ValueError):
        pass
    return None


def _listening_abstract_sockets() -> list[str]:
    names: set[str] = set()
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as handle:
            for line in list(handle)[1:]:
                fields = line.split(None, 7)
                # Num RefCount Protocol Flags Type St Inode Path; flag 0x10000 = listening
                if len(fields) == 8 and fields[7].startswith("@") and int(fields[3], 16) & 0x10000:
                    names.add(fields[7].strip())
    except (OSError, ValueError):
        pass
    return sorted(names)


# Each probe below answers one question with True when the door is open. The tests
# run every one of them outside the sandbox too, where it is open, to see it say so.


def _userns_opens() -> bool:
    """Whether this process can make a user namespace of its own. It changes the
    process that asks, so outside the sandbox it is only ever asked of a child."""
    return bool(ctypes.CDLL(None, use_errno=True).unshare(CLONE_NEWUSER) == 0)


def _vsock_opens() -> bool:
    try:
        socket.socket(seccomp.AF_VSOCK, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


def _keyring_answers() -> bool:
    """Whether the kernel keyring answers: the user keyring's number is asked for."""
    libc = ctypes.CDLL(None, use_errno=True)
    machine = seccomp.MACHINES[platform.machine()]
    return bool(libc.syscall(machine.keyctl, 0, -4, 0) != -1 or ctypes.get_errno() != errno.EPERM)


def _io_uring_answers() -> bool:
    """Whether io_uring is there at all: any answer but "no such call" counts."""
    libc = ctypes.CDLL(None, use_errno=True)
    machine = seccomp.MACHINES[platform.machine()]
    made = libc.syscall(machine.io_uring[0], 0, 0)
    return bool(made != -1 or ctypes.get_errno() != errno.ENOSYS)


def _reads(path: str) -> bool:
    """Whether `path` can be opened and holds anything."""
    try:
        with open(path, "rb") as handle:
            return bool(handle.read(1))
    except OSError:
        return False


def _same_file(path: str, device: int, inode: int) -> bool:
    """Whether `path` here is the very file that has this device and number outside."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (info.st_dev, info.st_ino) == (device, inode)


def _writable_on(path: str) -> list[str]:
    return [folder for folder in path.split(":") if folder and os.access(folder, os.W_OK)]


def _first_process_was_started_with() -> list[str] | None:
    """The names in the environment the sandbox's first process (pid 1, bubblewrap
    itself) was started with, or None when it cannot be read. It is started outside,
    so whatever it was handed there shows here."""
    try:
        with open("/proc/1/environ", "rb") as handle:
            block = handle.read()
    except OSError:
        return None
    names = [entry.split(b"=", 1)[0] for entry in block.split(b"\0") if entry]
    return [name.decode("utf-8", "replace") for name in names]


def _environments_holding(name: str) -> list[str]:
    """The processes whose starting environment can be read here and holds `name`.
    The sandbox's own first process (pid 1) is among those looked at: it is started
    outside, and what it was started with shows in /proc/1/environ."""
    held = []
    for pid in sorted(p for p in os.listdir("/proc") if p.isdigit()):
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                block = handle.read()
        except OSError:
            continue
        if any(entry.split(b"=", 1)[0] == name.encode() for entry in block.split(b"\0")):
            held.append(pid)
    return held


def _only_through_a_group(tops: list[str], uid: int) -> tuple[int, list[str]]:
    """Files under `tops` that others may not read but this process can: readable
    only because one of the operator's groups is still held."""
    seen, found = 0, []
    for top in tops:
        for folder, _folders, files in os.walk(top):
            for name in files:
                path = os.path.join(folder, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                seen += 1
                if (
                    stat.S_ISREG(info.st_mode)
                    and not info.st_mode & stat.S_IROTH
                    and info.st_uid != uid
                    and os.access(path, os.R_OK)
                ):
                    found.append(path)
    return seen, found


def run(facts: dict[str, Any]) -> int:
    report = Report()
    status = _status()

    report.section("who it is")
    uid = os.getuid()
    report.closed("being root, or another user than you", uid == 0 or uid != facts["uid"])
    report.closed("holding any capability", int(status.get("CapEff", "1"), 16) != 0)
    report.closed("gaining privileges through a setuid program", status.get("NoNewPrivs") != "1")
    report.closed("running without the system-call filter", status.get("Seccomp") != "2")
    report.closed("a user namespace of its own", _userns_opens())
    sudo = shutil.which("sudo")
    if sudo is not None:
        tried = subprocess.run([sudo, "-n", "true"], capture_output=True, check=False)
        report.closed("sudo", tried.returncode == 0)
    terminal = any(os.isatty(n) for n in (0, 1, 2))
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        terminal = True
    except OSError:
        pass
    report.closed("a terminal to type into or to send control codes to", terminal)
    others = sum(1 for name in os.listdir("/proc") if name.isdigit())
    report.closed("seeing the machine's other processes", others > 12, f"it sees {others}")

    report.section("what it can read")
    for path, device, inode in facts["yours"]:
        report.closed(f"your {path}", _same_file(path, device, inode))
    report.note(f"not on this machine, so not tried: {', '.join(facts['absent']) or 'nothing'}")
    report.closed("a file made as bait in your home folder", os.path.lexists(facts["bait_file"]))
    holding = _environments_holding(facts["bait_env"])
    report.closed(
        "a variable set as bait in your environment",
        facts["bait_env"] in os.environ or bool(holding),
        f"in the environment process {', '.join(holding)} was started with" if holding else "",
    )
    first = _first_process_was_started_with()
    if first is None:
        report.note("what the sandbox's first process was started with could not be read")
    else:
        report.closed(
            "anything at all in the environment the sandbox's first process was started with",
            bool(first),
            ", ".join(first[:8]),
        )
    for path in ("/etc/shadow", "/etc/sudoers", "/etc/ssh", "/etc/fstab", "/etc/environment",
                 "/root", "/mnt", "/media", "/srv", "/var/log", "/sys/kernel"):  # fmt: skip
        report.closed(f"the machine's {path}", os.path.lexists(path))
    report.closed("the list of your kernel keyrings (/proc/keys)", _reads("/proc/keys"))
    folders = [path for path in facts["read_only"] if os.path.isdir(path)]
    seen, found = _only_through_a_group(["/usr", "/etc", *folders], uid)
    report.closed(
        "a file it is shown that only one of your groups may read",
        bool(found),
        ", ".join(found[:5]) if found else f"none among {seen} files",
    )
    report.note("its environment, names only: " + " ".join(sorted(os.environ)))

    report.section("what it can change")
    for path in ["/", "/usr", "/etc", "/home", *facts["read_only"]]:
        report.closed(f"writing in {path}", _can_change(path))
    path = [d for d in os.environ.get("PATH", "").split(":") if d]
    writable = _writable_on(os.environ.get("PATH", ""))
    venv = facts["venv_bin"]
    if venv and writable == [venv] and path[-1] == venv:
        report.closed("a writable folder on the PATH, before its last", False)
        report.left_open(
            f"with --venv, the virtual environment's own folder ({venv}), last on the PATH: "
            "a program a build puts there is found under a name no other folder has"
        )
    else:
        report.closed("a writable folder on the PATH", bool(writable), ", ".join(writable))
    for path in facts["writable"]:
        report.works(f"writing in {path}", _can_create(path))

    report.section("system calls")
    if facts["vsock"]:
        report.closed(
            "a socket to the host of a virtual machine or WSL system (AF_VSOCK)", _vsock_opens()
        )
    else:
        report.note("this machine has no AF_VSOCK sockets, so that door was not tried")
    report.closed("the kernel keyring", _keyring_answers())
    report.closed("io_uring", _io_uring_answers())
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
        ordinary = True
    except OSError:
        ordinary = False
    report.works("an ordinary socket (the filter lets the rest through)", ordinary)

    report.section("the network")
    listener = facts["listener"]
    if facts["offline"]:
        internet = _connect("1.1.1.1", 443)
        report.closed("the internet (1.1.1.1, port 443)", internet in ("connected", "refused"))
        report.closed(
            "a service on this machine's own loopback",
            _connect("127.0.0.1", listener) == "connected",
        )
        report.closed("this machine's abstract unix sockets", bool(_listening_abstract_sockets()))
    else:
        print("          Without --offline the network is your machine's own. Each line")
        print("          below is something a build can reach from inside.")
        if _connect("127.0.0.1", listener) == "connected":
            report.left_open("services that listen on this machine's own loopback")
        else:
            report.note("the listener made for this test on the loopback was not reached")
        gateway = _gateway()
        if gateway is None:
            report.note("no default gateway was found, so the local network was not tried")
        elif _filtered(gateway):
            report.closed(f"your local network: a filter refuses the gateway {gateway}", False)
        else:
            answers = {_connect(gateway, port, 3.0) for port in (53, 80, 443)}
            if answers & {"connected", "refused"}:
                report.left_open(f"your local network (the gateway {gateway} answered)")
            else:
                report.left_open(
                    f"your local network (no filter refuses the gateway {gateway}; it "
                    "gave no answer on ports 53, 80 and 443)"
                )
        # On a rented machine this address hands the machine's own credentials to
        # whoever asks.
        if not _filtered("169.254.169.254") and _connect("169.254.169.254", 80, 2.0) == "connected":
            report.left_open("a cloud provider's metadata service (169.254.169.254)")
            report.note("It gives out this machine's credentials. Put the address filter")
            report.note("of docs/SANDBOX.md around the sandbox, or use --offline.")
        internet = _connect("api.anthropic.com", 443, 8.0)
        if internet == "connected":
            report.left_open("the internet (api.anthropic.com, port 443)")
        else:
            report.note(f"the internet was not reached from here ({internet})")
        abstract = _listening_abstract_sockets()
        if abstract:
            shown = ", ".join(abstract[:8]) + (" ..." if len(abstract) > 8 else "")
            report.left_open(
                f"{len(abstract)} abstract unix sockets that listen on this machine: {shown}"
            )
            if any("dbus" in name.lower() for name in abstract):
                report.note("One of them looks like a desktop's message bus, which can start")
                report.note("programs outside the sandbox. On this machine use --offline.")
        else:
            report.note("no abstract unix socket is listening on this machine")
    if facts["wsl"]:
        report.note("This is a WSL system. A Windows program file started inside is handed to")
        report.note("WSL's own launcher, which then runs inside the sandbox. Its two known")
        report.note("roads to Windows are closed (the AF_VSOCK socket, above) or not shown")
        report.note("(/run/WSL). That there is no third has not been proven.")

    print()
    print("PROOF PASSED" if report.failed == 0 else f"PROOF FAILED ({report.failed})")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run(json.loads(sys.argv[1])))
