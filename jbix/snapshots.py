"""Per-tag drift snapshot store + cross-tag aggregation.

Each forward ``jbix.py`` run records two things per tag, both in the data
directory (see ``jbix.paths``):

- **History**: one compact JSON line appended to ``snapshots/<tag>.jsonl``,
  counts only, so a year of daily runs across every tag stays small enough to
  keep in git. The drift site charts it.
- **Latest**: ``latest/<tag>.json``, overwritten each run, holding the per-bug
  drift rows, broken links and live query URLs for that run. These are only ever
  wanted for the most recent run, and are most of a run's bytes. The rows are
  capped for the page; ``latest/<tag>.csv`` has every one, for download.

:func:`aggregate_metrics` sums per-tag metrics into cross-tag totals.

Stdlib-only by design: this module is imported by ``jbix.py`` *and* by the site
builder (``jbix.site``), which must run without ``.env``. It must not pull in
``jbix.bugzilla``/``jbix.jira``/``jbix.people``, which read credentials at
import time (see the ``load_dotenv()`` ordering note in CLAUDE.md).
"""

import csv
import json
import os
import pathlib
from datetime import datetime

from jbix import paths

# None means the data directory's snapshots/ and latest/; tests set paths directly.
SNAPSHOT_DIR: pathlib.Path | None = None
LATEST_DIR: pathlib.Path | None = None

# Counts kept in each history line, and the extra keys kept in the latest file:
# the *_url keys are deterministic Bugzilla/Jira queries the site links to.
_BUGZILLA_COUNTS = ("total", "tagged", "untagged")
_JIRA_COUNTS = ("total", "linked", "unlinked")
_BUGZILLA_URLS = ("bugs_url", "untagged_url")
_JIRA_URLS = ("project_url", "issues_url", "unlinked_url")

# Per-bug rows kept in the latest file. One file per tag, overwritten, so the
# caps are generous; the full sets are in output/updates.csv and the CSVs.
_BROKEN_LINKS_CAP = 500
_DRIFT_ROWS_CAP = 500
_DRIFT_ROW_KEYS = ("bug_url", "jira_url", "field", "before", "after")


def _snapshot_dir() -> pathlib.Path:
    return SNAPSHOT_DIR or paths.snapshots_dir()


def _latest_dir() -> pathlib.Path:
    return LATEST_DIR or paths.latest_dir()


def _trim(d: dict | None, keys: tuple[str, ...]) -> dict | None:
    return {k: d[k] for k in keys if k in d} if d else None


def _history_diff(diff: dict | None) -> dict | None:
    if not diff:
        return None
    return {
        "total_pairs": diff["total_pairs"],
        "drifted_pairs": diff["drifted_pairs"],
        "fields": {f["name"]: f["n"] for f in diff["fields"]},
    }


def _latest_diff(diff: dict | None) -> dict | None:
    if not diff:
        return None
    rows = diff.get("rows") or []
    return {
        "total_pairs": diff["total_pairs"],
        "drifted_pairs": diff["drifted_pairs"],
        "fields": [{"name": f["name"], "n": f["n"]} for f in diff["fields"]],
        "rows": [{k: r[k] for k in _DRIFT_ROW_KEYS if k in r} for r in rows[:_DRIFT_ROWS_CAP]],
        "rows_total": len(rows),
    }


def _path(tag: str) -> pathlib.Path:
    return _snapshot_dir() / f"{tag}.jsonl"


def latest_path(tag: str) -> pathlib.Path:
    return _latest_dir() / f"{tag}.json"


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
    jbi: dict | None = None,
) -> pathlib.Path:
    """Append ``tag``'s history line and rewrite its latest file; return the history path.

    ``health_metrics`` / ``diff_metrics`` are the dicts returned by
    ``run_health_check`` / ``run_diff_check`` (or the silent ``compute_*``
    variants) in ``jbix.health``. ``broken_links`` is the list from
    ``find_broken_links``: counted in the history, listed in the latest file.
    ``jbi`` is how the run compared the tag (``jbix.config.describe_tag`` plus the
    fields compared), kept in the latest file only: it changes rarely, and the
    history is what is committed.
    """
    stamp = ts.isoformat(timespec="seconds")
    bz, jr = health_metrics.get("bugzilla"), health_metrics.get("jira")
    broken = broken_links or []
    history = {
        "ts": stamp,
        "jira_project": jira_project,
        "bugzilla": _trim(bz, _BUGZILLA_COUNTS),
        "jira": _trim(jr, _JIRA_COUNTS),
        "diff": _history_diff(diff_metrics),
        "broken_links": len(broken),
    }
    latest = {
        "ts": stamp,
        "jira_project": jira_project,
        "bugzilla": _trim(bz, _BUGZILLA_COUNTS + _BUGZILLA_URLS),
        "jira": _trim(jr, _JIRA_COUNTS + _JIRA_URLS),
        "diff": _latest_diff(diff_metrics),
        "broken_links": broken[:_BROKEN_LINKS_CAP],
        "broken_links_total": len(broken),
    }
    if jbi:
        latest["jbi"] = jbi
    path = _path(tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(history, separators=(",", ":")) + "\n")
    _write_json(latest_path(tag), latest)
    _write_changes_csv(latest_csv_path(tag), (diff_metrics or {}).get("rows") or [])
    return path


# -- what a drift row would change -------------------------------------------

def diff_values(field: str, before, after) -> tuple[list, list] | None:
    """What syncing from Bugzilla would add to and remove from a Jira field.

    Bugzilla is the authority. A list value (labels, components) gives what would
    be added and removed; a links row is one link to create or remove. A plain
    value gives None: it is simply replaced.
    """
    if isinstance(before, list) or isinstance(after, list):
        old = before if isinstance(before, list) else []
        new = after if isinstance(after, list) else []
        return [v for v in new if v not in old], [v for v in old if v not in new]
    if field == "links":
        return ([after] if after else []), ([before] if before else [])
    return None


def change_text(field: str, before, after) -> str:
    """A drift row's change as plain text: ``+ todo; − stale`` or ``P3 → P1``."""
    parts = diff_values(field, before, after)
    if parts is None:
        return f"{before or 'none'} → {after or 'none'}"
    added, removed = parts
    return "; ".join([f"+ {v}" for v in added] + [f"− {v}" for v in removed]) or "reordered only"


_CSV_COLUMNS = ("bug", "jira_issue", "field", "change", "jira_value", "bugzilla_value")


def latest_csv_path(tag: str) -> pathlib.Path:
    return _latest_dir() / f"{tag}.csv"


def _write_changes_csv(path: pathlib.Path, rows: list[dict]) -> None:
    """Every drift row, uncapped, or no file at all when nothing has drifted."""
    if not rows:
        path.unlink(missing_ok=True)
        return

    def text(v) -> str:
        return "; ".join(v) if isinstance(v, list) else str(v or "")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_COLUMNS)
        for r in rows:
            field, before, after = r.get("field", ""), r.get("before", ""), r.get("after", "")
            writer.writerow([r.get("bug_url", ""), r.get("jira_url", ""), field,
                             change_text(field, before, after), text(before), text(after)])
    os.replace(tmp, path)


def _write_json(path: pathlib.Path, obj: dict) -> None:
    """Write via a temporary file and rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


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
