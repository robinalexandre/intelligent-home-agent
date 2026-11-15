"""The documentation keeps up with the code: every setting is in .env.example and in
docs/settings.md, every optional feature is reachable from docs/setup.md, and links
between the pages lead somewhere."""

import re
from pathlib import Path

from intelligent_home_agent.config import Settings

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
# Pages that are not a feature: the guided path and the reference pages.
GUIDES = {"setup", "settings", "deployment"}
START = ["What it does", "What you need", "Settings", "Check it works", "Verified"]


def settings_names() -> list[str]:
    return [name.upper() for name in Settings.model_fields]


def test_every_setting_is_in_env_example_with_a_comment():
    lines = (ROOT / ".env.example").read_text().splitlines()
    where = {}
    for number, line in enumerate(lines):
        if found := re.match(r"(?:# ?)?([A-Z][A-Z0-9_]*)=", line):
            where.setdefault(found.group(1), []).append(number)
    missing = [name for name in settings_names() if name not in where]
    assert not missing, f"not in .env.example: {missing}"
    # Each setting once as a line of its own (an example of its value may follow, as a
    # comment), so a copy of the file never sets one twice.
    active = [m.group(1) for line in lines if (m := re.match(r"([A-Z][A-Z0-9_]*)=", line))]
    assert len(active) == len(set(active)), sorted({a for a in active if active.count(a) > 1})
    # A comment sits above each block of settings.
    for name in settings_names():
        above = where[name][0] - 1
        while above >= 0 and re.match(r"(?:# ?)?[A-Z][A-Z0-9_]*=", lines[above]):
            above -= 1
        assert above >= 0 and lines[above].startswith("#"), f"{name} has no comment"


def test_every_setting_is_in_the_settings_reference():
    text = (DOCS / "settings.md").read_text()
    named = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", text))
    missing = [name for name in settings_names() if name not in named]
    assert not missing, f"not in docs/settings.md: {missing}"
    # And nothing there that neither the agent nor a Compose file reads.
    compose = "".join(p.read_text() for p in ROOT.glob("compose/*.yml"))
    compose += (ROOT / "docker-compose.yml").read_text()
    table = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", text, flags=re.M))
    known = {*settings_names(), "COMPOSE_FILE", "ANTHROPIC_API_KEY"}
    stale = [name for name in table if name not in known and f"${{{name}" not in compose]
    assert not stale, f"docs/settings.md names settings nothing reads: {stale}"


def test_every_feature_page_is_reachable_from_the_setup_guide_and_starts_the_same_way():
    guide = (DOCS / "setup.md").read_text()
    for page in sorted(DOCS.glob("*.md")):
        if page.stem in GUIDES:
            continue
        assert f"({page.name}" in guide, f"docs/setup.md does not link {page.name}"
        lines = page.read_text().splitlines()
        assert lines[0].startswith("# ") and lines[2:4] == ["| | |", "|---|---|"], page.name
        labels = [re.match(r"\| \*\*(.+?)\*\* \|", line).group(1) for line in lines[4:9]]
        assert labels == START, page.name


def _slug(title: str) -> str:
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).strip().lower()
    return re.sub(r"[^\w\- ]", "", title).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    return {_slug(title) for title in re.findall(r"^#{1,6} (.+)$", text, flags=re.M)}


def test_links_between_the_pages_lead_somewhere():
    broken = []
    for page in [ROOT / "README.md", ROOT / "AGENTS.md", *DOCS.glob("*.md")]:
        text = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file, _, anchor = target.partition("#")
            dest = (page.parent / file).resolve() if file else page
            if not dest.exists() or (
                anchor and dest.suffix == ".md" and anchor not in _anchors(dest)
            ):
                broken.append(f"{page.name}: {target}")
    assert not broken, broken
