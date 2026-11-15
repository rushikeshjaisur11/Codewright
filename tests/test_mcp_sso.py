"""Single sign-on for the shared MCP server, against a local identity provider that signs real JWTs."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from codewright import mcp_server
from codewright.remote import Policy
from codewright.sso import SSOVerifier

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STATIC = "s" * 32


@pytest.fixture(scope="module")
def issuer():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}

    class IdP(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            docs = {
                "/.well-known/openid-configuration": {"issuer": base, "jwks_uri": f"{base}/jwks"},
                "/jwks": jwks,
            }
            body = json.dumps(docs.get(self.path, {})).encode()
            self.send_response(200 if self.path in docs else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), IdP)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def token(issuer, aud="https://cw.example.com/mcp", key=KEY, ttl=300, **claims):
    now = int(time.time())
    payload = {"iss": issuer, "aud": aud, "sub": "u1", "iat": now, "exp": now + ttl, "email": "ada@example.com"}
    return jwt.encode({**payload, **claims}, key, algorithm="RS256", headers={"kid": "k1"})


def verify(v, t):
    return asyncio.run(v.verify_token(t))


def test_tokens_from_the_identity_provider_are_checked_fully(issuer):
    v = SSOVerifier(issuer, "https://cw.example.com/mcp", ["example.com"], [STATIC])
    ok = verify(v, token(issuer, scope="mcp openid"))
    assert ok.subject == "u1" and ok.scopes == ["mcp", "openid"]
    assert verify(v, STATIC).client_id == "static-token"  # CI and scripts keep working
    assert verify(v, token(issuer, aud="https://other.example.com")) is None  # meant for another service
    assert verify(v, token(issuer, ttl=-120)) is None  # expired
    assert verify(v, token(issuer, key=OTHER_KEY)) is None  # not signed by the provider
    assert verify(v, token("https://evil.example.com")) is None  # another issuer
    assert verify(v, token(issuer, email="eve@elsewhere.com")) is None  # outside the allowed domain
    assert verify(v, token(issuer, email_verified=False)) is None
    assert verify(v, "not-a-jwt") is None


@pytest.fixture
def sso_server(issuer, tmp_path, monkeypatch):
    import socket

    import uvicorn

    monkeypatch.setattr(mcp_server, "POLICY", Policy(shared=True, allow_proof=False, cache=tmp_path / "cache"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    public = f"http://127.0.0.1:{port}/mcp"
    sso = SSOVerifier(issuer, public, [], [STATIC])
    app = mcp_server.http_app([STATIC], "127.0.0.1", sso=sso, public_url=public, required_scopes=["mcp"])
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield public
    srv.should_exit = True
    thread.join(5)


def test_clients_are_told_where_to_sign_in(sso_server, issuer):
    base = sso_server.removesuffix("/mcp")
    r = httpx.post(sso_server, json={})
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    meta = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["authorization_servers"] == [issuer + "/"] or meta["authorization_servers"] == [issuer]
    assert meta["resource"].rstrip("/") == sso_server and meta["scopes_supported"] == ["mcp"]
    assert httpx.get(f"{base}/healthz").json() == {"ok": True}


def test_a_signed_in_agent_uses_the_tools(sso_server, issuer):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def call(bearer):
        client = create_mcp_http_client(headers={"Authorization": f"Bearer {bearer}"})
        async with (
            client,
            streamable_http_client(sso_server, http_client=client) as st,
            ClientSession(st[0], st[1]) as s,
        ):
            await s.initialize()
            return json.loads((await s.call_tool("list_repos", {})).content[0].text)

    assert asyncio.run(call(token(issuer, aud=sso_server, scope="mcp")))["shared"] is True
    no_scope = httpx.post(sso_server, json={}, headers={"Authorization": f"Bearer {token(issuer, aud=sso_server)}"})
    assert no_scope.status_code == 403  # signed in, but without the required scope


def test_cli_needs_a_public_https_url_for_sso_on_a_public_address():
    from typer.testing import CliRunner

    from codewright.cli import app

    r = CliRunner().invoke(app, ["mcp", "--http", "--host", "0.0.0.0", "--sso-issuer", "https://idp.example.com"])
    assert r.exit_code == 2 and "--public-url" in r.output
