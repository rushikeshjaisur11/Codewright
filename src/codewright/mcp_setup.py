"""Copy-paste MCP client setup for Codewright: local (stdio) or a shared server (Streamable HTTP)."""

from __future__ import annotations

import json

# Runs the latest Codewright without installing it first (uv fetches and caches it).
UVX = ["uvx", "--from", "git+https://github.com/rushikeshjaisur11/codewright", "codewright", "mcp"]


def client_configs(url: str = "", token_env: str = "CODEWRIGHT_TOKEN") -> list[tuple[str, str]]:
    """(client, setup text) pairs. With `url`, for a shared server; without, for a local stdio server."""
    if not url:
        stdio = {"command": UVX[0], "args": UVX[1:]}
        return [
            ("Claude Code", f"claude mcp add codewright -- {' '.join(UVX)}"),
            ("Cursor (~/.cursor/mcp.json or .cursor/mcp.json)", _json({"mcpServers": {"codewright": stdio}})),
            ("VS Code (.vscode/mcp.json)", _json({"servers": {"codewright": {"type": "stdio", **stdio}}})),
            ("Claude Desktop and other clients (mcpServers)", _json({"mcpServers": {"codewright": stdio}})),
            (
                "Already installed (pip/uv tool install)",
                _json({"mcpServers": {"codewright": {"command": "codewright", "args": ["mcp"]}}}),
            ),
        ]
    url = url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    return [
        (
            "Claude Code",
            f'claude mcp add --transport http codewright {url} --header "Authorization: Bearer ${token_env}"',
        ),
        (
            "Cursor (~/.cursor/mcp.json or .cursor/mcp.json)",
            _json(
                {
                    "mcpServers": {
                        "codewright": {"url": url, "headers": {"Authorization": f"Bearer ${{env:{token_env}}}"}}
                    }
                }
            ),
        ),
        (
            "VS Code (.vscode/mcp.json; asks for the token once)",
            _json(
                {
                    "inputs": [
                        {
                            "type": "promptString",
                            "id": "codewright-token",
                            "description": "Codewright token",
                            "password": True,
                        }
                    ],
                    "servers": {
                        "codewright": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": "Bearer ${input:codewright-token}"},
                        }
                    },
                }
            ),
        ),
        (
            "Claude Desktop and other stdio-only clients (through mcp-remote)",
            _json(
                {
                    "mcpServers": {
                        "codewright": {
                            "command": "npx",
                            "args": ["-y", "mcp-remote", url, "--header", "Authorization:${AUTH_HEADER}"],
                            "env": {"AUTH_HEADER": "Bearer <your token>"},
                        }
                    }
                }
            ),
        ),
    ]


def _json(obj: dict) -> str:
    return json.dumps(obj, indent=2)
