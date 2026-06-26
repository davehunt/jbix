"""Tests for jbix/health.py — run_health_check and sub-functions."""

from unittest.mock import MagicMock, patch

from jbix.health import (
    _check_bugzilla,
    _check_jira,
    _diff_metrics,
    _find_link_candidates,
    _jira_metrics,
    aggregate_metrics,
    count_linked_jira_issues,
    run_diff_check,
    run_health_check,
)
from tests.conftest import make_bugz

COMPONENTS = ["Firefox::Profiler", "Testing::Raptor"]


# ---------------------------------------------------------------------------
# run_health_check
# ---------------------------------------------------------------------------


class TestRunHealthCheck:
    def test_calls_check_bugzilla_when_components_provided(self, capsys):
        bugz = make_bugz()
        bugz.client.query.return_value = []
        bugz.client.build_query.return_value = {}
        jira_client = MagicMock()
        jira_client.search_issues.return_value = []

        with patch("jbix.health._check_bugzilla") as mock_bz, patch(
            "jbix.health._check_jira"
        ) as mock_jira:
            run_health_check("fxp", "FXP", bugz, jira_client, COMPONENTS, jira_issues={})
        mock_bz.assert_called_once_with("fxp", COMPONENTS, bugz)
        mock_jira.assert_called_once_with("FXP", jira_client, jira_issues={}, linked=None)

    def test_skips_bugzilla_when_no_components(self, capsys):
        bugz = make_bugz()
        jira_client = MagicMock()
        jira_client.search_issues.return_value = []

        with patch("jbix.health._check_bugzilla") as mock_bz, patch(
            "jbix.health._check_jira"
        ):
            run_health_check("fxp", "FXP", bugz, jira_client, components=None)
        mock_bz.assert_not_called()
        # A "skipping" message printed to stdout
        captured = capsys.readouterr()
        assert "skip" in captured.out.lower() or "No components" in captured.out

    def test_calls_find_link_candidates_when_enabled(self, capsys):
        bugz = make_bugz()
        jira_client = MagicMock()

        with patch("jbix.health._check_bugzilla"), patch(
            "jbix.health._check_jira"
        ), patch("jbix.health._find_link_candidates") as mock_links:
            run_health_check(
                "fxp",
                "FXP",
                bugz,
                jira_client,
                COMPONENTS,
                link_candidates=True,
                threshold=80,
            )
        mock_links.assert_called_once_with("fxp", "FXP", COMPONENTS, bugz, jira_client, 80)

    def test_does_not_call_link_candidates_by_default(self, capsys):
        bugz = make_bugz()
        jira_client = MagicMock()

        with patch("jbix.health._check_bugzilla"), patch(
            "jbix.health._check_jira"
        ), patch("jbix.health._find_link_candidates") as mock_links:
            run_health_check("fxp", "FXP", bugz, jira_client, COMPONENTS)
        mock_links.assert_not_called()

    def test_outputs_header(self, capsys):
        bugz = make_bugz()
        jira_client = MagicMock()

        with patch("jbix.health._check_bugzilla"), patch("jbix.health._check_jira"):
            run_health_check("fxp", "FXP", bugz, jira_client)
        captured = capsys.readouterr()
        assert "─" * 10 in captured.out


# ---------------------------------------------------------------------------
# _check_bugzilla
# ---------------------------------------------------------------------------


class TestCheckBugzilla:
    def test_prints_totals(self, capsys):
        bugz = make_bugz()

        all_bugs = [MagicMock() for _ in range(100)]
        tagged_bugs = [MagicMock() for _ in range(60)]
        bugz.client.query.side_effect = [all_bugs, tagged_bugs]

        with patch("jbix.health.build_component_query") as mock_q:
            mock_q.side_effect = lambda *a, **kw: {"_next_field_num": 10}
            _check_bugzilla("fxp", COMPONENTS, bugz)

        captured = capsys.readouterr()
        assert "100" in captured.out
        assert "60" in captured.out

    def test_handles_zero_total_gracefully(self, capsys):
        bugz = make_bugz()
        bugz.client.query.return_value = []

        with patch("jbix.health.build_component_query") as mock_q:
            mock_q.side_effect = lambda *a, **kw: {"_next_field_num": 10}
            _check_bugzilla("fxp", COMPONENTS, bugz)

        captured = capsys.readouterr()
        assert "0" in captured.out

    def test_paginates_past_batch_size(self, capsys):
        bugz = make_bugz()
        batch1 = [MagicMock() for _ in range(1000)]
        batch2 = [MagicMock() for _ in range(1000)]
        batch3 = [MagicMock() for _ in range(500)]
        bugz.client.query.side_effect = [batch1, batch2, batch3, []]

        with patch("jbix.health.build_component_query") as mock_q:
            mock_q.side_effect = lambda *a, **kw: {"_next_field_num": 10}
            _check_bugzilla("fxp", COMPONENTS, bugz)

        captured = capsys.readouterr()
        assert "2,500" in captured.out


# ---------------------------------------------------------------------------
# _check_jira
# ---------------------------------------------------------------------------


class TestCheckJira:
    def test_prints_linked_and_unlinked_counts(self, capsys):
        jira_client = MagicMock()

        jira_issues = {
            "FXP-1": {"labels": ["bugzilla"]},
            "FXP-2": {"labels": ["bugzilla", "other"]},
            "FXP-3": {"labels": ["other"]},
        }
        _check_jira("FXP", jira_client, jira_issues=jira_issues)

        captured = capsys.readouterr()
        assert "3" in captured.out  # total
        assert "2" in captured.out  # linked
        assert "1" in captured.out  # unlinked

    def test_fetches_fresh_when_no_jira_issues(self, capsys):
        jira_client = MagicMock()

        with patch("jbix.health.fetch_all_jira_issues") as mock_fetch:
            mock_fetch.return_value = [
                {"key": "FXP-1", "has_bugzilla_label": True},
                {"key": "FXP-2", "has_bugzilla_label": True},
                {"key": "FXP-3", "has_bugzilla_label": False},
            ]
            _check_jira("FXP", jira_client)

        captured = capsys.readouterr()
        assert "3" in captured.out  # total
        assert "2" in captured.out  # linked
        assert "1" in captured.out  # unlinked

    def test_handles_empty_project(self, capsys):
        jira_client = MagicMock()
        _check_jira("FXP", jira_client, jira_issues={})
        captured = capsys.readouterr()
        assert "0" in captured.out


class TestCountLinkedJiraIssues:
    def _bugs(self):
        return {
            1: {"jira": {"FID-1": {"key": "FID-1"}}},
            2: {"jira": {"FID-2": {"key": "FID-2"}}},
            3: {"jira": {"FID-2": {"key": "FID-2"}}},   # same issue as bug 2 → distinct
            4: {"jira": {"OTHER-9": {"key": "OTHER-9"}}},  # cross-project
            5: {"external": True, "jira": {"FID-5": {"key": "FID-5"}}},  # external → skip
            6: {"jira": {}},                              # no link
        }

    def test_distinct_count_intersected_with_project(self):
        # Project has FID-1, FID-2 (not OTHER-9, not FID-5).
        jira_issues = {"FID-1": {}, "FID-2": {}, "FID-3": {}}
        assert count_linked_jira_issues(self._bugs(), jira_issues) == 2  # FID-1, FID-2

    def test_without_project_counts_all_distinct_non_external(self):
        # No intersection: distinct keys across non-external bugs = FID-1, FID-2, OTHER-9
        assert count_linked_jira_issues(self._bugs()) == 3


class TestJiraMetricsLinked:
    def test_uses_supplied_linked_count(self):
        jira_issues = {f"FID-{i}": {"labels": []} for i in range(100)}
        m = _jira_metrics("FIDEFE", MagicMock(), jira_issues=jira_issues, linked=42)
        assert m["total"] == 100
        assert m["linked"] == 42
        assert m["unlinked"] == 58

    def test_fallback_counts_bugzilla_label_when_no_linked(self):
        jira_issues = {
            "P-1": {"labels": ["bugzilla"]},
            "P-2": {"labels": ["other"]},
        }
        m = _jira_metrics("FXP", MagicMock(), jira_issues=jira_issues)
        assert m["linked"] == 1


# ---------------------------------------------------------------------------
# _find_link_candidates
# ---------------------------------------------------------------------------


class TestFindLinkCandidates:
    def test_skips_when_no_components(self, capsys):
        bugz = make_bugz()
        jira_client = MagicMock()
        _find_link_candidates("fxp", "FXP", None, bugz, jira_client, 85)
        captured = capsys.readouterr()
        assert "skip" in captured.out.lower() or "Skip" in captured.out

    def test_skips_when_no_unlinked_bugs(self, capsys):
        bugz = make_bugz()
        bugz.client.query.return_value = []  # no unlinked bugs
        jira_client = MagicMock()

        with patch("jbix.health.build_component_query") as mock_q, patch(
            "jbix.health.fetch_unlinked_jira_issues"
        ) as mock_issues:
            mock_q.side_effect = lambda *a, **kw: {"_next_field_num": 10}
            mock_issues.return_value = [MagicMock()]
            _find_link_candidates("fxp", "FXP", COMPONENTS, bugz, jira_client, 85)

        captured = capsys.readouterr()
        assert "No unlinked" in captured.out

    def test_exports_csv_when_matches_found(self, tmp_path, capsys):
        bugz = make_bugz()
        jira_client = MagicMock()
        raw_bug = MagicMock()
        raw_bug.id = 111
        raw_bug.summary = "Fix crash in profiler recording"
        raw_bug.product = "Firefox"
        raw_bug.component = "Profiler"
        bugz.client.query.return_value = [raw_bug]

        unlinked_issue = {
            "key": "FXP-1",
            "summary": "Fix crash in profiler recording",
            "components": ["Firefox::Profiler"],
            "url": "https://example.com/FXP-1",
        }

        with patch("jbix.health.build_component_query") as mock_q, patch(
            "jbix.health.fetch_unlinked_jira_issues", return_value=[unlinked_issue]
        ), patch("jbix.health.find_candidate_matches") as mock_find, patch(
            "jbix.health.export_matches_to_csv"
        ) as mock_export:
            mock_q.side_effect = lambda *a, **kw: {"_next_field_num": 10}
            mock_find.return_value = [
                ({"id": 111, "summary": "x"}, unlinked_issue, 92)
            ]
            _find_link_candidates("fxp", "FXP", COMPONENTS, bugz, jira_client, 85)

        mock_export.assert_called_once()
        captured = capsys.readouterr()
        assert "1" in captured.out  # "Found 1 candidate"

    def test_no_csv_when_no_matches(self, capsys):
        bugz = make_bugz()
        raw_bug = MagicMock()
        raw_bug.id = 111
        raw_bug.summary = "some bug"
        raw_bug.product = "Firefox"
        raw_bug.component = "Profiler"
        bugz.client.query.return_value = [raw_bug]

        jira_client = MagicMock()
        unlinked_issue = {
            "key": "FXP-1",
            "summary": "completely unrelated thing here",
            "components": ["Firefox::Profiler"],
            "url": "https://example.com",
        }

        with patch("jbix.health.build_component_query") as mock_q, patch(
            "jbix.health.fetch_unlinked_jira_issues", return_value=[unlinked_issue]
        ), patch("jbix.health.find_candidate_matches", return_value=[]), patch(
            "jbix.health.export_matches_to_csv"
        ) as mock_export:
            mock_q.side_effect = lambda *a, **kw: {"_next_field_num": 10}
            _find_link_candidates("fxp", "FXP", COMPONENTS, bugz, jira_client, 85)

        mock_export.assert_not_called()
        captured = capsys.readouterr()
        assert "No candidate" in captured.out


# ---------------------------------------------------------------------------
# run_diff_check
# ---------------------------------------------------------------------------


def _make_bugs(n: int = 3) -> dict:
    """Return a minimal bugzilla_bugs dict with n bug/jira pairs."""
    bugs = {}
    for i in range(1, n + 1):
        bugs[i] = {
            "id": i,
            "jira": {
                f"FXP-{i}": {
                    "key": f"FXP-{i}",
                    "url": f"https://hub/FXP-{i}",
                }
            },
            "url": f"https://bugzil.la/{i}",
        }
    return bugs


def _make_update(bug_id: int, jira_key: str, jira_field: str) -> dict:
    return {
        "bug_url": f"https://bugzil.la/{bug_id}",
        "jira_url": f"https://hub/{jira_key}",
        "jira_field": jira_field,
        "bug_status": "NEW",
        "bug_product": "Firefox",
        "bug_component": "General",
        "bug_field": "",
        "bug_before": "",
        "bug_after": "",
        "jira_before": "old",
        "jira_after": "new",
    }


class TestRunDiffCheck:
    def test_no_output_when_no_pairs(self, capsys):
        updates = [_make_update(1, "FXP-1", "summary")]
        run_diff_check(updates, {}, {"summary"})
        assert capsys.readouterr().out == ""

    def test_no_output_when_no_enabled_fields(self, capsys):
        run_diff_check([], _make_bugs(), set())
        assert capsys.readouterr().out == ""

    def test_prints_section_header(self, capsys):
        updates = [_make_update(1, "FXP-1", "summary")]
        run_diff_check(updates, _make_bugs(), {"summary"})
        assert "Field comparison" in capsys.readouterr().out

    def test_shows_zero_diffs_when_no_updates(self, capsys):
        # With enabled fields and pairs but no updates, shows 0 diffs
        run_diff_check([], _make_bugs(), {"summary"})
        out = capsys.readouterr().out
        assert "Field comparison" in out
        assert "/ 3" in out
        assert "summary" in out

    def test_counts_distinct_pairs(self, capsys):
        # Two updates with the same (bug_url, jira_url, field) → counted once
        u = _make_update(1, "FXP-1", "summary")
        run_diff_check([u, u], _make_bugs(), {"summary"})
        out = capsys.readouterr().out
        # 1 distinct pair out of 3 total
        assert "1" in out
        assert "/ 3" in out

    def test_diff_metrics_returns_repair_rows(self):
        # One row per update, with display field name + before/after values.
        from jbix.constants import JIRA_SEVERITY_FIELD
        m = _diff_metrics(
            [_make_update(1, "FXP-1", "summary"),
             _make_update(2, "FXP-2", JIRA_SEVERITY_FIELD)],
            _make_bugs(3), {"summary", "severity"},
        )
        rows = m["rows"]
        assert len(rows) == 2
        assert rows[0] == {"bug_url": "https://bugzil.la/1", "jira_url": "https://hub/FXP-1",
                           "field": "summary", "before": "old", "after": "new"}
        assert rows[1]["field"] == "severity"  # customfield mapped to display name

    def test_severity_display_name(self, capsys):
        from jbix.constants import JIRA_SEVERITY_FIELD
        updates = [_make_update(1, "FXP-1", JIRA_SEVERITY_FIELD)]
        run_diff_check(updates, _make_bugs(), {"severity"})
        assert "severity" in capsys.readouterr().out

    def test_unknown_field_uses_raw_name(self, capsys):
        # An update with an unrecognised jira_field is still shown via fallback
        updates = [_make_update(1, "FXP-1", "custom_field_xyz")]
        run_diff_check(updates, _make_bugs(), {"summary"})
        assert "custom_field_xyz" in capsys.readouterr().out

    def test_drift_score_shown(self, capsys):
        updates = [_make_update(1, "FXP-1", "summary")]
        run_diff_check(updates, _make_bugs(), {"summary"})
        assert "Drift score" in capsys.readouterr().out

    def test_drift_score_zero_when_no_updates(self, capsys):
        run_diff_check([], _make_bugs(), {"summary"})
        out = capsys.readouterr().out
        assert "Drift score" in out
        assert "0 / 3" in out
        assert "0.00%" in out

    def test_drift_score_counts_unique_pairs_across_fields(self, capsys):
        # Bug 1 drifts on both summary and priority — should count as 1 drifted pair
        updates = [
            _make_update(1, "FXP-1", "summary"),
            _make_update(1, "FXP-1", "priority"),
        ]
        run_diff_check(updates, _make_bugs(), {"summary", "priority"})
        out = capsys.readouterr().out
        # 1 unique pair drifted out of 3 total
        assert "1 / 3" in out
        assert "33.33%" in out

    def test_drift_score_100_percent(self, capsys):
        bugs = _make_bugs(2)
        updates = [
            _make_update(1, "FXP-1", "summary"),
            _make_update(2, "FXP-2", "summary"),
        ]
        run_diff_check(updates, bugs, {"summary"})
        out = capsys.readouterr().out
        assert "2 / 2" in out
        assert "100.00%" in out


class TestAggregateAndTotals:
    def _per_tag(self):
        a_diff = _diff_metrics([_make_update(1, "FXP-1", "summary")], _make_bugs(2), {"summary"})
        b_diff = _diff_metrics(
            [_make_update(1, "FXP-1", "summary"), _make_update(2, "FXP-2", "priority")],
            _make_bugs(3), {"summary", "priority"},
        )
        return [
            {"bugzilla": {"total": 100, "tagged": 80, "untagged": 20,
                          "pct_tagged": 80.0, "pct_untagged": 20.0, "untagged_url": "x"},
             "jira": {"total": 90, "linked": 70, "unlinked": 20,
                      "pct_linked": 77.8, "pct_unlinked": 22.2, "unlinked_url": "x"},
             "diff": a_diff},
            {"bugzilla": {"total": 300, "tagged": 240, "untagged": 60,
                          "pct_tagged": 80.0, "pct_untagged": 20.0, "untagged_url": "x"},
             "jira": {"total": 110, "linked": 80, "unlinked": 30,
                      "pct_linked": 72.7, "pct_unlinked": 27.3, "unlinked_url": "x"},
             "diff": b_diff},
        ]

    def test_aggregate_sums_counts(self):
        totals = aggregate_metrics(self._per_tag())
        assert totals["bugzilla"]["total"] == 400
        assert totals["bugzilla"]["tagged"] == 320
        assert totals["bugzilla"]["pct_tagged"] == 80.0
        assert totals["jira"]["total"] == 200
        assert totals["jira"]["linked"] == 150

    def test_aggregate_sums_diff_fields(self):
        totals = aggregate_metrics(self._per_tag())
        # tag A: 2 pairs, tag B: 3 pairs → 5 total
        assert totals["diff"]["total_pairs"] == 5
        # summary drifted in both tags (1 each) → 2; priority only in B → 1
        by_name = {f["name"]: f["n"] for f in totals["diff"]["fields"]}
        assert by_name["summary"] == 2
        assert by_name["priority"] == 1
        assert totals["diff"]["drifted_pairs"] == 3

    def test_aggregate_handles_missing_sections(self):
        per_tag = [{"bugzilla": None, "jira": None, "diff": None}]
        totals = aggregate_metrics(per_tag)
        assert totals == {"bugzilla": None, "jira": None, "diff": None,
                          "broken_links": []}

    def test_aggregate_concatenates_broken_links(self):
        per_tag = [
            {"bugzilla": None, "jira": None, "diff": None,
             "broken_links": [{"bug_id": 1, "jira_key": "A-1"}]},
            {"bugzilla": None, "jira": None, "diff": None,
             "broken_links": [{"bug_id": 2, "jira_key": "B-2"}]},
            {"bugzilla": None, "jira": None, "diff": None},  # no key → tolerated
        ]
        totals = aggregate_metrics(per_tag)
        assert totals["broken_links"] == [
            {"bug_id": 1, "jira_key": "A-1"},
            {"bug_id": 2, "jira_key": "B-2"},
        ]

    def test_aggregate_dedups_jira_total_for_shared_project(self):
        # Two tags map to one project: count its size once, sum the (disjoint) linked.
        per_tag = [
            {"jira_project": "FIDEFE", "bugzilla": None, "diff": None,
             "jira": {"total": 100, "linked": 30, "unlinked": 70,
                      "pct_linked": 30.0, "pct_unlinked": 70.0, "unlinked_url": None}},
            {"jira_project": "FIDEFE", "bugzilla": None, "diff": None,
             "jira": {"total": 100, "linked": 12, "unlinked": 88,
                      "pct_linked": 12.0, "pct_unlinked": 88.0, "unlinked_url": None}},
        ]
        totals = aggregate_metrics(per_tag)
        assert totals["jira"]["total"] == 100      # counted once
        assert totals["jira"]["linked"] == 42       # 30 + 12
        assert totals["jira"]["unlinked"] == 58

    def test_aggregate_sums_distinct_jira_projects(self):
        per_tag = [
            {"jira_project": "A", "bugzilla": None, "diff": None,
             "jira": {"total": 100, "linked": 30, "unlinked": 70,
                      "pct_linked": 30.0, "pct_unlinked": 70.0, "unlinked_url": None}},
            {"jira_project": "B", "bugzilla": None, "diff": None,
             "jira": {"total": 50, "linked": 10, "unlinked": 40,
                      "pct_linked": 20.0, "pct_unlinked": 80.0, "unlinked_url": None}},
        ]
        totals = aggregate_metrics(per_tag)
        assert totals["jira"]["total"] == 150
        assert totals["jira"]["linked"] == 40
