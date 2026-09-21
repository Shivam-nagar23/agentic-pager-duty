"""Configuration, all of it from the environment. No secret is ever in code.

The one non-obvious knob is ``ZOHO_TRANSPORT``:

* ``mcp``  -- talk to an MCP server (see ``mcp_server/zoho_desk_mcp.py``).
* ``rest`` -- talk to Zoho Desk REST v1 directly.
* ``fake`` -- the in-memory stub. Tests and local dry runs only.

The default is ``fake`` on purpose. A misconfigured deployment should fail to
reach a customer, not reach one by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Zoho runs one accounts server and one desk API host per data centre, and
# they are not the same hostname. Getting this wrong is the single most common
# Zoho OAuth failure.
ZOHO_DATA_CENTRES: dict[str, tuple[str, str]] = {
    # key: (accounts host, desk api host)
    "com": ("https://accounts.zoho.com", "https://desk.zoho.com"),
    "eu": ("https://accounts.zoho.eu", "https://desk.zoho.eu"),
    "in": ("https://accounts.zoho.in", "https://desk.zoho.in"),
    "au": ("https://accounts.zoho.com.au", "https://desk.zoho.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://desk.zoho.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://desk.zohocloud.ca"),
    "sa": ("https://accounts.zoho.sa", "https://desk.zoho.sa"),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # -- Zoho ---------------------------------------------------------------
    zoho_transport: str = "fake"
    zoho_data_centre: str = "com"
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_refresh_token: str = ""
    zoho_org_id: str = ""
    zoho_department_id: str = ""
    #: Verified "From" address for the department. Zoho requires it on every
    #: reply, and a non-verified one is a 422 at send time, not a bounce.
    zoho_from_email: str = ""
    zoho_mcp_url: str = ""
    #: Only tickets in these statuses are ever picked up by the poller.
    zoho_poll_statuses: tuple[str, ...] = ("Open",)
    zoho_poll_limit: int = 50

    # -- sprint-tasks -------------------------------------------------------
    github_token: str = ""
    sprint_tasks_repo: str = "devtron-labs/sprint-tasks"

    # -- model --------------------------------------------------------------
    #: Provider-prefixed string understood by ``init_chat_model``.
    #: ``claude-opus-5`` is the current default per the ``claude-api`` skill,
    #: which is the source of truth for Anthropic model ids. ``claude-sonnet-5``
    #: is the cheaper option if triage volume makes Opus uneconomic; measure
    #: classification accuracy before downgrading.
    model: str = "anthropic:claude-opus-5"
    #: Base URL of an OpenAI-compatible LLM gateway. When set, `TRIAGE_MODEL` is
    #: a gateway model id (`anthropic/claude-opus-5`) rather than a
    #: provider-prefixed string, and every call routes through the gateway.
    llm_gateway_base_url: str = ""
    #: Defaults to LANGSMITH_API_KEY, which is what the LangSmith gateway
    #: authenticates with. Separate only if the gateway key differs from the
    #: tracing key.
    llm_gateway_api_key: str = ""

    # -- safety -------------------------------------------------------------
    #: Hard kill switch. With this off, ``zoho_send_reply`` refuses to send
    #: even after a human approves the gate -- it reports the approval and
    #: stops. Lets the whole pipeline be exercised against production Zoho
    #: without any chance of a customer receiving agent-written text.
    replies_enabled: bool = False
    #: Same idea for the other side of the seam.
    issue_creation_enabled: bool = False

    @property
    def zoho_accounts_host(self) -> str:
        return ZOHO_DATA_CENTRES[self.zoho_data_centre][0]

    @property
    def zoho_api_base(self) -> str:
        return f"{ZOHO_DATA_CENTRES[self.zoho_data_centre][1]}/api/v1"

    def missing_for_transport(self) -> list[str]:
        """Which env vars are absent for the configured transport."""
        if self.zoho_transport == "rest":
            required = {
                "ZOHO_CLIENT_ID": self.zoho_client_id,
                "ZOHO_CLIENT_SECRET": self.zoho_client_secret,
                "ZOHO_REFRESH_TOKEN": self.zoho_refresh_token,
                "ZOHO_ORG_ID": self.zoho_org_id,
                "ZOHO_FROM_EMAIL": self.zoho_from_email,
            }
        elif self.zoho_transport == "mcp":
            required = {"ZOHO_MCP_URL": self.zoho_mcp_url}
        else:
            required = {}
        return [k for k, v in required.items() if not v]


def load_settings() -> Settings:
    dc = _env("ZOHO_DATA_CENTRE", "com").lower()
    if dc not in ZOHO_DATA_CENTRES:
        raise ValueError(
            f"ZOHO_DATA_CENTRE={dc!r} is not one of {sorted(ZOHO_DATA_CENTRES)}"
        )

    transport = _env("ZOHO_TRANSPORT", "fake").lower()
    if transport not in ("fake", "rest", "mcp"):
        raise ValueError(f"ZOHO_TRANSPORT={transport!r} must be fake, rest or mcp")

    statuses = tuple(
        s.strip() for s in _env("ZOHO_POLL_STATUSES", "Open").split(",") if s.strip()
    )

    return Settings(
        zoho_transport=transport,
        zoho_data_centre=dc,
        zoho_client_id=_env("ZOHO_CLIENT_ID"),
        zoho_client_secret=_env("ZOHO_CLIENT_SECRET"),
        zoho_refresh_token=_env("ZOHO_REFRESH_TOKEN"),
        zoho_org_id=_env("ZOHO_ORG_ID"),
        zoho_department_id=_env("ZOHO_DEPARTMENT_ID"),
        zoho_from_email=_env("ZOHO_FROM_EMAIL"),
        zoho_mcp_url=_env("ZOHO_MCP_URL"),
        zoho_poll_statuses=statuses or ("Open",),
        zoho_poll_limit=int(_env("ZOHO_POLL_LIMIT", "50")),
        github_token=_env("GITHUB_TOKEN"),
        sprint_tasks_repo=_env("SPRINT_TASKS_REPO", "devtron-labs/sprint-tasks"),
        model=_env("TRIAGE_MODEL", "anthropic:claude-opus-5"),
        llm_gateway_base_url=_env("LLM_GATEWAY_BASE_URL"),
        llm_gateway_api_key=_env("LLM_GATEWAY_API_KEY") or _env("LANGSMITH_API_KEY"),
        replies_enabled=_env_bool("REPLIES_ENABLED", False),
        issue_creation_enabled=_env_bool("ISSUE_CREATION_ENABLED", False),
    )
