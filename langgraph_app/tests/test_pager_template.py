"""The pager template: vocabulary, normalisation, rendering.

Everything asserted here was read off `data/corpus.json` — 135 real closed
`pager-duty` issues — not invented. Where the corpus and the brief disagreed,
the corpus won.

Note what this file does *not* claim to test: whether the model classifies a
ticket correctly. That needs an eval against the corpus with a live model and
is not built. See README, "What is not proven".
"""

from __future__ import annotations

import pytest

from pagerduty_triage.pager_template import (
    AFFECTED_AREAS,
    IMPACT_PERCENTAGE,
    TEMPLATE_FIELDS,
    USER_UNBLOCKED_REASON,
    blank_fields,
    normalise_affected_area,
    render_issue_body,
    validate_fields,
    vocabulary_prompt_block,
)
from pagerduty_triage.prompts import CLASSIFICATIONS, MAIN_AGENT_PROMPT

# ---------------------------------------------------------------------------
# Vocabulary fidelity
# ---------------------------------------------------------------------------


def test_affected_areas_matches_the_issue_form():
    """The 20 values the sprint-tasks form actually offers."""
    assert len(AFFECTED_AREAS) == 20
    assert AFFECTED_AREAS[0] == "devtron dashboard completely down"
    assert "security issue (secrets leak/log/visible etc)" in AFFECTED_AREAS
    assert "policies" in AFFECTED_AREAS
    # All lowercase — downstream tooling string-matches these.
    assert all(a == a.lower() for a in AFFECTED_AREAS)


def test_user_unblocked_reason_is_a_vocabulary_not_free_text():
    """The brief listed this as free text; the real form has five options."""
    assert len(USER_UNBLOCKED_REASON) == 5
    assert all(
        v.startswith(("temporarily - ", "permanently - ")) for v in USER_UNBLOCKED_REASON
    )


def test_unknown_is_a_legal_answer_where_the_form_allows_it():
    """"I could not tell" must always beat a confident guess."""
    assert "unknown" in IMPACT_PERCENTAGE


def test_template_has_the_modal_nineteen_fields():
    """108 of 135 corpus tickets use exactly this heading sequence."""
    headings = [h for h, _, _ in TEMPLATE_FIELDS]
    assert headings[0] == "\U0001f4dc Description"
    assert headings[1] == "Affected areas"
    assert headings[-1] == "✅ Proposed Solution"
    assert len(TEMPLATE_FIELDS) == 19


# ---------------------------------------------------------------------------
# Normalisation: push toward the vocabulary, never discard meaning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("area", AFFECTED_AREAS)
def test_every_canonical_value_round_trips(area):
    match = normalise_affected_area(area)
    assert match.canonical == area
    assert match.exact is True


@pytest.mark.parametrize(
    "written,expected",
    [
        ("RBAC Issues", "rbac issues"),
        ("CI (Blocking)", "ci (blocking)"),
        ("CD (Blocking new deployments)", "cd (blocking new deployments)"),
        ("Other CRITICAL Devtron functionality", "other critical devtron functionality"),
        ("  Login Issues  ", "login issues"),
    ],
)
def test_case_and_whitespace_drift_is_absorbed(written, expected):
    """Reporters type these by hand; case drifts constantly in the corpus."""
    match = normalise_affected_area(written)
    assert match.canonical == expected


def test_panic_in_code_is_mapped_but_flagged():
    """`PANIC IN CODE` appears 12 times and is outside the vocabulary.

    A panic is critical Devtron functionality, so it maps — but the mapping
    is inferred, and the note tells the caller to preserve the reporter's
    original wording rather than silently losing "this was a panic".
    """
    match = normalise_affected_area("PANIC IN CODE")
    assert match.canonical == "other critical devtron functionality"
    assert match.exact is False
    assert "Additional affected areas" in match.note
    assert match.original == "PANIC IN CODE"


def test_bare_cd_and_ci_resolve_to_the_non_blocking_variants():
    """5 tickets say just "CD", 1 says just "CI"."""
    assert normalise_affected_area("CD").canonical == "cd (non-blocking)"
    assert normalise_affected_area("CI").canonical == "ci (non blocking)"


def test_missing_area_is_reported_not_guessed():
    """19 of 135 tickets carry no affected area at all."""
    match = normalise_affected_area(None)
    assert match.matched is False
    assert "no affected area" in match.note

    match = normalise_affected_area("   ")
    assert match.matched is False


def test_reporter_saying_none_is_not_forced_into_a_category():
    match = normalise_affected_area("None")
    assert match.matched is False
    assert "none" in match.note


def test_unrecognised_value_keeps_the_original_and_refuses_to_guess():
    match = normalise_affected_area("everything is on fire")
    assert match.matched is False
    assert match.original == "everything is on fire"
    assert "do not force a fit" in match.note


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_missing_required_fields_are_errors_not_warnings():
    report = validate_fields(blank_fields())
    joined = " ".join(report.errors)
    assert "Description" in joined
    assert "Why this is Pager?" in joined
    assert report.ok is False


def test_out_of_vocabulary_value_warns_but_does_not_block():
    fields = blank_fields()
    fields.update(
        description="x",
        why_this_is_pager="y",
        steps_to_replicate="z",
        impact_percentage="about half",
    )
    report = validate_fields(fields)
    assert report.ok is True, "a vocabulary slip must not block a human review"
    assert any("impact_percentage" in w or "Impact percentage" in w
               for w in report.warnings)


def test_unknown_passes_validation_cleanly():
    fields = blank_fields()
    fields.update(
        description="x",
        why_this_is_pager="y",
        steps_to_replicate="z",
        impact_percentage="unknown",
        prod_environment="unknown",
        can_impact_other_clients="unknown",
    )
    report = validate_fields(fields)
    assert report.ok is True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_emits_every_heading_in_order():
    body = render_issue_body(blank_fields())
    positions = [body.index(f"### {h}") for h, _, _ in TEMPLATE_FIELDS]
    assert positions == sorted(positions), "headings rendered out of template order"


def test_empty_fields_render_as_no_response_like_the_real_form():
    body = render_issue_body(blank_fields())
    assert "_No response_" in body


def test_footer_checkboxes_are_present():
    body = render_issue_body(blank_fields())
    assert "Code of Conduct" in body
    assert "- [x]" in body


def test_values_survive_rendering_verbatim():
    fields = blank_fields()
    fields["description"] = "Users see a 403 on /orchestrator/app-store"
    fields["affected_areas"] = "rbac issues"
    body = render_issue_body(fields)
    assert "Users see a 403 on /orchestrator/app-store" in body
    assert "### Affected areas\n\nrbac issues" in body


# ---------------------------------------------------------------------------
# Routing facts that must reach the model
# ---------------------------------------------------------------------------


def test_the_four_classifications_are_what_the_prompts_use():
    assert CLASSIFICATIONS == (
        "platform_query",
        "k8s_issue",
        "platform_bug",
        "needs_more_info",
    )
    for c in CLASSIFICATIONS:
        assert c in MAIN_AGENT_PROMPT


def test_owner_confirmed_routing_facts_are_in_the_prompt():
    """Two facts the owner confirmed that a model would otherwise get wrong."""
    from pagerduty_triage.prompts import DOMAIN_CONTEXT

    # Secret *handling*, not image-scan findings.
    assert "secret" in DOMAIN_CONTEXT.lower()
    assert "image-scan" in DOMAIN_CONTEXT or "image scan" in DOMAIN_CONTEXT
    # Policies are enterprise-only.
    assert "enterprise-only" in DOMAIN_CONTEXT.lower()


def test_vocabulary_block_reaches_the_scribe_prompt():
    from pagerduty_triage.prompts import PAGER_SCRIBE_PROMPT

    block = vocabulary_prompt_block()
    assert "affected_areas" in block
    assert "rbac issues" in block
    # The scribe must see the literal values, not a description of them.
    assert "rbac issues" in PAGER_SCRIBE_PROMPT
    assert "temporarily - by doing some changes from the backend/db" in PAGER_SCRIBE_PROMPT


def test_main_agent_is_told_a_rejection_means_revise():
    assert "rejected" in MAIN_AGENT_PROMPT
    assert "revise" in MAIN_AGENT_PROMPT.lower()
