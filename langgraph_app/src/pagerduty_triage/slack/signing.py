"""Slack request signature verification. The security boundary, alone in a file.

This module imports nothing but the standard library, and nothing in it
imports anything else from this project. A boundary with dependencies is a
boundary with excuses.

## Why this is the *whole* boundary, not a layer of one

The interaction endpoint is a custom route on the LangGraph deployment, and
LangGraph's ``http.enable_custom_route_auth`` defaults to ``false`` — custom
routes bypass the API-key auth that protects ``/threads`` and ``/runs``. That
is not a misconfiguration, it is a requirement: Slack cannot present our
platform credentials, so the route must be reachable without them.

So the URL is public, and the only thing separating an internet stranger from
"send this text to a paying customer" is the HMAC below. "Only the owner has
access" is a true statement about a Slack workspace and a false one about a
public HTTPS endpoint. This check is what makes the first imply the second.

## The scheme, from Slack's documentation

Base string ``v0:{timestamp}:{raw_body}``, HMAC-SHA256 keyed by the app's
*signing secret* as a plain UTF-8 string, rendered as ``v0={hexdigest}``, and
compared with a constant-time compare. Headers are ``X-Slack-Signature`` and
``X-Slack-Request-Timestamp``, matched case-insensitively because HTTP header
names are case-insensitive and Slack's own docs warn not to assume the case.

Slack's documented replay window is five minutes.

## Three things it is easy to get wrong, and are not

1. **The body must be the raw bytes**, before any parsing. Slack posts
   ``application/x-www-form-urlencoded`` with a ``payload=`` field; the HMAC
   covers the percent-encoded bytes, not the decoded JSON. Re-encoding a
   parsed body produces a different string and a signature that never
   matches. :func:`verify_request` therefore takes ``bytes``.

2. **Every failure is a failure.** A missing header, an unparseable
   timestamp, a malformed signature, an empty signing secret — all return the
   same ``False``. There is no branch anywhere in this file that returns
   ``True`` without completing the HMAC comparison. In particular an unset
   ``SLACK_SIGNING_SECRET`` cannot mean "skip the check": it means every
   request is rejected.

3. **Compare with ``hmac.compare_digest``.** A ``==`` on a hex digest leaks
   timing, which is a genuine, published attack on exactly this pattern.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping

SIGNATURE_HEADER = "x-slack-signature"
TIMESTAMP_HEADER = "x-slack-request-timestamp"
VERSION = "v0"

#: Slack's documented replay window.
DEFAULT_MAX_SKEW_SECONDS = 60 * 5


class VerificationFailure(Exception):
    """Why a request was rejected. The reason is for our logs, never for the
    caller — telling an unauthenticated stranger *which* check they failed is
    free reconnaissance."""


def _headers_lower(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if isinstance(key, bytes):  # ASGI raw headers
            key = key.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        out[str(key).lower()] = str(value)
    return out


def signature_for(*, signing_secret: str, timestamp: str, body: bytes) -> str:
    """The signature Slack should have sent for this exact request.

    Exposed so tests can sign a request the same way Slack would — a test
    that hand-rolls its own signing is testing its own arithmetic, not ours.
    """
    basestring = f"{VERSION}:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return f"{VERSION}={digest}"


def verify_request(
    *,
    signing_secret: str,
    headers: Mapping[str, str],
    body: bytes,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: float | None = None,
) -> None:
    """Raise :class:`VerificationFailure` unless this really is a recent
    request from our Slack app.

    Returns ``None`` on success and raises on every other outcome, so a
    caller cannot accidentally treat a falsy return value as a pass.
    """
    if not signing_secret:
        # Fail closed. A deployment with no signing secret has no boundary,
        # and must therefore accept nothing at all.
        raise VerificationFailure("no signing secret is configured")

    if not isinstance(body, (bytes, bytearray)):
        raise VerificationFailure("body must be the raw request bytes")

    lower = _headers_lower(headers)
    timestamp = lower.get(TIMESTAMP_HEADER, "")
    signature = lower.get(SIGNATURE_HEADER, "")

    if not timestamp or not signature:
        raise VerificationFailure("missing signature headers")

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise VerificationFailure("unparseable timestamp") from exc

    current = time.time() if now is None else now
    # Absolute skew, so a *future*-dated request is rejected too — a clock
    # far ahead would otherwise widen the replay window indefinitely.
    if abs(current - sent_at) > max_skew_seconds:
        raise VerificationFailure("stale or future-dated timestamp")

    expected = signature_for(
        signing_secret=signing_secret, timestamp=timestamp, body=bytes(body)
    )
    if not hmac.compare_digest(expected, signature):
        raise VerificationFailure("signature mismatch")


def is_valid_request(
    *,
    signing_secret: str,
    headers: Mapping[str, str],
    body: bytes,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: float | None = None,
) -> bool:
    """Boolean form of :func:`verify_request`, for call sites that want one."""
    try:
        verify_request(
            signing_secret=signing_secret,
            headers=headers,
            body=body,
            max_skew_seconds=max_skew_seconds,
            now=now,
        )
    except VerificationFailure:
        return False
    return True
