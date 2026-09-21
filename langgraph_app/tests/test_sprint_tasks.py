"""Tests for the GitHub half of the seam.

`sprint_tasks.py` had no tests until a live run failed on a 422, which is the
same hole that let `platform.py` ship broken: the only client exercised
anywhere was the fake, and a fake agrees with whatever shape you give it. The
tests below pin the *request* the real client builds, because the request is
the part GitHub gets to reject.
"""

from __future__ import annotations

import urllib.parse

import pytest

from pagerduty_triage.sprint_tasks import (
    AGENT_LABEL,
    PAGER_DUTY_LABEL,
    GitHubSprintTasksClient,
    zoho_ticket_marker,
)


class RecordingClient(GitHubSprintTasksClient):
    """Real client with the network seam replaced, so the URL is observable."""

    def __init__(self, response: dict | None = None, **kw):
        super().__init__(token="t", **kw)
        self.calls: list[tuple[str, str, dict | None]] = []
        self._response = response or {}

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        self.calls.append((method, url, payload))
        return self._response


def _query_of(url: str) -> str:
    raw = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
    return raw


def test_duplicate_search_declares_the_result_type():
    """GitHub rejects a search/issues query that does not say what it wants.

    The live failure was:
        422 Query must include 'is:issue' or 'is:pull-request'

    Without this, every issue creation dies *after* the human gate is spent.
    """
    c = RecordingClient()
    c.find_issue_by_zoho_ticket("278544000000380001")

    q = _query_of(c.calls[0][1])
    assert "is:issue" in q, f"query must declare the type, got: {q}"


def test_duplicate_search_scopes_to_repo_marker_and_label():
    c = RecordingClient()
    c.find_issue_by_zoho_ticket("278544000000380001")

    q = _query_of(c.calls[0][1])
    assert "repo:devtron-labs/sprint-tasks" in q
    assert zoho_ticket_marker("278544000000380001") in q
    assert f"label:{PAGER_DUTY_LABEL}" in q


def test_duplicate_search_skipped_without_a_ticket_id():
    """No id means no provenance marker, so a search would match anything."""
    c = RecordingClient()
    assert c.find_issue_by_zoho_ticket("") is None
    assert c.calls == []


def test_duplicate_search_returns_none_when_nothing_matches():
    c = RecordingClient(response={"items": []})
    assert c.find_issue_by_zoho_ticket("123") is None


def test_duplicate_search_returns_the_existing_issue():
    c = RecordingClient(
        response={
            "items": [
                {
                    "number": 2960,
                    "html_url": "https://github.com/devtron-labs/sprint-tasks/issues/2960",
                }
            ]
        }
    )
    found = c.find_issue_by_zoho_ticket("123")
    assert found is not None
    assert found.number == 2960
    assert found.repo == "devtron-labs/sprint-tasks"


def test_create_issue_posts_to_the_repo_issues_endpoint():
    c = RecordingClient(
        response={
            "number": 3001,
            "html_url": "https://github.com/devtron-labs/sprint-tasks/issues/3001",
        }
    )
    got = c.create_issue(
        title="PagerBug: x", body="b", labels=[PAGER_DUTY_LABEL, AGENT_LABEL]
    )

    method, url, payload = c.calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/devtron-labs/sprint-tasks/issues"
    assert payload == {
        "title": "PagerBug: x",
        "body": "b",
        "labels": [PAGER_DUTY_LABEL, AGENT_LABEL],
    }
    assert got.number == 3001


def test_client_refuses_to_exist_without_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        GitHubSprintTasksClient(token="")
