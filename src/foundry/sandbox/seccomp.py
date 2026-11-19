"""The system-call filter the sandbox loads (classic BPF).

bubblewrap's namespaces and the lack of any privilege inside are the boundary. This
filter refuses the few things they do not cover:

* sockets of the families AF_VSOCK (the channel from a virtual machine or a WSL
  system to its host) and AF_BLUETOOTH;
* the kernel keyring calls (the user keyring is shared with outside the sandbox);
* io_uring, which can open sockets without the socket call being seen;
* any call made through another calling convention (32-bit, x32), so the numbers
  above cannot be reached by a second door.

Everything else is allowed. The program is assembled here, by hand, so that the
tool needs no compiler and no library for it; `decide` runs a program the way the
kernel would, for the tests.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

LD_ABS, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06
ALLOW = 0x7FFF0000
ERRNO = 0x00050000
EPERM, ENOSYS, EAFNOSUPPORT = 1, 38, 97
AF_BLUETOOTH, AF_VSOCK = 31, 40
X32_BIT = 0x40000000


class UnsupportedMachineError(RuntimeError):
    """No filter is written for this machine type; the sandbox does not start."""


@dataclass(frozen=True)
class Machine:
    audit_arch: int
    socket: int
    add_key: int
    request_key: int
    keyctl: int
    io_uring: tuple[int, int, int]
    has_x32: bool


MACHINES: dict[str, Machine] = {
    "x86_64": Machine(0xC000003E, 41, 248, 249, 250, (425, 426, 427), True),
    "aarch64": Machine(0xC00000B7, 198, 217, 218, 219, (425, 426, 427), False),
}

Step = tuple[str, int, str | None, str | None]


def _program(m: Machine) -> list[Step]:
    """(operation, value, label when true, label when false); None falls through."""
    steps: list[Step] = [
        ("ld", 4, None, None),  # the calling convention
        ("jeq", m.audit_arch, None, "enosys"),
        ("ld", 0, None, None),  # the call's number
    ]
    if m.has_x32:
        steps.append(("jge", X32_BIT, "enosys", None))
    steps += [
        ("jeq", m.socket, "family", None),
        ("jeq", m.add_key, "eperm", None),
        ("jeq", m.request_key, "eperm", None),
        ("jeq", m.keyctl, "eperm", None),
        *[("jeq", nr, "enosys", None) for nr in m.io_uring],
        ("ret", ALLOW, None, None),
        ("label", 0, "family", None),
        ("ld", 16, None, None),  # the first argument's low half: the socket family
        ("jeq", AF_VSOCK, "eaf", None),
        ("jeq", AF_BLUETOOTH, "eaf", None),
        ("ret", ALLOW, None, None),
        ("label", 0, "eperm", None),
        ("ret", ERRNO | EPERM, None, None),
        ("label", 0, "enosys", None),
        ("ret", ERRNO | ENOSYS, None, None),
        ("label", 0, "eaf", None),
        ("ret", ERRNO | EAFNOSUPPORT, None, None),
    ]
    return steps


def assemble(machine: str) -> bytes:
    """The filter for `machine` (as `platform.machine()` names it), ready for
    bubblewrap's `--seccomp`. Raises `UnsupportedMachineError` for any other."""
    if machine not in MACHINES:
        raise UnsupportedMachineError(
            f"the sandbox has no system-call filter for a {machine} machine "
            f"(written for: {', '.join(sorted(MACHINES))}). Nothing was started."
        )
    labels: dict[str, int] = {}
    steps: list[Step] = []
    for step in _program(MACHINES[machine]):
        if step[0] == "label":
            assert step[2] is not None
            labels[step[2]] = len(steps)
        else:
            steps.append(step)
    out = b""
    for index, (op, value, when_true, when_false) in enumerate(steps):
        if op == "ld":
            out += struct.pack("<HBBI", LD_ABS, 0, 0, value)
        elif op == "ret":
            out += struct.pack("<HBBI", RET, 0, 0, value)
        else:
            jump_true = labels[when_true] - index - 1 if when_true else 0
            jump_false = labels[when_false] - index - 1 if when_false else 0
            assert 0 <= jump_true < 256 and 0 <= jump_false < 256
            out += struct.pack("<HBBI", JEQ if op == "jeq" else JGE, jump_true, jump_false, value)
    return out


def decide(program: bytes, *, arch: int, nr: int, arg0: int = 0) -> int:
    """What `program` returns for one call: `ALLOW`, or `ERRNO | errno`. It reads the
    same record the kernel hands a filter (number at 0, convention at 4, the first
    argument at 16) and knows only the four instructions `assemble` writes."""
    record = struct.pack("<IIQQ", nr & 0xFFFFFFFF, arch, 0, arg0 & 0xFFFFFFFFFFFFFFFF)
    accumulator = 0
    pc = 0
    count = len(program) // 8
    while pc < count:
        code, jump_true, jump_false, value = struct.unpack_from("<HBBI", program, pc * 8)
        pc += 1
        if code == LD_ABS:
            (accumulator,) = struct.unpack_from("<I", record, value)
        elif code == RET:
            return int(value)
        elif code == JEQ:
            pc += jump_true if accumulator == value else jump_false
        elif code == JGE:
            pc += jump_true if accumulator >= value else jump_false
        else:
            raise ValueError(f"an instruction this reader does not know: {code:#x}")
    raise ValueError("the program ended without a verdict")
