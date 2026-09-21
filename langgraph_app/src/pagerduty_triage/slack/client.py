"""The Slack seam: a Protocol, a real implementation, a fake.

Same shape as ``zoho/client.py`` + ``zoho/fake.py``, for the same reason —
every test in this suite runs with the fake and therefore never touches a
network.

Only two operations, because only two are needed:

* ``post_message`` -- post the gate request into the review channel. Needs
  the bot token and ``chat:write``.
* ``respond``      -- POST to an interaction's ``response_url``, to replace
  the answered message or to tell one user their click was refused. Needs
  **no token at all**: the URL carries its own authorization.

That split is deliberate and worth keeping. The *notifier* holds the bot
token; the *interaction handler* — the part on a public URL — does not. A
compromise of the public endpoint yields no Slack credential.

Deliberately absent: anything that reads channel history, lists users, or
opens modals. The bot holds ``chat:write`` and nothing else, so a stolen bot
token cannot read this workspace's conversations.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

SLACK_API = "https://slack.com/api"


@dataclass(frozen=True)
class PostedMessage:
    channel: str
    ts: str


class SlackError(RuntimeError):
    """A Slack API call came back ``{"ok": false}``."""


@runtime_checkable
class SlackClient(Protocol):
    def post_message(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> PostedMessage: ...

    def respond(self, response_url: str, body: dict[str, Any]) -> None: ...


class HttpSlackClient:
    """Real Slack, over stdlib urllib. No SDK dependency.

    ``slack_sdk`` would be fine, but three JSON POSTs do not justify a
    dependency — and keeping it out means the test suite needs nothing
    installed to exercise the whole path.
    """

    def __init__(self, bot_token: str, *, timeout: float = 10.0):
        self._token = bot_token
        self._timeout = timeout

    # -- SlackClient ---------------------------------------------------------

    def post_message(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> PostedMessage:
        data = self._api(
            "chat.postMessage",
            {"channel": channel, "text": text, "blocks": blocks},
        )
        return PostedMessage(channel=data.get("channel", channel), ts=data.get("ts", ""))

    def respond(self, response_url: str, body: dict[str, Any]) -> None:
        """POST to an interaction ``response_url``.

        Not a Web API method: the URL carries its own authorization, so no
        bot token is sent. Slack allows five uses within 30 minutes of the
        interaction, which is ample for the one edit we make. Failures are
        swallowed by the caller — a message that failed to update is
        cosmetic, and the resume has already happened.
        """
        req = urllib.request.Request(
            response_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            resp.read()

    # -- internals -----------------------------------------------------------

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{SLACK_API}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise SlackError(f"{method} failed: {exc}") from exc
        if not data.get("ok"):
            # Slack returns HTTP 200 with {"ok": false, "error": "..."} for
            # application errors, so this branch is the real error handling.
            raise SlackError(f"{method} returned {data.get('error')!r}")
        return data


@dataclass
class FakeSlackClient:
    """In-memory Slack. Every test uses this; it never touches a network."""

    posted: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    fail_next: Exception | None = None
    _ts: int = 0

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err

    def post_message(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> PostedMessage:
        self._maybe_fail()
        self._ts += 1
        ts = f"1700000000.{self._ts:06d}"
        self.posted.append(
            {"channel": channel, "text": text, "blocks": blocks, "ts": ts}
        )
        return PostedMessage(channel=channel, ts=ts)

    def respond(self, response_url: str, body: dict[str, Any]) -> None:
        self._maybe_fail()
        self.responses.append({"response_url": response_url, "body": body})
