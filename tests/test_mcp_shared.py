"""The shared MCP server: one Codewright for a team over Streamable HTTP, behind tokens.

It must only reach what it serves (its --root repos, git URLs on allowed hosts, files sent
inline), never run a repository's code unless allowed, and answer a real MCP client over HTTP.
"""

import asyncio
import json
import socket
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codewright import mcp_server, remote
from codewright.remote import AccessError, Policy

MODELS = textwrap.dedent(
    """
    from pydantic import BaseModel, Field

    class Order(BaseModel):
        qty: int = Field(gt=0)
        name: str = Field(...)
    """
).lstrip()
LOCK = 'version = 1\n\n[[package]]\nname = "pydantic"\nversion = "2.14.3"\n'


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "models.py").write_text(MODELS)
    (path / "uv.lock").write_text(LOCK)
    return path


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEWRIGHT_HOME", str(tmp_path / "home"))
    policy = Policy(shared=True, roots=[repo(tmp_path / "shop").resolve()], allow_proof=False, cache=tmp_path / "cache")
    monkeypatch.setattr(mcp_server, "POLICY", policy)
    return policy


# ---------------------------------------------------------------- what a caller may reach


def test_shared_server_serves_only_its_repos(shared, tmp_path):
    assert shared.resolve("shop")[0] == tmp_path / "shop"
    (tmp_path / "shop" / "sub").mkdir()
    assert shared.resolve(str(tmp_path / "shop" / "sub"))[0] == tmp_path / "shop" / "sub"
    for outside in (str(tmp_path), "/etc", str(tmp_path / "shop" / ".." / "home"), "other"):
        with pytest.raises(AccessError, match="isn't served here"):
            shared.resolve(outside)


@pytest.mark.parametrize(
    "url,error",
    [
        ("http://github.com/org/repo", "only https"),
        ("file:///etc", "only https"),
        ("ext::sh -c touch% /tmp/pwned", "only https"),
        ("git@github.com:org/repo.git", "only https"),
        ("https://internal.corp/org/repo", "isn't allowed"),
        ("https://user:pw@github.com/org/repo", "credentials"),
        ("https://github.com/org", "owner/repo"),
        ("https://github.com/org/repo#--upload-pack=x", "branch or tag"),
        ("https://github.com/org/repo#a..b", "branch or tag"),
    ],
)
def test_git_urls_are_https_on_allowed_hosts_only(url, error):
    with pytest.raises(AccessError, match=error):
        remote.parse_git_url(url, remote.DEFAULT_GIT_HOSTS) if url.startswith("https") else Policy(shared=True).resolve(
            url
        )


def test_git_url_with_a_branch_is_parsed():
    assert remote.parse_git_url("https://GitHub.com/org/repo.git#release/2.1", ("github.com",)) == (
        "https://github.com/org/repo.git",
        "release/2.1",
    )


def test_tool_errors_tell_the_agent_what_it_can_use(shared):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match=r"`/etc` isn't served here.*`shop`.*github\.com.*review_files"):
        mcp_server.review("/etc")
    with pytest.raises(ToolError, match="proof is off on this shared server"):
        mcp_server.review("shop", proof=True)
    assert mcp_server.list_repos()["repos"] == [{"name": "shop"}]  # no server paths


def test_review_by_name_and_inline_files(shared, tmp_path):
    by_name = mcp_server.review("shop")
    assert any(f["card_id"] == "pydantic.annotated-field" for f in by_name["findings"])
    before = set(Path(tempfile_dir()).glob("codewright-files-*"))
    inline = mcp_server.review_files({"app/models.py": MODELS, "uv.lock": LOCK})
    f = next(f for f in inline["findings"] if f["card_id"] == "pydantic.annotated-field")
    assert f["file"] == "app/models.py" and "Annotated[int, Field(gt=0)]" in f["patch"]
    assert set(Path(tempfile_dir()).glob("codewright-files-*")) == before  # nothing left behind


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()


@pytest.mark.parametrize("name", ["../x.py", "/etc/passwd", "a/../../x.py", ".git/config", ""])
def test_inline_files_cannot_escape_their_folder(shared, name):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        mcp_server.review_files({name: "x = 1\n"})


# ---------------------------------------------------------------- git clone and refresh


def test_git_repos_are_cloned_refreshed_and_reviewed_fresh(shared, tmp_path, monkeypatch):
    upstream = repo(tmp_path / "upstream")
    run = lambda *a: subprocess.run(["git", *a], cwd=upstream, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    real_git = remote._git

    def local_git(args, cwd=None):  # the network part, pointed at a local repository
        args = [str(upstream) if a == "https://github.com/acme/shop" else a for a in args]
        subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, check=True, capture_output=True)

    monkeypatch.setattr(remote, "_git", local_git)
    first = mcp_server.review("https://github.com/acme/shop#main")
    assert any(f["card_id"] == "pydantic.annotated-field" for f in first["findings"])
    dest, changed = shared.resolve("https://github.com/acme/shop#main")
    assert not changed and (dest / "models.py").exists()  # fresh enough: no fetch
    (upstream / "models.py").write_text("x = 1\n")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two")
    marker = dest / ".git" / "codewright-fetched"
    old = time.time() - remote.REFRESH_AFTER - 1
    import os

    os.utime(marker, (old, old))
    assert mcp_server.review("https://github.com/acme/shop#main")["findings"] == []  # new commit, new review
    assert remote._git is not real_git


# ---------------------------------------------------------------- over HTTP, as an agent connects


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(shared):
    import uvicorn

    port = free_port()
    app = mcp_server.http_app(["t" * 32], "127.0.0.1")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def test_tokens_guard_everything_but_the_health_check(http_server):
    import httpx

    assert httpx.get(f"{http_server}/healthz").json() == {"ok": True}
    r = httpx.post(f"{http_server}/mcp", json={})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert httpx.post(f"{http_server}/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    rebinding = httpx.post(
        f"{http_server}/mcp", json={}, headers={"Authorization": "Bearer " + "t" * 32, "Host": "evil.example"}
    )
    assert rebinding.status_code in (403, 421)  # a browser page can't reach a local server by DNS rebinding


def test_an_agent_reviews_over_streamable_http(http_server):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def session():
        client = create_mcp_http_client(headers={"Authorization": "Bearer " + "t" * 32})
        async with (
            client,
            streamable_http_client(f"{http_server}/mcp", http_client=client) as streams,
            ClientSession(streams[0], streams[1]) as s,
        ):
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            repos = await s.call_tool("list_repos", {})
            found = await s.call_tool("review_files", {"files": {"m.py": MODELS, "uv.lock": LOCK}})
            return tools, repos, found

    tools, repos, found = asyncio.run(session())
    assert {"review", "review_files", "list_repos", "search_knowledge", "architecture_map"} <= tools
    assert json.loads(repos.content[0].text)["repos"] == [{"name": "shop"}]
    assert "pydantic.annotated-field" in found.content[0].text


# ---------------------------------------------------------------- CLI


def test_cli_refuses_a_public_server_without_a_token():
    from codewright.cli import app

    r = CliRunner().invoke(app, ["mcp", "--http", "--host", "0.0.0.0"])
    assert r.exit_code == 2 and "without a token" in r.output
    r = CliRunner().invoke(app, ["mcp", "--http", "--token", "short"])
    assert r.exit_code == 2 and "16 characters" in r.output


def test_cli_prints_client_setup():
    from codewright.cli import app

    out = CliRunner().invoke(app, ["mcp", "config", "--url", "https://cw.example.com"]).output
    assert "claude mcp add --transport http codewright https://cw.example.com/mcp" in out
    assert '"Authorization": "Bearer ${env:CODEWRIGHT_TOKEN}"' in out and '"type": "http"' in out
    local = CliRunner().invoke(app, ["mcp", "config"]).output
    assert "uvx --from git+https://github.com/rushikeshjaisur11/codewright codewright mcp" in local
    assert len(CliRunner().invoke(app, ["mcp", "token"]).output.strip()) >= 40
