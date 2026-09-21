"""Proof that the public interaction endpoint is closed.

The endpoint is a custom route on the LangGraph deployment, and LangGraph
leaves custom routes unauthenticated by design — Slack cannot present an API
key. So this HMAC is not *a* layer of the boundary, it is the boundary. A
request that gets past it resumes a thread that emails a paying customer and
opens pull requests.

Every test here is a way in that must stay shut.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from pagerduty_triage.slack.signing import (
    VerificationFailure,
    is_valid_request,
    signature_for,
    verify_request,
)

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"  # Slack's own doc example value
BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
NOW = 1_700_000_000.0


def _headers(ts: str, sig: str) -> dict[str, str]:
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def _signed(ts: float = NOW, secret: str = SECRET, body: bytes = BODY):
    stamp = str(int(ts))
    return _headers(stamp, signature_for(signing_secret=secret, timestamp=stamp, body=body))


# ---------------------------------------------------------------------------
# The scheme itself, against a hand-computed digest
# ---------------------------------------------------------------------------


def test_signature_matches_the_documented_scheme():
    """`v0:{ts}:{raw_body}`, HMAC-SHA256, hex, prefixed `v0=`.

    Computed here from first principles rather than by calling the function
    under test, so this fails if the scheme drifts.
    """
    ts = "1700000000"
    expected_digest = hmac.new(
        SECRET.encode("utf-8"),
        b"v0:" + ts.encode() + b":" + BODY,
        hashlib.sha256,
    ).hexdigest()

    assert signature_for(signing_secret=SECRET, timestamp=ts, body=BODY) == (
        f"v0={expected_digest}"
    )


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------


def test_a_genuine_recent_request_is_accepted():
    verify_request(
        signing_secret=SECRET, headers=_signed(), body=BODY, now=NOW
    )  # does not raise
    assert is_valid_request(
        signing_secret=SECRET, headers=_signed(), body=BODY, now=NOW
    )


def test_header_names_are_matched_case_insensitively():
    """HTTP header names are case-insensitive and Slack's docs warn not to
    assume the case."""
    stamp = str(int(NOW))
    sig = signature_for(signing_secret=SECRET, timestamp=stamp, body=BODY)

    verify_request(
        signing_secret=SECRET,
        headers={"x-slack-request-timestamp": stamp, "x-slack-signature": sig},
        body=BODY,
        now=NOW,
    )


def test_bytes_headers_from_a_raw_asgi_scope_are_accepted():
    stamp = str(int(NOW))
    sig = signature_for(signing_secret=SECRET, timestamp=stamp, body=BODY)

    verify_request(
        signing_secret=SECRET,
        headers={
            b"X-Slack-Request-Timestamp": stamp.encode(),
            b"X-Slack-Signature": sig.encode(),
        },
        body=BODY,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def test_unsigned_request_is_rejected():
    with pytest.raises(VerificationFailure):
        verify_request(signing_secret=SECRET, headers={}, body=BODY, now=NOW)


def test_missing_timestamp_is_rejected():
    _, sig = "", signature_for(
        signing_secret=SECRET, timestamp=str(int(NOW)), body=BODY
    )
    with pytest.raises(VerificationFailure):
        verify_request(
            signing_secret=SECRET,
            headers={"X-Slack-Signature": sig},
            body=BODY,
            now=NOW,
        )


def test_wrong_secret_is_rejected():
    headers = _signed(secret="a-different-signing-secret")
    with pytest.raises(VerificationFailure):
        verify_request(signing_secret=SECRET, headers=headers, body=BODY, now=NOW)


def test_tampered_body_is_rejected():
    """The whole point: a signature that was valid for *other* bytes is not
    valid for these."""
    headers = _signed()
    tampered = BODY.replace(b"block_actions", b"blXck_actions")

    with pytest.raises(VerificationFailure):
        verify_request(
            signing_secret=SECRET, headers=headers, body=tampered, now=NOW
        )


def test_replayed_old_request_is_rejected():
    """A capture from six minutes ago is refused even though its signature is
    genuine. This is the replay defence, and it is the reason the timestamp is
    inside the signed base string at all."""
    headers = _signed(ts=NOW)

    with pytest.raises(VerificationFailure):
        verify_request(
            signing_secret=SECRET, headers=headers, body=BODY, now=NOW + 60 * 6
        )


def test_request_just_inside_the_window_is_accepted():
    headers = _signed(ts=NOW)
    verify_request(
        signing_secret=SECRET, headers=headers, body=BODY, now=NOW + 60 * 4
    )


def test_future_dated_request_is_rejected():
    """Skew is absolute. A far-future timestamp would otherwise widen the
    replay window without limit."""
    headers = _signed(ts=NOW + 60 * 10)

    with pytest.raises(VerificationFailure):
        verify_request(signing_secret=SECRET, headers=headers, body=BODY, now=NOW)


def test_non_numeric_timestamp_is_rejected():
    sig = signature_for(signing_secret=SECRET, timestamp="not-a-number", body=BODY)
    with pytest.raises(VerificationFailure):
        verify_request(
            signing_secret=SECRET,
            headers=_headers("not-a-number", sig),
            body=BODY,
            now=NOW,
        )


def test_empty_signing_secret_rejects_everything():
    """An unconfigured deployment must accept nothing, not everything.

    This is the failure mode worth being paranoid about: a missing env var
    quietly turning verification off is how an endpoint like this becomes
    open, and it fails in the safe direction only if it is written to.
    """
    stamp = str(int(NOW))
    sig = signature_for(signing_secret="", timestamp=stamp, body=BODY)

    with pytest.raises(VerificationFailure):
        verify_request(
            signing_secret="", headers=_headers(stamp, sig), body=BODY, now=NOW
        )


def test_a_parsed_body_cannot_be_verified():
    """The HMAC covers the raw urlencoded bytes. Passing anything else in is a
    programming error and is refused rather than coerced."""
    with pytest.raises(VerificationFailure):
        verify_request(
            signing_secret=SECRET,
            headers=_signed(),
            body="payload={}",  # type: ignore[arg-type]
            now=NOW,
        )


def test_signature_comparison_is_constant_time():
    """Guard against someone 'simplifying' compare_digest into ==."""
    import inspect

    from pagerduty_triage.slack import signing

    src = inspect.getsource(signing)
    assert "compare_digest" in src, (
        "signature comparison must use hmac.compare_digest; a == on a hex "
        "digest leaks timing and is a published attack on this exact pattern"
    )


def test_no_code_path_returns_true_without_comparing():
    """Read the source: the only `return` in verify_request's happy path is
    the implicit one after compare_digest."""
    import inspect

    from pagerduty_triage.slack.signing import verify_request as fn

    src = inspect.getsource(fn)
    # Every early exit must be a raise, not a return.
    body = src.split('"""', 2)[-1]
    assert "return True" not in body
    assert "\n        return\n" not in body
