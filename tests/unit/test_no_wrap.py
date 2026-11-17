"""Paths and machine values reach the person whole: never wrapped, never truncated.

A path the CLI prints (a workspace, a skill library, a
bundle, a path inside an error) must survive any terminal width -- a pipe with
`COLUMNS=20`, a narrow terminal, a long temp directory. Rich wraps at the terminal width
by default, so Foundry's stderr console prints soft-wrapped: the terminal folds a long
line, the text keeps it whole. The same lines print a path literally: `[red]x[/]` in a
directory name is a name, not markup, and a control character is shown, not obeyed.

Paths are relative to the test's own working directory (never TMPDIR-length
dependent). Subprocess with stdout/stderr piped, COLUMNS unset / 20 / 60 / 200. Offline:
the build engine is replaced by a fake in the child process (no provider, no worker, no
model call), and SkillWarden is a local script that prints a fixed JSON verdict.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WIDTHS = [None, 20, 60, 200]
LENGTHS = [20, 130, 300]

# The child: `foundry` with `build` replaced by a fake that reports an event naming the
# workspace and returns a finished state -- or refuses the workspace, as fsprobe does.
_SHIM = textwrap.dedent(
    """
    import os
    import sys
    from types import SimpleNamespace
    from foundry import cli, fsprobe

    def fake_build(brief, **kw):
        ws = kw["workspace"]
        if brief == "refuse":
            raise fsprobe.UnsupportedWorkspaceError(f"refusing to build in {ws}")
        kw["on_event"](os.environ.get("FAKE_STAGE", "worktree-retained"),
                       os.environ.get("FAKE_DETAIL", f"{ws} retained: clean up manually"))
        return SimpleNamespace(plan=None, done_ids=[], done=True, workspace=ws)

    cli.build = fake_build
    cli.app(prog_name="foundry")
    """
)


def _long_dir(total: int, tag: str = "d") -> Path:
    """A relative directory path exactly `total` characters long, in parts of <= 90."""
    parts: list[str] = []
    left = total
    while left > 0:
        size = min(90, left)
        if left - size == 1:
            size -= 1
        parts.append(tag * size)
        left -= size + 1
    rel = Path(*parts)
    assert len(str(rel)) == total
    return rel


def _foundry(cwd: Path, columns: int | None, *args: str,
             extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES", "PYTHONPATH")}
    if columns is not None:
        env["COLUMNS"] = str(columns)
    env["FOUNDRY_SANDBOX"] = "pytest-offline-fakes"
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", _SHIM, *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120, env=env, check=False)


@pytest.mark.parametrize("columns", WIDTHS)
@pytest.mark.parametrize("length", LENGTHS)
def test_empty_library_is_named_whole(tmp_path: Path, columns: int | None,
                                      length: int) -> None:
    library = _long_dir(length) / "library"
    (tmp_path / library).mkdir(parents=True)
    proc = _foundry(tmp_path, columns, "vet", str(library),
                    extra_env={"FOUNDRY_SKILLWARDEN": "true"})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"No SKILL.md bundles found under {library}" in proc.stderr.splitlines(), proc.stderr


@pytest.mark.parametrize("columns", WIDTHS)
@pytest.mark.parametrize("length", LENGTHS)
def test_build_workspace_lines_are_whole(tmp_path: Path, columns: int | None,
                                         length: int) -> None:
    ws = _long_dir(length, "w") / "app"
    proc = _foundry(tmp_path, columns, "build", "a brief", "-w", str(ws), "--provider", "cli",
                    "--worker", "cli")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stderr.splitlines()
    assert f"workspace={ws} · provider=claude_cli" in lines, proc.stderr
    assert f"worktree-retained {ws} retained: clean up manually" in lines, proc.stderr
    assert f"Build complete: 0/0 tasks · workspace: {ws}" in lines, proc.stderr


@pytest.mark.parametrize("columns", [20, 60])
def test_a_refused_workspace_error_is_whole(tmp_path: Path, columns: int) -> None:
    ws = _long_dir(200, "r") / "app"
    proc = _foundry(tmp_path, columns, "build", "refuse", "-w", str(ws), "--provider", "cli",
                    "--worker", "cli")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"refusing to build in {ws}" in proc.stderr.splitlines(), proc.stderr


@pytest.mark.parametrize("columns", [20, 60, None])
@pytest.mark.parametrize("length", [40, 200])
def test_vetted_bundle_names_are_whole(tmp_path: Path, columns: int | None,
                                       length: int) -> None:
    fake = tmp_path / "fake-skillwarden"
    fake.write_text(
        "#!/bin/sh\necho '{\"severity_label\": \"low\", \"risk_score\": 3, \"status\": \"complete\"}'\n"
    )
    fake.chmod(0o755)
    name = ("bundle-" + "b" * length)[:length]
    (tmp_path / "lib" / name).mkdir(parents=True)
    (tmp_path / "lib" / name / "SKILL.md").write_text("# a skill\n")
    proc = _foundry(tmp_path, columns, "vet", "lib",
                    extra_env={"FOUNDRY_SKILLWARDEN": str(fake)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(line.endswith(f"  {name}") and "low" in line
               for line in proc.stderr.splitlines()), proc.stderr


@pytest.mark.parametrize("columns", [20, 200])
def test_markup_and_controls_in_a_path_print_literally(tmp_path: Path, columns: int) -> None:
    library = Path("[red]x[/]") / "[bold]y" / ("z" * 80)
    (tmp_path / library).mkdir(parents=True)
    proc = _foundry(tmp_path, columns, "vet", str(library),
                    extra_env={"FOUNDRY_SKILLWARDEN": "true"})
    assert f"No SKILL.md bundles found under {library}" in proc.stderr.splitlines(), proc.stderr

    ws = Path("[/]") / "a\x1b[31mred" / "b\nforged"
    proc = _foundry(tmp_path, columns, "build", "a brief", "-w", str(ws), "--provider", "cli",
                    "--worker", "cli")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "\x1b" not in proc.stderr
    shown = "[/]/a\\x1b[31mred/b\\x0aforged"
    assert f"Build complete: 0/0 tasks · workspace: {shown}" in proc.stderr.splitlines(), (
        proc.stderr)


def test_plan_review_shows_roles_and_model_text_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan a person approves is model text: `[backend]` is the task's role and must
    be shown (as markup it vanished), and a long verify command is never cut."""
    import io

    from foundry import cli
    from foundry.models import Plan, Task

    verify = "pytest -q " + "tests/" + "v" * 150 + "/test_x.py"
    plan = Plan(app_name="[red]app[/]", stack="Python", summary="a [bold]summary",
                tasks=[Task(id="t1", title="feature", role="backend", instruction="x",
                            depends_on=[], verify=verify)])
    buf = io.StringIO()
    monkeypatch.setenv("COLUMNS", "20")
    # `_file`, not the `file` property: undoing the property would pin the console to
    # whatever stream pytest had captured at the time, breaking later tests.
    monkeypatch.setattr(cli.err, "_file", buf)
    monkeypatch.setattr(cli.err, "input", lambda *a, **k: "")
    assert cli.CliController(cli.err).review_plan(plan) is plan
    lines = buf.getvalue().splitlines()
    assert "Plan: [red]app[/]  (Python)" in lines, lines
    assert "a [bold]summary" in lines, lines
    assert f"  t1 [backend] feature ✓ {verify}" in lines, lines


# --- Values are literal by construction ----------------------
#
# A value (a path, a stage, model text, an error) reaches the console as a token markup
# cannot read and is turned back into plain `Text` by the console: a trailing backslash
# prints once, `[/x]` cannot raise MarkupError, `[red]` is shown. Control, bidi and
# line-separator characters are shown as escapes (the SkillWarden `visible()` set).


@pytest.mark.parametrize(("value", "shown"), [
    ("ws\\", "ws\\"),
    ("ws\\\\", "ws\\\\"),
    ("a\\[red]b", "a\\[red]b"),
    ("c\\\\[bold]d", "c\\\\[bold]d"),
    ("[/x]", "[/x]"),
    ("[/]", "[/]"),
    ("[red]acme", "[red]acme"),
    (":thumbs_up: [link=http://x]y", ":thumbs_up: [link=http://x]y"),
    ("esc\x1b[2Jbel\x07del\x7fc1\x9b", "esc\\x1b[2Jbel\\x07del\\x7fc1\\x9b"),
    ("bidi‮x⁦y⁩؜‎‏", "bidi\\u202ex\\u2066y\\u2069\\u061c\\u200e\\u200f"),
    ("line para nl\ncr\r", "line\\u2028para\\u2029nl\\x0acr\\x0d"),
    ("pua", "pua"),
    ("", ""),
])
def test_a_value_round_trips_through_the_console(monkeypatch: pytest.MonkeyPatch,
                                                  value: str, shown: str) -> None:
    import io

    from foundry import cli

    buf = io.StringIO()
    monkeypatch.setattr(cli.err, "_file", buf)
    cli.err.print(f"[bold]<{cli._lit(value)}>[/] [dim]{cli._lit(value)}[/]{cli._lit(value)}")
    assert buf.getvalue() == f"<{shown}> {shown}{shown}\n"


@pytest.mark.parametrize("columns", [20, 200])
@pytest.mark.parametrize("ws", ["ws\\", "ws\\\\", "a\\[red]b/app", "[/]q/app"])
def test_a_backslash_in_the_workspace_prints_as_it_is(tmp_path: Path, columns: int,
                                                       ws: str) -> None:
    """The reviewer's repro: `workspace=ws\\\\` and `detail end\\\\` for a value ending in one
    backslash."""
    proc = _foundry(tmp_path, columns, "build", "a brief", "-w", ws, "--provider", "cli",
                    "--worker", "cli",
                    extra_env={"FAKE_STAGE": "stage", "FAKE_DETAIL": "detail end\\"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stderr.splitlines()
    assert f"workspace={ws} · provider=claude_cli" in lines, proc.stderr
    assert "stage        detail end\\" in lines, proc.stderr  # the stage padded to 12
    assert f"Build complete: 0/0 tasks · workspace: {ws}" in lines, proc.stderr


def test_a_bundle_name_ending_in_a_backslash_prints_once(tmp_path: Path) -> None:
    fake = tmp_path / "fake-skillwarden"
    fake.write_text(
        "#!/bin/sh\necho '{\"severity_label\": \"low\", \"risk_score\": 3, \"status\": \"complete\"}'\n"
    )
    fake.chmod(0o755)
    for name in ("bundle\\", "[red]b"):
        (tmp_path / "lib" / name).mkdir(parents=True)
        (tmp_path / "lib" / name / "SKILL.md").write_text("# a skill\n")
    proc = _foundry(tmp_path, 20, "vet", "lib", extra_env={"FOUNDRY_SKILLWARDEN": str(fake)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stderr.splitlines()
    assert any(ln.endswith("  bundle\\") and "low" in ln for ln in lines), proc.stderr
    assert any(ln.endswith("  [red]b") and "low " in ln for ln in lines), proc.stderr


def test_hostile_model_text_in_the_plan_prints_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from foundry import cli
    from foundry.models import Plan, Task

    plan = Plan(app_name="app\\", stack="st\\", summary="[/x] sum\x1b[2J\\",
                tasks=[Task(id="t1\\", title="title [/]", role="back[/]end", instruction="x",
                            depends_on=["d\\"], verify="pytest \\")])
    buf = io.StringIO()
    monkeypatch.setattr(cli.err, "_file", buf)
    monkeypatch.setattr(cli.err, "input", lambda *a, **k: "")
    assert cli.CliController(cli.err).review_plan(plan) is plan
    lines = buf.getvalue().splitlines()
    assert "Plan: app\\  (st\\)" in lines, lines
    assert "[/x] sum\\x1b[2J\\" in lines, lines
    assert "  t1\\ [back[/]end] title [/] after d\\ ✓ pytest \\" in lines, lines


# --- The sweep: no value reaches console markup except through `_lit` ------------------

_SINKS = {"print", "input", "rule", "add_row", "add_column", "Table"}
_NUMERIC_SPEC = set(",_d.%fegn0123456789<>^=+- ")
_RICH = {"Text", "Table", "Rule"}
_SAFE_CALLS = {"_lit", "len"}


def _markup_offenders(source: str) -> list[str]:
    """Every place a value could reach Rich markup without `_lit`: an interpolation in
    an f-string that is printed (or holds a Rich tag), or an argument of a console sink
    (print, input, rule, a table's add_row / add_column / title) that is not the tool's
    own text. Allowed: string and number literals; `_lit(...)`; `len(...)`; a value
    formatted with a numeric spec (`{n:,}` raises on anything but a number); a style
    name inside a tag (`[{style}]`); a Rich object built here; and a local name every
    assignment of which, in the same function, is itself allowed (the tool's own
    markup fragments such as `mode = "[green]dry run[/]"`)."""
    import ast
    import re

    tree = ast.parse(source)
    tag = re.compile(r"\[[a-z#/@]")
    found: list[str] = []

    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

    def funcs(node: ast.AST) -> list[ast.AST]:
        return [n for n in ast.walk(node) if isinstance(n, scopes)]

    def walk(scope: ast.AST) -> list[ast.AST]:
        """`scope`'s own nodes: a nested function is its own scope."""
        out: list[ast.AST] = [scope]
        todo = list(ast.iter_child_nodes(scope))
        while todo:
            n = todo.pop()
            if isinstance(n, scopes):
                continue
            out.append(n)
            todo.extend(ast.iter_child_nodes(n))
        return out

    def assignments(scope: ast.AST) -> dict[str, list[ast.expr | None]]:
        out: dict[str, list[ast.expr | None]] = {}
        for n in walk(scope):
            pairs: list[tuple[ast.expr, ast.expr | None]] = []
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple) \
                            and len(t.elts) == len(n.value.elts):
                        pairs += list(zip(t.elts, n.value.elts, strict=True))
                    else:
                        pairs.append((t, n.value if isinstance(t, ast.Name) else None))
            elif isinstance(n, ast.AnnAssign):
                pairs.append((n.target, n.value))
            elif isinstance(n, ast.For) and isinstance(n.iter, (ast.Tuple, ast.List)) \
                    and isinstance(n.target, ast.Name) \
                    and all(isinstance(x, ast.Constant) for x in n.iter.elts):
                out.setdefault(n.target.id, []).extend(n.iter.elts)  # the tool's own words
            elif isinstance(n, (ast.AugAssign, ast.For, ast.comprehension, ast.With,
                                ast.NamedExpr)):
                target = getattr(n, "target", None)
                for sub in ast.walk(target) if target is not None else []:
                    if isinstance(sub, ast.Name):
                        out.setdefault(sub.id, []).append(
                            n.value if isinstance(n, (ast.AugAssign, ast.NamedExpr))
                            else None)
                for item in getattr(n, "items", []):
                    for sub in ast.walk(item.optional_vars) if item.optional_vars else []:
                        if isinstance(sub, ast.Name):
                            out.setdefault(sub.id, []).append(None)
            for target, value in pairs:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        out.setdefault(sub.id, []).append(
                            value if sub is target else None)
            if isinstance(n, scopes) and n is scope:
                for a in [*n.args.args, *n.args.kwonlyargs, *n.args.posonlyargs]:
                    # A parameter typed as a Rich object is rendered, never parsed.
                    rich = isinstance(a.annotation, ast.Name) and a.annotation.id in _RICH
                    out.setdefault(a.arg, []).append(ast.Constant("") if rich else None)
        return out

    def check(scope: ast.AST, known: dict[str, list[ast.expr | None]]) -> None:
        seen: set[str] = set()

        def safe(e: ast.expr | None, depth: int = 0) -> bool:
            if e is None or depth > 20:
                return False
            if isinstance(e, ast.Constant):
                return True
            if isinstance(e, ast.JoinedStr):
                return all(safe_value(v, e, depth) for v in e.values)
            if isinstance(e, ast.IfExp):
                return safe(e.body, depth + 1) and safe(e.orelse, depth + 1)
            if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
                return safe(e.left, depth + 1) and safe(e.right, depth + 1)
            if isinstance(e, ast.Starred):
                return safe(e.value, depth + 1)
            if isinstance(e, (ast.ListComp, ast.GeneratorExp)):
                return safe(e.elt, depth + 1)
            if isinstance(e, ast.BoolOp):
                return all(safe(v, depth + 1) for v in e.values)
            if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) \
                    and e.func.id in _SAFE_CALLS | _RICH:
                return True
            if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) \
                    and isinstance(e.func.value, ast.Name) and e.func.value.id == "Text" \
                    and e.func.attr != "from_markup":
                return True  # Text.assemble(...) and friends take plain text
            if isinstance(e, ast.Name):
                if e.id in seen:
                    return True
                values = known.get(e.id)
                if not values:
                    return False
                seen.add(e.id)
                ok = all(safe(v, depth + 1) for v in values)
                seen.discard(e.id)
                return ok
            return False

        def safe_value(v: ast.expr, owner: ast.JoinedStr, depth: int) -> bool:
            if isinstance(v, ast.Constant):
                return True
            assert isinstance(v, ast.FormattedValue)
            spec = "" if v.format_spec is None else "".join(
                str(c.value) for c in v.format_spec.values if isinstance(c, ast.Constant))
            if spec:
                return set(spec) <= _NUMERIC_SPEC and any(c in spec for c in ",dfe%n")
            if v.conversion != -1:
                return False
            index = owner.values.index(v)
            before = owner.values[index - 1] if index else None
            after = owner.values[index + 1] if index + 1 < len(owner.values) else None
            if isinstance(before, ast.Constant) and isinstance(after, ast.Constant) \
                    and re.search(r"\[/?$", str(before.value)) \
                    and str(after.value).startswith("]"):
                return True  # a style name: `[{style}]`
            return safe(v.value, depth + 1)

        for n in walk(scope):
            exprs: list[ast.expr] = []
            if isinstance(n, ast.Call) and not (  # a console's own print reaching Rich's
                    isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Call)
                    and getattr(n.func.value.func, "id", None) == "super"):
                # `console.print(...)`, `table.add_row(...)`, `Table(...)`: not the builtin
                # print, which writes plain text and parses nothing.
                name = n.func.attr if isinstance(n.func, ast.Attribute) else \
                    ("Table" if getattr(n.func, "id", None) == "Table" else None)
                if name in _SINKS:
                    exprs += [a for a in n.args]
                    exprs += [k.value for k in n.keywords if k.arg in ("title", "caption",
                                                                       "header")]
            if isinstance(n, ast.JoinedStr) and tag.search("".join(
                    str(v.value) for v in n.values if isinstance(v, ast.Constant))):
                exprs.append(n)
            for e in exprs:
                if not safe(e):
                    found.append(f"{e.lineno}: {ast.get_source_segment(source, e)}")

    for scope in [tree, *funcs(tree)]:
        check(scope, assignments(scope))
    return sorted(set(found), key=lambda s: int(s.split(":")[0]))


def test_no_value_reaches_console_markup_except_through_lit() -> None:
    """The sweep, kept: a value interpolated into a printed f-string goes through `_lit`
    -- or it is the tool's own text. A new `err.print(f"... {path}")` fails here."""
    from foundry import cli

    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "import escape" not in source  # escape() into markup cannot round-trip a `\\`
    offenders = _markup_offenders(source)
    assert offenders == [], f"{len(offenders)} unescaped values:\n" + "\n".join(offenders)
