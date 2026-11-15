"""The package index Codewright asks about releases and builds environments from.

PyPI by default. Set your own (Artifactory, Nexus, devpi, CodeArtifact, GitLab...) as the
*Simple API* URL your pip or uv already uses, e.g.
``https://artifactory.example.com/artifactory/api/pypi/pypi-virtual/simple``.

Where the index comes from, first match wins:

1. ``CODEWRIGHT_INDEX_URL`` in the environment;
2. this repo's ``.codewright/config.json`` (``index_url``; shared through git, so no credentials);
3. this machine's ``~/.codewright/config.json`` (``index_url``);
4. ``UV_DEFAULT_INDEX``, ``UV_INDEX_URL`` or ``PIP_INDEX_URL``;
5. the repo's ``pyproject.toml``: ``[[tool.uv.index]]`` with ``default = true``, or ``[tool.uv] index-url``;
6. the user's ``uv.toml``, then ``pip.conf``;
7. PyPI.

Credentials: in the URL (``https://user:token@host/...``), in ``~/.netrc`` for the host (pip and
uv read it too), or ``CODEWRIGHT_INDEX_TOKEN`` (sent as a bearer token). They are never shown:
everything user-facing uses ``display``.
"""

from __future__ import annotations

import base64
import configparser
import json
import netrc
import os
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, parse_sdist_filename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

PYPI = "https://pypi.org/simple"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json, application/vnd.pypi.simple.v1+html;q=0.2, text/html;q=0.01"


@dataclass(frozen=True)
class PackageIndex:
    url: str  # as configured, possibly with credentials in it
    source: str  # where it was set, for the UI: "PyPI (default)", "CODEWRIGHT_INDEX_URL", ...

    @property
    def is_pypi(self) -> bool:
        parts = urlsplit(self.url)
        return parts.hostname in ("pypi.org", "pypi.python.org") and parts.path.rstrip("/") in ("/simple", "")

    @property
    def display(self) -> str:
        return redact(self.url)

    @property
    def base(self) -> str:
        """The URL without credentials, ending in a slash."""
        p = urlsplit(self.url)
        host = p.hostname or ""
        netloc = host + (f":{p.port}" if p.port else "")
        return urlunsplit((p.scheme, netloc, p.path.rstrip("/") + "/", "", ""))

    def headers(self) -> dict[str, str]:
        out = {"User-Agent": "codewright"}
        p = urlsplit(self.url)
        if p.username:
            pair = f"{urllib.request.unquote(p.username)}:{urllib.request.unquote(p.password or '')}"
            out["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        elif os.environ.get("CODEWRIGHT_INDEX_TOKEN"):
            out["Authorization"] = f"Bearer {os.environ['CODEWRIGHT_INDEX_TOKEN']}"
        else:
            login = _netrc(p.hostname)
            if login:
                out["Authorization"] = "Basic " + base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()
        return out

    def uv_env(self) -> dict[str, str]:
        """Environment for `uv` so environment builds use this index too (PyPI: nothing to set)."""
        if self.is_pypi:
            return {}
        url = self.url
        token = os.environ.get("CODEWRIGHT_INDEX_TOKEN")
        p = urlsplit(url)
        if token and not p.username:  # uv takes credentials in the URL or from netrc
            netloc = f"token:{urllib.request.quote(token, safe='')}@{p.netloc}"
            url = urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
        return {"UV_DEFAULT_INDEX": url}


def redact(url: str) -> str:
    p = urlsplit(url)
    if not (p.username or p.password):
        return url
    host = (p.hostname or "") + (f":{p.port}" if p.port else "")
    return urlunsplit((p.scheme, f"***@{host}", p.path, p.query, p.fragment))


def has_secret(url: str) -> bool:
    return bool(urlsplit(url).password)


def validate(url: str) -> str | None:
    """Why `url` can't be an index, or None."""
    p = urlsplit(url.strip())
    if p.scheme not in ("https", "http") or not p.hostname:
        return "use the index's full http(s) URL, e.g. https://artifactory.example.com/artifactory/api/pypi/pypi/simple"
    return None


def _netrc(host: str | None) -> tuple[str, str] | None:
    if not host:
        return None
    try:
        auth = netrc.netrc(os.environ.get("NETRC") or None).authenticators(host)
    except (FileNotFoundError, netrc.NetrcParseError, OSError):
        return None
    return (auth[0] or "", auth[2] or "") if auth else None


# --------------------------------------------------------------------------- configuration


def machine_config_path() -> Path:
    return Path(os.environ.get("CODEWRIGHT_HOME", "~/.codewright")).expanduser() / "config.json"


def repo_config_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "config.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(url: str, *, root: str | Path | None = None) -> None:
    """Set (or with "" clear) the index for one repo, or for this machine when `root` is None."""
    url = url.strip()
    if url:
        err = validate(url)
        if err:
            raise ValueError(err)
        if root is not None and has_secret(url):
            raise ValueError(
                "this URL has a password in it and the repo setting is committed with the repo: save it for this "
                "machine instead, or put the credentials in ~/.netrc or CODEWRIGHT_INDEX_TOKEN"
            )
    path = repo_config_path(root) if root is not None else machine_config_path()
    cfg = _read_json(path)
    if url:
        cfg["index_url"] = url
    else:
        cfg.pop("index_url", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    if root is None:
        path.chmod(0o600)  # may hold a token


def _pyproject_index(root: Path) -> str | None:
    try:
        uv = tomllib.loads((root / "pyproject.toml").read_text()).get("tool", {}).get("uv", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return _uv_index(uv)


def _uv_index(cfg: dict) -> str | None:
    for idx in cfg.get("index", []) or []:
        if isinstance(idx, dict) and idx.get("default") and idx.get("url"):
            return idx["url"]
    return cfg.get("index-url") or None


def _uv_toml() -> str | None:
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    for p in (base / "uv" / "uv.toml", Path("/etc/uv/uv.toml")):
        try:
            found = _uv_index(tomllib.loads(p.read_text()))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if found:
            return found
    return None


def _pip_conf() -> tuple[str, str] | None:
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    files = [os.environ.get("PIP_CONFIG_FILE")] if os.environ.get("PIP_CONFIG_FILE") else []
    files += [str(base / "pip" / "pip.conf"), str(Path("~/.pip/pip.conf").expanduser()), "/etc/pip.conf"]
    for f in files:
        cp = configparser.ConfigParser()
        try:
            cp.read(f)
        except configparser.Error:
            continue
        if cp.has_option("global", "index-url"):
            return cp.get("global", "index-url"), f
    return None


def resolve(root: str | Path | None = None) -> PackageIndex:
    if os.environ.get("CODEWRIGHT_INDEX_URL"):
        return PackageIndex(os.environ["CODEWRIGHT_INDEX_URL"], "CODEWRIGHT_INDEX_URL")
    if root is not None and (url := _read_json(repo_config_path(root)).get("index_url")):
        return PackageIndex(url, "this repo's .codewright/config.json")
    if url := _read_json(machine_config_path()).get("index_url"):
        return PackageIndex(url, "this machine's ~/.codewright/config.json")
    for var in ("UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_INDEX_URL"):
        if os.environ.get(var):
            return PackageIndex(os.environ[var], var)
    if root is not None and (url := _pyproject_index(Path(root))):
        return PackageIndex(url, "the repo's pyproject.toml [tool.uv]")
    if url := _uv_toml():
        return PackageIndex(url, "uv.toml")
    if found := _pip_conf():
        return PackageIndex(found[0], found[1])
    return PackageIndex(PYPI, "PyPI (default)")


# --------------------------------------------------------------------------- lookups


class IndexLookupError(Exception):
    """The index answered, but not with a usable page (auth, not found, bad URL)."""


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.files: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self._cur = {
                "url": a.get("href", ""),
                "requires-python": a.get("data-requires-python"),
                "yanked": "data-yanked" in a,
            }
            self._text = ""

    def handle_data(self, data):
        if hasattr(self, "_cur"):
            self._text += data

    def handle_endtag(self, tag):
        if tag == "a" and hasattr(self, "_cur"):
            self._cur["filename"] = self._text.strip() or self._cur["url"].rsplit("/", 1)[-1].split("#")[0]
            self.files.append(self._cur)
            del self._cur


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_of(filename: str) -> Version | None:
    try:
        if filename.endswith(".whl"):
            return parse_wheel_filename(filename)[1]
        return parse_sdist_filename(filename)[1]
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
        return None


def project_files(index: PackageIndex, name: str, timeout: float = 8.0) -> list[dict]:
    """The files the index lists for a project (PEP 691 JSON, or the PEP 503 HTML page)."""
    url = urljoin(index.base, _normalise(name) + "/")
    req = urllib.request.Request(url, headers={**index.headers(), "Accept": SIMPLE_ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        hint = {401: "authentication failed", 403: "access denied", 404: "not found on this index"}.get(exc.code, "")
        raise IndexLookupError(f"HTTP {exc.code}" + (f": {hint}" if hint else "")) from None
    if "json" in ctype:
        return json.loads(body).get("files", [])
    parser = _Links()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.files


def latest_release(index: PackageIndex, name: str, timeout: float = 8.0) -> dict | None:
    """{"version", "released", "requires_python"} of the newest final release, or None."""
    if index.is_pypi:
        return _latest_from_pypi_json(name, timeout)
    by_version: dict[Version, list[dict]] = {}
    for f in project_files(index, name, timeout):
        v = _version_of(f.get("filename", ""))
        if v is not None and not v.is_prerelease and not f.get("yanked"):
            by_version.setdefault(v, []).append(f)
    if not by_version:
        return None
    newest = max(by_version)
    files = by_version[newest]
    times = [f["upload-time"] for f in files if f.get("upload-time")]
    return {
        "version": str(newest),
        "released": min(times)[:10] if times else "",
        "requires_python": next((f["requires-python"] for f in files if f.get("requires-python")), None),
    }


def _latest_from_pypi_json(name: str, timeout: float) -> dict | None:
    req = urllib.request.Request(PYPI_JSON.format(name=name), headers={"User-Agent": "codewright"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    version = data["info"]["version"]
    files = data.get("releases", {}).get(version) or []
    released = min((f.get("upload_time_iso_8601", "") for f in files), default="")
    return {"version": version, "released": released[:10], "requires_python": data["info"].get("requires_python")}


def check(index: PackageIndex, name: str = "pip", timeout: float = 10.0) -> dict:
    """Can Codewright read this index? Looks up one package and says what went wrong if not."""
    try:
        found = latest_release(index, name, timeout)
    except IndexLookupError as exc:
        return {"ok": False, "detail": f"{index.display} answered {exc} for `{name}`"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "detail": f"couldn't reach {index.display}: {getattr(exc, 'reason', exc)}"}
    if found is None:
        return {"ok": False, "detail": f"{index.display} lists no releases of `{name}`"}
    return {"ok": True, "detail": f"{name} {found['version']} found on {index.display}"}
