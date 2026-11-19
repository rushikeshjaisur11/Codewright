"""How to verify one finding: the steps, and an AI-written test run before and after the patch.

`steps` lists what would prove or disprove the finding for this code: the tests that reach the function
(with the command to run them), the API check, the risk and "when not to" conditions to check by hand,
a speed measurement for performance findings, and what to do when no test reaches the code.

`ai_test` closes that last gap: the configured model writes a pytest test for the function, which runs in
a sandbox copy of the repo, first on the code as it is (the test must pass there, or it is wrong), then
with the patch applied (it must still pass). Nothing in the repo changes unless the user saves the test.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from .graph import CodeGraph, is_test_file
from .models import Finding

AI_TEST_SYSTEM = (
    "You write one pytest test module that checks a single Python function's behaviour, so a code change to it "
    "can be verified. Test what the function returns, raises or calls for a few representative inputs, using "
    "only the standard library, pytest and what the code already imports; no network, no sleeping, no real "
    "files outside tmp_path; patch external services with unittest.mock. The test must pass on the code as it "
    "is today and keep passing after the suggested change, which must not change behaviour."
)


def _module_of(path: str) -> str:
    parts = path[:-3].split("/")
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _tests_for(f: Finding, graph: CodeGraph | None) -> list[str]:
    files = {t.split("::", 1)[0] for t in f.reached_by}
    if graph is not None and not files:
        files = {p for p in graph.importers_of(f.file) if is_test_file(p)}
    return sorted(files)


def steps(f: Finding, graph: CodeGraph | None = None) -> list[dict]:
    """What would prove this finding right or wrong here, in order. Each step: {kind, title, detail, command?}."""
    out: list[dict] = []
    fn = f.function.rsplit(".", 1)[-1]
    tests = _tests_for(f, graph)
    if f.patch:
        out.append(
            {
                "kind": "branch",
                "title": "Apply the patch on a fix branch",
                "detail": "Start a fix branch from the bar above, then Apply patch: the change stays separate "
                "until you commit it.",
            }
        )
    if tests:
        reached = "ran it during the last proof" if f.reached_by else "import its module"
        out.append(
            {
                "kind": "tests",
                "title": f"Run the {len(tests)} test file(s) that {reached}",
                "detail": "They must pass before and after the change. Run the tests on this does it in a "
                "sandbox copy.",
                "command": f"python -m pytest -q {' '.join(tests[:8])}",
            }
        )
    else:
        out.append(
            {
                "kind": "ai",
                "title": f"No test reaches `{fn}`: write one",
                "detail": "Call it with the inputs that matter and assert what it returns or raises today; "
                "then make the "
                "change and run it again. Let AI write the test does both in a sandbox.",
                "command": f"python -m pytest -q -k {fn}",
            }
        )
    if f.symbol_check != "ok" and f.symbol:
        out.append(
            {
                "kind": "manual",
                "title": f"Check `{f.symbol}` exists in your version",
                "detail": f"Checked against: {f.api}. In the repo's environment: "
                f'python -c "import {f.symbol.rsplit(".", 1)[0]}"',
            }
        )
    if f.category == "performance":
        out.append(
            {
                "kind": "measure",
                "title": "Measure it",
                "detail": "Measure speed times the function before and after the patch with the tests that "
                "reach it (best of 3).",
            }
        )
    if f.risk:
        out.append({"kind": "manual", "title": "Check the risk by hand", "detail": f.risk})
    if f.when_not:
        out.append({"kind": "manual", "title": "Make sure this is not a case to leave alone", "detail": f.when_not})
    out.append(
        {
            "kind": "ai",
            "title": "Or ask the AI",
            "detail": "Ask AI to check reads the suggestion against the code; Let AI write a test writes and runs one.",
        }
    )
    return out


def _key(f: Finding) -> str:
    return hashlib.sha1(f"{f.id}\n{f.patch or ''}\n{f.code_today}".encode()).hexdigest()


def _store(root: Path) -> Path:
    return root / ".codewright" / "ai-tests.json"


def load(root: str | Path) -> dict:
    try:
        data = json.loads(_store(Path(root)).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def attach(root: str | Path, findings: list[dict]) -> None:
    store = load(root)
    for f in findings:
        hit = store.get(f["id"])
        if (
            hit
            and hit.get("key")
            == hashlib.sha1(f"{f['id']}\n{f.get('patch') or ''}\n{f['code_today']}".encode()).hexdigest()
        ):
            f["ai_test"] = {k: v for k, v in hit.items() if k != "key"}


def ai_test(
    root: str | Path, f: Finding, graph: CodeGraph, cfg=None, python: str | None = None, timeout: int = 180
) -> dict:
    """Have the model write a test for the finding's function, then run it without and with the patch."""
    from . import proof, proof_env
    from .llm import LLMConfig, complete_json, last_error
    from .llm import status as llm_status
    from .upgradesafety import CODE_SCHEMA, _unfence, code_from_text, compiles

    root = Path(root).resolve()
    cfg = cfg or LLMConfig.load(root)
    if not llm_status(cfg)["available"]:
        raise RuntimeError(f"no model to write the test: {llm_status(cfg)['reason']}. Set one up in Settings")
    mod = _module_of(f.file)
    fn = f.function
    near = [t for t in _tests_for(f, graph)][:2]
    examples = ""
    for t in near:
        with contextlib.suppress(OSError):
            examples += f"\n# an existing test in this repo ({t}):\n" + (root / t).read_text()[:3000]
    ask = (
        f"Function `{fn}` in `{f.file}` (import it with `from {mod} import {fn.split('.')[0]}`).\n\n"
        f"Code today:\n```python\n{f.code_today[:8000]}\n```\n\n"
        f"Suggested change ({f.title}): {f.suggestion}\n"
        + (f"\nThe patch:\n```diff\n{f.patch[:6000]}\n```\n" if f.patch else "")
        + (f"\nFollow the style of the repo's tests:{examples}" if examples else "")
    )
    value = complete_json(
        cfg,
        AI_TEST_SYSTEM,
        [{"role": "user", "content": ask}],
        CODE_SCHEMA,
        max_tokens=2500,
        purpose="write a test",
        check=compiles("ai_test.py"),
        fallback=code_from_text,
    )
    if value is None:
        raise RuntimeError(last_error() or "the model did not answer")
    code = _unfence(str(value["code"])).strip() + "\n"
    tests_dir = "tests" if (root / "tests").is_dir() else ""
    name = f"test_codewright_{re.sub(r'[^a-z0-9]+', '_', fn.lower()).strip('_')}_{f.id}.py"
    rel = f"{tests_dir}/{name}" if tests_dir else name
    py = python or proof_env.resolve(root, None, build=True).python
    with tempfile.TemporaryDirectory(prefix="codewright-aitest-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(root, work, ignore=proof.IGNORE, symlinks=True)
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(code)
        before = proof.run_tests(work, py, timeout, selection=[rel])
        after = None
        if before.ok and f.patch:
            if proof.apply_patch(work, f.patch):
                after = proof.run_tests(work, py, timeout, selection=[rel])
            else:
                after = proof.TestRun(False, 0.0, "the patch did not apply to this copy", None, [rel])
    if not before.ok:
        verdict, detail = (
            "test-fails-today",
            "The AI's test fails on the code as it is, so it is not a valid check; try again or edit it.",
        )
    elif not f.patch:
        verdict, detail = "no-patch", "The test passes on today's code. Make the change by hand and run it again."
    elif after is not None and after.ok:
        verdict, detail = (
            "verified",
            "An AI-written test passes before and after the patch: the change keeps the behaviour it checks.",
        )
    else:
        verdict, detail = (
            "broke",
            "The AI's test passes today and fails with the patch: the change alters behaviour it checks.",
        )
    out = {
        "key": _key(f),
        "verdict": verdict,
        "detail": detail,
        "path": rel,
        "test": code,
        "before": {"ok": before.ok, "seconds": round(before.seconds, 2), "output": before.output[-2500:]},
        "after": None
        if after is None
        else {"ok": after.ok, "seconds": round(after.seconds, 2), "output": after.output[-2500:]},
        "python": py,
        "model": cfg.model,
        "at": time.time(),
    }
    store = load(root)
    store[f.id] = out
    from .feedback import ensure_dir

    ensure_dir(root)
    _store(root).write_text(json.dumps(store, indent=1))
    return {k: v for k, v in out.items() if k != "key"}


def save_test(root: str | Path, fid: str) -> str:
    """Write the AI's test for finding `fid` into the repo; returns its path."""
    root = Path(root).resolve()
    hit = load(root).get(fid)
    if not hit:
        raise FileNotFoundError(fid)
    p = (root / hit["path"]).resolve()
    if root not in p.parents:
        raise ValueError("test path outside the repository")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(hit["test"])
    return hit["path"]
