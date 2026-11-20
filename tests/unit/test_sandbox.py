"""Foundry's own sandbox: what it decides, what it tells bubblewrap, what it refuses.

Nothing here starts bubblewrap except the tests marked `bubblewrap` at the end, which
run the proof and one harmless command, and are skipped where bubblewrap cannot start
a sandbox. No test here starts a build.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry import cli, sandbox, session
from foundry.models import BuildState
from foundry.sandbox import seccomp

runner = CliRunner()
KEY = "a-value-that-must-not-show-anywhere"


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A machine with bubblewrap, a home that is not the real one, and Foundry
    installed in a folder of the test's own. Nothing is started."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sandbox, "_bwrap", lambda: ("/usr/bin/bwrap", "0.12.0"))
    monkeypatch.setattr(sandbox, "_install", lambda: ([(str(install), str(install))], []))
    monkeypatch.setattr(sandbox, "_find_claude", lambda: "/opt/real/claude")
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "x86_64")
    environ = {
        "HOME": str(home),
        "PATH": "/usr/bin",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "ANTHROPIC_API_KEY": KEY,
        "FOUNDRY_WORKER_MODEL": "a-model",
        "FOUNDRY_SANDBOX": "named-outside",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "GITHUB_TOKEN": "gh-secret",
        "SSH_AUTH_SOCK": "/run/user/1000/agent",
        "NAMED": "passed-by-name",
    }
    return tmp_path, environ, install


def decided(machine, workspace="work", **options):
    tmp_path, environ, _install = machine
    return sandbox.prepare(
        sandbox.Options(**options), str(tmp_path / workspace), environ=environ
    )


def told(made, cwd=None):
    files = {place: 100 + n for n, place in enumerate(made.files())}
    places = [str(made.workspace), *[place for place, _real in made.shown]]
    places += [f"{sandbox.TOOLS}/{name}" for name, _program in made.tools]
    held = {place: 200 + n for n, place in enumerate(places)}
    return sandbox.arguments(made, cwd or str(made.workspace), files, held)


def shown(made):
    return [place for place, _real in made.shown]


def pairs(args, option, width):
    """Every use of `option` in bubblewrap's arguments, with its `width` values."""
    return [tuple(args[i + 1 : i + 1 + width]) for i, a in enumerate(args) if a == option]


# --- the system-call filter


@pytest.mark.parametrize("name", sorted(seccomp.MACHINES))
def test_the_filter_refuses_what_it_names_and_nothing_else(name):
    m = seccomp.MACHINES[name]
    program = seccomp.assemble(name)

    def verdict(nr, arg0=0, arch=m.audit_arch):
        return seccomp.decide(program, arch=arch, nr=nr, arg0=arg0)

    refused = seccomp.ERRNO
    assert verdict(m.socket, seccomp.AF_VSOCK) == refused | seccomp.EAFNOSUPPORT
    assert verdict(m.socket, seccomp.AF_BLUETOOTH) == refused | seccomp.EAFNOSUPPORT
    # The kernel reads the family as an int: high bits do not open the door.
    assert verdict(m.socket, seccomp.AF_VSOCK | 1 << 32) == refused | seccomp.EAFNOSUPPORT
    for family in (1, 2, 10, 16):  # unix, inet, inet6, netlink
        assert verdict(m.socket, family) == seccomp.ALLOW
    for nr in (m.add_key, m.request_key, m.keyctl):
        assert verdict(nr) == refused | seccomp.EPERM
    for nr in m.io_uring:
        assert verdict(nr) == refused | seccomp.ENOSYS
    for nr in (0, 1, 59, 63, 221, 435):  # ordinary calls on either machine
        if nr not in (m.socket, m.add_key, m.request_key, m.keyctl):
            assert verdict(nr) == seccomp.ALLOW
    # Another calling convention reaches nothing at all.
    assert verdict(m.socket, 2, arch=0x40000003) == refused | seccomp.ENOSYS
    other = next(o for n, o in seccomp.MACHINES.items() if n != name)
    assert verdict(other.socket, seccomp.AF_VSOCK, arch=other.audit_arch) == refused | seccomp.ENOSYS


def test_the_x32_door_is_shut_on_x86_64():
    m = seccomp.MACHINES["x86_64"]
    program = seccomp.assemble("x86_64")
    for nr in (m.socket, m.keyctl, 0):
        verdict = seccomp.decide(program, arch=m.audit_arch, nr=nr | seccomp.X32_BIT, arg0=2)
        assert verdict == seccomp.ERRNO | seccomp.ENOSYS


def test_a_machine_without_a_filter_is_refused(machine, monkeypatch):
    with pytest.raises(seccomp.UnsupportedMachineError):
        seccomp.assemble("riscv64")
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "riscv64")
    with pytest.raises(sandbox.SandboxRefused, match="riscv64"):
        decided(machine)


# --- what the inside's output may do to the operator's terminal


def test_control_and_direction_characters_are_shown_not_obeyed():
    raw = "ok\ttab\n\x1b]0;title\x07\x1b[31mred‮\r\x9b2J"
    assert sandbox.shown_safely(raw) == "ok\ttab\n\\x1b]0;title\\x07\\x1b[31mred\\u202e\\x0d\\x9b2J"


def test_the_relay_and_the_command_line_show_the_same_characters():
    for code in range(0x3000):
        ch = chr(code)
        want = bool(cli._UNSAFE.fullmatch(ch)) and ch not in "\t\n"
        assert bool(sandbox.UNSAFE_OUTPUT.fullmatch(ch)) == want, hex(code)


def test_the_relay_keeps_a_character_whole_across_two_reads():
    read_end, write_end = os.pipe()
    target = io.StringIO()
    with os.fdopen(read_end, "rb", buffering=0) as source:
        relay = threading.Thread(target=sandbox._relay, args=(source, target))
        relay.start()
        data = "é\x1b[2Jend\n".encode()
        os.write(write_end, data[:1])
        os.write(write_end, data[1:])
        os.close(write_end)
        relay.join(timeout=10)
    assert target.getvalue() == "é\\x1b[2Jend\n"


# --- deciding the sandbox: what is refused


def test_a_workspace_that_holds_the_home_is_refused(machine):
    tmp_path, environ, _ = machine
    for given in (tmp_path / "home", tmp_path, Path("/")):
        with pytest.raises(sandbox.SandboxRefused, match="home folder"):
            sandbox.prepare(sandbox.Options(), str(given), environ=environ)
    assert not (tmp_path / "state").exists()  # refused before anything was made


@pytest.mark.parametrize(
    "given", ["/usr/local/out", "/etc/out", "/proc/out", "/bin/out", "/run/user/1000/out"]
)
def test_a_workspace_in_a_system_folder_is_refused(machine, given):
    _tmp, environ, _ = machine
    with pytest.raises(sandbox.SandboxRefused, match="system folder"):
        sandbox.prepare(sandbox.Options(), given, environ=environ)


def test_a_workspace_inside_the_sandboxes_own_folder_is_refused(machine):
    _tmp_path, environ, _ = machine
    root = sandbox.state_root(environ)
    for given in (root / "x" / "home" / "out", root.parent.parent):
        with pytest.raises(sandbox.SandboxRefused, match="inside one another"):
            sandbox.prepare(sandbox.Options(), str(given), environ=environ)


@pytest.mark.parametrize(
    ("options", "says"),
    [
        ({"name": "Has Capitals"}, "--name"),
        ({"name": "../up"}, "--name"),
        ({"memory": "lots"}, "--memory"),
        ({"memory": "0G"}, "--memory"),
        ({"ro": ("/no/such/path",)}, "--ro"),
        ({"ro": ("/run",)}, "leaves that folder out"),
        # In these tests the home is under /tmp, so /tmp is refused for holding it.
        ({"ro": ("/tmp",)}, "leaves that folder out|whole home"),
        ({"ro": ("/var/tmp",)}, "leaves that folder out"),
        ({"ro": ("/etc",)}, "leaves that folder out"),
        ({"path": ("/somewhere/not/shown",)}, "--path"),
        ({"tool": ("/no/such/program",)}, "--tool"),
        ({"env": ("NOT_SET_HERE",)}, "--env"),
        ({"env": ("bad name",)}, "--env"),
        ({"env": ("PATH",)}, "set by the sandbox"),
        ({"dns": ("not-an-address",)}, "--dns"),
    ],
)
def test_a_wrong_option_is_refused_and_named(machine, options, says):
    with pytest.raises(sandbox.SandboxRefused, match=says):
        decided(machine, **options)


def test_showing_the_whole_home_is_refused(machine):
    tmp_path, _environ, _ = machine
    (tmp_path / "link-to-home").symlink_to(tmp_path / "home")
    for given in ("/", str(tmp_path), str(tmp_path / "home")):
        with pytest.raises(sandbox.SandboxRefused, match="whole home"):
            decided(machine, ro=(given,))
    with pytest.raises(sandbox.SandboxRefused, match="through a link"):
        decided(machine, ro=(str(tmp_path / "link-to-home"),))
    # Neither are the folders the keys are kept in, nor anything in or around them.
    (tmp_path / "home" / ".config" / "gh").mkdir(parents=True)
    for given in (".ssh", ".config", ".config/gh"):
        with pytest.raises(sandbox.SandboxRefused, match="keys and sign-ins"):
            decided(machine, ro=(str(tmp_path / "home" / given),))
    # One ordinary folder inside the home is the operator's to show.
    (tmp_path / "home" / "library").mkdir()
    assert str(tmp_path / "home" / "library") in shown(
        decided(machine, ro=(str(tmp_path / "home" / "library"),))
    )


def test_a_folder_that_holds_a_socket_is_not_shown(machine):
    """Read-only does not keep the inside from connecting to a socket."""
    import socket

    tmp_path, _environ, _ = machine
    folder = tmp_path / "agent"
    (folder / "deep").mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(folder / "deep" / "s"))
        with pytest.raises(sandbox.SandboxRefused, match="is a socket"):
            decided(machine, ro=(str(folder),))
    finally:
        listener.close()
    (folder / "deep" / "s").unlink()
    assert str(folder) in shown(decided(machine, ro=(str(folder),)))


def test_build_does_not_show_a_home_named_as_a_skill_library(started):
    tmp_path, calls = started
    result = runner.invoke(cli.app, ["sandbox", "build", "b", "-w", "out", "--skills", str(tmp_path / "home")])
    assert result.exit_code == 2 and "whole home" in result.output
    assert calls["launch"] == []


def test_root_is_refused(machine, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(sandbox.SandboxRefused, match="root"):
        decided(machine)


def test_a_build_needs_claude_code_inside(machine, monkeypatch):
    tmp_path, environ, _ = machine
    monkeypatch.setattr(sandbox, "_find_claude", lambda: None)
    with pytest.raises(sandbox.SandboxRefused, match="Claude Code"):
        sandbox.prepare(sandbox.Options(), str(tmp_path / "w"), environ=environ, need_claude=True)
    # A plain command does not need it.
    assert sandbox.prepare(sandbox.Options(), str(tmp_path / "w"), environ=environ).tools == ()


def test_a_home_or_tmp_that_is_a_link_is_refused(machine):
    tmp_path, environ, _ = machine
    state = sandbox.state_root(environ) / "linked"
    state.mkdir(parents=True)
    (state / "home").symlink_to(tmp_path / "home")
    with pytest.raises(sandbox.SandboxRefused, match="is a link"):
        decided(machine, name="linked")


def test_an_env_file_others_can_read_is_refused(machine):
    tmp_path, _environ, _ = machine
    file = tmp_path / "keys.env"
    file.write_text("ANTHROPIC_API_KEY=from-the-file\n")
    file.chmod(0o644)
    with pytest.raises(sandbox.SandboxRefused, match="chmod 600"):
        decided(machine, env_file=str(file))
    file.chmod(0o600)
    file.write_text("this is not a setting\n")
    with pytest.raises(sandbox.SandboxRefused, match="line 1"):
        decided(machine, env_file=str(file))


# --- the second seat's findings, each with the case that must stay refused


def test_the_operators_environment_never_comes_near_bubblewrap(machine, monkeypatch):
    """bubblewrap's first process is pid 1 inside, and what it was started with can
    be read there. It is started with nothing."""
    monkeypatch.setenv("GH_TOKEN", "must-not-arrive")
    assert sandbox._start_environment(decided(machine)) == {}
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    with_ceiling = decided(machine, memory="1G")
    assert sorted(sandbox._start_environment(with_ceiling)) == [
        "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"
    ]  # fmt: skip


def test_a_link_left_in_a_workspace_is_not_followed(machine):
    """Run 1 leaves `app -> somewhere outside` in its workspace; run 2 is asked to
    work in `app`. Nothing is made at the link's end and nothing is started."""
    tmp_path, environ, _ = machine
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"  # not there yet: it must not be made either
    (work / "app").symlink_to(outside)
    for given in (work / "app", work / "app" / "deeper"):
        with pytest.raises(sandbox.SandboxRefused, match="a link that leads nowhere"):
            sandbox.prepare(sandbox.Options(), str(given), environ=environ)
    assert not outside.exists()
    outside.mkdir()
    (outside / "tool").write_text("#!/bin/sh\n")
    (outside / "tool").chmod(0o755)
    (outside / "keys.env").write_text("A=1\n")
    (outside / "keys.env").chmod(0o600)
    for given in (work / "app", work / "app" / "deeper", work / "app" / ".." / "work"):
        with pytest.raises(sandbox.SandboxRefused, match="through a link") as refusal:
            sandbox.prepare(sandbox.Options(), str(given), environ=environ)
        assert str(outside) in str(refusal.value) or str(tmp_path) in str(refusal.value)
    assert sorted(entry.name for entry in outside.iterdir()) == ["keys.env", "tool"]
    for options in (
        {"ro": (str(work / "app"),)},
        {"tool": (str(work / "app" / "tool"),)},
        {"env_file": str(work / "app" / "keys.env")},
    ):
        with pytest.raises(sandbox.SandboxRefused, match="through a link"):
            decided(machine, workspace="other", **options)
    with pytest.raises(sandbox.SandboxRefused, match="through a link"):
        decided(machine, workspace="other", ro=(str(outside),), path=(str(work / "app"),))


def test_a_link_inside_a_shown_folder_does_not_put_a_writable_folder_on_the_path(machine):
    """The third pass: a folder shown read-only holds `bin -> /home/foundry/plant`.
    Read as written it lies in a shown folder; inside, it leads to the sandbox's own
    home, which a build can write."""
    tmp_path, _environ, _ = machine
    left = tmp_path / "left-by-a-build"
    left.mkdir()
    (left / "bin").symlink_to("/home/foundry/plant")
    for given in (left / "bin", left / "bin" / "deeper"):
        with pytest.raises(sandbox.SandboxRefused, match="--path"):
            decided(machine, ro=(str(left),), path=(str(given),))
    (left / "real-bin").mkdir()
    made = decided(machine, ro=(str(left),), path=(str(left / "real-bin"),))
    assert str(left / "real-bin") in made.env["PATH"].split(":")


def test_two_dots_after_a_link_do_not_get_past_it(machine):
    """`app/..` read as text is the workspace; the kernel goes through the link
    first. The two must agree, or the command outside and the build inside would
    mean different folders."""
    tmp_path, environ, _ = machine
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "far" / "away").mkdir(parents=True)
    (work / "app").symlink_to(tmp_path / "far" / "away")
    with pytest.raises(sandbox.SandboxRefused, match="through a link"):
        sandbox.prepare(sandbox.Options(), str(work / "app" / ".."), environ=environ)
    # Two dots after a real folder are only another way to write the path.
    (work / "real").mkdir()
    made = sandbox.prepare(sandbox.Options(), str(work / "real" / ".."), environ=environ)
    assert made.workspace == work


def test_any_link_on_the_way_is_refused_and_the_real_path_is_named(machine):
    """A system's own link (a /home that points to /var/home) is refused as well:
    the rule has no exceptions to get wrong, and the refusal says what to type."""
    tmp_path, environ, _ = machine
    (tmp_path / "var-home" / "out").mkdir(parents=True)
    (tmp_path / "homes").symlink_to(tmp_path / "var-home")
    with pytest.raises(sandbox.SandboxRefused) as refusal:
        sandbox.prepare(sandbox.Options(), str(tmp_path / "homes" / "out"), environ=environ)
    assert f"Name {tmp_path / 'var-home' / 'out'} itself" in str(refusal.value)
    made = sandbox.prepare(sandbox.Options(), str(tmp_path / "var-home" / "out"), environ=environ)
    assert made.workspace == tmp_path / "var-home" / "out"


def test_a_folder_swapped_for_a_link_while_it_is_checked_never_wins(machine):
    """The second seat's race: a build still running swaps `sub` (a real folder) with
    a link to a folder outside, over and over, while a second command is asked to
    work in `sub`. Whatever the checks return and whatever is then opened for
    bubblewrap must be the real folder, never the one outside."""
    import ctypes
    import threading

    tmp_path, environ, _ = machine
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        pytest.skip("no renameat2 in this C library")
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (work / "alt").symlink_to(target)
    inside, outside = os.stat(work / "sub").st_ino, os.stat(target).st_ino
    stop = threading.Event()
    sub, alt = os.fsencode(work / "sub"), os.fsencode(work / "alt")

    def swap():
        while not stop.is_set():
            libc.renameat2(-100, sub, -100, alt, 2)  # AT_FDCWD, RENAME_EXCHANGE

    swapper = threading.Thread(target=swap)
    swapper.start()
    held, refused = 0, 0
    try:
        for _ in range(4000):
            try:
                root = sandbox.state_root(environ)
                made = sandbox._workspace(str(work / "sub"), root, [], environ)
                descriptor = sandbox._hold(str(made), str(made))
            except sandbox.SandboxRefused:
                refused += 1
                continue
            try:
                assert made == work / "sub"
                assert os.fstat(descriptor).st_ino == inside != outside
                held += 1
            finally:
                os.close(descriptor)
    finally:
        stop.set()
        swapper.join()
    assert held + refused == 4000 and refused > 0, (held, refused)
    assert list(target.iterdir()) == []


def test_a_workspace_does_not_share_a_folder_with_programs_run_outside(machine, monkeypatch):
    """A build that can write a folder on the operator's PATH can put its own program
    in the place of one that is started outside later."""
    tmp_path, environ, _ = machine
    tools = tmp_path / "local" / "bin"
    tools.mkdir(parents=True)
    environ = {**environ, "PATH": f"{tools}:relative/bin::/usr/bin"}
    for given in (tools, tools / "x", tmp_path / "local"):
        with pytest.raises(sandbox.SandboxRefused, match="programs you run outside"):
            sandbox.prepare(sandbox.Options(), str(given), environ=environ)
    assert sandbox.prepare(sandbox.Options(), str(tmp_path / "work"), environ=environ).workspace
    # Nor with the folder the session's own sockets are in.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with pytest.raises(sandbox.SandboxRefused, match="system folder"):
        sandbox.prepare(
            sandbox.Options(), str(runtime / "x"), environ={**environ, "XDG_RUNTIME_DIR": str(runtime)}
        )


def test_the_sandbox_is_started_from_outside_the_workspace(machine, monkeypatch):
    """Python and uv read the folder a command is started in before Foundry has a
    say, and a build can write its workspace."""
    tmp_path, environ, _ = machine
    work = tmp_path / "work"
    (work / "deeper").mkdir(parents=True)
    for standing in (work, work / "deeper"):
        monkeypatch.chdir(standing)
        for given in (str(work), ".", ".." if standing != work else "."):
            with pytest.raises(sandbox.SandboxRefused, match="standing inside the workspace"):
                sandbox.prepare(sandbox.Options(), given, environ=environ)
    monkeypatch.chdir(tmp_path)
    assert sandbox.prepare(sandbox.Options(), "work", environ=environ).workspace == work


def test_a_workspace_and_foundry_itself_do_not_share_a_folder(machine, monkeypatch):
    """What a build can write, the command outside must never run."""
    tmp_path, environ, install = machine
    for given in (tmp_path, install, install / "out", install.parent / "install" / "x" / "y"):
        if given == tmp_path:
            continue  # refused already, for holding the home
        with pytest.raises(sandbox.SandboxRefused, match="Foundry's own installation"):
            sandbox.prepare(sandbox.Options(), str(given), environ=environ)
    repo = tmp_path / "repo"
    (repo / "src" / "foundry").mkdir(parents=True)
    monkeypatch.setattr(
        sandbox, "_install", lambda: ([(str(repo / "src" / "foundry"),) * 2], [])
    )
    with pytest.raises(sandbox.SandboxRefused, match="Foundry's own installation"):
        sandbox.prepare(sandbox.Options(), str(repo), environ=environ)
    assert sandbox.prepare(sandbox.Options(), str(repo / "out"), environ=environ).workspace


def test_a_folder_changed_after_the_checks_is_refused(tmp_path):
    real = tmp_path / "work"
    real.mkdir()
    descriptor = sandbox._hold(str(real), str(real))
    assert os.readlink(f"/proc/self/fd/{descriptor}") == str(real)
    os.close(descriptor)
    real.rmdir()
    (tmp_path / "elsewhere").mkdir()
    real.symlink_to(tmp_path / "elsewhere")  # put in the way after the checks
    with pytest.raises(sandbox.SandboxRefused, match="when it was checked"):
        sandbox._hold(str(real), str(real))


@pytest.mark.parametrize("inside_home", [".ssh", ".ssh/keys", ".config", ".claude"])
def test_a_workspace_among_the_keys_is_refused(machine, inside_home):
    tmp_path, environ, _ = machine
    with pytest.raises(sandbox.SandboxRefused, match="keys and sign-ins"):
        sandbox.prepare(sandbox.Options(), str(tmp_path / "home" / inside_home), environ=environ)
    assert sorted(e.name for e in (tmp_path / "home").iterdir()) == [".ssh"]  # nothing made
    assert list((tmp_path / "home" / ".ssh").iterdir()) == []


def test_each_probe_of_the_proof_says_open_where_the_door_is_open(tmp_path, monkeypatch):
    """The controls. A check that can only say "closed" proves nothing, so each
    probe is run here, outside the sandbox, where its door stands open."""
    from foundry.sandbox import proof

    file = tmp_path / "yours"
    file.write_text("x")
    info = os.lstat(file)
    assert proof._same_file(str(file), info.st_dev, info.st_ino)
    assert not proof._same_file(str(file), info.st_dev, info.st_ino + 1)
    assert not proof._same_file(str(tmp_path / "absent"), info.st_dev, info.st_ino)
    assert proof._reads(str(file)) and not proof._reads(str(tmp_path / "absent"))
    assert proof._writable_on(f"/usr/bin:{tmp_path}::/nowhere") == [str(tmp_path)]
    assert proof._can_change(str(tmp_path)) and proof._can_change(str(file))
    assert not proof._can_change("/usr") or os.getuid() == 0
    # Nothing was left behind by the probes.
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["yours"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux system calls")
def test_the_system_call_probes_say_open_outside_the_sandbox():
    """Run in a child, because asking for a user namespace changes the one who asks."""
    code = (
        "from foundry.sandbox import proof\n"
        "print(proof._keyring_answers(), proof._io_uring_answers(), proof._vsock_opens(),"
        " proof._userns_opens())\n"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", code], capture_output=True, text=True, timeout=60
    )
    keyring, ring, vsock, userns = done.stdout.split()
    if not _bubblewrap_starts():
        pytest.skip("this machine may itself filter these calls (a container, a CI runner)")
    assert (keyring, ring, userns) == ("True", "True", "True"), done.stdout + done.stderr
    assert vsock in ("True", "False")  # open only where the machine has the family


def test_the_proof_sees_a_variable_a_process_was_started_with():
    """The control for the proof's own check: where the door is open, it says so."""
    from foundry.sandbox import proof

    child = subprocess.Popen(["/bin/sleep", "30"], env={"BAIT_FOR_THE_CONTROL": "1"})
    try:
        assert str(child.pid) in proof._environments_holding("BAIT_FOR_THE_CONTROL")
        assert proof._environments_holding("BAIT_NOBODY_HAS") == []
    finally:
        child.kill()
        child.wait()


# --- deciding the sandbox: what the inside is given


def test_the_environment_is_built_from_nothing(machine):
    made = decided(machine)
    env = made.env
    assert env["HOME"] == sandbox.HOME and env["USER"] == "foundry"
    assert env["FOUNDRY_SANDBOX"] == "bubblewrap"  # its own name, not the one set outside
    assert env["ANTHROPIC_API_KEY"] == KEY and env["FOUNDRY_WORKER_MODEL"] == "a-model"
    assert env["PATH"] == "/opt/foundry/bin:/usr/local/bin:/usr/bin:/bin"
    for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "NAMED", "XDG_STATE_HOME"):
        assert name not in env
    assert made.passed == ("ANTHROPIC_API_KEY", "FOUNDRY_WORKER_MODEL")


def test_a_variable_is_passed_when_named_and_a_file_wins(machine):
    tmp_path, _environ, _ = machine
    file = tmp_path / "keys.env"
    file.write_text("# a comment\n\nexport ANTHROPIC_API_KEY='from-the-file'\nOTHER = \"two words\"\n")
    file.chmod(0o600)
    made = decided(machine, env=("NAMED",), env_file=str(file))
    assert made.env["NAMED"] == "passed-by-name"
    assert made.env["ANTHROPIC_API_KEY"] == "from-the-file"
    assert made.env["OTHER"] == "two words"
    assert made.passed == ("ANTHROPIC_API_KEY", "FOUNDRY_WORKER_MODEL", "NAMED", "OTHER")


def test_the_same_workspace_gets_the_same_name(machine):
    tmp_path, _environ, _ = machine
    first, again, other = decided(machine), decided(machine), decided(machine, workspace="Other One")
    assert first.name == again.name != other.name
    assert first.name.startswith("work-") and other.name.startswith("other-one-")
    assert first.home == first.state / "home" and first.home.is_dir() and first.tmp.is_dir()
    assert first.state.parent == tmp_path / "state" / "foundry" / "sandbox"


def test_the_virtual_environment_is_last_on_the_path_and_four_names_lead_to_it(machine):
    made = decided(machine, venv=True)
    path = made.env["PATH"].split(":")
    # The build can write its own environment, so nothing in it comes before git.
    assert path[0] == "/opt/foundry/bin" and path[-1] == "/home/foundry/venv/bin"
    assert made.env["VIRTUAL_ENV"] == "/home/foundry/venv"
    files = made.files()
    assert files["/opt/foundry/bin/python3"] == b'#!/bin/sh\nexec /home/foundry/venv/bin/python "$@"\n'
    # pip is the environment's own program, not `python -m pip`, which would put the
    # working folder on Python's path.
    assert files["/opt/foundry/bin/pip3"] == b'#!/bin/sh\nexec /home/foundry/venv/bin/pip "$@"\n'
    args = told(made)
    at = args.index("/opt/foundry/bin/python")
    assert args[at - 4 : at - 1] == ["--perms", "0555", "--ro-bind-data"]
    plain = decided(machine)
    assert "VIRTUAL_ENV" not in plain.env and "/opt/foundry/bin/python" not in plain.files()


def test_nothing_the_inside_can_write_goes_on_its_path(machine):
    tmp_path, _environ, _ = machine
    work = tmp_path / "work"
    (work / "bin").mkdir(parents=True)
    with pytest.raises(sandbox.SandboxRefused, match="can write there"):
        decided(machine, ro=(str(tmp_path / "work"),), path=(str(work / "bin"),))
    # Shown read-only for this run, it is still what an earlier build left there.
    with pytest.raises(sandbox.SandboxRefused, match="or has written it"):
        decided(machine, ro=(str(work / "bin"),), path=(str(work / "bin"),))


def test_the_resolver_file_names_what_was_asked(machine):
    assert decided(machine, dns=("1.1.1.1", "2606:4700:4700::1111")).resolv == (
        "nameserver 1.1.1.1\nnameserver 2606:4700:4700::1111\n"
    )
    for line in decided(machine).resolv.splitlines():
        assert line.split()[0] in ("nameserver", "options")  # no search list


def test_the_files_written_for_the_inside_name_one_user(machine):
    made = decided(machine)
    files = made.files()
    at = told(made).index("/etc/passwd")
    assert told(made)[at - 4 : at - 1] == ["--perms", "0444", "--ro-bind-data"]
    assert set(files) == {
        "/etc/passwd", "/etc/group", "/etc/hosts", "/etc/nsswitch.conf", "/etc/resolv.conf"
    }  # fmt: skip
    assert f"foundry:x:{os.getuid()}:{os.getgid()}:".encode() in files["/etc/passwd"]
    # Nothing of the machine outside is in them: no login of its own, no host name.
    logins = [line.split(":")[0] for line in files["/etc/passwd"].decode().splitlines()]
    assert logins == ["root", "foundry", "nobody"]
    hosts = files["/etc/hosts"].decode().split()
    names = {word for word in hosts if word not in ("127.0.0.1", "::1")}
    assert names == {"localhost", "foundry-sandbox", "ip6-localhost", "ip6-loopback"}


def test_the_links_on_the_way_to_a_program_are_found(tmp_path):
    real = tmp_path / "pythons" / "cpython-3.13.1"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.13").write_text("")
    (tmp_path / "pythons" / "cpython-3.13").symlink_to("cpython-3.13.1")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    link = tmp_path / "venv" / "bin" / "python"
    link.symlink_to(tmp_path / "pythons" / "cpython-3.13" / "bin" / "python3.13")
    found = [pair for pair in sandbox._links_on_the_way(str(link)) if str(tmp_path) in pair[0]]
    assert found == [
        (str(link), str(tmp_path / "pythons" / "cpython-3.13" / "bin" / "python3.13")),
        (str(tmp_path / "pythons" / "cpython-3.13"), "cpython-3.13.1"),
    ]


# --- what bubblewrap is told


def test_the_home_is_never_shown_and_the_workspace_is_the_one_writable_folder(machine):
    tmp_path, _environ, install = machine
    made = decided(machine)
    args = told(made)
    # By path, only the sandbox's own home and /tmp are writable; by descriptor
    # (opened and checked by `_hold`), only the workspace.
    assert sorted(pairs(args, "--bind", 2)) == sorted(
        [(str(made.tmp), "/tmp"), (str(made.tmp), "/var/tmp"), (str(made.home), "/home/foundry")]
    )
    assert pairs(args, "--bind-fd", 2) == [("200", str(made.workspace))]
    assert sorted(place for _fd, place in pairs(args, "--ro-bind-fd", 2)) == sorted(
        [str(install), "/opt/foundry/bin/claude"]
    )
    sources = [source for source, _place in pairs(args, "--bind", 2) + pairs(args, "--ro-bind", 2)]
    home = str(tmp_path / "home")
    assert not any(source == home or home.startswith(source + "/") for source in sources)
    assert not any(source.startswith(home + "/") for source in sources)
    assert "/" not in sources and "/etc" not in sources and "/home" not in sources
    # The list of the operator's keyrings is covered.
    assert ("/dev/null", "/proc/keys") in pairs(args, "--ro-bind", 2)


def test_the_order_closes_what_comes_before(machine):
    args = told(decided(machine))
    assert args.index("--clearenv") < args.index("--setenv")
    assert "--unshare-net" not in args
    for flag in ("--unshare-user", "--unshare-pid", "--new-session", "--disable-userns", "--die-with-parent"):
        assert flag in args
    # The root is made read-only after the last thing is put on it.
    remount = max(i for i, a in enumerate(args) if a == "--remount-ro" and args[i + 1] == "/")
    mounts = (
        "--bind", "--ro-bind", "--bind-fd", "--ro-bind-fd", "--ro-bind-data", "--dir",
        "--symlink", "--tmpfs", "--proc", "--dev",
    )  # fmt: skip
    last_mount = max(i for i, a in enumerate(args) if a in mounts)
    assert last_mount < remount < args.index("--chdir")
    assert "--unshare-net" in told(decided(machine, offline=True))


def test_the_written_files_come_from_descriptors_and_only_a_short_etc_is_shown(machine):
    args = told(decided(machine))
    assert ("100", "/etc/passwd") in pairs(args, "--ro-bind-data", 2)
    assert len(pairs(args, "--ro-bind-data", 2)) == 5
    etc = [place for _source, place in pairs(args, "--ro-bind", 2) if place.startswith("/etc")]
    assert etc and set(etc) <= set(sandbox.ETC_SHOWN)
    for secret in ("shadow", "sudoers", "ssh", "fstab", "environment", "gitconfig", "hosts"):
        assert f"/etc/{secret}" not in sandbox.ETC_SHOWN


def test_a_path_inside_another_lands_on_top_of_it(machine):
    tmp_path, environ, _install = machine
    # A workspace under a folder shown read-only stays writable: it is bound after it.
    data = tmp_path / "data"
    (data / "out").mkdir(parents=True)
    made = sandbox.prepare(sandbox.Options(ro=(str(data),)), str(data / "out"), environ=environ)
    args = told(made)
    assert args[args.index(str(data)) - 2] == "--ro-bind-fd"
    assert args[args.index(str(data / "out")) - 2] == "--bind-fd"
    assert args.index(str(data)) < args.index(str(data / "out"))
    # And a folder shown read-only inside the workspace stays read-only.
    (tmp_path / "work" / "vendor").mkdir(parents=True)
    made = decided(machine, ro=(str(tmp_path / "work" / "vendor"),))
    args = told(made)
    assert args.index(str(made.workspace)) < args.index(str(tmp_path / "work" / "vendor"))
    assert args[args.index(str(tmp_path / "work" / "vendor")) - 2] == "--ro-bind-fd"


def test_the_working_folder_is_made_inside_when_nothing_covers_it(machine):
    made = decided(machine)
    outside = "/somewhere/else"
    assert ("--dir", outside) == tuple(told(made, outside)[-6:-4])
    assert told(made, outside)[-2:] == ["--chdir", outside]
    inside = str(made.workspace / "sub")
    assert inside not in pairs(told(made, inside), "--dir", 1)[-1]


def test_nothing_passed_in_shows_on_the_command_line(machine, monkeypatch):
    monkeypatch.setattr(sandbox, "_find_claude", lambda: None)  # no program to open here
    made = decided(machine)
    line, descriptors = sandbox._command(made, ["/bin/true", "a brief"], str(made.workspace))
    try:
        assert line[0] == "/usr/bin/bwrap" and line[1] == "--args" and line[3] == "--"
        assert line[4:] == ["/bin/true", "a brief"]
        assert KEY not in " ".join(line) and str(made.home) not in " ".join(line)
        told_by_descriptor = os.pread(int(line[2]), 1 << 20, 0).split(b"\0")
        assert KEY.encode() in told_by_descriptor and b"--seccomp" in told_by_descriptor
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_a_memory_ceiling_goes_through_a_user_scope(machine, monkeypatch):
    monkeypatch.setattr(sandbox, "_system_program", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox, "_systemd_version", lambda _runner: 257)
    prefix = decided(machine, memory="3G").prefix()
    assert prefix[:3] == ["/usr/bin/systemd-run", "--user", "--scope"]
    # What systemd-run needed of the environment is dropped again before bubblewrap.
    assert prefix[-3:] == ["--", "/usr/bin/env", "-i"]
    assert "MemoryMax=3G" in prefix and "OOMPolicy=continue" in prefix
    monkeypatch.setattr(sandbox, "_systemd_version", lambda _runner: 252)
    assert "OOMPolicy=continue" not in decided(machine, memory="3G").prefix()
    assert decided(machine).prefix() == []
    monkeypatch.setattr(sandbox, "_system_program", lambda name: None)
    with pytest.raises(sandbox.SandboxRefused, match="systemd"):
        decided(machine, memory="3G").prefix()


def test_the_programs_the_command_runs_are_the_systems_own(tmp_path, monkeypatch):
    """The third pass: a `bwrap` in a folder that comes first on the PATH was run
    outside the sandbox. The PATH is not looked at, and a program that is not
    root's, or that others can change, is not taken."""
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert shutil.which("bwrap") == str(fake)  # the PATH would give the fake
    found = sandbox._system_program("bwrap")
    assert found is None or os.path.dirname(found) in sandbox.SYSTEM_PROGRAMS
    monkeypatch.setattr(sandbox, "SYSTEM_PROGRAMS", (str(tmp_path),))
    assert sandbox._system_program("bwrap") is None  # ours, not root's
    with pytest.raises(sandbox.SandboxRefused, match="of the system's own"):
        sandbox._bwrap()
    assert sandbox._system_program("no-such-program") is None


def test_an_end_by_a_signal_is_said_and_answered_as_a_shell_would(machine, monkeypatch, capsys):
    class Ended:
        returncode = -15
        stdin = None

        def __init__(self, *_a, **_k):
            self.stdout = open(os.devnull, "rb")  # noqa: SIM115
            self.stderr = open(os.devnull, "rb")  # noqa: SIM115

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sandbox.subprocess, "Popen", Ended)
    monkeypatch.setattr(sandbox, "_find_claude", lambda: None)
    assert _real_launch(decided(machine), ["/bin/true"]) == 143
    assert "stopped by signal 15" in capsys.readouterr().err


# The function itself, kept from before the guard in conftest replaces it. The one
# test that calls it hands it a process that never starts.
_real_launch = sandbox.launch


# --- the session file


def test_a_session_file_has_to_be_inside_the_workspace(tmp_path):
    work = tmp_path / "out"
    work.mkdir()
    sandbox.require_session_inside(str(work), str(work / ".foundry" / "session.json"))
    for outside in (tmp_path / "run.json", work, work / ".." / "run.json"):
        with pytest.raises(sandbox.SandboxRefused, match=r"\.foundry"):
            sandbox.require_session_inside(str(work), str(outside))
    # A link is not looked at here: the build opens the file itself, inside, where
    # a link that leads out of the workspace leads nowhere.
    (work / "elsewhere").symlink_to(tmp_path)
    sandbox.require_session_inside(str(work), str(work / "elsewhere" / "run.json"))


def test_saving_a_session_makes_its_folder(tmp_path):
    place = tmp_path / "out" / ".foundry" / "session.json"
    session.save(place, BuildState(brief="b", workspace=str(tmp_path / "out")))
    assert session.load(place).brief == "b"


# --- the commands


@pytest.fixture
def started(machine, monkeypatch):
    """The command line with a machine as above, and the three steps that would
    start bubblewrap recorded and never run."""
    tmp_path, environ, _ = machine
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    calls: dict[str, list] = {"check": [], "launch": [], "venv": []}
    monkeypatch.setattr(sandbox, "check", lambda made, tools=(), cwd="": calls["check"].append((made, tools, cwd)))
    monkeypatch.setattr(sandbox, "make_venv", lambda made: calls["venv"].append(made))

    def launch(made, command, cwd=None, feed=False):
        calls["launch"].append((made, list(command), cwd, feed))
        return 1

    monkeypatch.setattr(sandbox, "launch", launch)
    return tmp_path, calls


def test_build_runs_the_real_command_inside_with_its_own_words(started):
    tmp_path, calls = started
    given = ["a brief", "-w", "./out", "-i", "-g", "--session", "--session", "./out/.foundry/s.json"]
    result = runner.invoke(cli.app, ["sandbox", "--venv", "build", *given])
    assert result.exit_code == 1  # what the build inside answered
    (made, command, cwd, feed), = calls["launch"]
    assert command == [sys.executable, "-P", "-m", "foundry.cli", "build", *given]
    assert cwd == str(tmp_path) and feed is True
    assert made.workspace == tmp_path / "out" and made.env["ANTHROPIC_API_KEY"] == KEY
    assert calls["check"][0][1] == ("sh", "git", "claude") and len(calls["venv"]) == 1
    assert "passed in   ANTHROPIC_API_KEY" in result.output and KEY not in result.output


def test_build_shows_a_skill_library_read_only(started):
    tmp_path, calls = started
    (tmp_path / "skills").mkdir()
    runner.invoke(cli.app, ["sandbox", "build", "b", "-w", "out", "--skills", "skills"])
    assert str(tmp_path / "skills") in shown(calls["launch"][0][0])


@pytest.mark.parametrize(
    "given",
    [
        ["a brief"],  # no workspace: the build's own parser says so
        ["a brief", "-w", "out", "--session", "run.json"],  # a session outside the workspace
        ["a brief", "-w", "out", "--no-such-option"],
    ],
)
def test_build_is_refused_before_anything_starts(started, given):
    tmp_path, calls = started
    result = runner.invoke(cli.app, ["sandbox", "build", *given])
    assert result.exit_code == 2
    assert calls == {"check": [], "launch": [], "venv": []}
    assert not (tmp_path / "state").exists()


def test_build_without_an_api_sign_in_is_refused(started, monkeypatch):
    _tmp_path, calls = started
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    result = runner.invoke(cli.app, ["sandbox", "build", "a brief", "-w", "out"])
    assert result.exit_code == 2 and "API sign-in" in result.output
    assert calls["launch"] == [] and calls["check"] == []


def test_a_refusal_of_the_sandbox_is_exit_2_and_nothing_runs(started, monkeypatch):
    _tmp_path, calls = started

    def refuse(*_a, **_k):
        raise sandbox.SandboxRefused("bubblewrap could not start here. Nothing was started.")

    monkeypatch.setattr(sandbox, "check", refuse)
    for words in (["build", "b", "-w", "out"], ["run", "-w", "out", "--", "true"]):
        result = runner.invoke(cli.app, ["sandbox", *words])
        assert result.exit_code == 2 and "could not start here" in result.output
    assert calls["launch"] == []


def test_a_refusal_at_the_last_step_is_exit_2_not_a_traceback(started, monkeypatch):
    """The third pass: a folder swapped after the checks was caught when the sandbox
    started, and came out as a traceback with exit 1, which for a build also means
    "not finished"."""
    _tmp_path, _calls = started
    for error in (
        sandbox.SandboxRefused("out was /x when it was checked and is /y now. Nothing was started."),
        FileNotFoundError(2, "No such file or directory"),
    ):
        def refuse(*_a, _error=error, **_k):
            raise _error

        monkeypatch.setattr(sandbox, "launch", refuse)
        for words in (["build", "b", "-w", "out"], ["run", "-w", "out", "--", "true"]):
            result = runner.invoke(cli.app, ["sandbox", *words])
            assert result.exit_code == 2, result.output
            assert "Nothing was started" in result.output and "Traceback" not in result.output


def test_run_gives_the_command_as_it_is_and_needs_one(started):
    _tmp_path, calls = started
    result = runner.invoke(cli.app, ["sandbox", "--offline", "run", "-w", "out", "--", "git", "log", "-w"])
    assert result.exit_code == 1
    (made, command, cwd, _feed), = calls["launch"]
    assert command == ["git", "log", "-w"] and cwd is None and made.offline
    assert "none (--offline)" in result.output
    assert runner.invoke(cli.app, ["sandbox", "run", "-w", "out"]).exit_code == 2


# --- the real thing: the proof and one harmless command, where bubblewrap can start


def _bubblewrap_starts() -> bool:
    program = shutil.which("bwrap")
    if program is None or sys.platform != "linux" or os.getuid() == 0:
        return False
    try:
        tried = subprocess.run(
            [program, "--unshare-user", "--disable-userns", "--ro-bind", "/", "/", "/bin/true"],
            capture_output=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return tried.returncode == 0


needs_bubblewrap = pytest.mark.skipif(
    not _bubblewrap_starts(), reason="bubblewrap cannot start a sandbox on this machine"
)


@needs_bubblewrap
@pytest.mark.bubblewrap
@pytest.mark.parametrize("offline", [False, True])
def test_the_proof_passes_on_this_machine(tmp_path, monkeypatch, capfd, offline):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    code = sandbox.prove(sandbox.Options(offline=offline))
    out = capfd.readouterr().out
    assert "REACHED" not in out and "MISSING" not in out, out
    assert code == 0 and "PROOF PASSED" in out
    assert out.count("closed   ") > 25
    # What the proof made for itself is gone again.
    assert list((tmp_path / "state" / "foundry" / "sandbox").iterdir()) == []


@needs_bubblewrap
@pytest.mark.bubblewrap
def test_the_proof_fails_when_the_environment_is_handed_on(tmp_path, monkeypatch, capfd):
    """The fault the second seat found, put back on purpose: bubblewrap started with
    the operator's environment. The proof has to say REACHED, and fail."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sandbox, "_start_environment", lambda _sandbox: dict(os.environ))
    code = sandbox.prove(sandbox.Options(offline=True))
    out = capfd.readouterr().out
    assert code == 1 and "PROOF FAILED" in out
    assert "REACHED   a variable set as bait in your environment" in out


@needs_bubblewrap
@pytest.mark.bubblewrap
def test_the_proof_fails_when_the_memory_ceiling_leaves_its_two_variables(
    tmp_path, monkeypatch, capfd
):
    """With --memory, systemd-run is handed two variables, and `env -i` drops them
    before bubblewrap. With that step left out the bait is still absent, so the
    proof has to notice that the first process was started with anything at all."""
    if "XDG_RUNTIME_DIR" not in os.environ or shutil.which("systemd-run") is None:
        pytest.skip("no systemd user session here")
    tried = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "/bin/true"],
        capture_output=True, timeout=60, check=False,
    )  # fmt: skip
    if tried.returncode != 0:
        pytest.skip("no systemd user scope can be made here")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    whole = sandbox.Sandbox.prefix
    monkeypatch.setattr(sandbox.Sandbox, "prefix", lambda self: whole(self)[:-2])
    code = sandbox.prove(sandbox.Options(offline=True, memory="600M"))
    out = capfd.readouterr().out
    assert code == 1, out
    assert "REACHED   anything at all in the environment the sandbox's first process" in out
    assert "XDG_RUNTIME_DIR" in out


@needs_bubblewrap
@pytest.mark.bubblewrap
def test_the_proof_passes_with_a_virtual_environment_and_says_what_it_leaves(
    tmp_path, monkeypatch, capfd
):
    if shutil.which("python3") is None:
        pytest.skip("no python3 for the virtual environment")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    try:
        code = sandbox.prove(sandbox.Options(offline=True, venv=True))
    except sandbox.SandboxRefused as exc:
        pytest.skip(f"no virtual environment can be made here: {exc}")
    out = capfd.readouterr().out
    assert code == 0 and "REACHED" not in out, out
    assert "left open with --venv, the virtual environment's own folder" in out


@needs_bubblewrap
@pytest.mark.bubblewrap
def test_a_link_left_by_one_run_does_not_lead_the_next_one_out(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "target"
    target.mkdir()
    first = sandbox.prepare(sandbox.Options(offline=True), str(tmp_path / "work"))
    assert sandbox.launch(first, ["/bin/sh", "-c", f'ln -s "{target}" app']) == 0
    assert (tmp_path / "work" / "app").is_symlink()
    with pytest.raises(sandbox.SandboxRefused, match="through a link"):
        sandbox.prepare(sandbox.Options(offline=True), str(tmp_path / "work" / "app"))
    assert list(target.iterdir()) == []


@needs_bubblewrap
@pytest.mark.bubblewrap
def test_a_writable_folder_on_the_path_is_refused_at_every_start(tmp_path, monkeypatch):
    """Whatever the options let through, the sandbox looks from inside before it
    starts anything: nothing it can write may be on its PATH."""
    import dataclasses

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    made = sandbox.prepare(sandbox.Options(offline=True), str(tmp_path / "work"))
    sandbox.check(made)  # as decided, it is clean
    path = f"{sandbox.HOME}:{made.env['PATH']}"
    with pytest.raises(sandbox.SandboxRefused, match="on its PATH"):
        sandbox.check(dataclasses.replace(made, env={**made.env, "PATH": path}))


@needs_bubblewrap
@pytest.mark.bubblewrap
def test_a_command_inside_writes_only_where_it_may(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SECRET_OUTSIDE", "must-not-arrive")
    bait = tmp_path / "bait"
    bait.write_text("outside")
    made = sandbox.prepare(sandbox.Options(offline=True), str(tmp_path / "work"))
    # The folder above the workspace is not the one outside: a write there stays in
    # the sandbox's own /tmp, and the file beside the workspace cannot be read.
    script = (
        f'cat "{bait}" 2>/dev/null && echo READ-THE-BAIT; '
        f'touch "{tmp_path}/escaped" 2>/dev/null; '
        'echo "secret=${SECRET_OUTSIDE:-none} sandbox=$FOUNDRY_SANDBOX"; '
        'echo "first process started with $(wc -c < /proc/1/environ) bytes of environment"; '
        'printf "\\033[31mcolour\\n"; touch made-inside; exit 7'
    )
    assert sandbox.launch(made, ["/bin/sh", "-c", script]) == 7
    out = capfd.readouterr().out
    assert "READ-THE-BAIT" not in out
    assert "secret=none sandbox=bubblewrap" in out and "\\x1b[31mcolour" in out
    assert "first process started with 0 bytes of environment" in out
    assert (tmp_path / "work" / "made-inside").exists()
    assert not (tmp_path / "escaped").exists() and bait.read_text() == "outside"
