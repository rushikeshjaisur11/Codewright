"""Hidden links: git change coupling, stale copies of globals, split settings, copied constants, call order,
state set from outside, copied logic. Each must be found where it is, and stay quiet where the code shows the link."""

import subprocess
from pathlib import Path

from codewright import cochange, graph, links, untangle


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src.lstrip("\n"))
    return root


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def kinds(items, kind: str) -> list:
    return [i for i in items if i.kind == kind]


def test_files_that_change_together_without_a_link(tmp_path):
    root = write(
        tmp_path,
        {
            "api/routes.py": "def list_users():\n    return []\n",
            "web/src/client.ts": "export const listUsers = () => fetch('/users');\n",
            "api/service.py": "from api.store import load\n\ndef run():\n    return load()\n",
            "api/store.py": "def load():\n    return 1\n",
        },
    )
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "start")
    for n in range(6):
        # the route and its client always move together; service and store too, but service imports store
        (root / "api/routes.py").write_text(f"def list_users(page={n}):\n    return []\n")
        (root / "web/src/client.ts").write_text(f"export const listUsers = (p = {n}) => fetch('/users');\n")
        git(root, "commit", "-qam", f"users page {n}")
        (root / "api/service.py").write_text(f"from api.store import load\n\ndef run():\n    return load() + {n}\n")
        (root / "api/store.py").write_text(f"def load():\n    return {n}\n")
        git(root, "commit", "-qam", f"store {n}")
    items, note = links.hidden_links(graph.build(root))
    co = kinds(items, "co-change")
    assert note == ""
    assert [(i.file, i.data["other"]) for i in co] == [("api/routes.py", "web/src/client.ts")]
    assert co[0].confidence == "high" and "API contract written twice" in co[0].why
    assert co[0].data["shared"] == 7 and co[0].data["subjects"]  # the first commit added both


def test_big_sweeps_and_shallow_history_are_handled(tmp_path):
    commits = [(1, "reformat everything", [f"m{i}.py" for i in range(40)])] * 10
    assert cochange.coupled(commits, lambda p: True) == []  # sweeps say nothing about coupling
    items, note = links.hidden_links(graph.build(write(tmp_path, {"a.py": "X = 1\n"})))
    assert note == "not a git repository" and not kinds(items, "co-change")


CODE = {
    "app/__init__.py": "",
    "app/config.py": """
import os

client = None
TIMEOUT_SECONDS = 30
QUEUE_NAME = "jobs-high"
HANDLERS = {}
DEBUG = False


def setup():
    global client
    client = object()


def get_client():
    if client is None:
        setup()
    return client
""",
    "app/worker.py": """
import os
from app.config import client
from app import config

TIMEOUT_SECONDS = 60
QUEUE_NAME = "jobs-high"
RETRY_DELAY = os.getenv("RETRY_DELAY", "5")


def run():
    config.DEBUG = True
    config.HANDLERS["run"] = run
    return client
""",
    "app/api.py": """
import os
from app import config

RETRY_DELAY = os.environ.get("RETRY_DELAY", "10")
REGION = os.getenv("REGION")


def answer():
    return config.client.ping()
""",
    "app/jobs.py": """
import os

_pool = None
REGION = os.getenv("REGION", "")


def init_pool():
    global _pool
    _pool = object()


def submit(job):
    return _pool.submit(job)


def safe_submit(job):
    return _pool.submit(job) if _pool else None
""",
    "tests/test_x.py": "import os\nREGION = os.getenv('REGION', 'eu')\nTIMEOUT_SECONDS = 1\n",
}


def test_stale_copy_of_a_reassigned_global(tmp_path):
    items, _ = links.hidden_links(graph.build(write(tmp_path, CODE)), git=False)
    stale = kinds(items, "stale-global")
    assert [(i.file, i.data["global"]) for i in stale] == [("app/worker.py", "client")]  # api reads config.client
    assert "setup" in stale[0].why and stale[0].confidence == "high"


def test_settings_read_in_several_places(tmp_path):
    items, _ = links.hidden_links(graph.build(write(tmp_path, CODE)), git=False)
    by = {i.name: i for i in kinds(items, "split-setting")}
    assert by["RETRY_DELAY"].confidence == "high" and "different defaults" in by["RETRY_DELAY"].title
    assert by["REGION"].confidence == "medium"  # unset and "" are the same default; the test file doesn't count


def test_copied_constants(tmp_path):
    items, _ = links.hidden_links(graph.build(write(tmp_path, CODE)), git=False)
    by = {i.name: i for i in kinds(items, "copied-constant")}
    assert by["QUEUE_NAME"].confidence == "high"  # the same key in two places: a sure copy
    assert by["TIMEOUT_SECONDS"].confidence == "medium" and "different values" in by["TIMEOUT_SECONDS"].title


def test_call_order_only_without_a_check(tmp_path):
    items, _ = links.hidden_links(graph.build(write(tmp_path, CODE)), git=False)
    order = kinds(items, "call-order")
    assert [(i.file, i.data["global"]) for i in order] == [("app/jobs.py", "_pool")]
    assert "`submit` works only after `init_pool`" in order[0].title  # not safe_submit, not get_client


def test_state_set_from_another_module(tmp_path):
    items, _ = links.hidden_links(graph.build(write(tmp_path, CODE)), git=False)
    out = {(i.data["name"], i.data["how"]) for i in kinds(items, "outside-state")}
    assert out == {("DEBUG", "set"), ("HANDLERS", "item")}


def test_copied_logic_is_grouped_by_files(tmp_path):
    body = "\n".join(f"    x{n} = x{n - 1} + {n}" if n else "    x0 = a" for n in range(10))
    times = body.replace("+", "*")
    root = write(
        tmp_path,
        {
            "app/a.py": f"def one(a):\n{body}\n    return x9\n\n\ndef two(a):\n{times}\n    return x9\n",
            "app/b.py": f"def one_b(a):\n{body}\n    return x9\n\n\ndef two_b(a):\n{times}\n    return x9\n",
        },
    )
    items, _ = links.hidden_links(graph.build(root), git=False)
    copied = kinds(items, "copied-logic")
    assert len(copied) == 1 and "2 functions are copied between" in copied[0].title


def test_inventory_groups_and_agent_task(tmp_path):
    inv = untangle.inventory(graph.build(write(tmp_path, CODE)))
    assert inv["groups"]["stale-global"] == "links" and inv["groups"]["unused"] == "surplus"
    assert inv["links"] >= 5 and inv["history"] == "not a git repository"
    picked = [i for i in inv["items"] if inv["groups"][i["kind"]] == "links"]
    _task, context = untangle.agent_task(picked)
    assert "## Stale copies of a global" in context and "## Settings read in several places" in context
