"""The pipeline status page: server-rendered HTML of the /v1/status record."""

from datetime import UTC, datetime
from html import escape

from ran_lakehouse.api import models as m
from ran_lakehouse.api.contract import CONTRACT_VERSION

NO_LIVE_RUN = "no live run: the history was backfilled unpaced (simulated dates)"

STYLE = """
:root { --ink: #1d232a; --muted: #5b6570; --line: #d6dbe0; --bg: #ffffff; --head: #f2f4f6; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6e9ec; --muted: #9aa4ae; --line: #3a424b; --bg: #15191d; --head: #20262c; }
}
body { font: 15px/1.45 system-ui, sans-serif; color: var(--ink); background: var(--bg);
  margin: 0 auto; max-width: 1100px; padding: 16px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 8px; }
p { margin: 4px 0; color: var(--muted); }
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; }
th { background: var(--head); font-weight: 600; }
td.n { text-align: right; }
"""


def when(t: datetime) -> str:
    """A time for the page, in UTC to the minute.

    Args:
        t: Aware time.

    Returns:
        "YYYY-MM-DD HH:MM".
    """
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def table(headers: list[str], rows: list[list[str]], numeric: set[int]) -> str:
    """An HTML table.

    Args:
        headers: Column names.
        rows: Cells, already text.
        numeric: Indices of right-aligned columns.

    Returns:
        HTML.
    """
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="n">{escape(c)}</td>' if i in numeric else f"<td>{escape(c)}</td>"
            for i, c in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def render(status: m.Status) -> str:
    """The status page.

    Args:
        status: The status record.

    Returns:
        A complete HTML document.
    """
    clock = status.clock
    if status.simulated_time is not None:
        clock += f"; simulated time {when(status.simulated_time)} UTC"
    files = table(
        ["EMS", "Kind", "Files", "In the last day", "Skipped as repeats", "Latest arrival (UTC)"],
        [
            [
                f.ems,
                f.kind,
                f"{f.files:,}",
                f"{f.files_last_day:,}",
                f"{f.not_loaded:,}",
                when(f.latest_arrival),
            ]
            for f in status.files
        ],
        {2, 3, 4},
    )
    layers = table(
        ["Layer", "Table", "Rows"],
        [[r.layer, r.table, f"{r.rows:,}"] for r in status.layers],
        {2},
    )
    cases = table(
        ["Rule", "Flagged", "Count"],
        [[d.rule, d.kind, f"{d.count:,}"] for d in status.d_cases],
        {2},
    )
    events = table(
        ["Period start (UTC)", "Kind", "EMS", "Element", "File", "Detail"],
        [
            [
                when(e.period_start),
                e.kind,
                e.ems,
                e.managed_element or "",
                e.file_name or "",
                e.detail,
            ]
            for e in status.latest_quality_events
        ],
        set(),
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pipeline status</title>
<style>{STYLE}</style>
</head>
<body>
<h1>Pipeline status</h1>
<p>{escape(m.SYNTHETIC_NOTICE)}. Contract version {escape(CONTRACT_VERSION)}.</p>
<p>Clock: {escape(clock)}</p>
<h2>Files arriving per EMS</h2>
<p>A repeat is the same EMS, file name and content delivered again; it is skipped
(rule D2).</p>
{files}
<h2>Rows per layer</h2>
{layers}
<h2>Planted data-quality cases, as the pipeline flagged them</h2>
<p>Kind and count only (rule D). D7 (time travel) and D8 (lineage, <code>/v1/lineage</code>)
are queries, not flags.</p>
{cases}
<h2>Latest quality events (the last day of silver)</h2>
{events}
<p>Also as JSON: <a href="/v1/status">/v1/status</a>.</p>
</body>
</html>
"""
