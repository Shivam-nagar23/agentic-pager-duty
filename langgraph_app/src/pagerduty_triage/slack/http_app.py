"""The HTTP surface Slack POSTs to. Twenty lines of plumbing, no decisions.

## Where this runs: on the LangGraph deployment itself

LangGraph Platform hosts custom routes. Point ``langgraph.json`` at this
module and the route is served on the same origin as ``/threads`` and
``/runs``:

```json
"http": { "app": "./src/pagerduty_triage/slack/http_app.py:app" }
```

Custom routes are **merged, not sub-mounted**, and they take priority over
the platform's own — so the path is namespaced under ``/slack/`` where it
cannot shadow a system endpoint.

No separate service is needed. This module is nonetheless a plain Starlette
app with no LangGraph imports, so ``uvicorn
pagerduty_triage.slack.http_app:app`` runs it standalone if the endpoint ever
has to be isolated from the deployment that holds the Zoho and GitHub
credentials. That is a config change, not a rewrite.

## The route is public, deliberately, and that is the whole reason for signing

``http.enable_custom_route_auth`` defaults to ``false``: custom routes bypass
the API-key auth protecting ``/threads`` and ``/runs``. We leave it false,
because Slack cannot present a LangGraph API key. The flag is also
all-or-nothing — turning it on would break this route and every other custom
one — so per-route auth would mean branching on ``path`` inside
``@auth.authenticate``, which is more machinery for a check that would still
not tell us *which human* clicked.

So the endpoint is internet-reachable and ``signing.py`` is the entire
boundary. See its module docstring.

## Three seconds

Slack expects an HTTP 200 within three seconds or it shows the reviewer an
error. Verification and authorization are pure CPU and happen inline; the
resume and the message update are handed to a Starlette ``BackgroundTask``,
which runs *after* the response has been sent.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from pagerduty_triage.slack.config import SlackSettings, load_slack_settings
from pagerduty_triage.slack.handler import handle_interaction

#: Namespaced so a custom route cannot shadow a platform endpoint.
INTERACTIONS_PATH = "/slack/interactions"
HEALTH_PATH = "/slack/health"


def _deps(settings: SlackSettings) -> tuple[Any, Any]:
    """Real clients, built per request so a redeployed secret is picked up.

    Imported lazily: importing this module must not require ``langgraph_sdk``
    or a reachable platform, because the tests import it.
    """
    from pagerduty_triage.slack.client import HttpSlackClient
    from pagerduty_triage.slack.platform import PlatformThreads

    return (
        HttpSlackClient(settings.bot_token),
        PlatformThreads(
            url=settings.langgraph_api_url, api_key=settings.langgraph_api_key
        ),
    )


async def interactions(request: Request) -> Response:
    settings = load_slack_settings()

    # `await request.body()` before anything else parses it. The HMAC covers
    # these exact bytes; a re-encoded form body produces a different string
    # and a signature that never matches.
    raw_body = await request.body()

    slack, threads = _deps(settings)
    result = handle_interaction(
        raw_body=raw_body,
        headers=dict(request.headers),
        settings=settings,
        slack=slack,
        threads=threads,
    )

    background = BackgroundTask(result.followup) if result.followup else None
    # An empty 200 is Slack's documented bare acknowledgement: the reviewer
    # sees nothing change until the background task talks to `response_url`.
    return PlainTextResponse(
        result.body, status_code=result.status, background=background
    )


async def health(request: Request) -> Response:
    """Is this deployment able to answer a gate at all?

    Reports only whether each variable is *set*, never its value, so the
    endpoint is safe to leave public alongside the one that has to be.
    """
    settings = load_slack_settings()
    return JSONResponse(
        {
            "ok": not settings.missing(),
            "missing_env": settings.missing(),
            "approvers_configured": len(settings.approver_user_ids),
        }
    )


app = Starlette(
    routes=[
        Route(INTERACTIONS_PATH, interactions, methods=["POST"]),
        Route(HEALTH_PATH, health, methods=["GET"]),
    ]
)
