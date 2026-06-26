"""Per-tag drift snapshot store + cross-tag aggregation.

Each forward ``jbix.py`` run appends one compact JSON line per tag to
``snapshots/<tag>.jsonl``. A report (``make_report.py``) later assembles the
latest snapshot of every tag — even tags captured in earlier runs — and
recomputes cross-tag Totals via :func:`aggregate_metrics`.

Stdlib-only by design: this module is imported by ``jbix.py`` *and* by the
standalone ``make_report.py`` (which must run without ``.env``). It must not
pull in ``jbix.bugzilla``/``jbix.jira``/``jbix.people``, which read credentials at
import time (see the ``load_dotenv()`` ordering note in CLAUDE.md).
"""

import json
import pathlib
from datetime import datetime

SNAPSHOT_DIR = pathlib.Path("snapshots")

# Keys copied from each metric dict. The numeric counts drive the report's
# stats/pies; the *_url keys (deterministic Bugzilla/Jira queries) are kept so a
# report assembled later from the latest snapshot can link to live queries.
_BUGZILLA_KEYS = ("total", "tagged", "untagged", "pct_tagged", "pct_untagged",
                  "bugs_url", "untagged_url")
_JIRA_KEYS = ("total", "linked", "unlinked", "pct_linked", "pct_unlinked",
              "project_url", "issues_url", "unlinked_url")

# Cap broken-link rows stored per snapshot line (full set is in the CSV).
_BROKEN_LINKS_CAP = 50

# Cap per-field drift rows stored per snapshot line (full set is in output/updates.csv).
# These power the per-tag detail report's "Drifted fields" table.
_DRIFT_ROWS_CAP = 50
_DRIFT_ROW_KEYS = ("bug_url", "jira_url", "field", "before", "after")


def _trim(d: dict | None, keys: tuple[str, ...]) -> dict | None:
    return {k: d[k] for k in keys if k in d} if d else None


def _trim_diff(diff: dict | None) -> dict | None:
    if not diff:
        return None
    return {
        "total_pairs": diff["total_pairs"],
        "drifted_pairs": diff["drifted_pairs"],
        "drift_pct": diff["drift_pct"],
        "fields": [
            {"field": f.get("field", f["name"]), "name": f["name"],
             "n": f["n"], "pct": f["pct"]}
            for f in diff["fields"]
        ],
        "rows": [
            {k: r[k] for k in _DRIFT_ROW_KEYS if k in r}
            for r in (diff.get("rows") or [])[:_DRIFT_ROWS_CAP]
        ],
    }


def _path(tag: str) -> pathlib.Path:
    return SNAPSHOT_DIR / f"{tag}.jsonl"


def has_snapshot(tag: str) -> bool:
    """True if ``snapshots/<tag>.jsonl`` exists and holds at least one entry."""
    p = _path(tag)
    return p.is_file() and p.stat().st_size > 0


def record_snapshot(
    tag: str,
    jira_project: str,
    health_metrics: dict,
    diff_metrics: dict | None,
    ts: datetime,
    broken_links: list | None = None,
) -> pathlib.Path:
    """Append one snapshot line for ``tag`` to ``snapshots/<tag>.jsonl``.

    ``health_metrics`` / ``diff_metrics`` are the dicts returned by
    ``run_health_check`` / ``run_diff_check`` (or the silent ``compute_*``
    variants) in ``jbix.health``. ``broken_links`` is the list of stale
    Bugzilla→Jira see-also references from ``find_broken_links`` (capped to keep
    the line small; the full set is in ``output/broken_links_<tag>.csv``).
    """
    entry = {
        "ts": ts.isoformat(timespec="seconds"),
        "jira_project": jira_project,
        "bugzilla": _trim(health_metrics.get("bugzilla"), _BUGZILLA_KEYS),
        "jira": _trim(health_metrics.get("jira"), _JIRA_KEYS),
        "diff": _trim_diff(diff_metrics),
        "broken_links": (broken_links or [])[:_BROKEN_LINKS_CAP],
    }
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(tag)
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return path


def aggregate_metrics(per_tag: list[dict]) -> dict:
    """Sum per-tag metric dicts into cross-tag totals.

    Each entry is ``{"bugzilla": <dict|None>, "jira": <dict|None>, "diff": <dict|None>}``
    as produced by ``run_health_check`` / ``run_diff_check`` (extra keys such as
    ``ts``/``jira_project`` on stored snapshot entries are ignored). Counts are
    summed and percentages recomputed; ``bugzilla``/``jira``/``diff`` are None if
    no tag supplied them.
    """
    bzs = [m["bugzilla"] for m in per_tag if m.get("bugzilla")]
    bz_total = None
    if bzs:
        total = sum(b["total"] for b in bzs)
        tagged = sum(b["tagged"] for b in bzs)
        untagged = sum(b["untagged"] for b in bzs)
        bz_total = {
            "total": total, "tagged": tagged, "untagged": untagged,
            "pct_tagged": tagged / total * 100 if total else 0,
            "pct_untagged": untagged / total * 100 if total else 0,
            "untagged_url": None,
        }

    jrs = [m for m in per_tag if m.get("jira")]
    jr_total = None
    if jrs:
        # Per-tag "linked" is disjoint (each tag's bugs link to its own issues),
        # so sum it. But several tags can share one Jira project, so count each
        # project's total size only once (keyed by jira_project; entries without
        # a project key are treated as distinct so nothing collapses). If two
        # snapshots of the same project disagree on total, keep the larger.
        linked = sum(m["jira"]["linked"] for m in jrs)
        project_total: dict = {}
        for i, m in enumerate(jrs):
            key = m.get("jira_project") or f"__no_project_{i}"
            project_total[key] = max(project_total.get(key, 0), m["jira"]["total"])
        total = sum(project_total.values())
        unlinked = max(total - linked, 0)
        jr_total = {
            "total": total, "linked": linked, "unlinked": unlinked,
            "pct_linked": linked / total * 100 if total else 0,
            "pct_unlinked": unlinked / total * 100 if total else 0,
            "unlinked_url": None,
        }

    diffs = [m["diff"] for m in per_tag if m.get("diff")]
    diff_total = None
    if diffs:
        total_pairs = sum(d["total_pairs"] for d in diffs)
        drifted_pairs = sum(d["drifted_pairs"] for d in diffs)
        # Sum per-field counts by display name, preserving first-seen order.
        field_n: dict[str, int] = {}
        for d in diffs:
            for f in d["fields"]:
                field_n[f["name"]] = field_n.get(f["name"], 0) + f["n"]
        fields = [
            {"name": name, "n": n, "pct": n / total_pairs if total_pairs else 0}
            for name, n in field_n.items()
        ]
        diff_total = {
            "total_pairs": total_pairs,
            "fields": fields,
            "drifted_pairs": drifted_pairs,
            "drift_pct": drifted_pairs / total_pairs if total_pairs else 0,
        }

    broken_total: list = []
    for m in per_tag:
        broken_total.extend(m.get("broken_links") or [])

    return {"bugzilla": bz_total, "jira": jr_total, "diff": diff_total,
            "broken_links": broken_total}
