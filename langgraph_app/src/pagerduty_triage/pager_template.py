"""The sprint-tasks pager issue template.

Everything in this module was read off real data, not written from memory:

* The 21 headings and their order are the modal template in
  ``data/corpus.json`` -- 108 of 135 closed ``pager-duty`` issues use exactly
  this sequence.
* The controlled vocabularies are the literal option lists that the issue
  form embeds as an HTML comment at the bottom of every pager issue
  (see sprint-tasks#2960).

Two things the design brief did not mention but the real data does:

* ``How was the user un-blocked?`` has its own controlled vocabulary
  (``user_unblocked_reason``), it is not free text.
* ``prod_environment``, ``user_unblocked`` and ``can_it_impact_other_clients``
  all admit ``unknown`` -- so "I could not tell from the ticket" is a legal
  answer, and is always better than a guess.

Real-data caveat that shapes ``normalise_affected_area``: 19 of 135 tickets
carry no affected area at all, reporters routinely type values outside the
vocabulary (``PANIC IN CODE`` appears 12 times), and case drifts freely
(``CI (Non Blocking)`` vs ``ci (non blocking)``). Downstream tooling -- the
past-PR index, and therefore the GitHub Action's localization -- keys on this
field. So we push hard toward the canonical vocabulary but we never silently
drop what the reporter meant: an unmatched value is surfaced, not discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Controlled vocabularies (verbatim from the issue form's option lists)
# --------------------------------------------------------------------------

AFFECTED_AREAS: tuple[str, ...] = (
    "devtron dashboard completely down",
    "security issue (secrets leak/log/visible etc)",
    "policies",
    "login issues",
    "rbac issues",
    "ci (blocking)",
    "ci (non blocking)",
    "cd (blocking new deployments)",
    "cd (blocking configs update)",
    "cd (non-blocking but potential to impact prod)",
    "cd (non-blocking)",
    "pre-cd (blocking)",
    "pre-cd (non blocking)",
    "bulk update",
    "app creation",
    "deployment from chart store",
    "ci/cd plugins",
    "other critical devtron functionality",
    "other non-critical devtron functionality",
    "other critical issue but potential to impact prod",
)

PROD_ENVIRONMENT: tuple[str, ...] = ("prod", "non-prod", "unknown")

IMPACT_PERCENTAGE: tuple[str, ...] = ("1%-5%", "5%-20%", "more than 20%", "unknown")

YES_NO_UNKNOWN: tuple[str, ...] = ("yes", "no", "unknown")

IMPACTED_CLIENT_COUNT: tuple[str, ...] = ("1-2", "more than 2", "unknown")

USER_UNBLOCKED_REASON: tuple[str, ...] = (
    "temporarily - by disabling a critical functionality",
    "temporarily - by disabling a non-critical functionality",
    "temporarily - by doing some changes from the backend/db",
    "permanently - by giving a workaround (from outside devtron)",
    "permanently - by giving a workaround (within devtron)",
)

REPORTED_BY: tuple[str, ...] = ("sre", "internal team", "client", "none")

NO_RESPONSE = "_No response_"


# --------------------------------------------------------------------------
# Affected-area normalisation
# --------------------------------------------------------------------------

def _squash(value: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation that drifts."""
    return re.sub(r"[\s]+", " ", value.strip().lower())


# Synonyms observed in the corpus or confirmed by the repo owner.
# Left-hand side is a squashed observed value; right-hand side is canonical.
_ALIASES: dict[str, str] = {
    # bare "cd" / "ci" appear 5 and 1 times respectively
    "cd": "cd (non-blocking)",
    "ci": "ci (non blocking)",
    "cd (non blocking)": "cd (non-blocking)",
    "ci (non-blocking)": "ci (non blocking)",
    "pre-cd (non-blocking)": "pre-cd (non blocking)",
    "rbac": "rbac issues",
    "login": "login issues",
    "other critical functionality": "other critical devtron functionality",
    "other critical issue but potential to impact prod": (
        "other critical issue but potential to impact prod"
    ),
    # 12 tickets say only "PANIC IN CODE". A panic is a crash in the
    # orchestrator, which is critical Devtron functionality -- but the phrase
    # itself carries real routing signal, so the caller is told the mapping
    # was inferred and should keep the original in "Additional affected areas".
    "panic in code": "other critical devtron functionality",
    "none": "",
}


@dataclass
class AreaMatch:
    """Result of pushing a free-form area toward the vocabulary."""

    canonical: str | None
    original: str
    exact: bool
    note: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.canonical)


def normalise_affected_area(value: str | None) -> AreaMatch:
    """Map a reporter's free-form affected area onto the vocabulary.

    Never raises and never invents. If nothing matches, ``canonical`` is
    ``None`` and ``original`` still carries what the reporter wrote so the
    caller can put it in *Additional affected areas* rather than losing it.
    """
    if value is None or not value.strip():
        return AreaMatch(None, original="", exact=False, note="no affected area given")

    original = value.strip()
    squashed = _squash(original)

    for area in AFFECTED_AREAS:
        if squashed == area:
            return AreaMatch(area, original, exact=True)

    if squashed in _ALIASES:
        target = _ALIASES[squashed]
        if not target:
            return AreaMatch(None, original, exact=False, note="reporter said 'none'")
        return AreaMatch(
            target,
            original,
            exact=False,
            note=f"mapped {original!r} -> {target!r}; keep the original wording "
            "in 'Additional affected areas'",
        )

    # Last resort: unique substring containment in either direction.
    hits = [a for a in AFFECTED_AREAS if squashed in a or a in squashed]
    if len(hits) == 1:
        return AreaMatch(
            hits[0],
            original,
            exact=False,
            note=f"loose match {original!r} -> {hits[0]!r}",
        )

    return AreaMatch(
        None,
        original,
        exact=False,
        note=f"{original!r} is outside the vocabulary and matched "
        f"{len(hits)} candidates; do not force a fit -- say so in the gate payload",
    )


# --------------------------------------------------------------------------
# The template itself
# --------------------------------------------------------------------------

# (heading, field name, allowed vocabulary or None for free text)
TEMPLATE_FIELDS: tuple[tuple[str, str, tuple[str, ...] | None], ...] = (
    ("\U0001f4dc Description", "description", None),
    ("Affected areas", "affected_areas", AFFECTED_AREAS),
    ("Additional affected areas", "additional_affected_areas", None),
    ("Prod/Non-prod environments?", "prod_environment", PROD_ENVIRONMENT),
    ("Impact percentage", "impact_percentage", IMPACT_PERCENTAGE),
    ("Is User still blocked?", "user_still_blocked", YES_NO_UNKNOWN),
    ("How was the user un-blocked?", "user_unblocked_reason", USER_UNBLOCKED_REASON),
    ("Can it impact other clients?", "can_impact_other_clients", YES_NO_UNKNOWN),
    ("Number of Clients Affected", "impacted_client_count", IMPACTED_CLIENT_COUNT),
    ("Reported by", "reported_by", REPORTED_BY),
    ("Why this is Pager?", "why_this_is_pager", None),
    ("Impact on Enterprise", "impact_on_enterprise", None),
    ("\U0001f45f Steps to replicate the Issue", "steps_to_replicate", None),
    ("\U0001f44d Expected behavior", "expected_behavior", None),
    ("\U0001f44e Actual Behavior", "actual_behavior", None),
    ("☸ Kubernetes version", "kubernetes_version", None),
    ("Cloud provider", "cloud_provider", None),
    ("\U0001f30d Browser", "browser", None),
    ("✅ Proposed Solution", "proposed_solution", None),
)

# The two trailing acknowledgement checkboxes are fixed text, not agent output.
TEMPLATE_FOOTER = (
    "### \U0001f440 Have you spent some time to check if this issue has been "
    "raised before?\n\n"
    "- [x] I checked and didn't find any similar issue\n\n"
    "### \U0001f3e2 Have you read the Code of Conduct?\n\n"
    "- [x] I have read the [Code of Conduct]"
    "(https://github.com/devtron-labs/devtron/blob/main/CODE_OF_CONDUCT.md)"
)

FREE_TEXT_FIELDS = frozenset(
    name for _, name, vocab in TEMPLATE_FIELDS if vocab is None
)
VOCAB_FIELDS: dict[str, tuple[str, ...]] = {
    name: vocab for _, name, vocab in TEMPLATE_FIELDS if vocab is not None
}


@dataclass
class TemplateValidation:
    """Non-fatal report on how well a filled template respects the vocabulary."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_fields(fields: dict[str, str]) -> TemplateValidation:
    """Check a filled template against the controlled vocabularies.

    Deliberately lenient about *unknown* values and strict about *missing*
    ones: a human is about to read this at the gate, and a half-filled
    template that says so is more useful than a confidently wrong one.
    """
    report = TemplateValidation()

    for heading, name, vocab in TEMPLATE_FIELDS:
        value = (fields.get(name) or "").strip()

        if not value:
            if name in ("description", "why_this_is_pager", "steps_to_replicate"):
                report.errors.append(f"{heading!r} is empty and is required")
            else:
                report.warnings.append(f"{heading!r} is empty; will render as {NO_RESPONSE}")
            continue

        if vocab is None:
            continue

        if name == "affected_areas":
            match = normalise_affected_area(value)
            if not match.matched:
                report.warnings.append(
                    f"affected area {value!r} is outside the vocabulary: {match.note}"
                )
            elif not match.exact:
                report.warnings.append(match.note)
            continue

        if value.lower() not in vocab:
            report.warnings.append(
                f"{heading!r} = {value!r} is outside its vocabulary {vocab}"
            )

    return report


def render_issue_body(fields: dict[str, str]) -> str:
    """Render the pager template exactly as the sprint-tasks issue form does."""
    chunks: list[str] = []
    for heading, name, _vocab in TEMPLATE_FIELDS:
        value = (fields.get(name) or "").strip() or NO_RESPONSE
        chunks.append(f"### {heading}\n\n{value}")
    chunks.append(TEMPLATE_FOOTER)
    return "\n\n".join(chunks) + "\n"


def blank_fields() -> dict[str, str]:
    """An empty field dict, so prompts and tests share one source of keys."""
    return {name: "" for _, name, _ in TEMPLATE_FIELDS}


def vocabulary_prompt_block() -> str:
    """The vocabularies, formatted for injection into the pager-scribe prompt."""
    lines = ["Controlled vocabularies. Use these values verbatim, lowercase:", ""]
    for heading, name, vocab in TEMPLATE_FIELDS:
        if vocab is None:
            continue
        lines.append(f"{name}  (heading: {heading!r})")
        lines.extend(f"    {v}" for v in vocab)
        lines.append("")
    return "\n".join(lines)
