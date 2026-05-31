"""Per-worker MCP client config generation (CLI-native, zero paid API).

JohnStudio workers are CLI processes (claude / codex / gemini). Those CLIs load
MCP servers themselves when they find a ``.mcp.json`` in their working dir. This
module writes a per-worker ``.mcp.json`` wired ONLY to free / self-hosted MCP
backends — never a paid API.

HARD CONSTRAINT: no paid services. Anything in :data:`PAID_DENYLIST` (e.g. Exa)
is refused even if a caller asks for it. Servers are filtered through a per-role
allowlist so a worker only gets the servers its role needs.

Integration points (see ``context_builder`` / ``spawner``): both call
:func:`write_worker_mcp_json` with the worker's role + worktree so the file is
present before the CLI launches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

MCP_FILENAME = ".mcp.json"


@dataclass(frozen=True)
class MCPServer:
    """A single MCP server definition (stdio transport, CLI-loadable)."""

    name: str
    command: str
    args: List[str]
    # Env keys the server reads; resolved from the worker env at generation
    # time. Missing keys are omitted (the server degrades, never errors here).
    env_keys: List[str] = field(default_factory=list)
    free: bool = True

    def to_config(self, env: Optional[Mapping[str, str]] = None) -> dict:
        env = env or {}
        cfg: dict = {"command": self.command, "args": list(self.args)}
        resolved = {k: env[k] for k in self.env_keys if env.get(k)}
        if resolved:
            cfg["env"] = resolved
        return cfg


# --- Catalogue: FREE / self-hosted servers only -------------------------------
# n8n points at a SELF-HOSTED instance (local URL via env), Context7 uses its
# free tier, GitHub uses a personal token (free), Playwright is fully local.
CATALOGUE: Dict[str, MCPServer] = {
    "playwright": MCPServer(
        name="playwright",
        command="npx",
        args=["-y", "@playwright/mcp@latest"],
    ),
    "github": MCPServer(
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env_keys=["GITHUB_PERSONAL_ACCESS_TOKEN"],
    ),
    "n8n": MCPServer(
        name="n8n",
        command="npx",
        args=["-y", "n8n-mcp"],
        env_keys=["N8N_API_URL", "N8N_API_KEY"],  # self-hosted, local URL
    ),
    "context7": MCPServer(
        name="context7",
        command="npx",
        args=["-y", "@upstash/context7-mcp"],  # free tier, no key required
    ),
}

# Paid / metered backends that must NEVER be written into a worker config.
PAID_DENYLIST = {"exa", "exa-search", "perplexity", "tavily", "brave-paid", "firecrawl"}

# Per-role allowlist (keys are normalized role names from seeds/roles/*).
ROLE_ALLOWLIST: Dict[str, List[str]] = {
    "architect": ["context7", "github"],
    "plan-architect": ["context7", "github"],
    "backend-developer": ["github", "context7", "n8n"],
    "fullstack-developer": ["github", "context7", "playwright", "n8n"],
    "frontend-developer": ["playwright", "context7"],
    "test-automator": ["playwright", "github"],
    "code-reviewer": ["github", "context7"],
    "security-auditor": ["github", "context7"],
    "devops-engineer": ["github", "n8n"],
    "debugger": ["github", "context7"],
}

# Roles not in the allowlist fall back to this conservative default.
DEFAULT_ALLOWLIST: List[str] = ["context7"]


class PaidServerError(ValueError):
    """Raised when a paid / metered MCP server is requested."""


def allowed_servers_for_role(role: str) -> List[str]:
    """Return the allowlisted server names for ``role`` (normalized)."""
    key = (role or "").strip().lower()
    return list(ROLE_ALLOWLIST.get(key, DEFAULT_ALLOWLIST))


def build_mcp_config(
    role: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    extra_servers: Optional[Iterable[str]] = None,
) -> dict:
    """Build the ``.mcp.json`` dict for a worker of the given ``role``.

    ``extra_servers`` may add servers beyond the role default; they are still
    subject to the free/paid checks. Unknown server names are skipped.
    """
    names = allowed_servers_for_role(role)
    if extra_servers:
        for name in extra_servers:
            if name not in names:
                names.append(name)

    servers: Dict[str, dict] = {}
    for name in names:
        low = name.strip().lower()
        if low in PAID_DENYLIST:
            raise PaidServerError(f"refusing paid MCP server: {name!r}")
        server = CATALOGUE.get(low)
        if server is None:
            continue
        if not server.free:
            raise PaidServerError(f"refusing non-free MCP server: {name!r}")
        servers[server.name] = server.to_config(env)

    return {"mcpServers": servers}


def write_worker_mcp_json(
    role: str,
    worktree: Path | str,
    *,
    env: Optional[Mapping[str, str]] = None,
    extra_servers: Optional[Iterable[str]] = None,
) -> Path:
    """Write ``.mcp.json`` into ``worktree`` for ``role`` and return its path."""
    config = build_mcp_config(role, env=env, extra_servers=extra_servers)
    target = Path(worktree) / MCP_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return target
