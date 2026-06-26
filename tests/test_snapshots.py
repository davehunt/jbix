"""Tests for jbix/snapshots.py — the per-tag snapshot store."""

import json
from datetime import datetime

import jbix.snapshots as snapshots
from jbix.snapshots import record_snapshot

_HEALTH = {
    "bugzilla": {"total": 10, "tagged": 4, "untagged": 6,
                 "pct_tagged": 40.0, "pct_untagged": 60.0},
    "jira": {"total": 8, "linked": 3, "unlinked": 5,
             "pct_linked": 37.5, "pct_unlinked": 62.5},
}


def _read(path):
    return json.loads(path.read_text().splitlines()[-1])


def test_record_snapshot_stores_broken_links(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path)
    rows = [{"bug_id": 111, "bug_url": "https://bugzil.la/111",
             "jira_key": "FP-9", "jira_url": "https://j/FP-9"}]
    path = record_snapshot("fp", "FP", _HEALTH, None, datetime(2026, 6, 24, 9, 0, 0),
                           broken_links=rows)
    assert _read(path)["broken_links"] == rows


def test_record_snapshot_defaults_broken_links_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path)
    path = record_snapshot("fp", "FP", _HEALTH, None, datetime(2026, 6, 24, 9, 0, 0))
    assert _read(path)["broken_links"] == []


def test_record_snapshot_caps_broken_links(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path)
    rows = [{"bug_id": i, "jira_key": f"X-{i}"} for i in range(snapshots._BROKEN_LINKS_CAP + 50)]
    path = record_snapshot("fp", "FP", _HEALTH, None, datetime(2026, 6, 24, 9, 0, 0),
                           broken_links=rows)
    assert len(_read(path)["broken_links"]) == snapshots._BROKEN_LINKS_CAP


def _diff_with_rows(n):
    return {
        "total_pairs": n, "drifted_pairs": n, "drift_pct": 1.0, "fields": [],
        "rows": [{"bug_url": f"https://bugzil.la/{i}", "jira_url": f"https://j/X-{i}",
                  "field": "priority", "before": "P3", "after": "P1",
                  "extra": "dropped"} for i in range(n)],
    }


def test_trim_diff_stores_whitelisted_drift_rows():
    out = snapshots._trim_diff(_diff_with_rows(2))
    assert len(out["rows"]) == 2
    assert set(out["rows"][0]) == set(snapshots._DRIFT_ROW_KEYS)  # 'extra' dropped


def test_trim_diff_caps_drift_rows():
    out = snapshots._trim_diff(_diff_with_rows(snapshots._DRIFT_ROWS_CAP + 25))
    assert len(out["rows"]) == snapshots._DRIFT_ROWS_CAP


def test_trim_diff_handles_missing_rows():
    out = snapshots._trim_diff({"total_pairs": 1, "drifted_pairs": 0,
                                "drift_pct": 0.0, "fields": []})
    assert out["rows"] == []
