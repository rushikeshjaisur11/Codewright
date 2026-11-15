"""Single sign-on for the shared MCP server: accept access tokens from your identity provider.

The server acts as an OAuth 2.0 protected resource (the MCP authorization model): it publishes
``/.well-known/oauth-protected-resource`` naming your identity provider, MCP clients that support
OAuth sign the user in there, and every request carries the resulting access token. Codewright
checks the token's signature against the provider's published keys (JWKS, found through
``<issuer>/.well-known/openid-configuration``), its issuer, audience and expiry, and optionally
the user's email domain. Static tokens (``--token``) keep working alongside, for CI and scripts.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import urllib.request
from dataclasses import dataclass, field

import jwt
from mcp.server.auth.provider import AccessToken

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]


@dataclass
class SSOVerifier:
    issuer: str
    audience: str
    allowed_domains: list[str] = field(default_factory=list)  # e.g. ["example.com"]: users' email domains
    static_tokens: list[str] = field(default_factory=list)
    leeway: int = 30
    _jwks: jwt.PyJWKClient | None = None

    def jwks(self) -> jwt.PyJWKClient:
        if self._jwks is None:
            url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
            req = urllib.request.Request(url, headers={"User-Agent": "codewright"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                config = json.load(resp)
            if config.get("issuer", "").rstrip("/") != self.issuer.rstrip("/"):
                raise ValueError(f"{url} names issuer {config.get('issuer')!r}, not {self.issuer!r}")
            self._jwks = jwt.PyJWKClient(config["jwks_uri"], cache_keys=True, lifespan=3600)
        return self._jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        for static in self.static_tokens:
            if hmac.compare_digest(token.encode(), static.encode()):
                return AccessToken(token=token, client_id="static-token", scopes=[], subject="static-token")
        return await asyncio.to_thread(self._verify_jwt, token)

    def _verify_jwt(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, ValueError, OSError):
            return None  # bad signature, wrong audience/issuer, expired, or the provider unreachable
        if self.allowed_domains:
            email = str(claims.get("email") or claims.get("upn") or claims.get("preferred_username") or "")
            if email.rpartition("@")[2].lower() not in {d.lower() for d in self.allowed_domains}:
                return None
            if claims.get("email_verified") is False:
                return None
        scopes = (
            claims.get("scope", "").split() if isinstance(claims.get("scope"), str) else list(claims.get("scp") or [])
        )
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or claims.get("appid") or ""),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=str(claims["sub"]),
            claims=claims,
        )
