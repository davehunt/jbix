"""Generate the standalone HTML drift report from the per-tag snapshot store.

Reads every ``snapshots/<tag>.jsonl`` (one compact JSON line per run, written by
``jbix.py``) and always writes one self-contained file — ``output/report.html``
— holding every view:

  * the full report over all tags (Totals card, per-group breakdown, per-tag
    cards, drift-over-time chart)
  * one summary view per group in ``groups.yaml`` (scoped to the group's tags)
  * one drilldown view per tag: full field-by-field drift table (bug/Jira links
    plus current → should-be values) and the full broken-links table

Views live in the same document as ``<section class="view">`` blocks and are
switched client-side by a tiny hash router (``#/``, ``#/group/<group>``,
``#/tag/<tag>``, plus deep links such as ``#/tag/<tag>/drift``). Charts for a
view are built the first time it is shown, so Chart.js always measures a visible
canvas.

Chart.js is loaded from CDN; all snapshot data is embedded inline. This module
is standalone (only ``jbix.snapshots``/``jbix.groups``, both stdlib-only, are
imported) so it runs with plain ``python make_report.py`` without ``.env``.
"""

import argparse
import glob
import html
import json
import pathlib
from datetime import datetime

from jbix.groups import group_display_names, load_groups
from jbix.snapshots import SNAPSHOT_DIR, aggregate_metrics

OUTPUT_DIR = pathlib.Path("output")
OUT = OUTPUT_DIR / "report.html"

# Rows shown in a per-tag drift-detail table before pointing at updates.csv;
# matches jbix.snapshots._DRIFT_ROWS_CAP (rows are already capped at store time).
_DRIFT_ROWS_SHOWN = 50

COLORS = {"fp": "#2563eb", "fxp": "#f59e0b", "fxpe": "#10b981",
          "pcf": "#ef4444", "fxdroid": "#8b5cf6", "fidefe": "#ec4899",
          "Totals": "#111827"}
PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#ec4899", "#0891b2"]


def color_for(tag, i):
    return COLORS.get(tag, PALETTE[i % len(PALETTE)])


# ---------------------------------------------------------------------------
# In-document view ids (the hash router's targets)
# ---------------------------------------------------------------------------
FULL_VIEW = "view-full"


def group_view_id(group):
    return f"view-group-{group}"


def tag_view_id(tag):
    return f"view-tag-{tag}"


def _href(view_id, section=None):
    """Route hash for a view. Deliberately path-shaped (``#/tag/fp``) rather than
    an element id, so the browser never does its own fragment scrolling and the
    router owns positioning."""
    if view_id == FULL_VIEW:
        route = "/"
    elif view_id.startswith("view-group-"):
        route = f"/group/{view_id[len('view-group-'):]}"
    else:
        route = f"/tag/{view_id[len('view-tag-'):]}"
        if section:
            route += f"/{section}"
    return f"#{html.escape(route)}"


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------
def load_snapshots():
    """Return {tag: [entry, ...]} sorted by timestamp, for every tag in
    snapshots/*.jsonl."""
    data = {}
    for path in sorted(glob.glob(str(SNAPSHOT_DIR / "*.jsonl"))):
        tag = pathlib.Path(path).stem
        entries = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        if entries:
            entries.sort(key=lambda e: e["ts"])
            data[tag] = entries
    return data


# ---------------------------------------------------------------------------
# Entry → internal "section" shape consumed by the card renderers
# ---------------------------------------------------------------------------
def _section(title, bz, jr, diff, broken=None):
    metrics = {}
    if bz:
        metrics["bz_bugs"] = bz["total"]
        metrics["tagged"] = bz["tagged"]
        metrics["tagged_pct"] = f"{bz['pct_tagged']:.1f}%"
        metrics["bz_bugs_url"] = bz.get("bugs_url")
        metrics["untagged_url"] = bz.get("untagged_url")
    if jr:
        metrics["jira_issues"] = jr["total"]
        metrics["linked"] = jr["linked"]
        metrics["linked_pct"] = f"{jr['pct_linked']:.1f}%"
        metrics["jira_issues_url"] = jr.get("issues_url")
        metrics["jira_project_url"] = jr.get("project_url")
        metrics["unlinked_url"] = jr.get("unlinked_url")
    fields, drift, linked_pairs, drift_rows = {}, None, None, []
    if diff:
        linked_pairs = diff["total_pairs"]
        drift = {"out": diff["drifted_pairs"], "total": diff["total_pairs"],
                 "pct": diff["drift_pct"] * 100}
        for f in diff["fields"]:
            fields[f["name"]] = {"out": f["n"], "pct": f"{f['pct'] * 100:.2f}%"}
        drift_rows = diff.get("rows") or []
    return {"title": title, "metrics": metrics, "fields": fields,
            "drift": drift, "linked_pairs": linked_pairs,
            "drift_rows": drift_rows, "broken_links": broken or []}


def _section_from_entry(tag, entry):
    title = f"[{tag}] → {entry.get('jira_project', tag.upper())}"
    return _section(title, entry.get("bugzilla"), entry.get("jira"), entry.get("diff"),
                    broken=entry.get("broken_links"))


# ---------------------------------------------------------------------------
# HTML fragments
# ---------------------------------------------------------------------------
def fmt(n):
    return f"{n:,}" if isinstance(n, int) else (n or "—")


def _badge(pct):
    return "ok" if pct == 0 else ("warn" if pct < 3 else "bad")


def _delta(sec, prev):
    if prev and prev.get("drift") and sec["drift"]:
        diff = sec["drift"]["pct"] - prev["drift"]["pct"]
        if abs(diff) >= 0.005:
            arrow = "▲" if diff > 0 else "▼"
            cls = "up" if diff > 0 else "down"
            return f'<span class="delta {cls}">{arrow}&nbsp;{abs(diff):.2f} pts</span>'
    return ""


def _big(value, url=None):
    """Render a stat's big number, wrapped in a link when ``url`` is set."""
    inner = fmt(value)
    if url:
        return (f'<a class="big big-link" href="{html.escape(url)}">{inner}</a>')
    return f'<span class="big">{inner}</span>'


def _canvas(kind, key=None, title=None):
    """A chart placeholder. The router builds it from its view's payload the
    first time the view is shown — so no document-unique ids are needed."""
    attrs = f' data-chart="{kind}"'
    if key:
        attrs += f' data-key="{html.escape(key)}"'
    if title:
        attrs += f' data-title="{html.escape(title)}"'
    return f"<canvas{attrs}></canvas>"


def _stats(sec, tag=None):
    """Render the 5-stat row. When ``tag`` is set (per-tag / group cards), the
    "fields out of sync" and "broken links" numbers deep-link into that tag's
    view (``#drift-<tag>`` / ``#broken-<tag>``)."""
    m, d = sec["metrics"], sec["drift"]
    if "bz_bugs" in m:
        bz_stat = (f'<div class="stat">{_big(m["bz_bugs"], m.get("bz_bugs_url"))}'
                   f'<span class="lbl">Bugzilla bugs</span></div>')
    else:
        bz_stat = ('<div class="stat"><span class="big na">n/a</span>'
                   '<span class="lbl">no Bugzilla components</span></div>')

    out = fmt(d['out']) if d else '—'
    drift_href = _href(tag_view_id(tag), "drift") if tag else None
    drift_stat = (f'<a class="big big-link" href="{drift_href}">{out}</a>'
                  if drift_href and d else f'<span class="big">{out}</span>')

    n_broken = len(sec.get("broken_links") or [])
    broken_cls = "big" if n_broken == 0 else "big bad"
    broken_stat = (f'<a class="{broken_cls} big-link" href="{_href(tag_view_id(tag), "broken")}">{n_broken}</a>'
                   if tag else f'<span class="{broken_cls}">{n_broken}</span>')

    return f"""<div class="stats">
        {bz_stat}
        <div class="stat">{_big(m.get('jira_issues'), m.get('jira_issues_url'))}<span class="lbl">Jira issues</span></div>
        <div class="stat"><span class="big">{fmt(sec.get('linked_pairs'))}</span><span class="lbl">linked pairs</span></div>
        <div class="stat">{drift_stat}<span class="lbl">fields out of sync</span></div>
        <div class="stat">{broken_stat}<span class="lbl">broken links</span></div>
      </div>"""


def tag_card(key, sec, prev):
    m = sec["metrics"]
    d = sec["drift"]
    drift_pct = d["pct"] if d else 0.0
    detail_href = _href(tag_view_id(key))
    field_rows = ""
    # Render every drifted field present, in the order health recorded them
    # (canonical order then alphabetical) — covers type, remote_links, etc.
    for f, fv in sec["fields"].items():
        zero = "zero" if fv["out"] == 0 else "nonzero"
        field_rows += (f'<tr class="{zero}"><td>{html.escape(f)}</td><td class="num">{fmt(fv["out"])}</td>'
                       f'<td class="num">{html.escape(fv["pct"])}</td></tr>')

    title = html.escape(sec["title"])
    if m.get("jira_project_url"):
        title = (f'<a class="title-link" href="{html.escape(m["jira_project_url"])}" '
                 f'target="_blank" rel="noopener">{title}</a>')

    untagged_link = ""
    if m.get("untagged_url"):
        untagged_link = (f'<a class="pie-link" href="{html.escape(m["untagged_url"])}" '
                         f'target="_blank" rel="noopener">view untagged bugs ↗</a>')
    unlinked_link = ""
    if m.get("unlinked_url"):
        unlinked_link = (f'<a class="pie-link" href="{html.escape(m["unlinked_url"])}" '
                         f'target="_blank" rel="noopener">view unlinked issues ↗</a>')

    return f"""
    <section class="card">
      <header>
        <h2>{title}</h2>
        <span class="badge {_badge(drift_pct)}">drift {drift_pct:.2f}%</span>{_delta(sec, prev)}
      </header>
      {_stats(sec, key)}
      <div class="mini-pie-row">
        {f'<div class="mini-pie">{_canvas("bz", key)}</div>'
         if "bz_bugs" in sec["metrics"] else
         '<div class="mini-pie pie-na"><span>No Bugzilla<br>components configured</span></div>'}
        <div class="mini-pie">{_canvas("jira", key)}</div>
      </div>
      {f'<div class="pie-links"><span class="pl-slot">{untagged_link}</span><span class="pl-slot">{unlinked_link}</span></div>'
       if (untagged_link or unlinked_link) else ''}
      <div class="mini-line-box">{_canvas("mini", key)}</div>
      <table class="fields">
        <colgroup><col class="c-field"><col class="c-num"><col class="c-num"></colgroup>
        <thead><tr><th>Field</th><th class="num">Out of sync</th><th class="num">%</th></tr></thead>
        <tbody>{field_rows}</tbody>
      </table>
      <a class="detail-link" href="{detail_href}">View tag report →</a>
    </section>"""


def _broken_links_table(sec, csv_name=None, limit: int | None = 20):
    """Render the broken-links table (empty string when there are none).

    Handles both directions: ``bug→jira`` (see_also → dead issue) and
    ``jira→bug`` (orphaned issue → untagged/missing bug, where bug fields may be
    empty). Older snapshot rows lack ``direction``/``reason`` and degrade to the
    original bug→jira layout. ``limit=None`` renders every stored row.
    """
    rows = sec.get("broken_links") or []
    if not rows:
        return ""
    shown = rows if limit is None else rows[:limit]
    body = ""
    for r in shown:
        if r.get("bug_id") not in ("", None):
            bug = (f'<a href="{html.escape(r["bug_url"])}" target="_blank" rel="noopener">'
                   f'{r["bug_id"]}</a>')
        else:
            bug = '<span class="na">—</span>'
        jira = (f'<a href="{html.escape(r["jira_url"])}" target="_blank" rel="noopener">'
                f'{html.escape(r["jira_key"])}</a>')
        reason = html.escape(r.get("reason") or "")
        body += f'<tr><td>{bug}</td><td>{jira}</td><td>{reason}</td></tr>'
    more = ""
    if limit is not None and len(rows) > limit:
        extra = f" — see {csv_name}" if csv_name else ""
        more = f'<tr class="more"><td colspan="3">+{len(rows) - limit} more{extra}</td></tr>'
    return f"""
      <table class="fields broken">
        <thead><tr><th>Bug</th><th>Jira issue</th><th>Reason</th></tr></thead>
        <tbody>{body}{more}</tbody>
      </table>"""


def _url_tail(url):
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def _trunc(value, n=90):
    """HTML-escape ``value``; truncate long strings with a full-value tooltip."""
    s = "" if value is None else str(value)
    esc = html.escape(s)
    if len(s) > n:
        return f'<span title="{esc}">{html.escape(s[:n])}…</span>'
    return esc or '<span class="na">—</span>'


def _drift_detail_table(sec):
    """Render the per-tag "Drifted fields" table from stored drift rows."""
    rows = sec.get("drift_rows") or []
    if not rows:
        return ('<p class="empty">No field-level detail recorded for this '
                'snapshot (it predates detail capture, or there is no drift).</p>')
    body = ""
    for r in rows:
        bug = (f'<a href="{html.escape(r["bug_url"])}" target="_blank" rel="noopener">'
               f'{html.escape(_url_tail(r["bug_url"]))}</a>')
        jira = (f'<a href="{html.escape(r["jira_url"])}" target="_blank" rel="noopener">'
                f'{html.escape(_url_tail(r["jira_url"]))}</a>')
        body += (f'<tr><td>{bug}</td><td>{jira}</td><td>{html.escape(r.get("field", ""))}</td>'
                 f'<td>{_trunc(r.get("before"))}</td><td>{_trunc(r.get("after"))}</td></tr>')
    more = ""
    if len(rows) >= _DRIFT_ROWS_SHOWN:
        more = (f'<tr class="more"><td colspan="5">showing first {_DRIFT_ROWS_SHOWN} '
                f'— see output/updates.csv for the full list</td></tr>')
    return f"""
      <table class="fields detail">
        <thead><tr><th>Bug</th><th>Jira issue</th><th>Field</th>
          <th>Current (Jira)</th><th>Should be (Bugzilla)</th></tr></thead>
        <tbody>{body}{more}</tbody>
      </table>"""


def totals_card(sec, chart_caption="", bz_title=None):
    d = sec["drift"]
    drift_pct = d["pct"] if d else 0.0
    sub = f'{fmt(d["out"])} / {fmt(d["total"])} linked pairs out of sync' if d else ""
    caption_html = f'<p class="chart-caption">{html.escape(chart_caption)}</p>' if chart_caption else ""
    return f"""
    <section class="card featured">
      <header>
        <h2>{html.escape(sec["title"])}</h2>
      </header>
      <div class="featured-body">
        <div class="featured-left">
          {_stats(sec)}
          <div class="drift-headline">
            <span class="dh-num">{drift_pct:.2f}%</span>
            <span class="dh-sub">overall drift{f'<br>{sub}' if sub else ''}</span>
          </div>
        </div>
        <div class="featured-right">
          {f'<div class="mini-pie">{_canvas("bz", "Totals", bz_title or "Bugzilla bugs")}</div>'
           if "bz_bugs" in sec["metrics"] else
           '<div class="mini-pie pie-na"><span>No Bugzilla<br>components configured</span></div>'}
          <div class="mini-pie">{_canvas("jira", "Totals", "Jira issues")}</div>
        </div>
      </div>
      <div class="totals-line">
        <h3>Drift score over time</h3>
        {caption_html}
        <div class="big-line-box">{_canvas("all")}</div>
      </div>
    </section>"""


def pie_for(sec):
    m = sec["metrics"]
    bz, tg = m.get("bz_bugs", 0), m.get("tagged", 0)
    jr, lk = m.get("jira_issues", 0), m.get("linked", 0)
    other = min(m.get("linked_other", 0), max(jr - lk, 0))
    return {"tagged": tg, "untagged": max(bz - tg, 0),
            "linked": lk, "linked_other": other,
            "unlinked": max(jr - lk - other, 0)}


def _apply_linked_other(latest_sec, data, tags):
    """Record each tag's sibling-linked total when several tags share a Jira
    project (rendered as the "Linked (other tags)" pie slice)."""
    proj_tags: dict = {}
    for t in tags:
        p = data[t][-1].get("jira_project")
        if p and "linked" in latest_sec[t]["metrics"]:
            proj_tags.setdefault(p, []).append(t)
    for sibs in proj_tags.values():
        if len(sibs) < 2:
            continue
        total_linked = sum(latest_sec[t]["metrics"]["linked"] for t in sibs)
        for t in sibs:
            m = latest_sec[t]["metrics"]
            m["linked_other"] = total_linked - m["linked"]


# ---------------------------------------------------------------------------
# Shared page chrome
# ---------------------------------------------------------------------------
STYLE = """
  :root { --bg:#f4f5f7; --card:#fff; --ink:#1f2937; --mut:#6b7280; --line:#e7e9ee; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:1140px; margin:0 auto; padding:36px 24px 72px; }
  h1 { font-size:28px; margin:0 0 4px; letter-spacing:-.4px; }
  .sub { color:var(--mut); margin:0 0 28px; }
  a.backlink { display:inline-block; margin:0 0 18px; color:#2563eb; text-decoration:none; font-size:14px; }
  a.backlink:hover { text-decoration:underline; }
  .nav { display:flex; flex-wrap:wrap; gap:8px 10px; margin:0 0 10px; align-items:baseline; }
  .nav:last-of-type { margin-bottom:26px; }
  details.nav-details { margin:0 0 26px; }
  details.nav-details > summary { font-size:13px; font-weight:600; color:var(--mut);
    text-transform:uppercase; letter-spacing:.06em; cursor:pointer; width:max-content; }
  details.nav-details > summary:hover { color:var(--ink); }
  details.nav-details[open] > summary { margin-bottom:10px; }
  details.nav-details .nav { margin-bottom:0; }
  details.nav-details .nav .nav-lbl { display:none; }
  .nav .nav-lbl { font-size:13px; font-weight:600; color:var(--mut); text-transform:uppercase;
    letter-spacing:.06em; min-width:62px; }
  .nav a { font-size:13px; text-decoration:none; padding:2px 10px; color:#2563eb;
    border:1px solid var(--line); border-radius:999px; background:#fff; }
  .nav a:hover { text-decoration:underline; }
  .nav a.nav-here { color:#1f2937; font-weight:600; background:#eef2ff; border-color:#c7d2fe;
    text-decoration:none; }
  .section-label { font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:.06em;
    color:var(--mut); margin:4px 0 14px; }
  .breakdown { margin-top:40px; padding-top:28px; border-top:2px solid var(--line); margin-bottom:8px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:20px; }
  .grid.compact { grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); }

  .view { display:none; }
  .view.active { display:block; }

  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:20px 22px; box-shadow:0 1px 3px rgba(16,24,40,.05);
    transition:box-shadow .15s, transform .15s; }
  .card:hover { box-shadow:0 8px 22px rgba(16,24,40,.10); transform:translateY(-2px); }
  .card.group { border-left:4px solid #7c6fde; }
  .eyebrow { font-size:10.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:#6d5fd6; display:block; margin-bottom:2px; }
  .card header { display:flex; align-items:flex-start; gap:10px; margin-bottom:16px; }
  .card h2 { font-size:16px; margin:0; flex:1 1 auto; min-width:0; }
  .badge { font-size:12px; font-weight:600; padding:3px 10px; border-radius:999px; white-space:nowrap; }
  .badge.ok { background:#dcfce7; color:#166534; }
  .badge.warn { background:#fef9c3; color:#854d0e; }
  .badge.bad { background:#fee2e2; color:#991b1b; }
  .delta { font-size:12px; font-weight:600; }
  .delta.up { color:#b91c1c; } .delta.down { color:#15803d; }

  .stats { display:grid; grid-template-columns:repeat(2,1fr); gap:12px 14px; margin-bottom:14px; }
  .stat { display:flex; flex-direction:column; }
  .big { font-size:20px; font-weight:700; line-height:1.1; letter-spacing:-.3px; }
  .big.na { color:#9ca3af; font-weight:600; }
  .big.bad { color:#b91c1c; }
  a.big-link { color:inherit; text-decoration:none; }
  a.big-link:hover { color:#2563eb; text-decoration:underline; }
  a.title-link { color:inherit; text-decoration:none; }
  a.title-link:hover { color:#2563eb; text-decoration:underline; }
  a.detail-link { display:inline-block; margin-top:10px; font-size:13px; color:#2563eb; text-decoration:none; }
  a.detail-link:hover { text-decoration:underline; }
  .pie-links { display:flex; gap:14px; margin:-6px 0 12px; }
  .pl-slot { flex:1 1 0; min-width:0; text-align:center; }
  a.pie-link { font-size:12px; color:var(--mut); text-decoration:none; }
  a.pie-link:hover { color:#2563eb; text-decoration:underline; }
  .lbl { font-size:11.5px; color:var(--mut); margin-top:2px; }

  .mini-pie-row { display:flex; gap:14px; margin:4px 0 12px; }
  .mini-pie { flex:1 1 0; min-width:0; position:relative; height:128px; }
  .pie-na { display:flex; align-items:center; justify-content:center; text-align:center;
    color:#9ca3af; font-size:12px; border:1px dashed var(--line); border-radius:12px; }
  .mini-line-box { position:relative; height:118px; margin:2px 0 12px; }
  .chart-caption { margin:-4px 0 12px; color:var(--mut); font-size:12.5px; }

  table.fields { width:100%; border-collapse:collapse; font-size:13px; table-layout:fixed; }
  table.fields col.c-field { width:46%; }
  table.fields col.c-num { width:27%; }
  table.fields th { color:var(--mut); font-weight:600; text-align:left;
    border-bottom:1px solid var(--line); padding:5px 6px; }
  table.fields td { padding:5px 6px; border-bottom:1px solid #f3f4f6; }
  table.fields th.num, table.fields td.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr.zero td { color:#9ca3af; }
  tr.nonzero td.num { color:#b91c1c; font-weight:700; }
  table.fields.broken, table.fields.detail { table-layout:auto; margin-top:10px; }
  table.fields.broken a, table.fields.detail a { color:#2563eb; text-decoration:none; }
  table.fields.broken a:hover, table.fields.detail a:hover { text-decoration:underline; }
  table.fields.broken tr.more td, table.fields.detail tr.more td { color:var(--mut); font-style:italic; }
  table.fields.detail td { vertical-align:top; max-width:280px; overflow:hidden;
    text-overflow:ellipsis; }
  .na { color:#9ca3af; }
  .empty { color:var(--mut); font-size:13.5px; }
  .detail-section { background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:18px 22px; margin-bottom:22px; box-shadow:0 1px 3px rgba(16,24,40,.05); }
  .detail-section h3 { margin:0 0 6px; font-size:16px; }
  .detail-section .hint { color:var(--mut); font-size:13px; margin:0 0 12px; }

  .card.featured { border:2px solid #1e293b; box-shadow:0 6px 20px rgba(15,23,42,.12);
    margin-bottom:28px; padding:24px 26px; }
  .card.featured h2 { font-size:19px; }
  .featured-body { display:flex; gap:30px; align-items:center; flex-wrap:wrap; }
  .featured-left { flex:1 1 380px; }
  .featured-left .stats { margin-bottom:0; }
  .featured-right { flex:1 1 320px; display:flex; gap:18px; }
  .featured-right .mini-pie { height:190px; }
  .drift-headline { display:flex; flex-direction:column; align-items:flex-start; gap:4px;
    margin-top:14px; padding-top:14px; border-top:1px solid var(--line); }
  .dh-num { font-size:42px; font-weight:800; letter-spacing:-1.4px; line-height:1; }
  .dh-sub { color:var(--mut); font-size:13px; }
  .featured-right.stacked { flex-direction:column; }
  .totals-line { margin-top:22px; padding-top:18px; border-top:1px solid var(--line); }
  .totals-line h3 { margin:0 0 12px; font-size:15px; }
  .big-line-box { position:relative; height:320px; }

  footer { color:var(--mut); font-size:12.5px; margin-top:36px; text-align:center; }
  @media (max-width:560px) { .stats { grid-template-columns:repeat(2,1fr); } }
"""

CHART_HELPERS = """
const BZ_COLORS = ['#2563eb', '#cbd5e1'];
const JIRA_COLORS = ['#10b981', '#cbd5e1'];
const JIRA3_COLORS = ['#10b981', '#6ee7b7', '#cbd5e1'];

function doughnut(el, labels, values, colors, title) {
  const total = values.reduce((a, b) => a + b, 0);
  new Chart(el, {
    type: 'doughnut',
    data: { labels, datasets: [{ data: values, backgroundColor: colors,
            borderColor: '#fff', borderWidth: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '62%',
      plugins: {
        title: { display: !!title, text: title, color: '#6b7280', font: { size: 12, weight: '600' } },
        legend: { display: false },
        tooltip: { callbacks: { label: c => {
          const p = total ? (100 * c.parsed / total).toFixed(1) : '0';
          return ` ${c.label}: ${c.parsed.toLocaleString()} (${p}%)`;
        } } }
      }
    }
  });
}

function miniLine(el, labels, data, color) {
  new Chart(el, {
    type: 'line',
    data: { labels, datasets: [{ data, borderColor: color,
            backgroundColor: color + '22', borderWidth: 2, tension: 0.25,
            pointRadius: 2, pointHoverRadius: 5, fill: true, spanGaps: true }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: c => c.parsed.y == null ? '—' : c.parsed.y.toFixed(2) + '%' } } },
      scales: {
        y: { beginAtZero: true, ticks: { callback: v => v + '%', font: { size: 10 }, maxTicksLimit: 4 },
              grid: { color: '#f1f3f6' } },
        x: { ticks: { font: { size: 9 }, maxTicksLimit: 5, maxRotation: 0 }, grid: { display: false } }
      }
    }
  });
}

function multiLine(el, labels, series, showLegend) {
  new Chart(el, {
    type: 'line',
    data: { labels, datasets: series.map(s => ({
      label: s.label, data: s.data, borderColor: s.color,
      backgroundColor: s.color + '18', borderWidth: 2, tension: 0.25,
      pointRadius: 3, pointHoverRadius: 6, fill: false, spanGaps: true })) },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: showLegend !== false, position: 'bottom',
                  labels: { usePointStyle: true, padding: 14, font: { size: 12 } } },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y == null ? '—' : c.parsed.y.toFixed(2) + '%'}` } }
      },
      scales: {
        y: { beginAtZero: true, title: { display: true, text: 'Drift score (% of linked pairs out of sync)' },
              ticks: { callback: v => v + '%' }, grid: { color: '#eef0f4' } },
        x: { grid: { display: false }, ticks: { maxRotation: 45, minRotation: 0 } }
      }
    }
  });
}

/* Build every chart in a view, once, the first time it becomes visible —
   Chart.js needs a laid-out canvas to size itself correctly. */
function initCharts(view) {
  const V = VIEWS[view.id];
  if (!V || view.dataset.charted) return;
  view.dataset.charted = '1';
  view.querySelectorAll('canvas[data-chart]').forEach(el => {
    const kind = el.dataset.chart, key = el.dataset.key, title = el.dataset.title;
    const p = key ? V.pies[key] : null;
    if (kind === 'bz') {
      if (p) doughnut(el, ['Tagged', 'Untagged'], [p.tagged, p.untagged], BZ_COLORS, title || 'Bugzilla');
    } else if (kind === 'jira') {
      if (!p) return;
      if (p.linked_other > 0) {
        doughnut(el, ['Linked (this tag)', 'Linked (other tags)', 'Not linked'],
                 [p.linked, p.linked_other, p.unlinked], JIRA3_COLORS, title || 'Jira');
      } else {
        doughnut(el, ['Linked', 'Not linked'], [p.linked, p.unlinked], JIRA_COLORS, title || 'Jira');
      }
    } else if (kind === 'mini') {
      miniLine(el, V.labels, V.hist[key] || [], V.colors[key]);
    } else if (kind === 'big') {
      multiLine(el, V.labels, [{ label: key, data: V.hist[key] || [], color: V.colors[key] }], false);
    } else if (kind === 'all') {
      multiLine(el, V.labels, (V.chartTags || []).map(
        t => ({ label: t, data: V.hist[t] || [], color: V.colors[t] })));
    }
  });
}
"""

ROUTER = """
/* Hash router: every view lives in this one file, reached by a path-shaped hash
   — '#/' (full report), '#/group/<group>', '#/tag/<tag>' and the deep links
   '#/tag/<tag>/drift' | '#/tag/<tag>/broken'. Routes deliberately don't match
   any element id, so the browser never scroll-to-fragments behind our back. */
function route(hash) {
  const parts = (hash || '').replace(/^#\\/?/, '').split('/').filter(Boolean)
    .map(s => { try { return decodeURIComponent(s); } catch (e) { return s; } });
  if (parts[0] === 'group' && parts[1]) return { view: 'view-group-' + parts[1] };
  if (parts[0] === 'tag' && parts[1]) {
    return { view: 'view-tag-' + parts[1],
             anchor: parts[2] ? parts[2] + '-' + parts[1] : null };
  }
  return { view: 'view-full' };
}

let anchor = null;   // element to scroll to for the current view (null = top)

function position() {
  if (anchor) anchor.scrollIntoView({ block: 'start' });
  else window.scrollTo(0, 0);
}

function showView(hash) {
  const r = route(hash);
  const view = document.getElementById(r.view) ||
               document.getElementById('view-full') || document.querySelector('.view');
  if (!view) return;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v === view));
  document.querySelectorAll('.nav a[data-view]').forEach(
    a => a.classList.toggle('nav-here', a.dataset.view === view.id));
  const here = document.querySelector('.nav a.nav-here');
  const collapsed = here && here.closest('details');
  if (collapsed) collapsed.open = true;   // reveal the active link's nav row
  initCharts(view);
  anchor = (r.anchor && view.id === r.view) ? document.getElementById(r.anchor) : null;
  position();
  requestAnimationFrame(position);   // settle once the view has laid out
}
window.addEventListener('hashchange', () => showView(location.hash));
showView(location.hash);
"""


def _embed(obj):
    """JSON for inline <script>, with '<' escaped so a stray '</script>' can't
    close the tag early."""
    return json.dumps(obj).replace("<", "\\u003c")


def _html_page(title, body, view_payloads):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>{STYLE}</style>
<noscript><style>.view {{ display:block; }}</style></noscript></head>
<body><div class="wrap">
{body}
</div>
<script>
const VIEWS = {_embed(view_payloads)};
{CHART_HELPERS}
{ROUTER}
</script>
</body></html>"""


def _daily_drift(entries: list) -> dict:
    """Collapse a tag's runs to the latest run per calendar day.

    Returns ``{date: drift_pct%}`` (drift is None if that day's latest run had no
    diff). Charts use this so multiple runs in a day don't clutter the timeline.
    """
    by_day: dict = {}  # date -> (ts, drift%|None)
    for e in entries:
        ts = datetime.fromisoformat(e["ts"])
        drift = e["diff"]["drift_pct"] * 100 if e.get("diff") else None
        cur = by_day.get(ts.date())
        if cur is None or ts > cur[0]:
            by_day[ts.date()] = (ts, drift)
    return {day: drift for day, (_, drift) in by_day.items()}


def _hist_series(data, tags):
    """(chart labels, {tag: [drift% per label, None for gaps]}) — one point per
    calendar day covered by ``tags``."""
    daily = {t: _daily_drift(data[t]) for t in tags}
    all_days = sorted({d for t in tags for d in daily[t]})
    index = {d: i for i, d in enumerate(all_days)}
    hist = {}
    for t in tags:
        arr = [None] * len(all_days)
        for d, drift in daily[t].items():
            arr[index[d]] = drift
        hist[t] = arr
    return [d.strftime("%b %d") for d in all_days], hist


# ---------------------------------------------------------------------------
# Summary view (full report / group) — Totals + card grid + drift-over-time
# ---------------------------------------------------------------------------
def _summary_view(view_id, data, tags, latest_sec, prev_sec, *, scope="",
                  group_cards_html=""):
    """Return (html, chart payload) for a summary view scoped to ``tags``."""
    tags = sorted(tags)
    labels, hist = _hist_series(data, tags)
    total_entries = sum(len(data[t]) for t in tags)

    totals = aggregate_metrics([data[t][-1] for t in tags])
    total_sec = _section(f"Totals ({len(tags)} tags)",
                         totals["bugzilla"], totals["jira"], totals["diff"],
                         broken=totals.get("broken_links"))

    # Chart: show only the top-N tags by current (latest) drift, busiest first.
    CHART_N = 10
    latest_drift = {t: (latest_sec[t]["drift"]["pct"] if latest_sec[t]["drift"] else 0.0)
                    for t in tags}
    movers = sorted((t for t in tags if latest_drift[t] > 0),
                    key=lambda t: latest_drift[t], reverse=True)
    chart_tags = movers[:CHART_N]
    if not movers:
        chart_caption = f"All {len(tags)} tags currently at 0% drift (see per-tag cards)."
    elif len(movers) > len(chart_tags):
        chart_caption = (f"Showing top {len(chart_tags)} of {len(tags)} tags by current drift; "
                         f"{len(tags) - len(chart_tags)} more not shown (see per-tag cards).")
    else:
        chart_caption = (f"Showing {len(chart_tags)} tag(s) with current drift; "
                         f"{len(tags) - len(chart_tags)} steady at 0% (see per-tag cards).")

    n_with_bz = sum(1 for t in tags if "bz_bugs" in latest_sec[t]["metrics"])
    bz_title = f"Bugzilla bugs ({n_with_bz} of {len(tags)} tags)"

    cards = "\n".join(tag_card(t, latest_sec[t], prev_sec[t]) for t in tags)
    scope_html = f"{html.escape(scope)} · " if scope else ""

    body = f"""
  <section class="view" id="{html.escape(view_id)}">
    <p class="sub">{scope_html}Latest snapshot {_latest_label(data, tags)} · {total_entries} snapshots across {len(tags)} tags</p>
    {totals_card(total_sec, chart_caption, bz_title)}
    {group_cards_html}
    <section class="breakdown tags">
      <p class="section-label">Per-tag breakdown</p>
      <div class="grid">
        {cards}
      </div>
    </section>
  </section>"""

    payload = {
        "labels": labels,
        "chartTags": chart_tags,
        "hist": hist,
        "pies": {**{t: pie_for(latest_sec[t]) for t in tags}, "Totals": pie_for(total_sec)},
        "colors": {**{t: color_for(t, i) for i, t in enumerate(tags)},
                   "Totals": COLORS["Totals"]},
    }
    return body, payload


# Above this many tags the 'Tags' nav row is collapsed into a <details>, so the
# chrome stays one line tall; the router opens it when a tag view is active.
_NAV_TAGS_COLLAPSE = 12


def _nav_row(label, links):
    parts = [f'<a data-view="{html.escape(vid)}" href="{_href(vid)}">{html.escape(text)}</a>'
             for text, vid in links]
    return f'<div class="nav"><span class="nav-lbl">{label}</span> {" ".join(parts)}</div>'


def _site_nav(nav_groups, tags, names=None):
    """The document's nav: a 'Reports' row (full report + one link per group)
    and a 'Tags' row (one link per tag view). The router marks the active link,
    so the nav is rendered once for the whole file."""
    names = names or {}
    items = [("Full Report", FULL_VIEW)]
    items += [(names.get(g, g), group_view_id(g)) for g in nav_groups]
    out = _nav_row("Reports", items) + "\n"
    if tags:
        tag_links = [(t, tag_view_id(t)) for t in sorted(tags)]
        row = _nav_row("Tags", tag_links)
        if len(tag_links) > _NAV_TAGS_COLLAPSE:
            out += (f'<details class="nav-details"><summary>Tags ({len(tag_links)})</summary>'
                    f'{row}</details>\n')
        else:
            out += row + "\n"
    return out


def group_card(group, sec, display=None):
    """A compact per-group card for the full report's 'Per-group breakdown'."""
    d = sec["drift"]
    drift_pct = d["pct"] if d else 0.0
    href = _href(group_view_id(group))
    label = html.escape(display or group)
    return f"""
    <section class="card group">
      <header>
        <h2><span class="eyebrow">Group</span><a class="title-link" href="{href}">{label}</a></h2>
        <span class="badge {_badge(drift_pct)}">drift {drift_pct:.2f}%</span>
      </header>
      {_stats(sec)}
      <a class="detail-link" href="{href}">View group report →</a>
    </section>"""


def _group_sections(data, groups, nav_groups, names=None):
    """{group: section} of aggregated metrics for each group (latest snapshots)."""
    names = names or {}
    secs = {}
    for g in nav_groups:
        gtags = [t for t in groups[g] if t in data]
        totals = aggregate_metrics([data[t][-1] for t in gtags])
        secs[g] = _section(f"{names.get(g, g)} ({len(gtags)} tags)",
                           totals["bugzilla"], totals["jira"],
                           totals["diff"], broken=totals.get("broken_links"))
    return secs


def _group_breakdown(nav_groups, group_secs, names=None):
    """The full report's 'Per-group breakdown' section (one card per group)."""
    if not nav_groups:
        return ""
    names = names or {}
    cards = "".join(group_card(g, group_secs[g], names.get(g)) for g in nav_groups)
    return ('<section class="breakdown">'
            '<p class="section-label">Per-group breakdown</p>\n'
            f'<div class="grid compact">{cards}</div></section>')


# ---------------------------------------------------------------------------
# Per-tag detail view (drilldown)
# ---------------------------------------------------------------------------
def tag_detail_view(key, entries, sec):
    """Return (html, chart payload) for one tag's drilldown view."""
    labels, hist = _hist_series({key: entries}, [key])
    latest_ts = max(datetime.fromisoformat(e["ts"]) for e in entries)

    m = sec["metrics"]
    d = sec["drift"]
    drift_pct = d["pct"] if d else 0.0
    has_bz = "bz_bugs" in m

    title = html.escape(sec["title"])
    if m.get("jira_project_url"):
        title = (f'<a class="title-link" href="{html.escape(m["jira_project_url"])}" '
                 f'target="_blank" rel="noopener">{title}</a>')

    untagged_link = ""
    if m.get("untagged_url"):
        untagged_link = (f'<a class="pie-link" href="{html.escape(m["untagged_url"])}" '
                         f'target="_blank" rel="noopener">view untagged bugs ↗</a>')
    unlinked_link = ""
    if m.get("unlinked_url"):
        unlinked_link = (f'<a class="pie-link" href="{html.escape(m["unlinked_url"])}" '
                         f'target="_blank" rel="noopener">view unlinked issues ↗</a>')
    pie_links = (f'<div class="pie-links"><span class="pl-slot">{untagged_link}</span>'
                 f'<span class="pl-slot">{unlinked_link}</span></div>'
                 if (untagged_link or unlinked_link) else "")

    bz_pie = (f'<div class="mini-pie">{_canvas("bz", key)}</div>' if has_bz else
              '<div class="mini-pie pie-na"><span>No Bugzilla<br>components configured</span></div>')

    drift_sub = (f'{fmt(d["out"])} / {fmt(d["total"])} linked pairs out of sync' if d else "")
    n_broken = len(sec.get("broken_links") or [])
    esc_key = html.escape(key)
    body = f"""
  <section class="view" id="{html.escape(tag_view_id(key))}">
    <a class="backlink" href="{_href(FULL_VIEW)}">← All tags</a>
    <p class="sub">Tag detail · latest snapshot {latest_ts.strftime('%A %d %B %Y, %H:%M')}</p>

    <section class="card featured">
      <header>
        <h2>{title}</h2>
      </header>
      <div class="featured-body">
        <div class="featured-left">
          {_stats(sec)}
          <div class="drift-headline">
            <span class="dh-num">{drift_pct:.2f}%</span>
            <span class="dh-sub">overall drift{f'<br>{drift_sub}' if drift_sub else ''}</span>
          </div>
        </div>
        <div class="featured-right stacked">
          <div class="mini-pie-row">
            {bz_pie}
            <div class="mini-pie">{_canvas("jira", key)}</div>
          </div>
          {pie_links}
        </div>
      </div>
      <div class="totals-line">
        <h3>Drift score over time</h3>
        <div class="big-line-box">{_canvas("big", key)}</div>
      </div>
    </section>

    <section class="detail-section">
      <h3 id="drift-{esc_key}">Drifted fields</h3>
      <p class="hint">Each row is one out-of-sync field on a linked bug ↔ issue pair.
         "Should be" is the value forward-sync would write to Jira from Bugzilla.</p>
      {_drift_detail_table(sec)}
    </section>

    <section class="detail-section">
      <h3 id="broken-{esc_key}">Broken links ({n_broken})</h3>
      <p class="hint">Bugzilla see_also references to deleted/moved Jira issues, and
         orphaned Jira issues whose tagged bug no longer links them.</p>
      {_broken_links_table(sec, csv_name=f"output/broken_links_{key}.csv", limit=None) or '<p class="empty">None — all links resolve.</p>'}
    </section>
  </section>"""

    payload = {"labels": labels, "hist": hist, "pies": {key: pie_for(sec)},
               "colors": {key: color_for(key, 0)}}
    return body, payload


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------
def _nav_groups(data):
    """(groups→tags, sorted group keys worth their own view, group→display-name).

    A group is skipped when none of its tags are present, and — when it is the
    only group and covers every tag in the report — when its view would just
    duplicate the full report.
    """
    groups = load_groups()
    nav = [g for g in sorted(groups) if any(t in data for t in groups[g])]
    if len(nav) == 1 and {t for t in groups[nav[0]] if t in data} == set(data):
        nav = []
    return groups, nav, group_display_names()


def render(data):
    """Render the whole report — every view — as one HTML document."""
    tags = sorted(data)
    groups, nav_groups, names = _nav_groups(data)

    latest_sec = {t: _section_from_entry(t, data[t][-1]) for t in tags}
    prev_sec = {t: (_section_from_entry(t, data[t][-2]) if len(data[t]) > 1 else None)
                for t in tags}
    # linked_other is project-accurate across the whole loaded set.
    _apply_linked_other(latest_sec, data, tags)

    payloads, views = {}, []
    group_secs = _group_sections(data, groups, nav_groups, names)
    body, payloads[FULL_VIEW] = _summary_view(
        FULL_VIEW, data, tags, latest_sec, prev_sec,
        group_cards_html=_group_breakdown(nav_groups, group_secs, names))
    views.append(body)

    for g in nav_groups:
        gtags = [t for t in groups[g] if t in data]
        vid = group_view_id(g)
        body, payloads[vid] = _summary_view(
            vid, data, gtags, latest_sec, prev_sec, scope=f"Group: {names.get(g, g)}")
        views.append(body)

    for t in tags:
        vid = tag_view_id(t)
        body, payloads[vid] = tag_detail_view(t, data[t], latest_sec[t])
        views.append(body)

    latest_ts = max(datetime.fromisoformat(e["ts"]) for t in tags for e in data[t])
    doc_body = f"""  <h1>JBI Sync Drift Report</h1>
{_site_nav(nav_groups, tags, names)}
{"".join(views)}
  <footer>Generated by make_report.py from snapshots/*.jsonl · {latest_ts.strftime('%Y-%m-%d %H:%M')}</footer>"""

    return _html_page(f"JBI Drift Report — {latest_ts.strftime('%Y-%m-%d %H:%M')}",
                      doc_body, payloads)


def build_report():
    """Write the whole drift report — every tag, every view — to ``output/report.html``.

    Returns the loaded snapshot data.
    """
    data = load_snapshots()
    if not data:
        raise SystemExit(f"No snapshots found in {SNAPSHOT_DIR}/.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(data))
    return data


def _latest_label(data, tags):
    latest = max(datetime.fromisoformat(data[t][-1]["ts"]) for t in tags)
    return latest.strftime("%A %d %B %Y, %H:%M")


def main():
    # No options: the report always covers every tag in the store and always
    # lands at output/report.html. argparse is kept for --help.
    argparse.ArgumentParser(
        description=f"Build the standalone drift report ({OUT}) from the snapshot store."
    ).parse_args()
    data = build_report()
    total = sum(len(v) for v in data.values())
    print(f"wrote {OUT} from {total} snapshots across {len(data)} tags")


if __name__ == "__main__":
    main()
