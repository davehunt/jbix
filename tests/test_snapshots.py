"""Tests for jbix/snapshots.py — the per-tag history and latest-run store."""

import json
from datetime import datetime

import pytest

import jbix.snapshots as snapshots
from jbix.snapshots import record_snapshot

_HEALTH = {
    "bugzilla": {"total": 10, "tagged": 4, "untagged": 6,
                 "pct_tagged": 40.0, "pct_untagged": 60.0,
                 "bugs_url": "https://bz/all", "untagged_url": "https://bz/untagged"},
    "jira": {"total": 8, "linked": 3, "unlinked": 5,
             "pct_linked": 37.5, "pct_unlinked": 62.5,
             "project_url": "https://j/FP", "issues_url": "https://j/i",
             "unlinked_url": "https://j/u"},
}
_TS = datetime(2026, 6, 24, 9, 0, 0)


def _diff(n_rows=2):
    return {
        "total_pairs": 3, "drifted_pairs": 2, "drift_pct": 2 / 3,
        "fields": [{"field": "priority", "name": "priority", "n": 2, "pct": 2 / 3},
                   {"field": "summary", "name": "summary", "n": 0, "pct": 0.0}],
        "rows": [{"bug_url": f"https://bugzil.la/{i}", "jira_url": f"https://j/X-{i}",
                  "field": "priority", "before": "P3", "after": "P1",
                  "extra": "dropped"} for i in range(n_rows)],
    }


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(snapshots, "LATEST_DIR", tmp_path / "latest")


def _history(tag="fp"):
    return [json.loads(line) for line in snapshots._path(tag).read_text().splitlines()]


def _latest(tag="fp"):
    return json.loads(snapshots.latest_path(tag).read_text())


def test_history_line_holds_counts_only():
    rows = [{"bug_id": 111, "jira_key": "FP-9"}]
    record_snapshot("fp", "FP", _HEALTH, _diff(), _TS, broken_links=rows)
    [line] = _history()
    assert line == {
        "ts": "2026-06-24T09:00:00",
        "jira_project": "FP",
        "bugzilla": {"total": 10, "tagged": 4, "untagged": 6},
        "jira": {"total": 8, "linked": 3, "unlinked": 5},
        "diff": {"total_pairs": 3, "drifted_pairs": 2,
                 "fields": {"priority": 2, "summary": 0}},
        "broken_links": 1,
    }


def test_history_appends_and_latest_is_replaced():
    record_snapshot("fp", "FP", _HEALTH, _diff(n_rows=2), _TS)
    record_snapshot("fp", "FP", _HEALTH, _diff(n_rows=1), datetime(2026, 6, 25, 9, 0, 0))
    assert [h["ts"] for h in _history()] == ["2026-06-24T09:00:00", "2026-06-25T09:00:00"]
    latest = _latest()
    assert latest["ts"] == "2026-06-25T09:00:00"
    assert len(latest["diff"]["rows"]) == 1


def test_latest_keeps_query_urls_and_whitelisted_rows():
    record_snapshot("fp", "FP", _HEALTH, _diff(), _TS)
    latest = _latest()
    assert latest["bugzilla"]["untagged_url"] == "https://bz/untagged"
    assert latest["jira"]["unlinked_url"] == "https://j/u"
    assert "pct_linked" not in latest["jira"]  # derived, so not stored
    assert set(latest["diff"]["rows"][0]) == set(snapshots._DRIFT_ROW_KEYS)  # 'extra' dropped
    assert latest["diff"]["rows_total"] == 2


def test_latest_caps_rows_but_counts_them_all():
    n = snapshots._DRIFT_ROWS_CAP + 25
    broken = [{"bug_id": i, "jira_key": f"X-{i}"} for i in range(snapshots._BROKEN_LINKS_CAP + 5)]
    record_snapshot("fp", "FP", _HEALTH, _diff(n_rows=n), _TS, broken_links=broken)
    latest = _latest()
    assert len(latest["diff"]["rows"]) == snapshots._DRIFT_ROWS_CAP
    assert latest["diff"]["rows_total"] == n
    assert len(latest["broken_links"]) == snapshots._BROKEN_LINKS_CAP
    assert latest["broken_links_total"] == len(broken)
    assert _history()[0]["broken_links"] == len(broken)


def test_missing_metrics_and_links_are_recorded_as_empty():
    record_snapshot("omc", "OMC", {"bugzilla": None, "jira": _HEALTH["jira"]}, None, _TS)
    [line] = _history("omc")
    assert line["bugzilla"] is None and line["diff"] is None and line["broken_links"] == 0
    assert _latest("omc")["broken_links"] == []


def test_has_snapshot():
    assert not snapshots.has_snapshot("fp")
    record_snapshot("fp", "FP", _HEALTH, None, _TS)
    assert snapshots.has_snapshot("fp")


def test_paths_follow_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", None)
    monkeypatch.setattr(snapshots, "LATEST_DIR", None)
    monkeypatch.setenv("JBIX_DATA_DIR", str(tmp_path / "data"))
    record_snapshot("fp", "FP", _HEALTH, None, _TS)
    assert (tmp_path / "data" / "snapshots" / "fp.jsonl").is_file()
    assert (tmp_path / "data" / "latest" / "fp.json").is_file()


def test_every_change_goes_to_the_csv_uncapped(monkeypatch):
    monkeypatch.setattr(snapshots, "_DRIFT_ROWS_CAP", 1)
    record_snapshot("fp", "FP", _HEALTH, _diff(n_rows=3), _TS)
    lines = snapshots.latest_csv_path("fp").read_text().splitlines()
    assert len(lines) == 4 and len(_latest()["diff"]["rows"]) == 1
    assert lines[1] == "https://bugzil.la/0,https://j/X-0,priority,P3 → P1,P3,P1"


def test_csv_goes_when_nothing_has_drifted():
    record_snapshot("fp", "FP", _HEALTH, _diff(), _TS)
    assert snapshots.latest_csv_path("fp").exists()
    record_snapshot("fp", "FP", _HEALTH, None, _TS)
    assert not snapshots.latest_csv_path("fp").exists()


@pytest.mark.parametrize("field, before, after, text", [
    ("labels", ["a", "stale"], ["a", "todo"], "+ todo; − stale"),
    ("labels", ["a", "b"], ["b", "a"], "reordered only"),
    ("links", "", "duplicates FXP-1", "+ duplicates FXP-1"),
    ("links", "relates to FXP-2", "", "− relates to FXP-2"),
    ("priority", "", "P2", "none → P2"),
    ("status", "Backlog (none)", "Done (Won't Do)", "Backlog (none) → Done (Won't Do)"),
])
def test_change_text(field, before, after, text):
    assert snapshots.change_text(field, before, after) == text
