"""App entry detection: files that start the app, not only decorated routes and `main()`."""

from pathlib import Path

from codewright import graph
from codewright.archmap import ArchMap

FASTAPI_APP = "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/')\ndef root():\n    return 1\n"


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return root


def entries(root: Path) -> dict:
    return graph.build(root).app_entries


def test_streamlit_app_py_with_no_functions(tmp_path):
    write(
        tmp_path,
        {
            "app.py": "import streamlit as st\nfrom ui.charts import draw\n\nst.title('Sales')\ndraw(st)\n",
            "ui/__init__.py": "",
            "ui/charts.py": "def draw(st):\n    st.line_chart([1, 2])\n",
            "ui/unused.py": "def old():\n    return 1\n",
        },
    )
    e = entries(tmp_path)["app.py"]
    assert (e.framework, e.trigger) == ("Streamlit", "streamlit run app.py")
    assert e.calls == ["ui/charts.py::draw"]
    m = ArchMap(graph.build(tmp_path))
    assert m.entry_points()[0]["trigger"] == "streamlit run app.py"
    assert {u["file"] for u in m.unused} == {"ui/unused.py"}  # charts.py is reached from app.py
    flow = m.flow("app.py::<module>")
    assert {n.label for n in flow.nodes} == {"streamlit run app.py", "draw"}


def test_flask_app_run_without_main_guard(tmp_path):
    write(
        tmp_path,
        {
            "web/app.py": "from flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/')\ndef home():\n"
            "    return 'hi'\n\napp.run(port=5000)\n"
        },
    )
    e = entries(tmp_path)["web/app.py"]
    assert e.framework == "Flask" and e.trigger == "flask --app web.app:app run" and "web/app.py::home" in e.calls
    assert any("starts at module level" in r for r in e.reasons)


def test_main_guard_calling_something_other_than_main(tmp_path):
    write(
        tmp_path,
        {
            "server.py": "from core import start\n\nif __name__ == '__main__':\n    start()\n",
            "core.py": "def start():\n    return 1\n",
        },
    )
    g = graph.build(tmp_path)
    assert g.app_entries["server.py"].calls == ["core.py::start"]
    assert "core.py::start" in g.hot_functions()
    assert not ArchMap(g).unused


def test_uvicorn_fastapi_gradio_mcp_and_hidden_main(tmp_path):
    write(
        tmp_path,
        {
            "svc/api.py": "from fastapi import FastAPI\napi = FastAPI()\n",
            "demo.py": "import gradio as gr\n\ndef reply(x):\n    return x\n\n"
            "demo = gr.Interface(fn=reply, inputs='text', outputs='text')\n"
            "demo.launch()\n",
            "tools_server.py": "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('x')\n\n"
            "@mcp.tool()\ndef add(a: int, b: int) -> int:\n"
            "    return a + b\n",
            "cli.py": "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n",
        },
    )
    g = graph.build(tmp_path)
    e = g.app_entries
    assert e["svc/api.py"].trigger == "uvicorn svc.api:api"
    assert e["demo.py"].framework == "Gradio"
    assert e["tools_server.py"].framework == "MCP server" and "tools_server.py::add" in g.entry_points
    eps = ArchMap(g).entry_points()
    assert [x["id"] for x in eps if x["file"] == "cli.py"] == ["cli.py::<module>"]  # main folded into its file


def test_launch_files_name_the_script(tmp_path):
    write(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/dash_v2.py": "def build():\n    return 1\n",
            "src/pkg/api.py": FASTAPI_APP,
            "Dockerfile": 'CMD ["python", "src/pkg/dash_v2.py"]\n',
        },
    )
    assert not ArchMap(graph.build(tmp_path)).unused


def test_an_unimported_app_py_alone_does_not_hide_a_library(tmp_path):
    write(
        tmp_path,
        {
            "lib/__init__.py": "",
            "lib/core.py": "def f():\n    return 1\n",
            "lib/extra.py": "def g():\n    return 2\n",
            "app.py": "def helper():\n    return 3\n",  # named like an entry, but no app and nothing runs it
        },
    )
    g = graph.build(tmp_path)
    assert "app.py" in g.app_entries
    assert not ArchMap(g).unused  # a library: consumers may import any module


def test_a_route_handler_is_not_counted_as_called_by_code(tmp_path):
    write(
        tmp_path,
        {"app.py": FASTAPI_APP},
    )
    g = graph.build(tmp_path)
    assert not any("app.py::root" in callees for callees in g.edges.values())
    assert "app.py::root" in g.app_entries["app.py"].calls


def test_console_scripts_name_the_command(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n[project.scripts]\nmytool = "pkg.tool:app"\n',
            "pkg/__init__.py": "",
            "pkg/tool.py": "def app():\n    return 1\n",
        },
    )
    e = entries(tmp_path)["pkg/tool.py"]
    assert (e.framework, e.trigger) == ("CLI", "mytool")
