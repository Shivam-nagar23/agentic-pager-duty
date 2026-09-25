"""Zoho Desk REST v1 client.

Every endpoint, parameter name and quirk below was verified against Zoho's
published API document and their first-party OpenAPI 3.1 spec
(`github.com/zoho/zohodesk-oas`), not written from memory. The non-obvious
ones are commented where they bite.

Nothing in this file is exercised by the test suite — the suite uses
`fake.FakeZohoDeskClient` and never touches a network. This is the code that
needs a careful first run against a sandbox org.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..settings import Settings
from .client import Attachment, ReplyResult, Thread, Ticket


class ZohoError(RuntimeError):
    """A Zoho API error, carrying the bits that decide how to react."""

    def __init__(self, status: int, error_code: str, message: str):
        super().__init__(f"Zoho {status} {error_code}: {message}")
        self.status = status
        self.error_code = error_code

    @property
    def is_daily_limit(self) -> bool:
        """Credits exhausted for the day. Back off hard; honour Retry-After."""
        return self.error_code == "THRESHOLD_EXCEEDED"

    @property
    def is_concurrency_limit(self) -> bool:
        """Too many in-flight requests. Retry with jitter, no Retry-After."""
        return self.error_code == "TOO_MANY_REQUESTS"

    @property
    def is_config_error(self) -> bool:
        """403 SCOPE_MISMATCH / OAUTH_ORG_MISMATCH.

        Both are 403, not 401, and **no token refresh will fix them** — they
        mean the OAuth scopes or the orgId are wrong. Keeping them out of the
        refresh path avoids an infinite re-auth loop on a config mistake.
        """
        return self.error_code in ("SCOPE_MISMATCH", "OAUTH_ORG_MISMATCH")


class RestZohoDeskClient:
    """Implements `ZohoDeskClient` over Zoho Desk REST v1."""

    def __init__(self, settings: Settings):
        self._s = settings
        self._token: str = ""
        self._token_expires_at: float = 0.0

    # -- auth ----------------------------------------------------------------

    #: What a client-credentials token is minted for. Read-only on purpose:
    #: `Desk.tickets.UPDATE` is what `sendReply` needs, so leaving it out makes
    #: "cannot email a customer" a property of the credential rather than of a
    #: flag somebody could flip.
    DEFAULT_SCOPES = (
        "Desk.tickets.READ,Desk.search.READ,Desk.basic.READ,"
        "Desk.channels.email.READ"
    )

    def _access_token(self) -> str:
        """An access token, cached until shortly before expiry.

        Two grants, chosen by whether a refresh token is configured.

        **Client credentials** (no refresh token set) needs only the client id
        and secret. Zoho's guidance is that Self Client suits "a stand-alone
        application that performs only back-end jobs like data-sync (without
        any manual intervention)", which is this poller exactly. It returns no
        refresh token and is not supposed to: each expiry re-mints. That
        removes the 10-minute one-time code from setup entirely — a step that
        has failed twice here, once by being pasted into ZOHO_REFRESH_TOKEN and
        once by expiring before use.

        **Refresh token** (one is set) is kept because existing deployments
        have one, and silently changing how they authenticate would be worse
        than carrying both paths. The refresh response returns no new refresh
        token; the long-lived one stays put. Zoho evicts refresh tokens at 20
        active per client per user, so minting more per process would quietly
        break older deployments.

        Access tokens live 3600s either way.
        """
        if self._token and time.time() < self._token_expires_at - 120:
            return self._token

        if self._s.zoho_refresh_token:
            params = {
                "refresh_token": self._s.zoho_refresh_token,
                "client_id": self._s.zoho_client_id,
                "client_secret": self._s.zoho_client_secret,
                "grant_type": "refresh_token",
            }
        else:
            if not self._s.zoho_org_id:
                # `soid` scopes the token to one Desk org. Without it the token
                # authenticates and then sees nothing -- a misconfiguration
                # that looks exactly like a quiet support queue.
                raise ZohoError(
                    0,
                    "config",
                    "client-credentials auth needs ZOHO_ORG_ID (it becomes "
                    "`soid=ZohoDesk.<org>`); set it, or set ZOHO_REFRESH_TOKEN "
                    "to use the refresh-token grant instead.",
                )
            params = {
                "client_id": self._s.zoho_client_id,
                "client_secret": self._s.zoho_client_secret,
                "grant_type": "client_credentials",
                "scope": self.DEFAULT_SCOPES,
                "soid": f"ZohoDesk.{self._s.zoho_org_id}",
            }

        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{self._s.zoho_accounts_host}/oauth/v2/token", data=body, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())

        if "access_token" not in payload:
            # Zoho returns HTTP 200 with an `error` key on a bad grant.
            raise ZohoError(200, payload.get("error", "unknown"), str(payload))

        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    # -- transport -----------------------------------------------------------

    def _request(
        self, method: str, path: str, *, params: dict | None = None, body: dict | None = None
    ) -> Any:
        url = f"{self._s.zoho_api_base}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            url = f"{url}?{urllib.parse.urlencode(clean)}"

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Zoho-oauthtoken {self._access_token()}")
        # Exact casing matters: lowercase o, capital I.
        req.add_header("orgId", self._s.zoho_org_id)
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            raise ZohoError(
                exc.code,
                payload.get("errorCode", "UNKNOWN"),
                payload.get("message", raw[:400].decode("utf-8", "replace")),
            ) from exc

    # -- ZohoDeskClient ------------------------------------------------------

    def list_tickets(
        self,
        *,
        modified_since: str | None = None,
        statuses: tuple[str, ...] = ("Open",),
        limit: int = 50,
        from_index: int = 1,
    ) -> list[Ticket]:
        """Tickets modified since `modified_since`.

        Uses `/tickets/search`, **not** `/tickets`. The plain list endpoint has
        no `modifiedTimeRange` and cannot sort by modified time, so it cannot
        answer "what changed recently" at all. The search endpoint can — at the
        cost of the index lag documented in `poller.py`.
        """
        # limit is capped at 100 by the API; `from` is 0-based here (the
        # non-search list endpoint is 1-based — an easy off-by-one).
        params: dict[str, Any] = {
            "from": max(0, from_index - 1),
            "limit": min(limit, 100),
            "sortBy": "modifiedTime",
        }
        if modified_since:
            now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            params["modifiedTimeRange"] = f"{modified_since},{now}"
        if statuses:
            params["status"] = ",".join(statuses)
        if self._s.zoho_department_id:
            params["departmentId"] = self._s.zoho_department_id

        payload = self._request("GET", "/tickets/search", params=params)
        return [self._ticket(row.get("ticket", row)) for row in payload.get("data", [])]

    def get_ticket(self, ticket_id: str) -> Ticket:
        return self._ticket(
            self._request("GET", f"/tickets/{ticket_id}", params={"include": "contacts"})
        )

    def list_threads(self, ticket_id: str, *, limit: int = 50) -> list[Thread]:
        """Full conversation with real message bodies.

        **This is 1 + N requests, unavoidably.** The thread *list* response
        carries `summary`, `author`, `direction` and so on but **no `content`**
        — verified against Zoho's own response examples. Each thread's body
        needs its own GET.

        `include=plainText` is passed on the detail call because it is the only
        accepted value and, without it, `content` arrives as HTML.

        Truncation is handled: when `isContentTruncated` is true the body is
        incomplete and the full text needs a *third* call to `fullContentURL`.
        Long customer email chains hit this routinely.
        """
        listing = self._request(
            "GET", f"/tickets/{ticket_id}/threads", params={"limit": min(limit, 100)}
        )
        out: list[Thread] = []
        for stub in listing.get("data", []):
            thread_id = stub.get("id")
            if not thread_id:
                continue
            detail = self._request(
                "GET",
                f"/tickets/{ticket_id}/threads/{thread_id}",
                params={"include": "plainText"},
            )
            content = detail.get("plainText") or detail.get("content") or ""
            if detail.get("isContentTruncated"):
                content = self._full_content(ticket_id, thread_id) or content
            out.append(
                Thread(
                    id=str(thread_id),
                    created_time=detail.get("createdTime", ""),
                    direction=detail.get("direction", "in"),
                    author=(detail.get("author") or {}).get("name", ""),
                    content=content,
                    content_type="plainText",
                    from_address=detail.get("fromEmailAddress", ""),
                    to_address=detail.get("to", ""),
                )
            )
        return out

    def _full_content(self, ticket_id: str, thread_id: str) -> str:
        try:
            payload = self._request(
                "GET", f"/tickets/{ticket_id}/threads/{thread_id}/fullContent"
            )
        except ZohoError:
            return ""
        return payload.get("plainText") or payload.get("content") or ""

    def list_attachments(self, ticket_id: str) -> list[Attachment]:
        payload = self._request(
            "GET", f"/tickets/{ticket_id}/attachments", params={"limit": 100}
        )
        return [
            Attachment(
                id=str(a.get("id", "")),
                name=a.get("name", ""),
                size=int(a.get("size") or 0),
                # Use the href Zoho returns; do not construct download URLs.
                # They sometimes 302 through galleryDocuments/ and most HTTP
                # clients drop the Authorization header across redirects.
                href=a.get("href", ""),
            )
            for a in payload.get("data", [])
        ]

    def send_reply(
        self,
        ticket_id: str,
        *,
        content: str,
        to_address: str,
        content_type: str = "html",
    ) -> ReplyResult:
        """Send a public reply.

        Three traps, all verified:

        * `channel`, `content` and `fromEmailAddress` are **required** by the
          schema.
        * `contentType` defaults to `plainText`. Omit it and HTML ships as
          literal markup in the customer's inbox.
        * `fromEmailAddress` must be a **verified From address of the
          department**. A wrong one is a 422 at send time, not a bounce later.

        The response returns `status: "PENDING"` — the send is asynchronous and
        can later flip to `FAILED`. Nothing here polls for that yet; see README.
        """
        from_email = self._s.zoho_from_email
        if not from_email:
            raise ZohoError(
                0,
                "MISSING_FROM_ADDRESS",
                "ZOHO_FROM_EMAIL is unset. Zoho requires a verified department "
                "From address on every reply.",
            )
        payload = self._request(
            "POST",
            f"/tickets/{ticket_id}/sendReply",
            body={
                "channel": "EMAIL",
                "content": content,
                "contentType": content_type,
                "fromEmailAddress": from_email,
                "to": to_address,
            },
        )
        return ReplyResult(
            thread_id=str(payload.get("id", "")),
            ticket_id=ticket_id,
            sent_at=payload.get("createdTime", ""),
            raw=payload,
        )

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _ticket(row: dict) -> Ticket:
        contact = row.get("contact") or {}
        return Ticket(
            id=str(row.get("id", "")),
            ticket_number=str(row.get("ticketNumber", "")),
            subject=row.get("subject", ""),
            description=row.get("description") or "",
            status=row.get("status", ""),
            created_time=row.get("createdTime", ""),
            modified_time=row.get("modifiedTime") or row.get("createdTime", ""),
            web_url=row.get("webUrl", ""),
            contact_name=" ".join(
                x for x in (contact.get("firstName"), contact.get("lastName")) if x
            ),
            account_name=(row.get("account") or {}).get("accountName", ""),
            priority=row.get("priority") or "",
            channel=row.get("channel") or "",
            email=row.get("email") or contact.get("email") or "",
            department_id=str(row.get("departmentId", "")),
        )
