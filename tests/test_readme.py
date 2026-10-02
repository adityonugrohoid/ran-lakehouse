"""The README's headline numbers come from the committed reports (spec section 14)."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
REPORT = re.compile(r"`(results/[a-z_]+\.md)`")


def headline_bullets() -> list[str]:
    text = (REPO_ROOT / "README.md").read_text()
    section = text.split("## What the reports show", 1)[1].split("\n## ", 1)[0]
    return [b.replace("\n", " ") for b in section.split("\n- ")[1:]]


def test_every_headline_number_is_in_the_report_it_cites() -> None:
    bullets = headline_bullets()
    assert bullets
    for bullet in bullets:
        cited = REPORT.findall(bullet)
        assert cited, bullet
        report = " ".join((REPO_ROOT / c).read_text() for c in cited)
        for number in NUMBER.findall(bullet.split("`results/")[0]):
            assert number.rstrip(".,") in report, (number, cited)


def test_readme_shape() -> None:
    text = (REPO_ROOT / "README.md").read_text()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert text.startswith("# ran-lakehouse\n")
    assert headings[0] == "## Quickstart"
    assert headings[-2:] == ["## License", "## Author"]
