"""Foundry CLI. `foundry build "<brief>" -w ./out [options]`."""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from foundry import __version__, auth, constants, fsprobe, trust
from foundry import sandbox as box
from foundry.control import Controller, Revise
from foundry.engine import build
from foundry.isolation import HistoryUnreadableError
from foundry.models import Plan
from foundry.providers.base import get_provider
from foundry.worker import get_worker

app = typer.Typer(
    add_completion=False,
    help=(
        "Foundry: takes a brief and builds a working web app with a team of Claude "
        "workers, through quality gates."
    ),
)
# C0 + DEL + C1 controls, bidi embedding/override/isolate marks, line/paragraph separators:
# the set SkillWarden's `visible()` shows as escapes, the same in every chamber.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def visible(text: str) -> str:
    """`text` with every control / bidi / line-separator character shown as an escape
    (`\\x1b`, `\\u202e`): a character that would restyle or rewrite the terminal, reorder
    what the reader sees or forge a line is shown, never obeyed."""

    def shown(match: re.Match[str]) -> str:
        code = ord(match.group())
        return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"

    return _UNSAFE.sub(shown, text)


# A value travels through a markup string as a token markup cannot read: its UTF-8 bytes
# as private-use characters U+E000..U+E0FF between two private-use delimiters. No `[`,
# no backslash, no `:emoji:` -- so a value can neither open nor close a tag, nor escape
# one next to it. The console turns each token back into plain `Text` after the markup
# around it is parsed (`_Literal.render_str`).
_OPEN, _CLOSE = "\uf8fe", "\uf8ff"
_BYTES = [chr(0xE000 + b) for b in range(256)]
_TOKEN = re.compile("\uf8fe([\ue000-\ue0ff]*)\uf8ff")


def _lit(value: object) -> str:
    """`value` (a path, a name, an error, model text) for a console markup string,
    printed exactly as it is: `[red]` and `[/x]` are text, a trailing backslash prints
    once, and control / bidi / line-separator characters are shown as escapes.

    Only for what a `_Literal` console prints; the token means nothing anywhere else.
    """
    raw = str(value).encode("utf-8", "surrogatepass")
    return _OPEN + "".join([_BYTES[b] for b in raw]) + _CLOSE


def _literal_text(text: Text) -> Text:
    """`text` (parsed markup) with each `_lit` token replaced by its value as plain text,
    in the style the markup around the token gave it."""
    plain = text.plain
    if _OPEN not in plain:
        return text
    out = text.blank_copy()
    position = 0
    for match in _TOKEN.finditer(plain):
        out.append_text(text[position : match.start()])
        token = text[match.start() : match.end()]
        raw = bytes(ord(ch) - 0xE000 for ch in match.group(1))
        value = Text(visible(raw.decode("utf-8", "surrogatepass")), style=token.style)
        for span in token.spans:  # markup cannot start or end inside a token
            if span.start == 0 and span.end == len(token):
                value.stylize(span.style)
        out.append_text(value)
        position = match.end()
    out.append_text(text[position:])
    return out


class _Literal(Console):
    """A console on which values are literal: every string it renders (a printed line,
    a prompt) comes through `render_str`, where a `_lit` token becomes plain text -- no
    value is ever read as markup, so a MarkupError from data cannot happen."""

    def render_str(self, text: str, **kwargs: Any) -> Text:
        return _literal_text(super().render_str(text, **kwargs))


# Soft-wrapped: a path or value is never wrapped or cut at
# the terminal width -- the terminal folds a long line, the text keeps it whole, so a
# workspace or library path can be copied back into the next command.
err = _Literal(stderr=True, soft_wrap=True)


class CliController(Controller):
    """Interactive plan approval — review the architecture before the build runs."""

    def __init__(self, console: _Literal) -> None:
        self.c = console

    def review_plan(self, plan: Plan) -> Plan | Revise:
        self.c.print(f"\n[bold]Plan[/]: {_lit(plan.app_name)}  [dim]({_lit(plan.stack)})[/]")
        self.c.print(f"[dim]{_lit(plan.summary)}[/]")
        for t in plan.tasks:
            dep = f" [dim]after {_lit(','.join(t.depends_on))}[/]" if t.depends_on else ""
            gate = f" [dim]✓ {_lit(t.verify)}[/]" if t.verify else ""
            role = _lit(f"[{t.role}]")
            self.c.print(f"  [cyan]{_lit(t.id)}[/] {role} {_lit(t.title)}{dep}{gate}")
        raw = self.c.input(
            "[dim]\\[Enter] approve & build · 'regen <guidance>' to replan ›[/] "
        ).strip()
        if not raw:
            return plan
        if raw.lower().startswith("regen"):
            return Revise(raw[5:].strip())
        return Revise(raw)


def _version_cb(value: bool) -> None:
    if value:
        _Literal().print(f"Foundry {_lit(__version__)}")
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(False, "--version", callback=_version_cb, is_eager=True),
) -> None:
    pass


@app.command(name="build")
def build_cmd(
    brief: str = typer.Argument("", help="What to build (omit when resuming a --session)."),
    workspace: str = typer.Option(..., "--workspace", "-w", help="Directory to build the app in."),
    session: str | None = typer.Option(
        None, "--session", help="Persist/resume build state at FILE (crash-safe)."
    ),
    guidance: str = typer.Option(
        "", "--guidance", "-g", help="Constraints/steer (stack, scope, conventions)."
    ),
    skills: str | None = typer.Option(
        None, "--skills", help="Skill library directory to inject per task (role-relevant)."
    ),
    constitution: str | None = typer.Option(
        None, "--constitution", help="constitution.md overriding the built-in standard."
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Review/approve the plan before building."
    ),
    no_archetypes: bool = typer.Option(
        False,
        "--no-archetypes",
        help="Don't apply project-type planning floors (e.g. the web-app security task).",
    ),
    no_discovery: bool = typer.Option(
        False, "--no-discovery", help="Skip DISCOVERY; plan straight from the brief (no spec)."
    ),
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Skip the VERIFY phase (running & exercising the built app)."
    ),
    no_review: bool = typer.Option(
        False, "--no-review", help="Skip the multi-axis code-review phase after the build."
    ),
    worker_backend: str = typer.Option(
        constants.WORKER_BACKEND, "--worker", help="Worker backend: sdk (Agent SDK) | cli."
    ),
    isolation_mode: str = typer.Option(
        constants.ISOLATION, "--isolation", help="Parallel isolation: worktree | none."
    ),
    provider: str = typer.Option(constants.PROVIDER, "--provider"),
    sandbox: str | None = typer.Option(
        None,
        "--sandbox",
        envvar=trust.SANDBOX_ENV,
        help="Attest the sandbox this run is inside (set ONLY by the container/VM wrapper).",
    ),
    unattended: bool = typer.Option(
        False,
        "--unattended",
        help="Declare an unattended run; refused unless a sandbox is attested.",
    ),
) -> None:
    """Plan and autonomously BUILD an app from a brief, through quality gates."""
    # Decided before anything else: no LLM call, worker or workspace write precedes it.
    try:
        decision = trust.resolve(sandbox=sandbox, unattended=unattended)
    except trust.TrustRefused as exc:
        err.print(f"[red]{_lit(exc)}[/]")
        raise typer.Exit(2) from None
    # Then the sign-in: a build runs on an API method, never on a claude.ai sign-in.
    try:
        auth.require_api_auth()
    except auth.AuthRefused as exc:
        err.print(f"[red]{_lit(exc)}[/]")
        raise typer.Exit(2) from None
    resuming = bool(session and Path(session).exists())
    if not resuming and not brief.strip():
        err.print("[red]Provide a brief (or --session FILE to resume an existing build).[/]")
        raise typer.Exit(2)

    controller = CliController(err) if interactive else None
    err.print(
        f"[dim]workspace={_lit(workspace)} · provider={_lit(get_provider(provider).name)}"
        f"{' · interactive' if interactive else ''}[/]"
    )
    err.print(f"[yellow]{_lit(decision.statement)}[/]")

    try:
        state = build(
            brief,
            workspace=workspace,
            provider=get_provider(provider),
            worker=get_worker(worker_backend),
            controller=controller,
            guidance=guidance,
            skill_library=skills,
            constitution_path=constitution,
            use_archetypes=not no_archetypes,
            discovery=not no_discovery,
            verify=not no_verify,
            review=not no_review,
            isolation=isolation_mode,
            session_path=session,
            on_event=lambda stage, detail: err.print(f"[cyan]{_lit(format(stage, '12'))}[/] "
                                                     f"{_lit(detail)}"),
            trust=decision,
        )
    except (
        fsprobe.UnsupportedWorkspaceError,
        HistoryUnreadableError,
        constants.JudgeDowngradeRefused,
        auth.AuthRefused,
    ) as exc:
        # Refused, like the trust check: a message and exit 2. The filesystem and the
        # model settings are refused before any model is called. An unreadable git
        # history is found later, when the worktrees are set up, after the plan exists.
        err.print(f"[red]{_lit(exc)}[/]")
        raise typer.Exit(2) from None

    plan = state.plan
    total = len(plan.tasks) if plan else 0
    done = len(state.done_ids)
    style = "green" if state.done else "yellow"
    err.print(
        f"[{style}]Build {'complete' if state.done else 'incomplete'}: "
        f"{done}/{total} tasks · workspace: {_lit(state.workspace)}[/]"
    )
    raise typer.Exit(0 if state.done else 1)


@app.command()
def vet(
    library: str = typer.Argument(..., help="Skill library directory to scan with SkillWarden."),
) -> None:
    """Vet a skill library with SkillWarden before adopting it (adopt → vet → upgrade)."""
    from foundry.security import skillwarden_available, vet_library

    if not skillwarden_available():
        err.print(
            f"[red]SkillWarden CLI not found[/] (looked for '{_lit(constants.SKILLWARDEN_CMD)}'). "
            "Install it, or set FOUNDRY_SKILLWARDEN to its command."
        )
        raise typer.Exit(2)
    results = vet_library(library)
    assert results is not None  # available checked above
    if not results:
        err.print(f"[yellow]No SKILL.md bundles found under {_lit(library)}[/]")
        raise typer.Exit(1)

    clean = [r for r in results if r.ok]
    for r in sorted(results, key=lambda x: -x.score):
        mark = "[green]✓[/]" if r.ok else "[red]✗[/]"
        note = "" if r.complete else "  [yellow](scan incomplete: not a clearance)[/]"
        err.print(f"  {mark} {_lit(format(r.label, '8'))} {_lit(format(r.score, '3'))}  "
                  f"{_lit(r.skill)}{note}")
    err.print(
        f"\n[bold]{len(clean)}/{len(results)} skills clean[/] "
        f"(safe to adopt; {_lit(len(results) - len(clean))} need review/upgrade)."
    )
    raise typer.Exit(0 if len(clean) == len(results) else 1)


# --- foundry sandbox: the same commands, inside Foundry's own sandbox

sandbox_app = typer.Typer(
    add_completion=False,
    help=(
        "Run a build, or any command, inside Foundry's own sandbox: bubblewrap, without "
        "root. The sandbox's options come before the word build, run or proof."
    ),
)
app.add_typer(sandbox_app, name="sandbox")

# The usage errors of the build command's own parser. typer does not export their
# base class under one name in all its versions; BadParameter descends from it.
_UsageError: type[Exception] = next(
    c for c in typer.BadParameter.__mro__ if c.__name__ == "ClickException"
)
_PASS_ON = {"allow_extra_args": True, "ignore_unknown_options": True}


@sandbox_app.callback()
def sandbox_main(
    ctx: typer.Context,
    name: str | None = typer.Option(
        None,
        "--name",
        help="Keep the sandbox's own home and /tmp under this name (default: one made "
        "from the workspace, so a resumed build finds its own).",
    ),
    offline: bool = typer.Option(False, "--offline", help="No network at all inside."),
    ro: Annotated[
        list[str] | None,
        typer.Option("--ro", help="Also show this file or folder inside, read-only, same path."),
    ] = None,
    path: Annotated[
        list[str] | None,
        typer.Option("--path", help="Put this folder on the PATH inside (under /usr or --ro)."),
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Put this one program on the PATH inside, under its name."),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Pass this variable of your environment to the inside."),
    ] = None,
    env_file: str | None = typer.Option(
        None,
        "--env-file",
        help="Pass the NAME=VALUE lines of this file to the inside. Only you may be "
        "able to read the file.",
    ),
    venv: bool = typer.Option(
        False,
        "--venv",
        help="Give the inside a Python virtual environment of its own, first on its "
        "PATH, so that `python` and `pip` work for the app being built.",
    ),
    memory: str | None = typer.Option(
        None, "--memory", help="A ceiling on memory, such as 3G (needs systemd)."
    ),
    dns: Annotated[
        list[str] | None,
        typer.Option("--dns", help="Name these resolvers inside, in place of the machine's."),
    ] = None,
) -> None:
    ctx.obj = box.Options(
        name=name,
        offline=offline,
        ro=tuple(ro or ()),
        path=tuple(path or ()),
        tool=tuple(tool or ()),
        env=tuple(env or ()),
        env_file=env_file,
        venv=venv,
        memory=memory,
        dns=tuple(dns or ()),
    )


def _refused(exc: Exception) -> typer.Exit:
    err.print(f"[red]{_lit(exc)}[/]")
    return typer.Exit(2)


def _ready(
    options: box.Options, workspace: str, cwd: str, *, tools: tuple[str, ...], building: bool
) -> box.Sandbox:
    """The sandbox for `workspace`, decided, tried once and described; or exit 2."""
    try:
        made = box.prepare(options, workspace, need_claude=building)
        if building:
            auth.require_api_auth(made.env)
        box.check(made, tools, cwd=cwd)
        if options.venv:
            box.make_venv(made)
    except (box.SandboxRefused, auth.AuthRefused) as exc:
        raise _refused(exc) from None
    except OSError as exc:  # a folder that changed under the checks, a full disk
        raise _refused(
            box.SandboxRefused(f"the sandbox could not be prepared ({exc}). Nothing was started.")
        ) from None
    for line in box.describe(made):
        err.print(f"[dim]{_lit(line)}[/]")
    return made


def _started(made: box.Sandbox, command: list[str], cwd: str | None) -> int:
    """Start `command` inside and return its exit code; or exit 2 when the sandbox
    refuses at this last step (a folder that changed since it was checked)."""
    try:
        return box.launch(made, command, cwd=cwd, feed=True)
    except box.SandboxRefused as exc:
        raise _refused(exc) from None
    except OSError as exc:
        raise _refused(
            box.SandboxRefused(f"the sandbox could not be started ({exc}). Nothing was started.")
        ) from None


@sandbox_app.command("build", context_settings=_PASS_ON)
def sandbox_build(ctx: typer.Context) -> None:
    """Run `foundry build` inside the sandbox. Everything after the word build is
    given to it as it is: the brief, -w, -i, --session and the rest."""
    given = list(ctx.args)
    parser = getattr(typer.main.get_command(app), "commands")["build"]  # noqa: B009
    try:
        with parser.make_context("foundry sandbox build", list(given)) as parsed:
            params = dict(parsed.params)
    except _UsageError as exc:
        raise _refused(exc) from None
    workspace = str(params["workspace"])
    options: box.Options = ctx.obj
    # A skill library and a constitution are read, never written: show them.
    extra = [p for p in (params.get("skills"), params.get("constitution")) if p]
    options = dataclasses.replace(
        options, ro=(*options.ro, *[p for p in extra if os.path.exists(p)])
    )
    if params.get("session"):
        try:
            box.require_session_inside(workspace, str(params["session"]))
        except box.SandboxRefused as exc:
            raise _refused(exc) from None
    cwd = os.getcwd()
    made = _ready(options, workspace, cwd, tools=("sh", "git", "claude"), building=True)
    command = [sys.executable, "-P", "-m", "foundry.cli", "build", *given]
    raise typer.Exit(_started(made, command, cwd))


@sandbox_app.command("run", context_settings=_PASS_ON)
def sandbox_run(
    ctx: typer.Context,
    workspace: str = typer.Option(
        ...,
        "--workspace",
        "-w",
        help="The one folder the command may write, beside the sandbox's own home and /tmp.",
    ),
) -> None:
    """Run any command inside the sandbox, in the workspace, for example to look at
    what a build left: foundry sandbox run -w ./out -- git log --oneline"""
    command = list(ctx.args)
    if not command:
        err.print("[red]Give the command to run after the options, behind `--`.[/]")
        raise typer.Exit(2)
    made = _ready(ctx.obj, workspace, "", tools=(), building=False)
    raise typer.Exit(_started(made, command, None))


@sandbox_app.command("proof")
def sandbox_proof(ctx: typer.Context) -> None:
    """Try each door from inside a sandbox made new for it, and print what is
    closed and what is left open on this machine."""
    try:
        code = box.prove(ctx.obj)
    except box.SandboxRefused as exc:
        raise _refused(exc) from None
    except OSError as exc:
        raise _refused(
            box.SandboxRefused(f"the proof could not be prepared ({exc}). Nothing was started.")
        ) from None
    raise typer.Exit(code)


if __name__ == "__main__":
    sys.exit(app())
