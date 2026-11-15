"""Package index: PyPI by default, your own Artifactory/Nexus/devpi when set, credentials never shown."""

import base64
import contextlib
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from codewright import index as idx
from codewright import stack, watcher

FILES = {
    "fastapi": [
        ("fastapi-0.120.0-py3-none-any.whl", "2025-10-01T10:00:00Z", ">=3.9", False),
        ("fastapi-0.121.0.tar.gz", "2025-11-01T10:00:00Z", ">=3.9", False),
        ("fastapi-0.121.0-py3-none-any.whl", "2025-11-01T09:00:00Z", ">=3.9", False),
        ("fastapi-0.122.0-py3-none-any.whl", "2025-12-01T10:00:00Z", ">=3.9", True),  # yanked
        ("fastapi-0.123.0b1-py3-none-any.whl", "2025-12-05T10:00:00Z", ">=3.9", False),  # pre-release
    ],
}
USER, PASSWORD, TOKEN = "dev", "s3cret", "tok-123"


class Handler(BaseHTTPRequestHandler):
    mode = "json"  # or "html"

    def log_message(self, *args):
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        ok = auth in (f"Basic {base64.b64encode(f'{USER}:{PASSWORD}'.encode()).decode()}", f"Bearer {TOKEN}")
        if not ok:
            self.send_response(401)
            self.end_headers()
            return
        name = self.path.strip("/").split("/")[-1]
        if self.path.startswith("/simple/") and name in FILES:
            files = FILES[name]
            if self.mode == "json":
                body = json.dumps(
                    {
                        "meta": {"api-version": "1.1"},
                        "name": name,
                        "files": [
                            {"filename": f, "url": f, "upload-time": t, "requires-python": rp, "yanked": y}
                            for f, t, rp, y in files
                        ],
                    }
                ).encode()
                ctype = "application/vnd.pypi.simple.v1+json"
            else:  # PEP 503 HTML, as older Artifactory/Nexus serve it: no upload times
                links = "".join(
                    f'<a href="../../files/{f}#sha256=00" data-requires-python="{rp.replace(">", "&gt;")}"'
                    f"{' data-yanked' if y else ''}>{f}</a><br/>"
                    for f, _, rp, y in files
                )
                body = f"<html><body>{links}</body></html>".encode()
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for var in (
        "CODEWRIGHT_INDEX_URL",
        "CODEWRIGHT_INDEX_TOKEN",
        "UV_DEFAULT_INDEX",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_CONFIG_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODEWRIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("NETRC", str(tmp_path / "netrc"))
    Handler.mode = "json"


# ---------------------------------------------------------------- where the index comes from


def test_pypi_is_the_default(tmp_path):
    current = idx.resolve(tmp_path)
    assert current.is_pypi and current.source == "PyPI (default)" and current.uv_env() == {}


def test_settings_win_in_order(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[[tool.uv.index]]\nname = "corp"\nurl = "https://nexus.example.com/repository/pypi/simple"\ndefault = true\n'
    )
    pip_conf = tmp_path / "pip.conf"
    pip_conf.write_text("[global]\nindex-url = https://pip.example.com/simple\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_conf))
    assert idx.resolve(repo).url == "https://nexus.example.com/repository/pypi/simple"
    assert idx.resolve(tmp_path).url == "https://pip.example.com/simple"  # no pyproject there: pip.conf
    monkeypatch.setenv("PIP_INDEX_URL", "https://env.example.com/simple")
    assert idx.resolve(repo).source == "PIP_INDEX_URL"
    idx.save("https://machine.example.com/simple")
    assert idx.resolve(repo).url == "https://machine.example.com/simple"
    idx.save("https://repo.example.com/simple", root=repo)
    assert idx.resolve(repo).source == "this repo's .codewright/config.json"
    monkeypatch.setenv("CODEWRIGHT_INDEX_URL", "https://override.example.com/simple")
    assert idx.resolve(repo).url == "https://override.example.com/simple"
    monkeypatch.delenv("CODEWRIGHT_INDEX_URL")
    idx.save("", root=repo)  # reset falls back to the next source
    assert idx.resolve(repo).url == "https://machine.example.com/simple"


def test_credentials_never_go_into_the_repo_and_are_never_shown(tmp_path):
    url = "https://dev:s3cret@artifactory.example.com/artifactory/api/pypi/pypi/simple"
    with pytest.raises(ValueError, match="committed"):
        idx.save(url, root=tmp_path)
    idx.save(url)
    path = idx.machine_config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    current = idx.resolve(tmp_path)
    assert current.display == "https://***@artifactory.example.com/artifactory/api/pypi/pypi/simple"
    assert "s3cret" not in current.base and current.headers()["Authorization"].startswith("Basic ")
    assert idx.validate("artifactory.example.com/simple")  # no scheme


# ---------------------------------------------------------------- reading the index


@pytest.mark.parametrize("mode", ["json", "html"])
def test_latest_final_release_from_a_simple_api(server, mode):
    Handler.mode = mode
    found = idx.latest_release(idx.PackageIndex(f"http://{USER}:{PASSWORD}@{server}/simple", "t"), "FastAPI")
    assert found["version"] == "0.121.0"  # 0.122.0 is yanked, 0.123.0b1 a pre-release
    assert found["requires_python"] == ">=3.9"
    assert found["released"] == ("2025-11-01" if mode == "json" else "")  # PEP 503 pages have no dates


def test_credentials_from_netrc_or_a_token(server, tmp_path, monkeypatch):
    index = idx.PackageIndex(f"http://{server}/simple", "t")
    assert not idx.check(index, "fastapi")["ok"]
    assert "authentication failed" in idx.check(index, "fastapi")["detail"]
    (tmp_path / "netrc").write_text(f"machine 127.0.0.1 login {USER} password {PASSWORD}\n")
    assert idx.check(index, "fastapi")["ok"]
    (tmp_path / "netrc").unlink()
    monkeypatch.setenv("CODEWRIGHT_INDEX_TOKEN", TOKEN)
    assert idx.check(index, "fastapi") == {"ok": True, "detail": f"fastapi 0.121.0 found on http://{server}/simple"}
    assert index.uv_env() == {"UV_DEFAULT_INDEX": f"http://token:{TOKEN}@{server}/simple"}


def test_missing_package_and_unreachable_index_are_explained(server):
    index = idx.PackageIndex(f"http://{USER}:{PASSWORD}@{server}/simple", "t")
    assert "not found on this index" in idx.check(index, "no-such-package")["detail"]
    assert "couldn't reach" in idx.check(idx.PackageIndex("http://127.0.0.1:9/simple", "t"))["detail"]


def test_releases_page_uses_the_configured_index_and_its_own_cache(server, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "fastapi"\nversion = "0.120.0"\n')
    monkeypatch.setenv("CODEWRIGHT_INDEX_URL", f"http://{USER}:{PASSWORD}@{server}/simple")
    (row,) = watcher.check(stack.discover(repo), repo)
    assert (row["latest"], row["bump"], row["released"]) == ("0.121.0", "minor", "2025-11-01")
    saved = json.loads((repo / ".codewright" / "releases.json").read_text())
    assert saved["_index"] == f"http://***@{server}/simple"  # no password on disk
    # Switching index ignores answers cached from the other one.
    assert watcher._load_cache(repo, idx.PackageIndex("https://pypi.org/simple", "")) == {}


def test_design_starter_installs_from_the_same_index_without_credentials(server, monkeypatch):
    import io
    import zipfile

    from codewright import design

    monkeypatch.setenv("CODEWRIGHT_INDEX_URL", f"http://{USER}:{PASSWORD}@{server}/simple")
    s = design.Session("t")
    proposal = design.propose(s, live_versions=False)
    assert proposal["index"] == f"http://{server}/simple/"
    z = zipfile.ZipFile(io.BytesIO(design.starter_repo(s, proposal, proposal["recommended"])))
    pyproject = next(z.read(n).decode() for n in z.namelist() if n.endswith("pyproject.toml"))
    assert f'url = "http://{server}/simple/"\ndefault = true' in pyproject and PASSWORD not in pyproject


def test_web_api_sets_tests_and_resets_the_index(server, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "fastapi"\nversion = "0.120.0"\n')
    c = TestClient(create_app([str(repo)]), base_url="http://localhost")
    wid = c.post("/api/workspaces", json={"path": str(repo)}).json()["id"]
    assert c.get(f"/api/w/{wid}/index").json()["is_pypi"] is True
    url = f"http://{USER}:{PASSWORD}@{server}/simple"
    assert c.post(f"/api/w/{wid}/index/test", json={"url": url}).json()["ok"] is True  # looks up `fastapi`
    assert c.post(f"/api/w/{wid}/index", json={"url": url, "scope": "repo"}).status_code == 400  # has a password
    state = c.post(f"/api/w/{wid}/index", json={"url": url, "scope": "machine"}).json()
    assert state["url"] == f"http://***@{server}/simple" and PASSWORD not in json.dumps(state)
    assert c.post(f"/api/w/{wid}/index", json={"url": "", "scope": "machine"}).json()["is_pypi"] is True


def test_proof_environment_builds_get_the_index(monkeypatch, tmp_path):
    from codewright import proof_env

    monkeypatch.setenv("CODEWRIGHT_INDEX_URL", "https://artifactory.example.com/api/pypi/pypi/simple")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, (kw.get("env") or {}).get("UV_DEFAULT_INDEX")))
        return proof_env.subprocess.CompletedProcess(cmd, 0, "", "")

    (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
    monkeypatch.setattr(proof_env.subprocess, "run", fake_run)
    monkeypatch.setattr(proof_env, "_has_pytest", lambda python: False)
    with contextlib.suppress(OSError):  # the fake commands build nothing to mark as done
        proof_env._build(Path(tmp_path), "uv")
    installs = [c for c in calls if c[0][:3] == ["uv", "pip", "install"]]
    assert [c[0][-1] for c in installs] == [str(tmp_path / "requirements.txt"), "pytest"]
    assert {c[1] for c in installs} == {"https://artifactory.example.com/api/pypi/pypi/simple"}  # pytest too
