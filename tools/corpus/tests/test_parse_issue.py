from parse_issue import parse_affected_areas, parse_pr_links

BODY = """### Affected areas

RBAC Issues

### Additional affected areas

None

## PR Links
- https://github.com/devtron-labs/devtron-enterprise/pull/3402
- https://github.com/devtron-labs/devtron/pull/7034
"""


def test_parses_single_affected_area():
    assert parse_affected_areas(BODY) == ["RBAC Issues"]


def test_ignores_none_in_additional_areas():
    assert "None" not in parse_affected_areas(BODY)


def test_parses_pr_links():
    assert parse_pr_links(BODY) == [
        "https://github.com/devtron-labs/devtron-enterprise/pull/3402",
        "https://github.com/devtron-labs/devtron/pull/7034",
    ]


def test_missing_sections_return_empty():
    assert parse_affected_areas("no headings here") == []
    assert parse_pr_links("no headings here") == []


def test_pr_links_deduplicated():
    body = BODY + "\n- https://github.com/devtron-labs/devtron/pull/7034\n"
    assert len(parse_pr_links(body)) == 2


def test_additional_affected_areas_section_is_not_conflated():
    """The given `test_ignores_none_in_additional_areas` passes vacuously: "None"
    is filtered by the none-check no matter which section it came from. This test
    fails if `_section` stops distinguishing the two headings."""
    body = """### Affected areas

RBAC Issues

### Additional affected areas

Helm Apps
"""
    assert parse_affected_areas(body) == ["RBAC Issues"]
    assert parse_affected_areas("### Additional affected areas\n\nHelm Apps\n") == []


# The real sprint-tasks template ships an HTML-commented example under "PR Links":
#   <!-- Example: - https://github.com/devtron-labs/devtron/pull/123 -->
# Scanning the raw body harvests that example as if it were a fix PR. It polluted
# all 60 issues on the first live run, and 23 of them had *only* the example.
TEMPLATE_BODY = """### Affected areas

RBAC Issues

<!--
Example affected area:
Helm Apps
-->

## PR Links
<!--
Include all the relevant PR links in a list format
Example:
- https://github.com/devtron-labs/devtron/pull/123
-->
- https://github.com/devtron-labs/devtron/pull/7034
"""


def test_ignores_example_pr_links_inside_html_comments():
    assert parse_pr_links(TEMPLATE_BODY) == [
        "https://github.com/devtron-labs/devtron/pull/7034"
    ]


def test_blank_pr_links_section_yields_no_links():
    body = "## PR Links\n<!--\nExample:\n- https://github.com/devtron-labs/devtron/pull/123\n-->\n- \n"
    assert parse_pr_links(body) == []


def test_ignores_affected_areas_inside_html_comments():
    assert parse_affected_areas(TEMPLATE_BODY) == ["RBAC Issues"]
