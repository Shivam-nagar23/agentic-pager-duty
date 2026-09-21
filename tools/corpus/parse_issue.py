"""Parse the Devtron pager-bug issue template."""
import re

PR_URL = re.compile(r"https://github\.com/devtron-labs/[\w.-]+/pull/\d+")

# The issue template carries its instructions — including an example PR link and
# example affected areas — inside HTML comments. They are invisible on GitHub but
# not to a regex, so strip them before parsing anything.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_comments(body: str) -> str:
    return HTML_COMMENT.sub("", body)


def _section(body: str, heading: str) -> str:
    """Text between `heading` and the next markdown heading."""
    pattern = re.compile(
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$(.*?)(?=^#{{1,3}}\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    return match.group(1) if match else ""


def parse_affected_areas(body: str) -> list[str]:
    text = _section(_strip_comments(body), "Affected areas")
    lines = [line.strip(" -*") for line in text.splitlines()]
    return [
        line for line in lines
        if line and line.lower() not in {"none", "_no response_"}
    ]


def parse_pr_links(body: str) -> list[str]:
    seen: dict[str, None] = {}
    for url in PR_URL.findall(_strip_comments(body)):
        seen.setdefault(url, None)
    return list(seen)
