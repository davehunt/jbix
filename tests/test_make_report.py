"""Tests for make_report.py — fragment rendering, routing, and CLI scoping."""

import sys

import pytest

import make_report

_BROKEN = [
    {"bug_id": 111, "bug_url": "https://bugzil.la/111",
     "jira_key": "AIACT-19", "jira_url": "https://j/AIACT-19"},
    {"bug_id": 222, "bug_url": "https://bugzil.la/222",
     "jira_key": "AIMOD-1320", "jira_url": "https://j/AIMOD-1320"},
]


def _section(broken):
    return make_report._section(
        "[aiact] → AIACT",
        {"total": 5, "tagged": 2, "pct_tagged": 40.0},
        {"total": 10, "linked": 3, "pct_linked": 30.0},
        None,
        broken=broken,
    )


def test_section_carries_broken_links():
    sec = _section(_BROKEN)
    assert sec["broken_links"] == _BROKEN


def test_section_from_entry_reads_broken_links():
    entry = {"jira_project": "AIACT", "bugzilla": None, "jira": None,
             "diff": None, "broken_links": _BROKEN}
    assert make_report._section_from_entry("aiact", entry)["broken_links"] == _BROKEN


def test_stats_shows_broken_count_emphasised_when_nonzero():
    out = make_report._stats(_section(_BROKEN))
    assert "broken links" in out
    assert "big bad" in out  # emphasised
    assert ">2<" in out      # count


def test_stats_broken_count_zero_not_emphasised():
    out = make_report._stats(_section([]))
    assert "broken links" in out
    assert "big bad" not in out


def test_broken_links_table_renders_links():
    html = make_report._broken_links_table(_section(_BROKEN))
    assert ">111<" in html and "https://bugzil.la/111" in html  # no "bug " prefix
    assert "AIACT-19" in html and "https://j/AIACT-19" in html


def test_broken_links_table_empty_when_none():
    assert make_report._broken_links_table(_section([])) == ""


def test_broken_links_table_caps_at_20():
    many = [{"bug_id": i, "bug_url": f"https://bugzil.la/{i}",
             "jira_key": f"X-{i}", "jira_url": f"https://j/X-{i}"} for i in range(25)]
    html = make_report._broken_links_table(_section(many), csv_name="broken_links_x.csv")
    assert "+5 more" in html and "broken_links_x.csv" in html


_DIFF = {
    "total_pairs": 3, "drifted_pairs": 2, "drift_pct": 2 / 3,
    "fields": [{"field": "priority", "name": "priority", "n": 2, "pct": 2 / 3}],
    "rows": [
        {"bug_url": "https://bugzil.la/111",
         "jira_url": "https://mozilla-hub.atlassian.net/browse/FXPE-5",
         "field": "priority", "before": "P3", "after": "P1"},
        {"bug_url": "https://bugzil.la/222",
         "jira_url": "https://mozilla-hub.atlassian.net/browse/FXPE-6",
         "field": "summary", "before": "x" * 200, "after": "<b>y</b>"},
    ],
}


def _diff_section(broken=None):
    return make_report._section(
        "[fxpe] → FXPE",
        {"total": 5, "tagged": 2, "pct_tagged": 40.0},
        {"total": 10, "linked": 3, "pct_linked": 30.0},
        _DIFF,
        broken=broken or _BROKEN,
    )


def test_section_carries_drift_rows():
    assert _diff_section()["drift_rows"] == _DIFF["rows"]


def test_stats_links_to_detail_when_tag_set():
    out = make_report._stats(_diff_section(), "fxpe")
    assert 'href="#/tag/fxpe/drift"' in out
    assert 'href="#/tag/fxpe/broken"' in out


def test_stats_no_detail_links_when_tag_absent():
    out = make_report._stats(_diff_section())
    assert "/tag/" not in out   # no per-tag view to deep-link into


def test_tag_card_has_detail_link_and_no_broken_table():
    card = make_report.tag_card("fxpe", _diff_section(), None)
    assert "View tag report" in card
    # the bottom link goes to the view top, not the #drift-<tag> anchor
    assert '<a class="detail-link" href="#/tag/fxpe">View tag report' in card
    assert "fields broken" not in card  # broken table moved to the detail view


def test_tag_card_renders_all_drifted_fields():
    # Any field health records must appear, not just a hard-coded subset.
    diff = {"total_pairs": 2, "drifted_pairs": 1, "drift_pct": 0.5, "rows": [],
            "fields": [{"field": "issuetype", "name": "type", "n": 1, "pct": 0.5},
                       {"field": "remote_links", "name": "remote_links", "n": 1, "pct": 0.5}]}
    sec = make_report._section("[fxpe] → FXPE", None, None, diff)
    card = make_report.tag_card("fxpe", sec, None)
    assert "<td>type</td>" in card
    assert "<td>remote_links</td>" in card


def test_drift_detail_table_renders_links_and_values():
    html = make_report._drift_detail_table(_diff_section())
    assert ">111<" in html and "https://bugzil.la/111" in html  # no "bug " prefix
    assert "FXPE-5" in html
    assert "P3" in html and "P1" in html
    assert "Should be (Bugzilla)" in html


def test_drift_detail_table_truncates_and_escapes():
    html = make_report._drift_detail_table(_diff_section())
    assert "…" in html                 # long summary truncated
    assert "&lt;b&gt;y&lt;/b&gt;" in html  # HTML-escaped value


def test_drift_detail_table_empty_note():
    sec = make_report._section("[x] → X", None, None, None)
    assert "No field-level detail" in make_report._drift_detail_table(sec)


def test_daily_drift_keeps_latest_run_per_day():
    from datetime import date
    entries = [
        {"ts": "2026-07-14T09:00:00", "diff": {"drift_pct": 0.10}},
        {"ts": "2026-07-14T15:00:00", "diff": {"drift_pct": 0.20}},  # latest that day wins
        {"ts": "2026-07-15T08:00:00", "diff": {"drift_pct": 0.30}},
    ]
    d = make_report._daily_drift(entries)
    assert d == {date(2026, 7, 14): 20.0, date(2026, 7, 15): 30.0}


def test_daily_drift_latest_run_without_diff_is_none():
    from datetime import date
    entries = [
        {"ts": "2026-07-14T09:00:00", "diff": {"drift_pct": 0.10}},
        {"ts": "2026-07-14T15:00:00"},  # latest run that day has no diff → gap
    ]
    assert make_report._daily_drift(entries) == {date(2026, 7, 14): None}


def test_site_nav_links_full_groups_and_tags():
    nav = make_report._site_nav(["performance", "genai"], ["fp", "fxpe"])
    assert 'href="#/"' in nav and "Full Report" in nav
    assert 'href="#/group/performance"' in nav and 'href="#/group/genai"' in nav
    assert 'href="#/tag/fp"' in nav and 'href="#/tag/fxpe"' in nav
    # the router marks the active link at runtime, via data-view
    assert 'data-view="view-full"' in nav
    assert "nav-here" not in nav


def test_site_nav_uses_display_names():
    nav = make_report._site_nav(["deng"], [], names={"deng": "Data Engineering"})
    assert ">Data Engineering</a>" in nav        # label is the display name
    assert 'href="#/group/deng"' in nav          # route still uses the group key


def test_group_card_uses_display_name():
    card = make_report.group_card("deng", _diff_section(), "Data Engineering")
    assert ">Data Engineering</a>" in card
    assert 'href="#/group/deng"' in card         # link keyed by group slug


def test_group_card_links_to_group_view():
    sec = _diff_section()
    card = make_report.group_card("performance", sec)
    assert 'href="#/group/performance"' in card
    assert "View group report" in card
    assert 'class="card group"' in card                  # visually distinct
    assert "mini-pie" not in card                         # no pies in group cards


_ENTRY = {
    "ts": "2026-06-25T10:00:00", "jira_project": "FXPE",
    "bugzilla": {"total": 5, "tagged": 2, "untagged": 3,
                 "pct_tagged": 40.0, "pct_untagged": 60.0},
    "jira": {"total": 10, "linked": 3, "unlinked": 7,
             "pct_linked": 30.0, "pct_unlinked": 70.0},
    "diff": _DIFF, "broken_links": _BROKEN,
}


def test_tag_detail_view_has_anchored_sections():
    sec = make_report._section_from_entry("fxpe", _ENTRY)
    view, payload = make_report.tag_detail_view("fxpe", [_ENTRY], sec)
    assert 'id="view-tag-fxpe"' in view                  # router target
    assert 'id="drift-fxpe"' in view and 'id="broken-fxpe"' in view
    assert "card featured" in view                       # totals-style featured card
    assert 'href="#/"' in view                           # back to the full report
    assert ">111<" in view                               # drift row (no "bug " prefix)
    assert "AIACT-19" in view                            # broken-links row in full
    assert payload["pies"]["fxpe"]["linked"] == 3        # chart payload for this view


_ENTRY2 = {**_ENTRY, "ts": "2026-06-26T10:00:00", "jira_project": "FP"}


def test_build_report_writes_one_file_with_every_view(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(make_report, "load_snapshots",
                        lambda: {"fxpe": [_ENTRY], "fp": [_ENTRY2]})
    monkeypatch.setattr(make_report, "load_groups",
                        lambda: {"perf": ["fxpe"], "other": ["fp"]})
    monkeypatch.setattr(make_report, "group_display_names", lambda: {"perf": "Performance"})
    make_report.build_report()
    page = (tmp_path / "output" / "report.html").read_text()
    for view_id in ("view-full", "view-group-perf", "view-group-other",
                    "view-tag-fxpe", "view-tag-fp"):
        assert f'id="{view_id}"' in page                 # every view in one file
        assert f'"{view_id}"' in page                    # ...and its chart payload
    assert "Per-group breakdown" in page and "View group report" in page
    assert 'href="#/tag/fxpe/drift"' in page             # card deep link
    assert "JBI Sync Drift Report" in page               # single h1
    assert "function showView" in page                   # hash router


def test_single_group_covering_all_tags_gets_no_group_view(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(make_report, "load_snapshots", lambda: {"fxpe": [_ENTRY]})
    monkeypatch.setattr(make_report, "load_groups", lambda: {"perf": ["fxpe"]})
    make_report.build_report()
    page = (tmp_path / "output" / "report.html").read_text()
    assert 'id="view-group-perf"' not in page  # would duplicate the full report
    assert "Per-group breakdown" not in page


def test_no_snapshots_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(make_report, "load_snapshots", lambda: {})
    with pytest.raises(SystemExit):
        make_report.build_report()


def test_main_takes_no_scoping_options(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(make_report, "load_snapshots", lambda: {"fxpe": [_ENTRY]})
    monkeypatch.setattr(make_report, "load_groups", lambda: {})
    monkeypatch.setattr(sys, "argv", ["make_report.py"])
    make_report.main()
    assert "output/report.html" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["make_report.py", "--tags", "fxpe"])
    with pytest.raises(SystemExit):
        make_report.main()
