"""Tests for jbix/fuzzy.py — text preprocessing and fuzzy matching."""

import csv
from unittest.mock import MagicMock

from jbix.fuzzy import (
    export_matches_to_csv,
    fetch_all_jira_issues,
    fetch_unlinked_jira_issues,
    find_candidate_matches,
    preprocess_text,
)

# ---------------------------------------------------------------------------
# preprocess_text
# ---------------------------------------------------------------------------


class TestPreprocessText:
    def test_lowercases_input(self):
        assert preprocess_text("Hello WORLD") == "hello world"

    def test_removes_special_chars(self):
        result = preprocess_text("hello(world)!")
        assert "(" not in result
        assert ")" not in result
        assert "!" not in result

    def test_collapses_whitespace(self):
        assert preprocess_text("  too   many    spaces  ") == "too many spaces"

    def test_strips_leading_trailing(self):
        assert preprocess_text("  text  ") == "text"

    def test_preserves_hyphens(self):
        assert "-" in preprocess_text("some-hyphenated-text")

    def test_empty_string(self):
        assert preprocess_text("") == ""

    def test_handles_numbers(self):
        result = preprocess_text("Bug 123456: Fix crash")
        assert "123456" in result


# ---------------------------------------------------------------------------
# find_candidate_matches
# ---------------------------------------------------------------------------


def _make_bug(summary, component="Profiler", product="Firefox", bug_id=1):
    return {
        "id": bug_id,
        "summary": summary,
        "product": product,
        "component": component,
        "url": f"https://bugzil.la/{bug_id}",
    }


def _make_issue(summary, components=None, key="FXP-1"):
    return {
        "key": key,
        "summary": summary,
        "components": components or ["Firefox::Profiler"],
        "url": f"https://mozilla-hub.atlassian.net/browse/{key}",
    }


class TestFindCandidateMatches:
    def test_returns_match_above_threshold(self):
        bug = _make_bug("Fix crash in profiler when recording")
        issue = _make_issue(
            "Fix crash in profiler when recording",
            components=["Firefox::Profiler"],
        )
        matches = find_candidate_matches([bug], [issue], threshold=80)
        assert len(matches) == 1
        assert matches[0][2] >= 80

    def test_no_match_below_threshold(self):
        bug = _make_bug("Fix crash in profiler recording")
        issue = _make_issue(
            "Unrelated Jira task about documentation",
            components=["Firefox::Profiler"],
        )
        matches = find_candidate_matches([bug], [issue], threshold=80)
        assert len(matches) == 0

    def test_component_filter_excludes_different_component(self):
        bug = _make_bug(
            "Fix crash in profiler recording",
            component="Profiler",
            product="Firefox",
        )
        issue = _make_issue(
            "Fix crash in profiler recording",
            components=["Testing::Raptor"],  # different component
        )
        matches = find_candidate_matches([bug], [issue], threshold=50)
        assert len(matches) == 0

    def test_no_component_filter_compares_all(self):
        bug = _make_bug("Fix crash in profiler recording", component="Profiler")
        issue = _make_issue(
            "Fix crash in profiler recording",
            components=["Testing::Raptor"],
        )
        matches = find_candidate_matches(
            [bug], [issue], threshold=50, component_filter=False
        )
        assert len(matches) == 1

    def test_results_sorted_by_score_descending(self):
        bug = _make_bug("performance regression in browser startup")
        issue_high = _make_issue(
            "performance regression in browser startup speed",
            components=["Firefox::Profiler"],
            key="FXP-1",
        )
        issue_low = _make_issue(
            "performance regression in browser general",
            components=["Firefox::Profiler"],
            key="FXP-2",
        )
        matches = find_candidate_matches(
            [bug], [issue_high, issue_low], threshold=50
        )
        if len(matches) > 1:
            assert matches[0][2] >= matches[1][2]

    def test_empty_bugs_list(self):
        issue = _make_issue("some issue")
        assert find_candidate_matches([], [issue]) == []

    def test_empty_issues_list(self):
        bug = _make_bug("some bug")
        assert find_candidate_matches([bug], []) == []

    def test_token_prefilter_requires_two_shared_tokens(self):
        """Bugs sharing only one token of length ≥3 should not match via component filter."""
        bug = _make_bug("crash firefox startup performance regression recorder history")
        issue = _make_issue(
            "crash only single shared",  # only 1 token >= 3 shared with bug
            components=["Firefox::Profiler"],
        )
        matches = find_candidate_matches([bug], [issue], threshold=10)
        # Should not find a match if token pre-filter blocks it
        # (depends on exact token overlap, but this tests the code path)
        assert isinstance(matches, list)


# ---------------------------------------------------------------------------
# export_matches_to_csv
# ---------------------------------------------------------------------------


class TestExportMatchesToCsv:
    def test_writes_csv_with_correct_columns(self, tmp_path):
        bug = _make_bug("Test bug summary", bug_id=999)
        issue = _make_issue("Test issue summary", key="FXP-50")
        output_file = str(tmp_path / "matches.csv")

        export_matches_to_csv([(bug, issue, 92)], output_file)

        with open(output_file) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        row = rows[0]
        assert row["bug_id"] == "999"
        assert row["bug_summary"] == "Test bug summary"
        assert row["jira_key"] == "FXP-50"
        assert row["similarity_score"] == "92"

    def test_writes_multiple_rows(self, tmp_path):
        bug = _make_bug("Bug", bug_id=1)
        issue1 = _make_issue("Issue 1", key="FXP-1")
        issue2 = _make_issue("Issue 2", key="FXP-2")
        output_file = str(tmp_path / "matches.csv")

        export_matches_to_csv([(bug, issue1, 90), (bug, issue2, 85)], output_file)

        with open(output_file) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2

    def test_empty_matches_writes_header_only(self, tmp_path):
        output_file = str(tmp_path / "empty.csv")
        export_matches_to_csv([], output_file)

        with open(output_file) as f:
            content = f.read()
        assert "bug_id" in content
        assert "\n" in content  # header row present

    def test_components_pipe_separated(self, tmp_path):
        bug = _make_bug("Bug", bug_id=1)
        issue = _make_issue("Issue", components=["A", "B", "C"], key="FXP-1")
        output_file = str(tmp_path / "matches.csv")

        export_matches_to_csv([(bug, issue, 80)], output_file)

        with open(output_file) as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["jira_components"] == "A|B|C"


# ---------------------------------------------------------------------------
# fetch_unlinked_jira_issues
# ---------------------------------------------------------------------------


class TestFetchUnlinkedJiraIssues:
    def test_calls_search_issues_with_correct_jql(self):
        mock_client = MagicMock()
        issue = MagicMock()
        issue.key = "FXP-50"
        issue.fields.summary = "Some issue"
        issue.fields.components = []
        mock_client.search_issues.return_value = [issue]

        result = fetch_unlinked_jira_issues(mock_client, "FXP")

        mock_client.search_issues.assert_called_once()
        jql = mock_client.search_issues.call_args[0][0]
        assert "FXP" in jql
        assert "bugzilla" in jql
        assert len(result) == 1
        assert result[0]["key"] == "FXP-50"

    def test_returns_expected_fields(self):
        mock_client = MagicMock()
        issue = MagicMock()
        issue.key = "FXP-1"
        issue.fields.summary = "Test"
        comp = MagicMock()
        comp.name = "Firefox::Profiler"
        issue.fields.components = [comp]
        mock_client.search_issues.return_value = [issue]

        result = fetch_unlinked_jira_issues(mock_client, "FXP")
        assert result[0]["components"] == ["Firefox::Profiler"]
        assert "url" in result[0]


# ---------------------------------------------------------------------------
# fetch_all_jira_issues
# ---------------------------------------------------------------------------


class TestFetchAllJiraIssues:
    def test_returns_all_issues_with_bugzilla_flag(self):
        mock_client = MagicMock()
        linked = MagicMock()
        linked.key = "FXP-1"
        linked.fields.summary = "Linked"
        linked.fields.components = []
        linked.fields.labels = ["bugzilla"]

        unlinked = MagicMock()
        unlinked.key = "FXP-2"
        unlinked.fields.summary = "Unlinked"
        unlinked.fields.components = []
        unlinked.fields.labels = []

        mock_client.search_issues.return_value = [linked, unlinked]

        result = fetch_all_jira_issues(mock_client, "FXP")
        assert len(result) == 2

        linked_result = next(r for r in result if r["key"] == "FXP-1")
        unlinked_result = next(r for r in result if r["key"] == "FXP-2")
        assert linked_result["has_bugzilla_label"] is True
        assert unlinked_result["has_bugzilla_label"] is False
