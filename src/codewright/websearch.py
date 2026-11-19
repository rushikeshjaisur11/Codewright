"""Web search and page reading for the AI agent: release notes, migration guides, API docs.

Search backends, chosen in Settings (or CODEWRIGHT_SEARCH_PROVIDER / _KEY / _URL):

- ``duckduckgo`` (the default): no key; reads DuckDuckGo's HTML results page.
- ``brave``: the Brave Search API (key; BRAVE_API_KEY works too).
- ``tavily``: the Tavily API (key; TAVILY_API_KEY works too).
- ``searxng``: your own SearXNG instance (its address).
- ``off``: no web access for the agent.

Pages are fetched only from public addresses: a URL (or a redirect) that points at localhost, a private
network or a cloud metadata address is refused, so a model can't use the agent to reach internal services.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from typing import ClassVar

from .llm import _read, settings_path

PROVIDERS = ("duckduckgo", "brave", "tavily", "searxng", "off")
ENV = {"provider": "CODEWRIGHT_SEARCH_PROVIDER", "api_key": "CODEWRIGHT_SEARCH_KEY", "url": "CODEWRIGHT_SEARCH_URL"}
KEY_ENV = {"brave": "BRAVE_API_KEY", "tavily": "TAVILY_API_KEY"}
UA = "Mozilla/5.0 (compatible; codewright-agent)"
MAX_BYTES = 3 * 1024 * 1024


class SearchError(RuntimeError):
    """A search or fetch that failed, with a message fit to show the model and the user."""


# --------------------------------------------------------------------------- settings


def stored() -> dict:
    s = _read(settings_path()).get("search")
    return s if isinstance(s, dict) else {}


def save(values: dict) -> dict:
    """Merge search settings into this machine's settings file (an empty api_key keeps the stored one)."""
    cur = stored()
    for k in ("provider", "api_key", "url"):
        if k not in values:
            continue
        v = values[k]
        if k == "api_key" and v == "":
            continue
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = str(v).strip()
    if cur.get("provider") not in PROVIDERS:
        cur.pop("provider", None)
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _read(p)
    data["search"] = cur
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it can hold an API key
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return public()


def effective() -> dict:
    """Saved settings overlaid with the environment."""
    cfg = {k: v for k, v in stored().items() if v}
    for k, name in ENV.items():
        if os.environ.get(name):
            cfg[k] = os.environ[name]
    if cfg.get("provider") not in PROVIDERS:
        cfg.pop("provider", None)
        for pid, var in KEY_ENV.items():  # a provider's own key variable alone turns it on
            if os.environ.get(var):
                cfg["provider"] = pid
                break
    cfg.setdefault("provider", "duckduckgo")
    if not cfg.get("api_key") and cfg["provider"] in KEY_ENV:
        cfg["api_key"] = os.environ.get(KEY_ENV[cfg["provider"]], "")
    return cfg


def status() -> dict:
    cfg = effective()
    pid = cfg["provider"]
    reason = ""
    if pid == "off":
        reason = "web search is turned off in Settings"
    elif pid in KEY_ENV and not cfg.get("api_key"):
        reason = f"no API key for {pid}"
    elif pid == "searxng" and not cfg.get("url"):
        reason = "no SearXNG address"
    return {"available": not reason, "provider": pid, "reason": reason}


def public() -> dict:
    cfg = effective()
    key = cfg.get("api_key", "")
    return {
        "provider": cfg["provider"],
        "url": cfg.get("url", ""),
        "has_key": bool(key),
        "key_hint": f"…{key[-4:]}" if len(key) >= 12 else "",
        "providers": list(PROVIDERS),
        **{k: v for k, v in status().items() if k in ("available", "reason")},
    }


# --------------------------------------------------------------------------- HTTP with an address check


def _public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback, private, link-local or otherwise internal addresses."""
    if not host:
        raise SearchError("the URL has no host")
    if host.lower() in ("localhost", "metadata.google.internal") or host.lower().endswith((".local", ".internal")):
        raise SearchError(f"{host} is not a public address")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            return  # resolved by the proxy, which only reaches the public internet
        raise SearchError(f"cannot resolve {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise SearchError(f"{host} resolves to a non-public address ({ip}); refused")


class _CheckRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str) -> urllib.parse.ParseResult:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise SearchError("only http and https URLs can be fetched")
    _public_host(u.hostname or "")
    return u


def _open(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: int = 20):
    _check_url(url)
    opener = urllib.request.build_opener(_CheckRedirects())
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise SearchError(f"{urllib.parse.urlparse(url).hostname} answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SearchError(f"cannot reach {urllib.parse.urlparse(url).hostname}: {getattr(e, 'reason', e)}") from None


def _json(url: str, **kw) -> dict:
    with _open(url, **kw) as r:
        try:
            return json.loads(r.read(MAX_BYTES).decode("utf-8", "replace"))
        except ValueError:
            raise SearchError("the search service did not answer with JSON") from None


# --------------------------------------------------------------------------- search


def search(query: str, limit: int = 6) -> list[dict]:
    """[{title, url, snippet}] for `query` from the configured backend; raises SearchError."""
    st = status()
    if not st["available"]:
        raise SearchError(st["reason"])
    cfg = effective()
    pid, limit = cfg["provider"], max(1, min(10, limit))
    q = urllib.parse.quote_plus(query)
    if pid == "brave":
        data = _json(
            f"https://api.search.brave.com/res/v1/web/search?q={q}&count={limit}",
            headers={"X-Subscription-Token": cfg["api_key"], "Accept": "application/json"},
        )
        rows = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in (data.get("web") or {}).get("results", [])
        ]
    elif pid == "tavily":
        data = _json(
            "https://api.tavily.com/search",
            data=json.dumps({"query": query, "max_results": limit}).encode(),
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
        )
        rows = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])
        ]
    elif pid == "searxng":
        data = _json(f"{cfg['url'].rstrip('/')}/search?q={q}&format=json")
        rows = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])
        ]
    else:
        rows = []
        for page in ("https://lite.duckduckgo.com/lite/?q=", "https://html.duckduckgo.com/html/?q="):
            with _open(page + q) as r:
                rows = parse_duckduckgo(r.read(MAX_BYTES).decode("utf-8", "replace"))
            if rows:
                break
        else:
            raise SearchError(
                "DuckDuckGo returned no results (it sometimes blocks automated requests); set up Brave, Tavily "
                "or SearXNG in Settings for dependable search"
            )
    return [{k: _clean(v)[:500] for k, v in row.items()} for row in rows if row.get("url")][:limit]


# The lite page (class='result-link' / 'result-snippet') and the HTML page (class="result__a" / "result__snippet")
_DDG_LINK = re.compile(
    r"<a(?=[^>]*class=[\"'](?:result__a|result-link)[\"'])[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S
)
_DDG_SNIPPET = re.compile(r"class=[\"'](?:result__snippet|result-snippet)[\"'][^>]*>(.*?)</(?:a|div|td)>", re.S)


def parse_duckduckgo(html: str) -> list[dict]:
    links = _DDG_LINK.findall(html)
    snippets = _DDG_SNIPPET.findall(html)
    out = []
    for i, (href, title) in enumerate(links):
        href = unescape(href)
        if "uddg=" in href:  # a DuckDuckGo redirect: the real address is in the uddg parameter
            href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/y.js" in href:  # an ad
            continue
        out.append({"title": title, "url": href, "snippet": snippets[i] if i < len(snippets) else ""})
    return out


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


# --------------------------------------------------------------------------- reading a page


class _Text(HTMLParser):
    SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button"}
    BLOCK: ClassVar[set[str]] = {
        "p",
        "div",
        "section",
        "article",
        "li",
        "tr",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "table",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "pre":
            self.pre += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4"):
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "pre" and self.pre:
            self.pre -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    p = _Text()
    with contextlib.suppress(Exception):  # malformed markup: keep what was read
        p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch(url: str, *, offset: int = 0, max_chars: int = 12000) -> dict:
    """{url, title, text, offset, total, more} for a public web page, as plain text; raises SearchError."""
    with _open(url, headers={"Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9"}) as r:
        kind = r.headers.get("Content-Type", "")
        body = r.read(MAX_BYTES)
        final = r.geturl()
    if not any(t in kind for t in ("html", "text", "json", "xml", "markdown")) and kind:
        raise SearchError(f"{url} is {kind.split(';')[0]}, not a page that can be read as text")
    raw = body.decode("utf-8", "replace")
    title = ""
    if "html" in kind or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        title = _clean(m.group(1)) if m else ""
        raw = html_to_text(raw)
    offset = max(0, offset)
    chunk = raw[offset : offset + max_chars]
    return {
        "url": final,
        "title": title,
        "text": chunk,
        "offset": offset,
        "total": len(raw),
        "more": offset + len(chunk) < len(raw),
    }
