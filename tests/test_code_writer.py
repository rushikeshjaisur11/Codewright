"""The AI code writer: structured output from every provider, answers checked and sent back with the reason,
and patches that apply to the file as it is on disk (no trailing newline, CRLF, methods, edits since the scan)."""

import json
import subprocess
import textwrap
from typing import ClassVar

import pytest

from codewright import llm, proof, upgradesafety
from codewright.engine import review
from codewright.refactor import unified


def _problem(file, function, line=1, detail="use proxy= instead of proxies="):
    return {
        "file": file,
        "function": function,
        "line": line,
        "kind": "call",
        "symbol": "httpx.Client",
        "detail": detail,
        "old_signature": None,
        "new_signature": None,
        "new_doc": "",
    }


@pytest.fixture
def model(monkeypatch):
    """A configured model; `model.replies` is what it answers, in turn, and `model.calls` what it was asked."""
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})

    class M:
        replies: ClassVar[list] = []
        calls: ClassVar[list] = []

    def complete(cfg, system, messages, max_tokens=1200, purpose="", schema=None):
        M.calls.append({"system": system, "messages": messages, "schema": schema, "max_tokens": max_tokens})
        reply = M.replies.pop(0)
        return json.dumps(reply) if isinstance(reply, dict) else reply

    monkeypatch.setattr(llm, "complete", complete)
    M.replies, M.calls = [], []
    return M


FETCH = textwrap.dedent("""\
    import httpx


    def fetch(url):
        with httpx.Client(proxies="http://p:1", timeout=3) as c:
            return c.get(url)
""")
FIXED = 'def fetch(url):\n    with httpx.Client(proxy="http://p:1", timeout=3) as c:\n        return c.get(url)\n'


def _scan(root, files):
    for name, text in files.items():
        (root / name).write_bytes(text.encode())
    return review(root, run_proof=False, top=None).graph


def test_the_code_writer_asks_for_a_schema_and_sends_a_broken_answer_back(tmp_path, model):
    graph = _scan(tmp_path, {"client.py": FETCH})
    model.replies = [
        {"explanation": "x", "code": "def fetch(url:\n    pass\n"},  # does not compile
        {"explanation": "Use proxy=.", "code": FIXED},
    ]
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("client.py", "fetch")])
    first, second = model.calls
    assert first["schema"] is upgradesafety.REWRITE_SCHEMA and '"code"' in first["system"]
    assert "can't be used: the file would not compile" in second["messages"][-1]["content"]
    [p] = out["patches"]
    assert p["explanation"] == "Use proxy=." and out["skipped"] == []
    assert proof.apply_patch(tmp_path, p["patch"]) and 'proxy="http://p:1"' in (tmp_path / "client.py").read_text()


def test_a_method_written_without_its_class_indentation_is_fitted_back(tmp_path, model):
    src = 'import httpx\n\n\nclass Api:\n    def fetch(self, url):\n        return httpx.Client(proxies="p").get(url)\n'
    graph = _scan(tmp_path, {"api.py": src})
    model.replies = [{"explanation": "e", "code": 'def fetch(self, url):\n    return httpx.Client(proxy="p").get(url)'}]
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("api.py", "Api.fetch")])
    [p] = out["patches"]
    assert proof.apply_patch(tmp_path, p["patch"])
    assert (tmp_path / "api.py").read_text() == src.replace("proxies=", "proxy=")


@pytest.mark.parametrize("newline,last", [("\n", ""), ("\r\n", "\r\n"), ("\n", "\n")])
def test_patches_apply_with_crlf_and_without_a_final_newline(tmp_path, model, newline, last):
    src = FETCH.rstrip("\n").replace("\n", newline) + last
    graph = _scan(tmp_path, {"client.py": src})
    model.replies = ["Use proxy=.\n```py\n" + FIXED + "```"]  # a model that ignored the schema: still read
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("client.py", "fetch")])
    [p] = out["patches"]
    assert proof.patch_error(tmp_path, p["patch"]) == ""
    want = src.replace("proxies=", "proxy=")
    assert (tmp_path / "client.py").read_bytes().decode() == want


def test_code_with_parts_left_out_is_refused(tmp_path, model):
    graph = _scan(tmp_path, {"client.py": FETCH})
    elided = {"explanation": "e", "code": "def fetch(url):\n    # rest of the function unchanged\n    ...\n"}
    model.replies = [elided, elided]
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("client.py", "fetch")])
    assert out["patches"] == [] and "leaves parts out" in out["skipped"][0]["why"]


def test_an_answer_that_drops_the_function_is_refused(tmp_path, model):
    graph = _scan(tmp_path, {"client.py": FETCH})
    model.replies = [{"explanation": "e", "code": "x = 1\n"}, {"explanation": "e", "code": FIXED}]
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("client.py", "fetch")])
    assert "no longer defines `fetch`" in model.calls[1]["messages"][-1]["content"] and len(out["patches"]) == 1


def test_a_file_edited_since_the_scan_is_skipped_with_the_reason(tmp_path, model):
    graph = _scan(tmp_path, {"client.py": FETCH})
    (tmp_path / "client.py").write_text(FETCH.replace("timeout=3", "timeout=9"))
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("client.py", "fetch")])
    assert out["patches"] == [] and "changed since the scan" in out["skipped"][0]["why"] and model.calls == []


def test_an_edit_elsewhere_in_the_file_does_not_stop_the_rewrite(tmp_path, model):
    graph = _scan(tmp_path, {"client.py": FETCH})
    (tmp_path / "client.py").write_bytes(("# a new header\n" + FETCH).encode())
    model.replies = [{"explanation": "e", "code": FIXED}]
    out = upgradesafety.rewrite(tmp_path, graph, "httpx", "0.27", "0.28", [_problem("client.py", "fetch")])
    [p] = out["patches"]
    assert proof.apply_patch(tmp_path, p["patch"])
    assert (tmp_path / "client.py").read_text() == "# a new header\n" + FETCH.replace("proxies=", "proxy=")


# ---------------------------------------------------------------- structured output per provider


SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
    "additionalProperties": False,
}


@pytest.fixture
def server(monkeypatch):
    """The HTTP layer: `server.answers` are returned (or raised) in turn; `server.bodies` holds the requests."""

    class S:
        answers: ClassVar[list] = []
        bodies: ClassVar[list] = []

    def post(url, body, headers, who, timeout):
        S.bodies.append(json.loads(json.dumps(body)))
        a = S.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(llm, "_post", post)
    S.answers, S.bodies = [], []
    llm._CAPS.clear()
    return S


def test_claude_answers_through_a_forced_tool_call(server):
    server.answers = [{"content": [{"type": "tool_use", "name": "answer", "input": {"code": "x = 1"}}]}]
    cfg = llm.LLMConfig(model="claude-haiku-5-5", provider="anthropic", api_key="k")
    assert llm.complete_json(cfg, "s", [{"role": "user", "content": "hi"}], SCHEMA) == {"code": "x = 1"}
    body = server.bodies[0]
    assert body["tools"][0]["input_schema"] == SCHEMA and body["tool_choice"] == {"type": "tool", "name": "answer"}


def test_an_openai_compatible_server_without_json_schema_falls_back_once(server):
    ok = {"choices": [{"message": {"content": '{"code": "y = 2"}'}, "finish_reason": "stop"}]}
    server.answers = [
        llm.LLMError("the server answered HTTP 400: Unsupported parameter: 'max_tokens'; use 'max_completion_tokens'"),
        llm.LLMError("the server answered HTTP 400: response_format json_schema is not supported"),
        ok,
        ok,
    ]
    cfg = llm.LLMConfig(model="m", provider="openai", api_key="k")
    assert llm.complete_json(cfg, "s", [{"role": "user", "content": "hi"}], SCHEMA) == {"code": "y = 2"}
    assert server.bodies[0]["response_format"]["type"] == "json_schema" and "max_tokens" in server.bodies[0]
    assert "max_completion_tokens" in server.bodies[1]
    assert server.bodies[2]["response_format"] == {"type": "json_object"}
    llm.complete_json(cfg, "s", [{"role": "user", "content": "hi"}], SCHEMA)  # remembered: one request
    assert len(server.bodies) == 4 and server.bodies[3]["response_format"] == {"type": "json_object"}


def test_gemini_gets_a_response_schema_it_accepts(server):
    server.answers = [{"candidates": [{"content": {"parts": [{"text": '{"code": "z"}'}]}, "finishReason": "STOP"}]}]
    cfg = llm.LLMConfig(model="gemini-2.5-flash", provider="gemini", api_key="k")
    assert llm.complete_json(cfg, "s", [{"role": "user", "content": "hi"}], SCHEMA) == {"code": "z"}
    gen = server.bodies[0]["generationConfig"]
    assert gen["responseMimeType"] == "application/json" and "additionalProperties" not in gen["responseSchema"]


def test_an_answer_cut_off_at_the_limit_is_asked_again_with_more_room(server):
    cut = {"choices": [{"message": {"content": '{"code": "def f(:'}, "finish_reason": "length"}]}
    ok = {"choices": [{"message": {"content": '{"code": "def f(): pass"}'}, "finish_reason": "stop"}]}
    server.answers = [cut, ok]
    cfg = llm.LLMConfig(model="m", provider="openai", api_key="k")
    out = llm.complete_json(cfg, "s", [{"role": "user", "content": "hi"}], SCHEMA, max_tokens=1000)
    assert out == {"code": "def f(): pass"}
    assert [b["max_tokens"] for b in server.bodies] == [1000, 2000]


def test_parse_json_reads_prose_fences_and_bare_lists():
    wants_list = {"type": "object", "properties": {"files": {"type": "array"}}, "required": ["files"]}
    assert llm.parse_json('Sure:\n[{"file": "a.py"}]', wants_list) == {"files": [{"file": "a.py"}]}
    assert llm.parse_json('```json\n{"code": "x"}\n```', SCHEMA) == {"code": "x"}
    assert llm.parse_json("no json here", SCHEMA) is None
    assert llm.validate({"code": 3}, SCHEMA) == "the answer.code should be string, not int"
    assert llm.validate({}, SCHEMA) == "the answer is missing 'code'"


# ---------------------------------------------------------------- diffs and applying them


def test_a_diff_of_a_file_without_a_final_newline_applies(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1")
    patch = unified(["def f():\n", "    return 1"], ["def f():\n", "    return 2"], "a/a.py", "b/a.py")
    assert "\\ No newline at end of file" in patch
    assert proof.patch_error(tmp_path, patch) == ""
    assert (tmp_path / "a.py").read_text() == "def f():\n    return 2"


def test_a_patch_that_does_not_apply_says_why_and_changes_nothing(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n one\n-TWO\n+2\n three\n@@ -1,1 +1,1 @@\n-one\n+1\n"
    err = proof.patch_error(tmp_path, patch)
    assert err and (tmp_path / "a.py").read_text() == "one\ntwo\nthree\n"
    assert not list(tmp_path.glob("*.rej")) and not list(tmp_path.glob("*.orig"))


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_a_patch_keeps_each_files_line_endings(tmp_path, eol):
    raw = b"def f():" + eol + b"    return 1" + eol
    (tmp_path / "a.py").write_bytes(raw)
    patch = unified(["def f():\n", "    return 1\n"], ["def f():\n", "    return 2\n"], "a/a.py", "b/a.py")
    assert proof.patch_error(tmp_path, patch) == ""
    assert (tmp_path / "a.py").read_bytes() == raw.replace(b"1", b"2")
    assert proof.patch_error(tmp_path, patch, reverse=True) == "" and (tmp_path / "a.py").read_bytes() == raw


def test_a_probe_that_stubs_record_or_never_calls_it_is_sent_back():
    stub = "from app import f\n\ndef record(name, fn):\n    pass\n\nrecord('f: one', lambda: f(1))\n"
    assert "defines `record`" in upgradesafety.probe_check({"code": stub})
    assert "never calls `record" in upgradesafety.probe_check({"code": "from app import f\nf(1)\n"})
    assert upgradesafety.probe_check({"code": "from app import f\nrecord('f: one', lambda: f(1))\n"}) is None


def test_breaking_changes_shows_the_model_the_lines_that_use_the_library(tmp_path, model):
    from codewright import upgrade

    graph = _scan(tmp_path, {"client.py": FETCH})
    model.replies = [{"changes": []}]
    notes = {"notes": [{"version": "0.28", "body": "Removed the `proxies` argument."}]}
    upgrade.breaking_changes(tmp_path, graph, "httpx", "0.27", "0.28", notes)
    assert 'httpx.Client(proxies="http://p:1", timeout=3)' in model.calls[0]["messages"][0]["content"]
