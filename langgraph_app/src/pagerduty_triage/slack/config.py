"""Slack configuration, read from the environment. No secret is ever in code.

Kept separate from ``pagerduty_triage.settings`` on purpose: the Slack layer is
a *presentation* over the gates, it runs in its own process (see
``http_app.py``), and nothing in the graph imports it. A separate dataclass
keeps that boundary visible and means the triage graph cannot accidentally
acquire a Slack signing secret.

One approver, not an allowlist system. The normal case is
``SLACK_APPROVER_USER_ID=U…`` — Shivam, and nobody else. ``…USER_IDS`` (plural,
comma-separated) is also read and unioned in, so adding a second reviewer is a
config change rather than a code change. Nothing here has roles, groups or
per-gate permissions, and nothing should grow them without a reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: How much clock skew a Slack request timestamp may carry before it is
#: treated as a replay. Slack's own documented guidance is five minutes.
DEFAULT_MAX_SKEW_SECONDS = 60 * 5


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class SlackSettings:
    #: ``xoxb-…``. Needs ``chat:write`` only.
    bot_token: str = ""
    #: The ``Signing Secret`` from the app's Basic Information page. This is
    #: NOT the bot token and NOT the (deprecated) verification token.
    signing_secret: str = ""
    #: Channel id (``C…``), not ``#name``. The bot must be a member.
    channel_id: str = ""
    #: Slack user ids (``U…``) permitted to answer a gate. Usually one.
    approver_user_ids: frozenset[str] = frozenset()
    #: Base URL of the LangGraph deployment the handler resumes threads on.
    langgraph_api_url: str = ""
    #: Server-side API key for that deployment, if it is a managed one.
    langgraph_api_key: str = ""
    #: Assistant id used when creating the resume run.
    triage_assistant_id: str = "triage"
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS

    def missing(self) -> list[str]:
        """Which env vars are absent. The HTTP app refuses to start without
        all of them — a Slack endpoint running with no signing secret is an
        unauthenticated public URL that can email a paying customer."""
        required = {
            "SLACK_BOT_TOKEN": self.bot_token,
            "SLACK_SIGNING_SECRET": self.signing_secret,
            "SLACK_CHANNEL_ID": self.channel_id,
            "SLACK_APPROVER_USER_ID": ",".join(sorted(self.approver_user_ids)),
            "LANGGRAPH_API_URL": self.langgraph_api_url,
        }
        return [k for k, v in required.items() if not v]

    def is_approver(self, user_id: str) -> bool:
        """The whole authorization model, in one line.

        Empty allowlist means *nobody*, never *everybody*. A deployment that
        forgot to set ``SLACK_APPROVER_USER_ID`` must be unable to approve
        anything rather than able to approve everything.

        Note this checks the Slack **user id**, not the display name or the
        email. A display name is not an identity: it is changeable by its
        owner and duplicable by anyone else in the workspace.
        """
        if not self.approver_user_ids:
            return False
        return user_id in self.approver_user_ids


def load_slack_settings() -> SlackSettings:
    # Singular is the documented form; plural is read too so a second
    # reviewer never requires a code change.
    raw_ids = f"{_env('SLACK_APPROVER_USER_ID')},{_env('SLACK_APPROVER_USER_IDS')}"
    ids = frozenset(part.strip() for part in raw_ids.split(",") if part.strip())
    return SlackSettings(
        bot_token=_env("SLACK_BOT_TOKEN"),
        signing_secret=_env("SLACK_SIGNING_SECRET"),
        channel_id=_env("SLACK_CHANNEL_ID"),
        approver_user_ids=ids,
        langgraph_api_url=_env("LANGGRAPH_API_URL"),
        langgraph_api_key=_env("LANGGRAPH_API_KEY"),
        triage_assistant_id=_env("TRIAGE_ASSISTANT_ID", "triage"),
        max_skew_seconds=int(
            _env("SLACK_MAX_SKEW_SECONDS", str(DEFAULT_MAX_SKEW_SECONDS))
        ),
    )
