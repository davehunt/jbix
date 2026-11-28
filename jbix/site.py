"""Build the JBI drift site: a static, multi-page view of the snapshot store.

Reads the data directory (see ``jbix.paths``) and writes a self-contained site:

- ``index.html`` — every tag: totals, drift over time, groups and a tag table
- ``groups/<group>.html`` — the same, for one group in ``groups.yaml``
- ``tags/<tag>.html`` — one tag: its trend, which fields drift, the bugs and
  issues that have drifted, and its broken links

Charts are inline SVG drawn here, coloured by CSS variables so they follow the
light and dark themes without redrawing, and the only script is the theme
control and the chart hover. Links are relative, so the site works from disk as
well as from a web server. It is built into a temporary directory and swapped
into place, so a tag that disappears never leaves a stale page behind.

Stdlib + pyyaml only (through ``jbix.groups``): it must run without credentials.

Usage: ``jbix-site [--data-dir DIR] [--out DIR]``
"""

import argparse
import html
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources

from jbix import groups as groups_mod
from jbix import paths
from jbix.snapshots import diff_values

DEFAULT_OUT = pathlib.Path("output", "site")
SITE_NAME = "JBIX Drift Report"
REPO_URL = "https://github.com/davehunt/jbix"
JBI_URL = "https://github.com/mozilla/jira-bugzilla-integration"
# Marks a directory as one this module built, so a rebuild may replace it.
MARKER = ".jbix-site"
SPARK_DAYS = 60

REPO_SETUP_URL = f"{REPO_URL}#setup"

# The jbix flags that sync each field the report shows, for the fix command.
FIX_FLAGS = {
    "summary": "--summary", "assignee": "--assignee", "components": "--components",
    "labels": "--whiteboard-labels --keyword-labels", "priority": "--priority",
    "severity": "--severity", "status": "--status", "resolution": "--resolution",
    "type": "--type", "links": "--dependencies --duplicates --see-also --regressions",
    "remote links": "--remote-links", "time_tracking": "--time-tracking",
    "deadline": "--time-tracking",
}

REASONS = {
    "dead-jira": "The Jira issue no longer exists",
    "stale-label": "Labelled for this tag, but no bug links to it",
    "whiteboard-removed": "The bug links to it, but no longer carries the tag",
    "unlinked": "The bug links to it, but the issue is not linked back",
}
DIRECTIONS = {"bug→jira": "Bugzilla → Jira", "jira→bug": "Jira → Bugzilla"}
# How to fix each kind of broken link, shown under a list of them. Unlike drift,
# none of these is fixed by a sync: each needs a person, in Bugzilla or in Jira.
FIXES = {
    "dead-jira": "Remove or correct the see-also link in Bugzilla.",
    "stale-label": "Remove the tag's label from the Jira issue, or link the issue from the bug it belongs to.",
    "whiteboard-removed": "Put the whiteboard tag back on the bug, or remove the tag's label from the Jira issue.",
    "unlinked": "Check that the bug's see-also link names this issue by its current key.",
}

# The fields a run can compare, by jbix's flag names (jbix.config.STEP_TO_FLAG),
# in the order the config section lists them. The last two are jbix's own.
CONFIG_FIELDS = {
    "summary": "Summary", "assignee": "Assignee", "components": "Components",
    "whiteboard_labels": "Whiteboard labels", "keyword_labels": "Keyword labels",
    "priority": "Priority", "severity": "Severity", "status": "Status", "resolution": "Resolution",
    "type": "Issue type", "dependencies": "Dependencies", "see_also": "See also",
    "duplicates": "Duplicates", "regressions": "Regressions",
    "time_tracking": "Time tracking", "remote_links": "Remote links",
}
EXTENSION_FIELDS = frozenset({"time_tracking", "remote_links"})
LINK_FIELDS = frozenset({"dependencies", "see_also", "duplicates", "regressions"})
# Synced by steps that, on an update, add only what changed (jbi/steps.py).
LABEL_FIELDS = frozenset({"whiteboard_labels", "keyword_labels"})
# The value maps the config section shows, for the fields that use them.
MAP_FIELDS = {"priority": "Priority", "severity": "Severity", "status": "Status",
              "resolution": "Resolution", "type": "Issue type"}
# JBI's labels_brackets, as a sentence ends.
BRACKETS = {"no": "without brackets", "yes": "with brackets", "both": "both with and without brackets"}

# Drawn, like the theme icons, so it is the same size everywhere.
CHEVRON = ('<svg class="chevron" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">'
           '<path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" stroke-width="1.6" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')

# Status icons beside a highlighted count, so state never rests on colour alone.
WARN_ICON = ('<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
             '<path d="M8 1.5 15 14H1z" fill="currentColor"/>'
             '<path d="M8 6v3.5M8 11.5v.5" stroke="var(--page)" stroke-width="1.6" stroke-linecap="round"/></svg>')
CRIT_ICON = ('<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
             '<circle cx="8" cy="8" r="7" fill="currentColor"/>'
             '<path d="M8 4.5V9M8 11.2v.3" stroke="var(--page)" stroke-width="1.6" stroke-linecap="round"/></svg>')
GOOD_ICON = ('<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
             '<circle cx="8" cy="8" r="7" fill="currentColor"/>'
             '<path d="M4.8 8.2 7 10.4l4.2-4.6" fill="none" stroke="var(--page)" stroke-width="1.7" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')


# -- data -------------------------------------------------------------------

@dataclass
class Point:
    """One day of one tag (or of a set of tags, summed)."""
    day: date
    pairs: int = 0
    drifted: int = 0
    broken: int = 0
    jira_total: int | None = None
    jira_linked: int | None = None
    bz_total: int | None = None
    bz_tagged: int | None = None

    @property
    def drift(self) -> float:
        return self.drifted / self.pairs if self.pairs else 0.0


@dataclass
class Tag:
    name: str
    project: str
    history: list[Point]
    latest: dict = field(default_factory=dict)
    changes_csv: pathlib.Path | None = None  # every change, uncapped (jbix.snapshots)

    @property
    def now(self) -> Point:
        return self.history[-1]


def _point(line: dict) -> Point:
    """A history line (see jbix.snapshots) as a Point."""
    diff, jira, bz = line.get("diff") or {}, line.get("jira"), line.get("bugzilla")
    return Point(
        day=datetime.fromisoformat(line["ts"]).date(),
        pairs=diff.get("total_pairs", 0),
        drifted=diff.get("drifted_pairs", 0),
        broken=line.get("broken_links", 0),
        jira_total=jira["total"] if jira else None,
        jira_linked=jira["linked"] if jira else None,
        bz_total=bz["total"] if bz else None,
        bz_tagged=bz["tagged"] if bz else None,
    )


def load_tag(name: str, history_path: pathlib.Path, latest_path: pathlib.Path) -> Tag | None:
    """A tag's history, one point per calendar day (the day's last run), and its latest detail."""
    lines = [json.loads(s) for s in history_path.read_text().splitlines() if s.strip()]
    if not lines:
        return None
    by_day: dict[date, Point] = {}
    for line in lines:
        p = _point(line)
        by_day[p.day] = p
    latest = json.loads(latest_path.read_text()) if latest_path.is_file() else {}
    changes = latest_path.with_suffix(".csv")
    return Tag(name, lines[-1].get("jira_project") or "", [by_day[d] for d in sorted(by_day)], latest,
               changes if changes.is_file() else None)


def load_tags(data_dir: pathlib.Path) -> dict[str, Tag]:
    out = {}
    for path in sorted((data_dir / "snapshots").glob("*.jsonl")):
        tag = load_tag(path.stem, path, data_dir / "latest" / f"{path.stem}.json")
        if tag:
            out[tag.name] = tag
    return out


def combine(tags: list[Tag]) -> list[Point]:
    """Sum several tags into one series, a point per day any of them was captured.

    A tag missing a day contributes its most recent earlier value, so a set of
    tags captured on different days still sums to a sensible total.
    """
    days = sorted({p.day for t in tags for p in t.history})
    out = []
    for d in days:
        total = Point(d)
        for t in tags:
            known = [p for p in t.history if p.day <= d]
            if known:
                p = known[-1]
                total.pairs += p.pairs
                total.drifted += p.drifted
                total.broken += p.broken
        out.append(total)
    return out


# -- formatting --------------------------------------------------------------

e = html.escape


def num(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def pct(ratio: float) -> str:
    v = ratio * 100
    if v == 0:
        return "0%"
    if v < 0.01:
        return "<0.01%"
    return f"{v:.2f}%" if v < 1 else f"{v:.1f}%"


def long_date(d: date) -> str:
    return f"{d.day} {d:%b} {d.year}"


def short_date(d: date) -> str:
    return f"{d.day} {d:%b}"


def safe_url(url: str | None) -> str | None:
    """Only web links from the data become hrefs."""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def link(url: str | None, text: str) -> str:
    u = safe_url(url)
    return f'<a href="{e(u)}">{e(text)}</a>' if u else e(text)


def url_tail(url: str | None) -> str:
    """The visible part of a bug or issue link: the bug id or the Jira key."""
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1].split("=")[-1]


def slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)


# -- charts -------------------------------------------------------------------

def _nice_max(v: float) -> float:
    for step in (0.5, 1, 2, 5, 10, 20, 25, 50, 100):
        if v <= step:
            return step
    return 100


def _nice_count_max(v: float) -> int:
    """A round top for a count axis whose half is a whole number too: 2, 4, 10, 20, 40, 100..."""
    step = 1
    while True:
        for m in (2, 4, 10):
            if v <= m * step:
                return m * step
        step *= 10


# What a line chart plots: how to get the value, label the axis, the end and the tooltip.
MEASURES = {
    "drift": {
        "value": lambda p: p.drift * 100,
        "top": lambda vmax: _nice_max(vmax * 1.1) if vmax else 1,  # all in sync: 0% to 1%
        "tick": lambda t: f"{t:g}%",
        "text": lambda p: pct(p.drift),
        "detail": lambda p: f"{num(p.drifted)} of {num(p.pairs)} pairs",
    },
    "broken": {
        "value": lambda p: p.broken,
        "top": lambda vmax: _nice_count_max(vmax * 1.1) if vmax else 2,  # none: 0 to 2
        "tick": lambda t: num(int(t)),
        "text": lambda p: num(p.broken),
        "detail": lambda p: f"{num(p.broken)} broken link{'s' if p.broken != 1 else ''}",
    },
}


def line_chart(points: list[Point], title: str, measure: str = "drift") -> str:
    """A measure over time: one series, hairline grid, end label, hover crosshair."""
    m = MEASURES[measure]
    w, h, left, right, top, bottom = 960, 220, 44, 64, 14, 26
    pw, ph = w - left - right, h - top - bottom
    values = [m["value"](p) for p in points]
    ymax = m["top"](max(values))
    d0, d1 = points[0].day.toordinal(), points[-1].day.toordinal()
    span = max(d1 - d0, 1)

    def x(p: Point) -> float:
        return left + (p.day.toordinal() - d0) / span * pw if len(points) > 1 else left + pw

    def y(v: float) -> float:
        return top + ph - v / ymax * ph

    grid = []
    for t in (0, ymax / 2, ymax):
        yy = y(t)
        label = m["tick"](t)
        grid.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
                    f'<text class="tick" x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{label}</text>')
    xlabels = [(points[0], short_date(points[0].day))]
    if span >= 45:
        seen = set()
        for p in points:
            month = (p.day.year, p.day.month)
            if month not in seen and p.day.day <= 7 and abs(x(p) - x(points[0])) > 60:
                xlabels.append((p, f"{p.day:%b}"))
            seen.add(month)
    if len(points) > 1:
        xlabels = [(p, s) for p, s in xlabels if abs(x(p) - x(points[-1])) > 60]
        xlabels.append((points[-1], short_date(points[-1].day)))
    for p, s in xlabels:
        anchor = "end" if p is points[-1] and len(points) > 1 else "start" if p is points[0] else "middle"
        grid.append(f'<text class="tick" x="{x(p):.1f}" y="{h - 6}" text-anchor="{anchor}">{e(s)}</text>')

    coords = [(x(p), y(v)) for p, v in zip(points, values)]
    marks = []
    if len(coords) > 1:
        path = " ".join(f"{'M' if i == 0 else 'L'}{cx:.1f},{cy:.1f}" for i, (cx, cy) in enumerate(coords))
        base = y(0)
        marks.append(f'<path class="area" d="{path} L{coords[-1][0]:.1f},{base:.1f} L{coords[0][0]:.1f},{base:.1f} Z"/>')
        marks.append(f'<path class="line" d="{path}"/>')
    ex, ey = coords[-1]
    marks.append(f'<circle class="dot" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
    marks.append(f'<text class="end-label" x="{ex + 8:.1f}" y="{ey + 4:.1f}">{m["text"](points[-1])}</text>')

    data = [[round(cx, 1), round(cy, 1), f"{long_date(p.day)} · {m['detail'](p)}", m["text"](p)]
            for (cx, cy), p in zip(coords, points)]
    first, last = points[0], points[-1]
    summary = (f"{title}: {m['text'](first)} on {long_date(first.day)}"
               + (f", {m['text'](last)} on {long_date(last.day)}" if len(points) > 1 else ""))
    return (
        f'<figure class="chart {measure}" data-points="{e(json.dumps(data))}" tabindex="0" '
        f'aria-label="{e(summary)}. Use the arrow keys to step through the days.">'
        f'<figcaption>{e(title)}</figcaption>'
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{e(summary)}">'
        + "".join(grid) + "".join(marks)
        + f'<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{top + ph}" style="display:none"/>'
        f'<circle class="hover-dot" r="4" cx="0" cy="0" style="display:none"/>'
        "</svg>"
        '<div class="tip" hidden><span class="tip-value"></span><span class="tip-label"></span></div>'
        "</figure>"
    )


def sparkline(points: list[Point]) -> str:
    """The shape of the last SPARK_DAYS of drift; the table carries the numbers."""
    recent = [p for p in points if (points[-1].day - p.day).days < SPARK_DAYS]
    if len(recent) < 2:
        return '<span class="muted">—</span>'
    w, h, pad = 96, 24, 3
    vals = [p.drift for p in recent]
    top = max(vals) or 1
    d0, span = recent[0].day.toordinal(), max(recent[-1].day.toordinal() - recent[0].day.toordinal(), 1)
    coords = [(pad + (p.day.toordinal() - d0) / span * (w - 2 * pad), h - pad - v / top * (h - 2 * pad))
              for p, v in zip(recent, vals)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{cx:.1f},{cy:.1f}" for i, (cx, cy) in enumerate(coords))
    ex, ey = coords[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<path class="line" d="{path}"/><circle class="dot" cx="{ex:.1f}" cy="{ey:.1f}" r="2.5"/></svg>')


def field_bars(fields: list[dict], pairs: int) -> str:
    """Which fields have drifted, as bars sharing one scale; the matching ones in a line."""
    drifting = sorted((f for f in fields if f.get("n")), key=lambda f: (-f["n"], f["name"]))
    matching = sorted(f["name"] for f in fields if not f.get("n"))
    if not drifting:
        return '<p class="muted">Every synced field matches.</p>'
    top = drifting[0]["n"]
    items = []
    for f in drifting:
        width = max(f["n"] / top * 100, 1.5)
        share = pct(f["n"] / pairs) if pairs else ""
        items.append(
            f'<li><span class="bar-name">{e(f["name"])}</span>'
            f'<span class="bar-track"><span class="bar" style="width:{width:.1f}%"'
            f' title="{e(f["name"])}: {num(f["n"])} pairs, {share}"></span>'
            f'<span class="bar-value">{num(f["n"])}'
            + (f' <span class="muted">({share})</span>' if share else "") + '</span></span></li>')
    tail = (f'<p class="muted">Matching everywhere: {e(", ".join(matching))}.</p>' if matching else "")
    return f'<ul class="bars">{"".join(items)}</ul>{tail}'


# -- page parts ---------------------------------------------------------------

INFO_ICON = ('<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
             '<circle cx="8" cy="8" r="7" fill="currentColor"/>'
             '<path d="M8 7.2v4.3M8 4.6v.3" stroke="var(--page)" stroke-width="1.6" stroke-linecap="round"/></svg>')
ICONS = {"warn": WARN_ICON, "crit": CRIT_ICON, "good": GOOD_ICON}

# What a section says when it has nothing to list: good news, so marked as such.
ALL_CLEAR = f'<p class="all-clear good">{GOOD_ICON}None.</p>'


def tiles(items: list[tuple]) -> str:
    """Stat tiles: label, value, a line of context, and optionally a status (warn, crit, good)."""
    cells = []
    for label, value, note, *status in items:
        kind = status[0] if status else None
        cls = f"tile {kind}" if kind else "tile"
        icon = ICONS[kind] if kind else ""
        cells.append(
            f'<div class="{cls}"><div class="tile-label">{icon}{e(label)}</div>'
            f'<div class="tile-value">{value}</div>'
            + (f'<div class="tile-note">{note}</div>' if note else "") + "</div>")
    return f'<div class="tiles">{"".join(cells)}</div>'


def status_tiles(now: Point, pairs_note: str) -> list[tuple]:
    """The three tiles every page leads with; the two problem counts are marked either way.

    Not zero is a warning (drift) or an alert (broken links); zero is good news.
    Zero drift is only good news where there are pairs that could have drifted.
    """
    drift = "warn" if now.drifted else ("good" if now.pairs else None)
    return [
        ("Linked pairs", num(now.pairs), pairs_note),
        ("Drifted", num(now.drifted), f"{pct(now.drift)} of linked pairs differ", *([drift] if drift else [])),
        ("Broken links", num(now.broken), "links to a missing bug or issue",
         "crit" if now.broken else "good"),
    ]


def summary_tiles(now: Point, n_tags: int) -> str:
    return tiles(status_tiles(now, f"bug and issue pairs across {n_tags} tag{'s' if n_tags != 1 else ''}"))


def sort_head(label: str, kind: str = "num", default: bool = False) -> str:
    """A column header the reader can click to sort by; ``default`` marks the starting order."""
    cls = ' class="n"' if kind == "num" else ""
    state = ' aria-sort="descending"' if default else ""
    return (f'<th scope="col"{cls}{state} data-sort="{kind}">'
            f'<button type="button">{e(label)}</button></th>')


def cell(value: float | int | str, text: str, numeric: bool = True) -> str:
    """A table cell carrying its raw value for sorting."""
    return (f'<td class="n" data-v="{value}">{text}</td>' if numeric
            else f'<td data-v="{e(str(value))}">{text}</td>')


def tag_table(tags: list[Tag], prefix: str, newest: date) -> str:
    """Every tag, worst drift first; any column sorts on a click."""
    rows = []
    for t in sorted(tags, key=lambda t: (-t.now.drift, -t.now.drifted, t.name)):
        p = t.now
        stale = f' <span class="muted">({short_date(p.day)})</span>' if p.day < newest else ""
        rows.append(
            f'<tr><th scope="row" data-v="{e(t.name)}"><a href="{prefix}tags/{e(slug(t.name))}.html">'
            f'{e(t.name)}</a>{stale}</th>'
            + cell(t.project, e(t.project), numeric=False) + cell(p.pairs, num(p.pairs))
            + cell(p.drifted, num(p.drifted)) + cell(round(p.drift, 6), pct(p.drift))
            + f'<td class="spark-cell">{sparkline(t.history)}</td>'
            + cell(p.broken, num(p.broken)) + "</tr>")
    return ('<div class="table-wrap"><table class="tags sortable"><thead><tr>'
            + sort_head("Tag", "text") + sort_head("Jira project", "text") + sort_head("Linked pairs")
            + sort_head("Drifted") + sort_head("Drift", default=True)
            + f'<th scope="col">Last {SPARK_DAYS} days</th>' + sort_head("Broken links")
            + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def history_table(points: list[Point]) -> str:
    """The chart's numbers, newest first, for readers who want them rather than the line."""
    rows = "".join(
        f'<tr><th scope="row">{long_date(p.day)}</th><td class="n">{num(p.pairs)}</td>'
        f'<td class="n">{num(p.drifted)}</td><td class="n">{pct(p.drift)}</td>'
        f'<td class="n">{num(p.broken)}</td></tr>'
        for p in reversed(points))
    return ('<details class="history"><summary>Every day as a table</summary><div class="table-wrap">'
            '<table><thead><tr><th scope="col">Day</th><th scope="col" class="n">Linked pairs</th>'
            '<th scope="col" class="n">Drifted</th><th scope="col" class="n">Drift</th>'
            f'<th scope="col" class="n">Broken links</th></tr></thead><tbody>{rows}</tbody></table>'
            '</div></details>')


def change(field: str, before, after) -> str:
    """What syncing from Bugzilla would do to one Jira field, taking Bugzilla as the authority.

    A list (labels, components) shows what would be added and removed. Each links
    row is one link to create or remove. Anything else shows the value it replaces.
    """
    values = diff_values(field, before, after)
    if values is None:
        return (f'<del>{e(_clip(before) or "none")}</del> <span class="muted">→</span> '
                f'<ins>{e(_clip(after) or "none")}</ins>')
    added, removed = values
    parts = ([f'<ins>+ {e(_clip(v))}</ins>' for v in added]
             + [f'<del>− {e(_clip(v))}</del>' for v in removed])
    return " ".join(parts) or '<span class="muted">reordered only</span>'


def drift_rows_table(diff: dict, csv_href: str | None = None) -> str:
    """One row per bug and issue pair, listing every field syncing would change.

    Above it, a field menu filters to the pairs where that field drifted,
    showing only that field's change; the choice lives in the URL as
    ``#field=<name>``, so a filtered view can be linked to. Without script every
    row is shown.

    The page lists the first changes only (``_DRIFT_ROWS_CAP`` in jbix.snapshots),
    so the menu's counts come from the complete field totals, and ``csv_href``
    links to every change.
    """
    rows = diff.get("rows") or []
    total = diff.get("rows_total", len(rows))
    if not rows:
        return ALL_CLEAR if not diff.get("drifted_pairs") else (
            '<p class="muted">No field-level detail was recorded for this run.</p>')
    pairs: dict[tuple, list[dict]] = {}
    for r in rows:
        pairs.setdefault((r.get("bug_url"), r.get("jira_url")), []).append(r)
    def fields_of(changes: list[dict]) -> list[str]:
        return sorted({r.get("field", "") for r in changes})

    body = "".join(
        f'<tr data-fields="{e("|".join(fields_of(changes)))}"><td>{link(bug, url_tail(bug))}</td>'
        f'<td>{link(issue, url_tail(issue))}</td><td><ul class="changes">'
        + "".join(f'<li data-field="{e(r.get("field", ""))}"><span class="change-field">'
                  f'{e(r.get("field", ""))}</span> '
                  f'{change(r.get("field", ""), r.get("before", ""), r.get("after", ""))}</li>'
                  for r in changes)
        + "</ul></td></tr>"
        for (bug, issue), changes in pairs.items())
    counts = {f["name"]: f["n"] for f in diff.get("fields") or [] if f.get("n")}
    drifted = diff.get("drifted_pairs", len(pairs))
    options = "".join(
        f'<option value="{e(f)}">{e(f)} ({num(n)})</option>'
        for f, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    filters = (f'<p class="filters"><label for="field-filter">Field</label> '
               f'<select id="field-filter"><option value="">All fields ({num(drifted)})</option>'
               f'{options}</select></p>'
               if len(counts) > 1 else "")
    download = (f' <a href="{e(csv_href)}" download>Download all {num(total)} changes (CSV)</a>'
                if csv_href else "")
    listed = (f"Listing {num(len(pairs))} of {num(drifted)} drifted pairs."
              if total > len(rows) else "")
    note = f'<p class="table-note muted">{listed}{download}</p>' if listed or download else ""
    # How to read the changes, so only where there are changes to read.
    intro = ('<p class="muted">Bugzilla is the authority: each change is what syncing would do '
             'to the Jira issue.</p>')
    return (intro + filters + '<div class="table-wrap"><table class="drift"><thead><tr><th scope="col">Bug</th>'
            '<th scope="col">Jira issue</th><th scope="col">What syncing from Bugzilla would change</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div>{note}')


def fixing_section(tag: str, fields: list[dict], config_url: str | None = None) -> str:
    """How to put this tag's drift right with jbix, preview first.

    Only the preview command is shown, so nothing copied from the page changes
    anything; prompt and apply are named as the next steps. The command follows
    the field menu above it, syncing only that field when one is chosen.
    ``config_url``, the tag's entry in JBI's config, is linked if known, else JBI.
    """
    base = f"python jbix.py --tags {tag} --mode preview"
    flags = {f["name"]: FIX_FLAGS[f["name"]] for f in fields if f.get("n") and f["name"] in FIX_FLAGS}
    by_field = {name: f"python jbix.py --tags {tag} --manual {flag} --mode preview"
                for name, flag in flags.items()}
    config_link = new_tab(config_url or JBI_URL, "JBI's configuration")
    return (
        '<h2 id="fixing">Fixing this drift</h2>'
        f'<p><a href="{REPO_URL}">JBIX</a> can sync these pairs from Bugzilla to Jira. Once it is '
        f'<a href="{REPO_SETUP_URL}">set up</a>, start with a preview, which lists every change it '
        'would make and makes none:</p>'
        f'<div class="command"><code id="fix-command" data-all="{e(base)}" '
        f'data-by-field="{e(json.dumps(by_field))}">{e(base)}</code>'
        '<button type="button" class="copy" hidden>Copy</button></div>'
        '<p class="muted">Choose a field above to sync only that field: the command gains '
        '<code>--manual</code> and that field\'s option.</p>'
        '<p>When the preview looks right, run it again with <code>--mode prompt</code> to approve '
        'each change in turn, or <code>--mode apply</code> to make them all if you are confident.</p>'
        f'<aside class="callout warn" aria-labelledby="before-you-apply">'
        f'<p class="callout-title" id="before-you-apply">{WARN_ICON}Before you apply</p>'
        '<ul class="cautions">'
        '<li><strong>Bugzilla wins.</strong> A value someone changed in Jira on purpose is '
        'overwritten with Bugzilla\'s.</li>'
        '<li><strong>Changes are made as you,</strong> through your Jira token. Status changes and '
        'new links notify the issue\'s watchers. Other fields are updated quietly, which Jira only '
        'allows if you can administer the project: otherwise JBIX stops and says so, and '
        '<code>--notify</code> makes the edits with notifications, once the project\'s owners '
        'agree.</li>'
        '<li><strong>Drift that keeps coming back</strong> may need a change to '
        f'{config_link} rather than another sync.</li>'
        '</ul></aside>'
        '<p>This report shows your changes after its next run is deployed. To check them sooner, '
        f'run <code>python jbix.py --tags {e(tag)} --mode check</code>, which rebuilds the report '
        'locally in <code>output/site/</code>.</p>'
    )


def _fields_text(names: list[str]) -> str:
    return ", ".join(CONFIG_FIELDS.get(n, n) for n in names)


def _whose(entry: dict) -> str:
    return "JBI's default" if entry.get("default") else "set for this tag"


def _codes(values) -> str:
    """Values as a run of code chips, which wrap rather than take a line each."""
    return "".join(f"<li><code>{e(str(v))}</code></li>" for v in values)


def new_tab(url: str | None, text: str) -> str:
    """A link that opens in a new tab: for pages off the report, such as JBI's code."""
    u = safe_url(url)
    return f'<a href="{e(u)}" target="_blank" rel="noopener">{e(text)}</a>' if u else e(text)


def _map_row(label: str, entry: dict) -> str:
    """One value map: the values kept as they are on one line, then each translation on its own."""
    value = entry.get("value") or {}
    whose = f' <span class="muted">({_whose(entry)})</span>'
    if not value:
        return f'<dt>{e(label)}{whose}</dt><dd class="muted">None: no value is mapped.</dd>'
    same = [k for k, v in value.items() if str(k) == str(v)]
    changed = [(k, v) for k, v in value.items() if str(k) != str(v)]

    def code(v) -> str:
        # An empty value (Bugzilla's unset priority) would be a blank chip; name it.
        return f"<code>{e(str(v))}</code>" if str(v) else '<span class="muted">(empty)</span>'

    items = ([f'<li><span class="muted">Unchanged:</span> <ul class="codes">{_codes(same)}</ul></li>']
             if same else [])
    items += [f"<li>{code(k)} → {code(v)}</li>" for k, v in changed]
    return f'<dt>{e(label)}{whose}</dt><dd><ul class="translations">{"".join(items)}</ul></dd>'


def _steps_table(check: dict, steps: dict, defaults_url: str | None) -> str:
    """JBI's steps for a new and an existing bug side by side, folded.

    Grouped by the field each step syncs, in the order fields are listed above,
    then the steps that sync no field, so a step on one side only sits beside
    its field. A row whose field is synced on one side only is marked. A kind the
    tag leaves to JBI's defaults links to them, as they are not copied here.
    """
    rows = check.get("rows") or []
    if not rows and steps.get("new") is None and steps.get("existing") is None:
        defaults = new_tab(defaults_url, "JBI's defaults")
        return (f'<p class="steps-default muted">JBI\'s steps: not set for this tag, so '
                f'{defaults} apply to new and existing bugs.</p>')
    flagged = set(check.get("existing_only") or []) | set(check.get("new_only") or [])
    order = {f: i for i, f in enumerate(CONFIG_FIELDS)}
    rows = sorted(rows, key=lambda r: order.get(r["field"], len(order)) if r["field"] else len(order) + 1)

    def cell(value) -> str:
        if value is None:
            return '<td class="muted">default</td>'
        return '<td>✓</td>' if value else '<td class="muted">–</td>'

    body = []
    for r in rows:
        field = r["field"]
        mismatch = field in flagged
        syncs = e(CONFIG_FIELDS.get(field, field)) if field else ""
        body.append('<tr class="mismatch">' if mismatch else "<tr>")
        # A field create_issue sets is not missing for a new bug, so say how it is set.
        new = (f'<td class="muted">by <code>{e(r["new_via"])}</code></td>' if r.get("new_via")
               else cell(r["new"]))
        body.append(f'<td><code>{e(r["step"])}</code></td>'
                    f'<td>{syncs}{" " + WARN_ICON if mismatch else ""}</td>'
                    f'{new}{cell(r["existing"])}</tr>')
    body = "".join(body)
    unset = [key for key in ("new", "existing") if steps.get(key) is None]
    defaults = new_tab(defaults_url, "JBI's defaults")
    note = (f'<p class="table-note muted">Not set for {" or ".join(unset)} bugs, so {defaults} '
            'apply there, and steps synced on one side only cannot be found.</p>' if unset else "")
    return ('<details class="history"><summary>JBI\'s steps, for new and existing bugs</summary>'
            '<div class="table-wrap"><table class="steps"><thead><tr><th scope="col">Step</th>'
            '<th scope="col">Syncs</th><th scope="col">New bug</th><th scope="col">Existing bug</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div>{note}</details>')


def _mismatch_callout(check: dict, compared: set) -> str:
    """Fields synced for new bugs or existing bugs but not both, and the drift that follows."""
    items = []
    existing_only = check.get("existing_only") or []
    # Labels and other fields drift differently, so each gets its own explanation.
    if labels := [f for f in existing_only if f in LABEL_FIELDS]:
        items.append(
            f'<li><strong>Synced for existing bugs but not new ones:</strong> {e(_fields_text(labels))}. '
            'An issue JBI creates starts without these labels, and the label steps only add what changes '
            'afterwards, so they stay missing and every new issue drifts.</li>')
    if others := [f for f in existing_only if f not in LABEL_FIELDS]:
        one = len(others) == 1
        items.append(
            f'<li><strong>Synced for existing bugs but not new ones:</strong> {e(_fields_text(others))}. '
            f'An issue JBI creates starts without {"it" if one else "them"}, which shows when the bug '
            'already has a value at that point, for example when the tag is added to an older bug. A '
            f'later change in Bugzilla brings {"it" if one else "them"} into line.</li>')
    if new_only := check.get("new_only"):
        one = len(new_only) == 1
        unseen = [f for f in new_only if f not in compared]
        items.append(
            f'<li><strong>Synced for new bugs but not existing ones:</strong> '
            f'{e(_fields_text(new_only))}. {"It is" if one else "They are"} set when JBI creates the '
            'issue, but a later change in Bugzilla is not copied to Jira.'
            + (f' This report compares the existing-bug steps, so it does not count '
               f'{"this" if len(unseen) == 1 else "these"} either.' if unseen else '') + '</li>')
    if not items:
        return ""
    return ('<aside class="callout warn" aria-labelledby="steps-differ">'
            f'<p class="callout-title" id="steps-differ">{WARN_ICON}New and existing bugs are synced differently</p>'
            f'<ul class="fixes">{"".join(items)}</ul></aside>')


def config_section(jbi: dict) -> str:
    """How the latest run compared this tag: JBI's config for it, and any override.

    Answers the questions a reader of the drift brings: which fields are compared
    and why not others, how a value is translated, and whether the run did what
    JBI's config says. Links to the tag's lines at the commit the run read, so the
    link still shows what was used after JBI's config changes.
    """
    source = jbi.get("source") or {}
    url = safe_url(source.get("url"))
    commit, lines = source.get("commit"), source.get("lines")
    where = new_tab(url, "JBI's configuration")
    if commit:
        at = f" at commit <code>{e(commit[:12])}</code>"
        if lines:
            at += f", lines {num(lines[0])} to {num(lines[1])}"
    else:
        at = ", read from its main branch"
    desc = jbi.get("description")
    intro = (f'<p>Which fields are compared, and how values are translated for Jira, come from '
             f'this tag\'s entry in {where}{at}.'
             + (f' JBI describes it as “{e(desc)}”.' if desc else "") + '</p>')

    jbi_fields, compared = set(jbi.get("fields") or []), set(jbi.get("compared") or [])
    names = [n for n in CONFIG_FIELDS if n in jbi_fields | compared] + sorted(
        (jbi_fields | compared) - set(CONFIG_FIELDS))
    # A field synced for new bugs only is explained in the warning above, not here.
    new_only = set((jbi.get("step_check") or {}).get("new_only") or [])
    unsynced = [n for n in CONFIG_FIELDS
                if n not in jbi_fields | compared | new_only and n not in EXTENSION_FIELDS]
    notes = []
    if shown_fields := [n for n in names if n in compared]:
        notes.append(f'<li><strong>Compared:</strong> {e(_fields_text(shown_fields))}.</li>')
    if left_out := [n for n in names if n in jbi_fields and n not in compared]:
        one = len(left_out) == 1
        notes.append(f'<li><strong>Left out of this run:</strong> {e(_fields_text(left_out))}. JBI syncs '
                     f'{"it" if one else "them"}, but the run that made this report was told not to '
                     f'compare {"it" if one else "them"}, so {"its" if one else "their"} drift is not '
                     'counted.</li>')
    if added := [n for n in names if n in compared and n not in jbi_fields]:
        notes.append(f'<li><strong>Compared, though JBI does not sync them:</strong> '
                     f'{e(_fields_text(added))}.</li>')
    if unsynced:
        notes.append(f'<li><strong>Not compared, as JBI does not sync them:</strong> '
                     f'{e(_fields_text(unsynced))}.</li>')
    brackets = jbi.get("labels_brackets") or {}
    if "whiteboard_labels" in compared and brackets.get("value"):
        notes.append(f'<li>Whiteboard labels are written {BRACKETS.get(brackets["value"], e(str(brackets["value"])))}'
                     f'{" (JBI&#39;s default)" if brackets.get("default") else ""}.</li>')
    excludes = jbi.get("linked_project_excludes") or {}
    if compared & LINK_FIELDS and excludes.get("value"):
        notes.append(f'<li>Links to issues in {e(", ".join(excludes["value"]))} are never made'
                     f'{" (JBI&#39;s default)" if excludes.get("default") else ""}, so they are not expected.</li>')
    callout = ('<aside class="callout info" aria-labelledby="what-was-compared">'
               f'<p class="callout-title" id="what-was-compared">{INFO_ICON}What was compared</p>'
               f'<ul class="fixes">{"".join(notes)}</ul></aside>')

    maps = jbi.get("maps") or {}

    def map_row(f: str) -> str:
        # Every mapped field gets a row, so one missing from the list is never a puzzle:
        # a map that is not used says why, rather than leaving the field out.
        label = MAP_FIELDS[f]
        if f in compared and f in maps:
            return _map_row(label, maps[f])
        why = ("JBI syncs it, but this run left it out" if f in jbi_fields
               else f"not synced for this tag, so JBI leaves Jira's {label.lower()} as it is")
        return f'<dt>{e(label)}</dt><dd class="muted">Not compared: {why}.</dd>'

    maps_html = ('<details class="history"><summary>How values are translated</summary>'
                 '<dl class="mappings">' + "".join(map_row(f) for f in MAP_FIELDS)
                 + '</dl></details>' if maps else "")
    steps = jbi.get("steps") or {}
    # A latest file recorded before steps were compared has no table to show.
    check = jbi.get("step_check")
    steps_html = _steps_table(check, steps, source.get("defaults_url")) if check else ""
    return ('<h2 id="config">How this was compared</h2>' + intro
            + (_mismatch_callout(check, compared) if check else "") + callout + maps_html + steps_html)


def broken_table(rows: list[dict], total: int, prefix: str | None = None,
                 csv_href: str | None = None) -> str:
    """Broken links as a table, then how to fix each kind present.

    Given ``prefix``, each row also names its tag (``row["tag"]``), linked to the
    tag's page, as a group page lists several tags' links together.
    """
    if not rows:
        return ALL_CLEAR
    with_tag = prefix is not None

    def tag_cell(r):
        if not with_tag:
            return ""
        t = r["tag"]
        return f'<td data-v="{e(t)}"><a href="{prefix}tags/{e(slug(t))}.html#broken">{e(t)}</a></td>'

    body = "".join(
        f'<tr>{tag_cell(r)}'
        f'<td data-v="{e(r.get("direction", ""))}">{e(DIRECTIONS.get(r.get("direction", ""), r.get("direction", "")))}</td>'
        f'<td data-v="{e(str(r.get("bug_id") or ""))}">{link(r.get("bug_url"), str(r.get("bug_id") or "—"))}</td>'
        f'<td data-v="{e(r.get("jira_key", ""))}">{link(r.get("jira_url"), r.get("jira_key", ""))}</td>'
        f'<td data-v="{e(r.get("reason", ""))}">{e(REASONS.get(r.get("reason", ""), r.get("reason", "")))}</td></tr>'
        for r in rows)
    heads = ([sort_head("Tag", "text")] if with_tag else []) + [
        sort_head("Direction", "text"), sort_head("Bug", "text"), sort_head("Jira issue", "text"),
        sort_head("Why it is broken", "text")]
    listed = (f"Listing {num(len(rows))} of {num(total)}." if total > len(rows) else "")
    download = (f' <a href="{e(csv_href)}" download>Download all {num(len(rows))} (CSV)</a>'
                if csv_href else "")
    note = f'<p class="table-note muted">{listed}{download}</p>' if listed or download else ""
    present = [r for r in FIXES if any(row.get("reason") == r for row in rows)]
    fixes = ('<aside class="callout info" aria-label="How to fix these">'
             f'<p class="callout-title">{INFO_ICON}How to fix these</p><ul class="fixes">'
             + "".join(f"<li><strong>{e(REASONS[r])}:</strong> {e(FIXES[r])}</li>" for r in present)
             + "</ul></aside>" if present else "")
    return ('<div class="table-wrap"><table class="sortable"><thead><tr>' + "".join(heads)
            + f'</tr></thead><tbody>{body}</tbody></table></div>{note}{fixes}')


def write_broken_csv(path: pathlib.Path, rows: list[dict]) -> None:
    """Broken links, with their tag, as a CSV to download."""
    import csv
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["tag", "direction", "bug_id", "bug_url", "jira_key", "jira_url", "why"])
        for r in rows:
            w.writerow([r["tag"], DIRECTIONS.get(r.get("direction", ""), r.get("direction", "")),
                        r.get("bug_id") or "", r.get("bug_url") or "", r.get("jira_key", ""),
                        r.get("jira_url", ""), REASONS.get(r.get("reason", ""), r.get("reason", ""))])


def tagged_broken(tags: list["Tag"]) -> tuple[list[dict], int]:
    """Every broken link the latest runs of ``tags`` recorded, each with its tag, and their total."""
    rows = [{**r, "tag": t.name} for t in sorted(tags, key=lambda t: t.name)
            for r in t.latest.get("broken_links") or []]
    total = sum(t.latest.get("broken_links_total", len(t.latest.get("broken_links") or [])) for t in tags)
    return rows, total


def _clip(value: str, n: int = 80) -> str:
    value = str(value)
    return value if len(value) <= n else value[: n - 1] + "…"


# -- pages --------------------------------------------------------------------

THEME_HEAD = """<script>
(function () {
  var t = localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
})();
</script>"""

SUN_ICON = (
    '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round">'
    '<circle cx="8" cy="8" r="3"/><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15'
    'M3.05 3.05l1.06 1.06M11.89 11.89l1.06 1.06M3.05 12.95l1.06-1.06M11.89 4.11l1.06-1.06"/></svg>'
)
MOON_ICON = (
    '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="currentColor">'
    '<path d="M13.5 10.2A6 6 0 0 1 5.8 2.5a6 6 0 1 0 7.7 7.7z"/></svg>'
)

SCRIPTS = """<script>
(function () {
  var root = document.documentElement;
  var toggle = document.getElementById("theme-toggle");
  var buttons = toggle.querySelectorAll("button");
  function current() {
    return root.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function label() {
    var t = current();
    toggle.dataset.current = t;  // places the sliding highlight
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.themeChoice === t ? "true" : "false");
    });
  }
  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      root.dataset.theme = b.dataset.themeChoice;
      localStorage.setItem("theme", b.dataset.themeChoice);
      label();
    });
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", label);
  function apply() {
    var t = localStorage.getItem("theme");
    if (t === "light" || t === "dark") root.dataset.theme = t;
    else delete root.dataset.theme;
    label();
  }
  addEventListener("pageshow", apply);
  addEventListener("storage", function (e) { if (e.key === "theme") apply(); });
  label();
  // Animate only from here on, so the highlight starts in place rather than
  // sliding into it as each page loads.
  requestAnimationFrame(function () { requestAnimationFrame(function () {
    toggle.classList.add("ready");
  }); });

  // The groups menu is a <details>, so it works without this; this closes it on
  // a click elsewhere or Escape, as a menu is expected to.
  var menu = document.querySelector("details.menu");
  if (menu) {
    document.addEventListener("click", function (ev) {
      if (menu.open && !menu.contains(ev.target)) menu.open = false;
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && menu.open) { menu.open = false; menu.querySelector("summary").focus(); }
    });
  }

  // Sortable tables: a click on a column's header sorts by it, again to reverse.
  // Each cell carries its raw value in data-v; numbers start biggest first.
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var heads = table.querySelectorAll("th[data-sort]");
    heads.forEach(function (th) {
      th.querySelector("button").addEventListener("click", function () {
        var numeric = th.dataset.sort === "num";
        var current = th.getAttribute("aria-sort");
        var dir = current ? (current === "descending" ? "ascending" : "descending")
                          : (numeric ? "descending" : "ascending");
        var i = th.cellIndex, body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = a.cells[i].dataset.v, y = b.cells[i].dataset.v;
          var c = numeric ? parseFloat(x) - parseFloat(y) : x.localeCompare(y);
          return dir === "ascending" ? c : -c;
        });
        rows.forEach(function (r) { body.appendChild(r); });
        heads.forEach(function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);
      });
    });
  });

  // Drifted pairs filtered by field, from the URL (#field=assignee), so a
  // filtered view can be linked to. A filter shows only that field's change.
  var select = document.getElementById("field-filter");
  var command = document.getElementById("fix-command");
  var copy = document.querySelector(".command .copy");
  if (copy && navigator.clipboard) {
    copy.hidden = false;
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(command.textContent).then(function () {
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = "Copy"; }, 1500);
      });
    });
  }
  var drift = document.querySelector("table.drift");
  function applyFilter(scroll) {
    if (!drift) return;
    var m = location.hash.match(/^#field=(.+)$/);
    var f = m ? decodeURIComponent(m[1]) : "";
    Array.prototype.forEach.call(drift.tBodies[0].rows, function (row) {
      row.hidden = !!f && row.dataset.fields.split("|").indexOf(f) < 0;
      row.querySelectorAll("li[data-field]").forEach(function (li) {
        li.hidden = !!f && li.dataset.field !== f;
      });
    });
    if (select) select.value = f;
    if (command) {
      var byField = JSON.parse(command.dataset.byField);
      command.textContent = (f && byField[f]) || command.dataset.all;
    }
    if (f && scroll) (select || drift).scrollIntoView({block: "center"});
  }
  var fromMenu = false;  // the menu is already in view, so a choice there doesn't scroll
  if (select) select.addEventListener("change", function () {
    fromMenu = true;
    location.hash = select.value ? "field=" + encodeURIComponent(select.value) : "drift";
  });
  addEventListener("hashchange", function () { applyFilter(!fromMenu); fromMenu = false; });
  applyFilter(true);

  // Chart hover: a crosshair snapped to the nearest day, and the same readout
  // from the arrow keys when the chart has focus.
  document.querySelectorAll(".chart[data-points]").forEach(function (chart) {
    var pts = JSON.parse(chart.dataset.points);
    var svg = chart.querySelector("svg");
    var cross = chart.querySelector(".crosshair");
    var dot = chart.querySelector(".hover-dot");
    var tip = chart.querySelector(".tip");
    var index = pts.length - 1;
    function show(i) {
      index = i;
      var p = pts[i], box = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
      cross.setAttribute("x1", p[0]); cross.setAttribute("x2", p[0]);
      dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]);
      cross.style.display = dot.style.display = "";
      tip.querySelector(".tip-value").textContent = p[3];
      tip.querySelector(".tip-label").textContent = p[2];
      tip.hidden = false;
      var left = p[0] * box.width / vb.width;
      tip.style.left = Math.min(left, box.width - tip.offsetWidth) + "px";
    }
    function hide() { cross.style.display = dot.style.display = "none"; tip.hidden = true; }
    svg.addEventListener("pointermove", function (ev) {
      var box = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
      var x = (ev.clientX - box.left) * vb.width / box.width, best = 0;
      pts.forEach(function (p, i) { if (Math.abs(p[0] - x) < Math.abs(pts[best][0] - x)) best = i; });
      show(best);
    });
    svg.addEventListener("pointerleave", hide);
    chart.addEventListener("focus", function () { show(index); });
    chart.addEventListener("blur", hide);
    chart.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowLeft" && index > 0) { show(index - 1); ev.preventDefault(); }
      if (ev.key === "ArrowRight" && index < pts.length - 1) { show(index + 1); ev.preventDefault(); }
    });
  });
})();
</script>"""


def inline_wordmark(uid: str) -> str:
    """The JBIX wordmark, inline so it follows the page's theme.

    The file switches its letters' colour on the system's dark mode alone, which
    the theme toggle cannot reach; inline, the letters take the page's ink and the
    crossover's blue and green take --mark-blue and --mark-green, a step lighter
    in dark mode. Its title goes (the label names it), and ``uid`` makes its
    clip-path id unique, should a page ever carry it twice. Fails if the file changes
    shape, rather than putting a wordmark with the wrong colours on the page.
    """
    svg = resources.files("jbix").joinpath("assets", "wordmark.svg").read_text()
    edits = [  # (pattern, replacement, every occurrence)
        (r"\s*<title[^>]*>.*?</title>", "", False),
        (r"\s*<style>.*?</style>", "", False),
        (r'<g class="letters">', '<g class="letters" fill="currentColor">', False),
        (r'role="img" aria-labelledby="title"', 'class="wordmark" role="img" aria-label="JBIX"', False),
        (r'id="green-underpass"', f'id="green-underpass-{uid}"', False),
        (r"url\(#green-underpass\)", f"url(#green-underpass-{uid})", True),
        (r"#08783F", "var(--mark-green)", True),
        (r"#165DF5", "var(--mark-blue)", True),
    ]
    for pattern, replacement, every in edits:
        svg, n = re.subn(pattern, replacement, svg, count=0 if every else 1, flags=re.S | re.I)
        if not n:
            raise RuntimeError(f"jbix/assets/wordmark.svg no longer matches {pattern!r}")
    return svg.strip()


def wordmark_title(uid: str) -> str:
    """The report's name with the wordmark standing for JBIX, as the header shows it."""
    return f'{inline_wordmark(uid)}<span>Drift Report</span>'


class Site:
    def __init__(self, tags: dict[str, Tag], groups: dict[str, list[str]], names: dict[str, str]):
        self.tags = tags
        # Groups with at least one captured tag, in groups.yaml order.
        self.groups = {g: [t for t in members if t in tags] for g, members in groups.items()}
        self.groups = {g: members for g, members in self.groups.items() if members}
        self.names = names
        self.newest = max(t.now.day for t in tags.values()) if tags else date.today()
        self.brand = wordmark_title("header")

    def nav(self, prefix: str, here: str) -> str:
        """Overview, then the groups in a menu, so the header holds however many there are."""
        current = ' aria-current="page"'
        out = f'<a href="{prefix}index.html"{current if here == "index" else ""}>Overview</a>'
        if self.groups:
            items = "".join(
                f'<li><a href="{prefix}groups/{e(slug(g))}.html"'
                f'{current if here == f"group:{g}" else ""}>{e(self.names.get(g, g))}</a></li>'
                for g in sorted(self.groups, key=lambda g: self.names.get(g, g).lower()))
            here_cls = ' class="here"' if here.startswith("group:") else ""
            out += (f'<details class="menu"><summary{here_cls}>Groups{CHEVRON}</summary>'
                    f'<ul>{items}</ul></details>')
        return out

    def page(self, out: pathlib.Path, rel: str, title: str, here: str, body: str) -> None:
        prefix = "../" * rel.count("/")
        full_title = SITE_NAME if title == SITE_NAME else f"{title} · {SITE_NAME}"
        doc = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="darkreader-lock">
<title>{e(full_title)}</title>
<meta name="description" content="How closely Jira matches Bugzilla for the projects JBI syncs.">
<link rel="stylesheet" href="{prefix}style.css">
<link rel="icon" href="{prefix}favicon.ico" sizes="32x32">
<link rel="icon" href="{prefix}icon.svg" type="image/svg+xml">
{THEME_HEAD}
</head>
<body>
<header class="site">
<a class="brand" href="{prefix}index.html">{self.brand}</a>
<nav class="site-nav" aria-label="Reports">{self.nav(prefix, here)}</nav>
<div id="theme-toggle" role="group" aria-label="Theme"><span class="thumb" aria-hidden="true"></span>
<button type="button" data-theme-choice="light" aria-pressed="false" aria-label="Light mode" title="Light mode">{SUN_ICON}</button><button type="button" data-theme-choice="dark" aria-pressed="false" aria-label="Dark mode" title="Dark mode">{MOON_ICON}</button>
</div>
</header>
<main>
{body}
</main>
<footer>Data as of {long_date(self.newest)}. Compares each Bugzilla bug with the Jira issue
<a href="{JBI_URL}">JBI</a> syncs it to, for every whiteboard tag in JBI's configuration.
Built by <a href="{REPO_URL}">JBIX</a>.</footer>
{SCRIPTS}
</body>
</html>
"""
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc, encoding="utf-8")

    def overview(self, title: str, lede: str, tags: list[Tag], prefix: str, extra: str = "",
                 broken: str = "") -> str:
        series = combine(tags)
        return (f"<h1>{e(title)}</h1><p class=\"lede\">{lede}</p>"
                + summary_tiles(series[-1], len(tags))
                + line_chart(series, "Drifted pairs")
                + line_chart(series, "Broken links", "broken")
                + history_table(series)
                + extra
                + f'<h2 id="tags">Tags</h2>{tag_table(tags, prefix, self.newest)}'
                + broken)

    def groups_table(self) -> str:
        if not self.groups:
            return ""
        series = {g: combine([self.tags[t] for t in members]) for g, members in self.groups.items()}
        rows = []
        # Worst drift first, as in the tags table; any column sorts on a click.
        for g in sorted(series, key=lambda g: (-series[g][-1].drift, -series[g][-1].drifted,
                                               self.names.get(g, g))):
            now, name = series[g][-1], self.names.get(g, g)
            rows.append(
                f'<tr><th scope="row" data-v="{e(name)}"><a href="groups/{e(slug(g))}.html">{e(name)}</a></th>'
                + cell(len(self.groups[g]), str(len(self.groups[g]))) + cell(now.pairs, num(now.pairs))
                + cell(now.drifted, num(now.drifted)) + cell(round(now.drift, 6), pct(now.drift))
                + f'<td class="spark-cell">{sparkline(series[g])}</td>'
                + cell(now.broken, num(now.broken)) + "</tr>")
        return ('<h2 id="groups">Groups</h2><div class="table-wrap"><table class="sortable"><thead><tr>'
                + sort_head("Group", "text") + sort_head("Tags") + sort_head("Linked pairs")
                + sort_head("Drifted") + sort_head("Drift", default=True)
                + f'<th scope="col">Last {SPARK_DAYS} days</th>' + sort_head("Broken links")
                + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')

    def tag_page(self, t: Tag) -> str:
        latest, now = t.latest, t.now
        diff = latest.get("diff") or {}
        jira, bz = latest.get("jira") or {}, latest.get("bugzilla") or {}
        cards = status_tiles(now, "bugs with a linked Jira issue")
        if now.jira_total:
            # A tag's linked count covers only its own bugs, so on a project shared
            # by several tags, say how many more the others link.
            others = sum(s.now.jira_linked or 0 for s in self.tags.values()
                         if s.project == t.project and s.name != t.name)
            note = (f"{num(now.jira_linked)} of {link(jira.get('project_url'), f'{num(now.jira_total)} in {t.project}')}"
                    + (f"; other tags link {num(others)} more" if others else ""))
            cards.append(("Jira issues linked", pct(now.jira_linked / now.jira_total), note))
        if now.bz_total:
            cards.append(("Bugs tagged", pct(now.bz_tagged / now.bz_total),
                          f"{num(now.bz_tagged)} of {link(bz.get('bugs_url'), num(now.bz_total) + ' in its components')}"))
        stale = (f'<p class="notice">Last captured on {long_date(now.day)}; other tags are as of '
                 f'{long_date(self.newest)}.</p>' if now.day < self.newest else "")
        queries = []
        if safe_url(jira.get("unlinked_url")):
            queries.append(link(jira["unlinked_url"], f"{t.project} issues not linked to a bug"))
        if safe_url(bz.get("untagged_url")):
            queries.append(link(bz["untagged_url"], f"Bugs in its components without [{t.name}]"))
        more = ('<h2 id="queries">Live queries</h2><ul class="queries">'
                + "".join(f"<li>{q}</li>" for q in queries) + "</ul>") if queries else ""
        # Latest files from before the config was recorded have none; say nothing then.
        jbi = latest.get("jbi")
        config_url = safe_url((jbi or {}).get("source", {}).get("url"))
        how = ' <a href="#config">How this was compared</a>' if jbi else ""
        return (
            f'<h1><span class="tag-name">[{e(t.name)}]</span> <span class="muted">→ {e(t.project)}</span></h1>'
            f'<p class="lede">Bugs tagged <code>[{e(t.name)}]</code>, compared with their issues in '
            f'{e(t.project)}.{how}</p>'
            + stale + tiles(cards)
            + line_chart(t.history, "Drifted pairs")
            + line_chart(t.history, "Broken links", "broken")
            + history_table(t.history)
            + '<h2 id="fields">Drifted fields</h2>'
            + field_bars(diff.get("fields") or [], now.pairs)
            + '<h2 id="drift">Drifted pairs</h2>'
            + drift_rows_table(diff, f"{slug(t.name)}-changes.csv" if t.changes_csv else None)
            + (fixing_section(t.name, diff.get("fields") or [], config_url) if now.drifted else "")
            + '<h2 id="broken">Broken links</h2>'
            + broken_table(latest.get("broken_links") or [],
                           latest.get("broken_links_total", len(latest.get("broken_links") or [])))
            + (config_section(jbi) if jbi else "")
            + more)

    def build(self, out: pathlib.Path) -> None:
        if not self.tags:
            body = ('<h1>Overview</h1><p class="lede">No snapshots yet. Run '
                    "<code>jbix.py --mode check</code> to record some.</p>")
            self.page(out, "index.html", SITE_NAME, "index", body)
            return
        everything = list(self.tags.values())
        lede = (f'The <a href="{JBI_URL}">Jira Bugzilla Integration</a> (JBI) mirrors Bugzilla bugs '
                f"as Jira issues. This report shows how closely Jira matches Bugzilla across "
                f"{len(everything)} whiteboard tags, and where the two have drifted apart. "
                f'It is built by <a href="{REPO_URL}">JBIX</a>, which compares each bug with its '
                "Jira issue, field by field, and can sync either way to put them right.")
        self.page(out, "index.html", SITE_NAME, "index",
                  self.overview("Overview", lede, everything, "", self.groups_table()))
        for g, members in self.groups.items():
            name = self.names.get(g, g)
            tags = [self.tags[t] for t in members]
            lede = f"How closely Jira matches Bugzilla across the {e(name)} group's {len(tags)} tag{'s' if len(tags) != 1 else ''}."
            rows, total = tagged_broken(tags)
            csv_name = f"{slug(g)}-broken-links.csv"
            if rows:
                (out / "groups").mkdir(parents=True, exist_ok=True)
                write_broken_csv(out / "groups" / csv_name, rows)
            group_broken = ('<h2 id="broken">Broken links</h2>'
                            + broken_table(rows, total, "../", csv_name if rows else None))
            self.page(out, f"groups/{slug(g)}.html", name, f"group:{g}",
                      self.overview(name, lede, tags, "../", broken=group_broken))
        for t in everything:
            self.page(out, f"tags/{slug(t.name)}.html", f"[{t.name}]", "", self.tag_page(t))
            if t.changes_csv:
                shutil.copyfile(t.changes_csv, out / "tags" / f"{slug(t.name)}-changes.csv")
        (out / "style.css").write_text(resources.files("jbix").joinpath("site.css").read_text())
        for asset in ("icon.svg", "favicon.ico"):  # the jbix crossover mark
            (out / asset).write_bytes(resources.files("jbix").joinpath("assets", asset).read_bytes())


def build_site(out: pathlib.Path = DEFAULT_OUT, data_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Build the whole site into ``out``, replacing what was there; return ``out``.

    ``out`` must be absent or a directory this function built before (it carries
    a marker file), so a mistyped path can never replace an unrelated directory.
    """
    data_dir = data_dir or paths.data_dir()
    out = pathlib.Path(out)
    if out.exists() and not (out / MARKER).exists():
        raise SystemExit(f"{out} exists and was not built by jbix-site; refusing to replace it.")
    groups_file = data_dir / "groups.yaml"
    site = Site(load_tags(data_dir), groups_mod.load_groups(groups_file),
                groups_mod.group_display_names(groups_file))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=".jbix-site-", dir=out.parent))
    try:
        site.build(tmp)
        (tmp / MARKER).write_text("Built by jbix-site; replaced in full on every build.\n")
        old = None
        if out.exists():
            old = out.with_name(f".{out.name}-old-{os.getpid()}")
            out.rename(old)
        tmp.rename(out)
        if old:
            shutil.rmtree(old)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jbix-site", description="Build the JBI drift site.")
    parser.add_argument("--data-dir", type=pathlib.Path,
                        help=f"Directory holding snapshots/, latest/ and groups.yaml "
                             f"(default: ${paths.DATA_DIR_ENV}, else the current directory)")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT,
                        help=f"Directory to build the site into (default: {DEFAULT_OUT})")
    args = parser.parse_args(argv)
    out = build_site(args.out, args.data_dir)
    print(f"Built {out / 'index.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
