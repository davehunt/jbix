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


def line_chart(points: list[Point], title: str) -> str:
    """Drift % over time: one series, hairline grid, end label, hover crosshair."""
    w, h, left, right, top, bottom = 960, 220, 44, 64, 14, 26
    pw, ph = w - left - right, h - top - bottom
    values = [p.drift * 100 for p in points]
    ymax = _nice_max(max(values) * 1.1) if max(values) else 1  # all in sync: 0% to 1%
    d0, d1 = points[0].day.toordinal(), points[-1].day.toordinal()
    span = max(d1 - d0, 1)

    def x(p: Point) -> float:
        return left + (p.day.toordinal() - d0) / span * pw if len(points) > 1 else left + pw

    def y(v: float) -> float:
        return top + ph - v / ymax * ph

    grid = []
    for t in (0, ymax / 2, ymax):
        yy = y(t)
        label = f"{t:g}%"
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
    marks.append(f'<text class="end-label" x="{ex + 8:.1f}" y="{ey + 4:.1f}">{pct(points[-1].drift)}</text>')

    data = [[round(cx, 1), round(cy, 1),
             f"{long_date(p.day)} · {num(p.drifted)} of {num(p.pairs)} pairs", pct(p.drift)]
            for (cx, cy), p in zip(coords, points)]
    first, last = points[0], points[-1]
    summary = (f"{title}: {pct(first.drift)} on {long_date(first.day)}"
               + (f", {pct(last.drift)} on {long_date(last.day)}" if len(points) > 1 else ""))
    return (
        f'<figure class="chart" data-points="{e(json.dumps(data))}" tabindex="0" '
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


def fixing_section(tag: str, fields: list[dict]) -> str:
    """How to put this tag's drift right with jbix, preview first.

    Only the preview command is shown, so nothing copied from the page changes
    anything; prompt and apply are named as the next steps. The command follows
    the field menu above it, syncing only that field when one is chosen.
    """
    base = f"python jbix.py --tags {tag} --mode preview"
    flags = {f["name"]: FIX_FLAGS[f["name"]] for f in fields if f.get("n") and f["name"] in FIX_FLAGS}
    by_field = {name: f"python jbix.py --tags {tag} --manual {flag} --mode preview"
                for name, flag in flags.items()}
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
        'new links notify the issue\'s watchers; other fields are updated quietly.</li>'
        '<li><strong>Drift that keeps coming back</strong> may need a change to '
        f'<a href="{JBI_URL}">JBI\'s configuration</a> rather than another sync.</li>'
        '</ul></aside>'
        '<p>This report shows your changes after its next run is deployed. To check them sooner, '
        f'run <code>python jbix.py --tags {e(tag)} --mode check</code>, which rebuilds the report '
        'locally in <code>output/site/</code>.</p>'
    )


def broken_table(latest: dict) -> str:
    rows = latest.get("broken_links") or []
    total = latest.get("broken_links_total", len(rows))
    if not rows:
        return ALL_CLEAR
    body = "".join(
        f'<tr><td>{e(DIRECTIONS.get(r.get("direction", ""), r.get("direction", "")))}</td>'
        f'<td>{link(r.get("bug_url"), str(r.get("bug_id") or "—"))}</td>'
        f'<td>{link(r.get("jira_url"), r.get("jira_key", ""))}</td>'
        f'<td>{e(REASONS.get(r.get("reason", ""), r.get("reason", "")))}</td></tr>'
        for r in rows)
    note = (f'<p class="muted">Showing the first {num(len(rows))} of {num(total)}.</p>'
            if total > len(rows) else "")
    return ('<div class="table-wrap"><table><thead><tr><th scope="col">Direction</th><th scope="col">Bug</th>'
            '<th scope="col">Jira issue</th><th scope="col">Why it is broken</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>{note}')


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

    def overview(self, title: str, lede: str, tags: list[Tag], prefix: str, extra: str = "") -> str:
        series = combine(tags)
        return (f"<h1>{e(title)}</h1><p class=\"lede\">{lede}</p>"
                + summary_tiles(series[-1], len(tags))
                + line_chart(series, "Drifted pairs")
                + history_table(series)
                + extra
                + f'<h2 id="tags">Tags</h2>{tag_table(tags, prefix, self.newest)}')

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
        return (
            f'<h1><span class="tag-name">[{e(t.name)}]</span> <span class="muted">→ {e(t.project)}</span></h1>'
            f'<p class="lede">Bugs tagged <code>[{e(t.name)}]</code>, compared with their issues in '
            f'{e(t.project)}.</p>'
            + stale + tiles(cards)
            + line_chart(t.history, "Drifted pairs")
            + history_table(t.history)
            + '<h2 id="fields">Drifted fields</h2>'
            + field_bars(diff.get("fields") or [], now.pairs)
            + '<h2 id="drift">Drifted pairs</h2>'
            + drift_rows_table(diff, f"{slug(t.name)}-changes.csv" if t.changes_csv else None)
            + (fixing_section(t.name, diff.get("fields") or []) if now.drifted else "")
            + f'<h2 id="broken">Broken links</h2>{broken_table(latest)}'
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
            self.page(out, f"groups/{slug(g)}.html", name, f"group:{g}",
                      self.overview(name, lede, tags, "../"))
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
