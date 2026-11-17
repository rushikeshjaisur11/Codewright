"""Where an app starts: files that are run directly, not only decorated routes and `main()`.

A file is an app entry when any of these holds (tests excluded):

- it builds an app object at module level: FastAPI, Flask, Starlette, Quart, Litestar, Sanic,
  Django's WSGI/ASGI application, aiohttp, Falcon, Dash, Gradio, Typer, Celery, an MCP
  `FastMCP` server, a Modal app, an ADK `App`...;
- it is a script framework app: Streamlit (`streamlit run app.py`), Chainlit, NiceGUI, Panel;
- it starts something at module level, with or without a `__main__` guard: `app.run()`,
  `uvicorn.run(...)`, `demo.launch()`, `asyncio.run(main())`, `web.run_app(...)`, `mcp.run()`...;
- it has a `__main__` guard, or is a package's `__main__.py`;
- it is named like an entry point (`app.py`, `main.py`, `server.py`, `run.py`, `manage.py`,
  `wsgi.py`, `streamlit_app.py`, `bot.py`...) near the top of the repo, and nothing imports it.

Each entry is drawn as a `<module>` node, `path::<module>`, that runs the repo functions the file
calls at module level (including inside its `__main__` block) and its decorated handlers, so the
request flow, the hot path and the Map's in-use check all start from it.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from .graph import CodeGraph, ParsedFile, _calls, is_test_file, text
from .models import Function

MODULE = "<module>"

# Constructor (resolved import, or its last part) -> (framework, how it is started; {mod} {var} {file} filled in)
APP_OBJECTS: list[tuple[str, str, str]] = [
    ("fastapi.FastAPI", "FastAPI", "uvicorn {mod}:{var}"),
    ("starlette.applications.Starlette", "Starlette", "uvicorn {mod}:{var}"),
    ("quart.Quart", "Quart", "quart --app {mod}:{var} run"),
    ("flask.Flask", "Flask", "flask --app {mod}:{var} run"),
    ("litestar.Litestar", "Litestar", "litestar --app {mod}:{var} run"),
    ("sanic.Sanic", "Sanic", "sanic {mod}:{var}"),
    ("falcon.App", "Falcon", "gunicorn {mod}:{var}"),
    ("falcon.asgi.App", "Falcon", "uvicorn {mod}:{var}"),
    ("aiohttp.web.Application", "aiohttp", "python {file}"),
    ("django.core.wsgi.get_wsgi_application", "Django", "gunicorn {mod}:{var}"),
    ("django.core.asgi.get_asgi_application", "Django", "uvicorn {mod}:{var}"),
    ("dash.Dash", "Dash", "python {file}"),
    ("gradio.Blocks", "Gradio", "gradio {file}"),
    ("gradio.Interface", "Gradio", "gradio {file}"),
    ("gradio.ChatInterface", "Gradio", "gradio {file}"),
    ("typer.Typer", "Typer", "python -m {mod}"),
    ("celery.Celery", "Celery", "celery -A {mod} worker"),
    ("mcp.server.fastmcp.FastMCP", "MCP server", "mcp run {file}"),
    ("fastmcp.FastMCP", "MCP server", "fastmcp run {file}"),
    ("modal.App", "Modal", "modal deploy {file}"),
    ("modal.Stub", "Modal", "modal deploy {file}"),
    ("google.adk.apps.App", "Google ADK", "adk web"),
    ("slack_bolt.App", "Slack bot", "python {file}"),
    ("discord.Client", "Discord bot", "python {file}"),
    ("telegram.ext.Application", "Telegram bot", "python {file}"),
    ("functions_framework", "Cloud Functions", "functions-framework --source {file}"),
]
SCRIPT_FRAMEWORKS = {  # importing these makes the file itself the app
    "streamlit": ("Streamlit", "streamlit run {file}"),
    "chainlit": ("Chainlit", "chainlit run {file}"),
    "nicegui": ("NiceGUI", "python {file}"),
    "panel": ("Panel", "panel serve {file}"),
    "solara": ("Solara", "solara run {file}"),
    "taipy": ("Taipy", "python {file}"),
}
# Calls that start a server, worker or app when made at module level.
START_CALLS = re.compile(
    r"(^|\.)(run|run_app|run_server|run_polling|start_polling|launch|serve|serve_forever|run_forever|"
    r"main|start|cli|app|kickoff|run_until_complete|polling|infinity_polling|worker_main)$"
)
# Decorators that wrap a function without registering it with anything that runs it.
WRAPPER_DECORATORS = re.compile(
    r"^(functools\.)?(cache|lru_cache|cached_property|wraps|singledispatch|total_ordering)\b"
    r"|^(staticmethod|classmethod|property|abstractmethod|abc\.abstractmethod|dataclass|dataclasses\.dataclass"
    r"|contextmanager|contextlib\.\w+|overload|typing\.overload|override)\b"
)


def _is_handler(fn: Function) -> bool:
    return any(not WRAPPER_DECORATORS.match(d) for d in fn.decorators)


ENTRY_FILENAMES = {
    "app.py",
    "main.py",
    "server.py",
    "run.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "cli.py",
    "bot.py",
    "application.py",
    "serve.py",
    "start.py",
    "worker.py",
    "streamlit_app.py",
    "dashboard.py",
    "webapp.py",
}


@dataclass
class AppEntry:
    file: str
    framework: str  # what kind of app ("FastAPI", "Streamlit", "script"...)
    trigger: str  # how it is started, e.g. "streamlit run app.py"
    line: int  # where the app object or start call is, else 1
    reasons: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # repo functions called at module level


def _module_statements(pf: ParsedFile):
    """Top-level statements, plus those inside a `__main__` guard, that are not definitions or imports."""
    for child in pf.root.named_children:
        if child.type in (
            "function_definition",
            "class_definition",
            "decorated_definition",
            "import_statement",
            "import_from_statement",
            "comment",
            "future_import_statement",
        ):
            continue
        if child.type == "expression_statement" and child.named_children and child.named_children[0].type == "string":
            continue  # docstring
        yield child


def _resolve(pf: ParsedFile, dotted: str) -> str:
    head, _, rest = dotted.partition(".")
    target = pf.module.imports.get(head)
    if target is None:
        return dotted
    return f"{target}.{rest}" if rest else target


def _app_object(pf: ParsedFile, stmt: Node) -> tuple[str, str, str, int] | None:
    """(framework, trigger template, variable, line) when `stmt` is `var = SomeApp(...)`."""
    node = stmt.named_children[0] if stmt.type == "expression_statement" and stmt.named_children else stmt
    if node.type != "assignment":
        return None
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None or right.type != "call" or left.type != "identifier":
        return None
    callee = right.child_by_field_name("function")
    if callee is None:
        return None
    resolved = _resolve(pf, text(callee))
    for target, framework, trigger in APP_OBJECTS:
        if (
            resolved == target
            or resolved.startswith(target + ".")
            or (
                "." in target
                and resolved.endswith("." + target.rsplit(".", 1)[-1])
                and resolved.split(".")[0] == target.split(".")[0]
            )
        ):
            return framework, trigger, text(left), stmt.start_point[0] + 1
    return None


def _start_call(pf: ParsedFile, stmt: Node) -> tuple[str, int] | None:
    """(call as written, line) when the statement starts something: `app.run()`, `uvicorn.run(...)`..."""
    for n in [stmt, *stmt.named_children]:
        if n.type == "expression_statement" and n.named_children:
            n = n.named_children[0]
        if n.type == "await" and n.named_children:
            n = n.named_children[0]
        if n.type == "call" and (callee := n.child_by_field_name("function")) is not None:
            written = text(callee)
            if START_CALLS.search(written) and written not in ("print", "len"):
                return written, n.start_point[0] + 1
    return None


def console_scripts(root: str | Path) -> dict[str, list[str]]:
    """Module -> command names, from [project.scripts] and [tool.poetry.scripts] in the repo's pyproject files."""
    out: dict[str, list[str]] = {}
    root = Path(root)
    for py in [root / "pyproject.toml", *root.glob("*/pyproject.toml"), *root.glob("*/*/pyproject.toml")]:
        try:
            data = tomllib.loads(py.read_text())
        except (OSError, ValueError):
            continue
        scripts = {
            **data.get("project", {}).get("scripts", {}),
            **data.get("tool", {}).get("poetry", {}).get("scripts", {}),
        }
        for name, target in scripts.items():
            if isinstance(target, str):
                out.setdefault(target.split(":", 1)[0].strip(), []).append(name)
    return out


def detect(graph: CodeGraph) -> dict[str, AppEntry]:
    """App entries by file, kept on `graph.app_entries`."""
    from .archmap import importable_names, module_name

    out: dict[str, AppEntry] = {}
    commands = console_scripts(graph.root)
    for path, pf in graph.files.items():
        if is_test_file(path) or not path.endswith(".py"):
            continue
        name = path.rsplit("/", 1)[-1]
        mod = module_name(path)
        reasons, framework, trigger, line = [], "", "", 1
        stmts = list(_module_statements(pf))
        for stmt in stmts:
            if app := _app_object(pf, stmt):
                framework, tmpl, var, line = app
                trigger = tmpl.format(mod=mod, var=var, file=path)
                reasons.append(f"builds a {framework} app (`{var}`)")
                break
        for prefix, (fw, tmpl) in SCRIPT_FRAMEWORKS.items():
            if any(t == prefix or t.startswith(prefix + ".") for t in pf.module.imports.values()) and stmts:
                framework, trigger = framework or fw, trigger or tmpl.format(file=path)
                reasons.append(f"a {fw} app")
                break
        start = None
        for stmt in stmts:
            if stmt.type == "if_statement":
                continue  # the __main__ guard is reported on its own below
            if (start := _start_call(pf, stmt)) is not None:
                reasons.append(f"starts at module level: `{start[0]}()`")
                line = line if framework else start[1]
                break
        if pf.module.has_main_guard:
            reasons.append('has an `if __name__ == "__main__"` block')
        if name == "__main__.py":
            pkg = mod.rsplit(".", 1)[0] if "." in mod else mod
            reasons.append("is a package's `__main__.py`")
            trigger = trigger or f"python -m {pkg}"
        names = [n for n in (mod, *importable_names(mod)) if n in commands]
        if names:
            cmds = sorted({c for n in names for c in commands[n]})
            reasons.append(f"is the `{'`, `'.join(cmds)}` command in pyproject.toml")
            trigger = cmds[0]
            framework = framework or "CLI"
        depth = path.count("/") - (1 if path.startswith("src/") else 0)
        if name in ENTRY_FILENAMES and depth <= 2 and not any(not is_test_file(i) for i in graph.importers_of(path)):
            reasons.append(f"is named `{name}` and nothing in the repo imports it")
        if not reasons:
            continue
        trigger = trigger or f"python {path}"
        framework = framework or ("script" if (pf.module.has_main_guard or start) else "entry file")
        out[path] = AppEntry(path, framework, trigger, line, reasons)

    for path, entry in out.items():
        pf = graph.files[path]
        calls = sorted({c for stmt in _module_statements(pf) for c in _calls(stmt)})
        pseudo = Function(MODULE, MODULE, path, 1, max(1, len(pf.lines)), 1, calls=calls)
        key = f"{path}::{MODULE}"
        callees: set[str] = set()
        if graph.resolver is not None:
            for call in calls:
                callees.update(c for c in graph.resolver.callees(pf, pseudo, call) if c != key)
        # Decorated handlers (routes, commands, tools) in an app file are run by the app too.
        callees.update(
            f"{path}::{fn.qualname}" for fn in pf.module.functions if _is_handler(fn) and "." not in fn.qualname
        )
        # Kept off graph.edges and graph.entry_points: detectors read those as "who calls this function",
        # and a route handler is not called by code. hot_functions, the Map and the inspector use these.
        entry.calls = sorted(callees)
    graph.app_entries = out
    return out


def is_module_key(key: str) -> bool:
    return key.endswith("::" + MODULE)
