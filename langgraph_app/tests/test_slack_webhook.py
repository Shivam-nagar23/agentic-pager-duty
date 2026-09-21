"""The run-finished webhook: what replaces the manual notifier tick.

LangGraph Platform calls a URL when a run ends, including when it ends by
parking on an ``interrupt()``. That callback is what posts the gate card, so a
reviewer sees it seconds after the thread parks rather than on the next cron.

The endpoint is internet-reachable for the same reason ``/slack/interactions``
is: ``http.enable_custom_route_auth`` is false, so custom routes bypass the API
key. Unlike Slack, the platform does not sign its callbacks -- so the boundary
here is a shared secret in the query string, compared in constant time, with an
unset secret closing the endpoint rather than opening it.

There were no tests for ``http_app`` at all before this file. That is the same
hole that let ``platform.py`` ship with a bug that made every gate click fail.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pagerduty_triage.slack import http_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PAGER_WEBHOOK_SECRET", "s3cret-token")
    calls: list[str] = []
    monkeypatch.setattr(http_app, "_run_notifier_sweep", lambda: calls.append("swept"))
    return TestClient(http_app.app), calls


def test_correct_token_triggers_a_sweep(client):
    c, calls = client
    r = c.post(f"{http_app.RUN_FINISHED_PATH}?token=s3cret-token", json={})
    assert r.status_code == 200
    assert calls == ["swept"]


def test_wrong_token_is_refused_and_sweeps_nothing(client):
    c, calls = client
    r = c.post(f"{http_app.RUN_FINISHED_PATH}?token=wrong", json={})
    assert r.status_code == 403
    assert calls == []


def test_missing_token_is_refused(client):
    c, calls = client
    r = c.post(http_app.RUN_FINISHED_PATH, json={})
    assert r.status_code == 403
    assert calls == []


def test_unset_secret_closes_the_endpoint(monkeypatch):
    """Fail closed. An endpoint that accepts anything because nobody set a
    secret is worse than one that is switched off."""
    monkeypatch.delenv("PAGER_WEBHOOK_SECRET", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(http_app, "_run_notifier_sweep", lambda: calls.append("swept"))
    c = TestClient(http_app.app)
    r = c.post(f"{http_app.RUN_FINISHED_PATH}?token=anything", json={})
    assert r.status_code == 403
    assert calls == []


def test_a_failing_sweep_does_not_500_the_platform(client, monkeypatch):
    """The platform retries a failing webhook. A sweep that throws must not
    turn into a retry storm -- log it and acknowledge."""
    def boom():
        raise RuntimeError("slack is down")

    monkeypatch.setattr(http_app, "_run_notifier_sweep", boom)
    c, _ = client
    r = c.post(f"{http_app.RUN_FINISHED_PATH}?token=s3cret-token", json={})
    assert r.status_code == 200


def test_get_is_not_allowed(client):
    c, _ = client
    assert c.get(http_app.RUN_FINISHED_PATH).status_code == 405


def test_health_still_works(client):
    c, _ = client
    assert c.get(http_app.HEALTH_PATH).status_code == 200


# ---------------------------------------------------------------------------
# The other half: what the poller tells the platform to call.


def test_webhook_url_is_none_without_config(monkeypatch):
    """Local `langgraph dev` has no public URL. None means "do not call back",
    which is the same as not passing the parameter."""
    from pagerduty_triage import poller_graph

    monkeypatch.delenv("PAGER_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PAGER_WEBHOOK_SECRET", raising=False)
    assert poller_graph.run_finished_webhook() is None


def test_webhook_url_needs_both_halves(monkeypatch):
    """A URL with no secret would be an open endpoint; a secret with no URL is
    nothing to call. Either alone is a misconfiguration, not a partial one."""
    from pagerduty_triage import poller_graph

    monkeypatch.setenv("PAGER_PUBLIC_URL", "https://x.example")
    monkeypatch.delenv("PAGER_WEBHOOK_SECRET", raising=False)
    assert poller_graph.run_finished_webhook() is None

    monkeypatch.delenv("PAGER_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PAGER_WEBHOOK_SECRET", "s")
    assert poller_graph.run_finished_webhook() is None


def test_webhook_url_is_built_and_escaped(monkeypatch):
    from pagerduty_triage import poller_graph
    from pagerduty_triage.slack.http_app import RUN_FINISHED_PATH

    monkeypatch.setenv("PAGER_PUBLIC_URL", "https://deploy.example.com/")
    monkeypatch.setenv("PAGER_WEBHOOK_SECRET", "a b/c&d")
    url = poller_graph.run_finished_webhook()

    assert url is not None
    assert url.startswith(f"https://deploy.example.com{RUN_FINISHED_PATH}?token=")
    # A secret with & or / in it must not split the query string or the path.
    assert "a%20b%2Fc%26d" in url
    assert url.count("?") == 1


def test_webhook_secret_round_trips_through_the_route(monkeypatch):
    """The URL the poller builds must be the one the route accepts. These two
    are written in different modules and nothing else checks they agree."""
    from urllib.parse import urlparse, parse_qs

    from pagerduty_triage import poller_graph

    secret = "tok en&/?#"
    monkeypatch.setenv("PAGER_PUBLIC_URL", "https://deploy.example.com")
    monkeypatch.setenv("PAGER_WEBHOOK_SECRET", secret)
    url = poller_graph.run_finished_webhook()
    assert url is not None

    calls: list[str] = []
    monkeypatch.setattr(http_app, "_run_notifier_sweep", lambda: calls.append("swept"))
    c = TestClient(http_app.app)

    parsed = urlparse(url)
    token = parse_qs(parsed.query)["token"][0]
    r = c.post(parsed.path, params={"token": token}, json={})

    assert r.status_code == 200
    assert calls == ["swept"]


# ---------------------------------------------------------------------------
# Health has to say whether the automation is actually armed.


def test_health_reports_the_webhook_as_unarmed(monkeypatch):
    """A deployment with no webhook config is *functional* — gates park, they
    just never get announced, and someone has to notice on their own.

    This exact configuration shipped and looked perfectly healthy: `ok: true`,
    `missing_env: []`, and a ticket that triaged correctly into a gate nobody
    was told about. Reporting it is the difference between a five-minute fix and
    an afternoon of wondering why Slack is quiet.
    """
    monkeypatch.delenv("PAGER_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAGER_PUBLIC_URL", raising=False)
    r = TestClient(http_app.app).get(http_app.HEALTH_PATH)
    body = r.json()

    assert body["gate_notification"] == "manual"
    assert "PAGER_PUBLIC_URL" in body["webhook_unset"]
    assert "PAGER_WEBHOOK_SECRET" in body["webhook_unset"]


def test_health_reports_the_webhook_as_armed(monkeypatch):
    monkeypatch.setenv("PAGER_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("PAGER_PUBLIC_URL", "https://x.example")
    body = TestClient(http_app.app).get(http_app.HEALTH_PATH).json()

    assert body["gate_notification"] == "automatic"
    assert body["webhook_unset"] == []


def test_partial_webhook_config_is_not_automatic(monkeypatch):
    """Half-configured is manual, not automatic. Either half alone announces
    nothing, and reporting it as armed would be the same lie in a new place."""
    monkeypatch.setenv("PAGER_PUBLIC_URL", "https://x.example")
    monkeypatch.delenv("PAGER_WEBHOOK_SECRET", raising=False)
    body = TestClient(http_app.app).get(http_app.HEALTH_PATH).json()

    assert body["gate_notification"] == "manual"
    assert body["webhook_unset"] == ["PAGER_WEBHOOK_SECRET"]
