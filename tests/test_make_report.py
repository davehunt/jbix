"""Tests for make_report.py — broken-link rendering + --group dispatch."""

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


def test_stats_links_to_detail_when_url_set():
    out = make_report._stats(_diff_section(), detail_url="tags/fxpe.html")
    assert "tags/fxpe.html#drift" in out
    assert "tags/fxpe.html#broken" in out


def test_stats_no_detail_links_when_url_absent():
    out = make_report._stats(_diff_section())
    assert "#drift" not in out and "#broken" not in out


def test_tag_card_has_detail_link_and_no_broken_table():
    card = make_report.tag_card("fxpe", _diff_section(), None, "tags/fxpe.html")
    assert "View tag report" in card
    # the bottom link goes to the page top, not the #drift anchor
    assert '<a class="detail-link" href="tags/fxpe.html">View tag report' in card
    assert "fields broken" not in card  # broken table moved to the detail page


def test_tag_card_renders_all_drifted_fields():
    # Any field health records must appear, not just a hard-coded subset.
    diff = {"total_pairs": 2, "drifted_pairs": 1, "drift_pct": 0.5, "rows": [],
            "fields": [{"field": "issuetype", "name": "type", "n": 1, "pct": 0.5},
                       {"field": "remote_links", "name": "remote_links", "n": 1, "pct": 0.5}]}
    sec = make_report._section("[fxpe] → FXPE", None, None, diff)
    card = make_report.tag_card("fxpe", sec, None, "tags/fxpe.html")
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


def test_site_nav_links_full_and_groups():
    nav = make_report._site_nav(["performance", "genai"], active="full")
    assert 'href="index.html"' not in nav        # 'full' is the active page → plain text
    assert "nav-here" in nav and "Full Report" in nav
    assert 'href="performance.html"' in nav and 'href="genai.html"' in nav


def test_site_nav_prefix_for_tag_pages():
    nav = make_report._site_nav(["performance"], prefix="../")
    assert 'href="../index.html"' in nav and 'href="../performance.html"' in nav


def test_site_nav_uses_display_names():
    nav = make_report._site_nav(["deng"], names={"deng": "Data Engineering"})
    assert ">Data Engineering</a>" in nav   # label is the display name
    assert 'href="deng.html"' in nav        # href still uses the group key


def test_group_card_uses_display_name():
    card = make_report.group_card("deng", _diff_section(), "Data Engineering")
    assert ">Data Engineering</a>" in card
    assert 'href="deng.html"' in card       # link + filename keyed by group slug


def test_group_card_links_to_group_report():
    sec = _diff_section()
    card = make_report.group_card("performance", sec)
    assert 'href="performance.html"' in card
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


def test_render_tag_detail_has_sections_and_nav():
    sec = make_report._section_from_entry("fxpe", _ENTRY)
    nav = make_report._site_nav(["perf"], prefix="../")
    page = make_report.render_tag_detail("fxpe", [_ENTRY], sec, nav_html=nav)
    assert 'id="drift"' in page and 'id="broken"' in page
    assert "JBI Sync Drift Report" in page  # consistent h1
    assert "card featured" in page          # totals-style featured card
    assert 'href="../index.html"' in page   # nav back to full report
    assert ">111<" in page                  # drift row (no "bug " prefix)
    assert "AIACT-19" in page               # broken-links row rendered in full


def test_build_site_writes_index_group_and_tag_pages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(make_report, "load_snapshots", lambda tags=None: {"fxpe": [_ENTRY]})
    monkeypatch.setattr(make_report, "load_groups", lambda: {"perf": ["fxpe"]})
    make_report.build_site()
    assert (tmp_path / "reports" / "index.html").is_file()
    assert (tmp_path / "reports" / "perf.html").is_file()
    assert (tmp_path / "reports" / "tags" / "fxpe.html").is_file()
    index = (tmp_path / "reports" / "index.html").read_text()
    assert 'href="perf.html"' in index      # site nav + per-group breakdown
    assert "Per-group breakdown" in index
    assert "View group report" in index
    assert 'href="tags/fxpe.html#drift"' in index  # card detail link


class TestGroupDispatch:
    @pytest.fixture
    def captured(self, monkeypatch):
        calls = []
        monkeypatch.setattr(make_report, "generate", lambda tags=None, output=None: calls.append((tags, output)) or {})
        monkeypatch.setattr(make_report, "expand_groups",
                            lambda names: [f"{n}-tag" for n in names])
        return calls

    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["make_report.py", *argv])
        make_report.main()

    def test_single_group_default_name(self, monkeypatch, captured):
        self._run(monkeypatch, ["--group", "performance"])
        assert captured == [(["performance-tag"], "reports/performance.html")]

    def test_multiple_groups_one_file_each(self, monkeypatch, captured):
        self._run(monkeypatch, ["--group", "performance", "genai"])
        assert captured == [(["performance-tag"], "reports/performance.html"),
                            (["genai-tag"], "reports/genai.html")]

    def test_single_group_output_override(self, monkeypatch, captured):
        self._run(monkeypatch, ["--group", "performance", "-o", "x.html"])
        assert captured == [(["performance-tag"], "x.html")]

    def test_tags_only(self, monkeypatch, captured):
        self._run(monkeypatch, ["--tags", "fp", "fxp"])
        assert captured == [(["fp", "fxp"], "reports/index.html")]

    def test_group_and_tags_rejected(self, monkeypatch, captured):
        with pytest.raises(SystemExit):
            self._run(monkeypatch, ["--group", "performance", "--tags", "sp3"])

    def test_output_with_multiple_groups_rejected(self, monkeypatch, captured):
        with pytest.raises(SystemExit):
            self._run(monkeypatch, ["--group", "performance", "genai", "-o", "x.html"])
