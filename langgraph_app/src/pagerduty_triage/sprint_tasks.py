"""The other side of the seam: creating the sprint-tasks issue.

This is the handoff to the GitHub Action half of the project. Our
responsibility ends the moment the issue exists with the ``pager-duty`` label
-- applying that label is what triggers the Action, so labelling is not
cosmetic, it is the trigger.

Same shape as the Zoho seam: a Protocol, a real implementation, a fake.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

PAGER_DUTY_LABEL = "pager-duty"
AGENT_LABEL = "agent-triaged"

#: What the fix engine triggers on, in `sprint-tasks`. Deliberately not
#: `pager-duty`: the human process applies that to every pager issue, so
#: triggering on it would run the agent on everything ever filed.
#:
#: Applied at creation rather than by a human afterwards. Gate 2 is already the
#: authorisation -- a reviewer who approves the issue has seen the
#: classification and the filled template and said yes to the whole chain; a
#: second click adds nothing. It is in the gate payload, so it shows in the
#: Slack card *before* the approval, which is what keeps that approval informed.
#:
#: A human can still add it by hand to any pager issue the triage agent never
#: touched. That path is unchanged.
FIX_LABEL = "agent-fix"


@dataclass(frozen=True)
class CreatedIssue:
    number: int
    url: str
    repo: str


@runtime_checkable
class SprintTasksClient(Protocol):
    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: list[str],
    ) -> CreatedIssue:
        ...

    def find_issue_by_zoho_ticket(self, zoho_ticket_id: str) -> CreatedIssue | None:
        """Search for an already-created issue for this ticket.

        Second line of defence against a duplicate issue: the ledger is the
        first, but the ledger lives in our store and the issue lives in
        GitHub, so on a restore-from-backup the two can disagree. GitHub wins.
        """


@dataclass
class FakeSprintTasksClient:
    repo: str = "devtron-labs/sprint-tasks"
    created: list[dict] = field(default_factory=list)
    fail_next: Exception | None = None

    def create_issue(self, *, title: str, body: str, labels: list[str]) -> CreatedIssue:
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err
        number = 9000 + len(self.created)
        self.created.append({"title": title, "body": body, "labels": labels, "number": number})
        return CreatedIssue(
            number=number,
            url=f"https://github.com/{self.repo}/issues/{number}",
            repo=self.repo,
        )

    def find_issue_by_zoho_ticket(self, zoho_ticket_id: str) -> CreatedIssue | None:
        for row in self.created:
            if zoho_ticket_id and zoho_ticket_id in row["body"]:
                return CreatedIssue(
                    number=row["number"],
                    url=f"https://github.com/{self.repo}/issues/{row['number']}",
                    repo=self.repo,
                )
        return None


class GitHubSprintTasksClient:
    """Minimal GitHub REST client. No dependency beyond the stdlib.

    Deliberately not PyGithub: two endpoints do not justify a dependency, and
    a thin client is easier to audit for "can this thing do anything other
    than open an issue?" The answer is no -- there is no merge, no push, no
    write path other than ``POST /issues``.
    """

    def __init__(self, token: str | None = None, repo: str = "devtron-labs/sprint-tasks"):
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self.repo = repo
        if not self._token:
            raise RuntimeError("GITHUB_TOKEN is required to create sprint-tasks issues")

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raise RuntimeError(
                f"GitHub {method} {url} failed: {exc.code} {exc.read()[:400]!r}"
            ) from exc

    def create_issue(self, *, title: str, body: str, labels: list[str]) -> CreatedIssue:
        out = self._request(
            "POST",
            f"https://api.github.com/repos/{self.repo}/issues",
            {"title": title, "body": body, "labels": labels},
        )
        return CreatedIssue(number=out["number"], url=out["html_url"], repo=self.repo)

    def find_issue_by_zoho_ticket(self, zoho_ticket_id: str) -> CreatedIssue | None:
        if not zoho_ticket_id:
            return None
        # The marker line that ``zoho_ticket_marker`` writes into every body.
        marker = zoho_ticket_marker(zoho_ticket_id)
        # `is:issue` is not optional: GitHub rejects a search/issues query that
        # does not declare the result type with a 422, and that 422 lands
        # *after* the human gate is spent -- an approval that creates nothing.
        query = urllib.parse.quote(
            f'repo:{self.repo} is:issue in:body "{marker}" label:{PAGER_DUTY_LABEL}'
        )
        out = self._request(
            "GET", f"https://api.github.com/search/issues?q={query}&per_page=1"
        )
        items = out.get("items") or []
        if not items:
            return None
        return CreatedIssue(
            number=items[0]["number"], url=items[0]["html_url"], repo=self.repo
        )


def zoho_ticket_marker(zoho_ticket_id: str) -> str:
    """Stable, searchable provenance marker embedded in every created issue."""
    return f"zoho-ticket-id: {zoho_ticket_id}"
