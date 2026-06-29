"""Tests for jbix/bugzilla.py — build_component_query and BugzillaClient."""

from unittest.mock import Mock, patch

import pytest

from jbix.bugzilla import (
    BZ_QUERY_MAX_RETRIES,
    BugzillaHTTPError,
    build_component_query,
    retry_on_transient,
)
from tests.conftest import make_bugz


def _http_error(status):
    err = BugzillaHTTPError(f"{status} Server Error")
    err.response = Mock(status_code=status)
    return err


class TestRetryOnTransient:
    def test_returns_result_without_retry(self):
        func = Mock(return_value="ok")
        sleeps = []
        assert retry_on_transient(func, _sleep=sleeps.append) == "ok"
        assert func.call_count == 1
        assert sleeps == []

    def test_retries_then_succeeds_on_502(self):
        func = Mock(side_effect=[_http_error(502), _http_error(503), "ok"])
        sleeps = []
        assert retry_on_transient(func, _sleep=sleeps.append) == "ok"
        assert func.call_count == 3
        assert len(sleeps) == 2  # backed off before each retry

    def test_non_transient_reraised_immediately(self):
        func = Mock(side_effect=_http_error(401))
        sleeps = []
        with pytest.raises(BugzillaHTTPError):
            retry_on_transient(func, _sleep=sleeps.append)
        assert func.call_count == 1
        assert sleeps == []

    def test_exhausts_retries_and_raises(self):
        func = Mock(side_effect=_http_error(502))
        sleeps = []
        with pytest.raises(BugzillaHTTPError):
            retry_on_transient(func, _sleep=sleeps.append)
        assert func.call_count == BZ_QUERY_MAX_RETRIES
        assert len(sleeps) == BZ_QUERY_MAX_RETRIES - 1

# ---------------------------------------------------------------------------
# build_component_query
# ---------------------------------------------------------------------------


class TestBuildComponentQuery:
    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            build_component_query([])

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid component format"):
            build_component_query(["NoColonSeparator"])

    def test_single_pair_structure(self):
        q = build_component_query(["Firefox::Profiler"])
        # Outer OR group opens at f1
        assert q["f1"] == "OP"
        assert q["j1"] == "OR"
        # Inner group for the pair
        assert q["f2"] == "OP"
        assert q["f3"] == "product"
        assert q["o3"] == "equals"
        assert q["v3"] == "Firefox"
        assert q["f4"] == "component"
        assert q["o4"] == "equals"
        assert q["v4"] == "Profiler"
        assert q["f5"] == "CP"
        # Outer OR group closes
        assert q["f6"] == "CP"

    def test_two_pairs_both_present(self):
        q = build_component_query(["Firefox::Profiler", "Testing::Raptor"])
        products = [q[k] for k in q if k.startswith("v") and q[k] in ("Firefox", "Testing")]
        assert "Firefox" in products
        assert "Testing" in products

    def test_wildcard_no_inner_group(self):
        q = build_component_query(["Firefox::*"])
        # Wildcard means only a product= condition, no component= and no inner OP/CP
        field_values = [q[k] for k in q if k.startswith("f")]
        assert "component" not in field_values
        # Product condition should appear directly
        assert any(q.get(k) == "Firefox" for k in q)

    def test_wildcard_has_no_component_condition(self):
        q = build_component_query(["Firefox::*"])
        assert all(q.get(k) != "component" for k in q if k.startswith("o"))

    def test_mixed_wildcard_and_specific(self):
        q = build_component_query(["Firefox::*", "Testing::Raptor"])
        values = [q[k] for k in q if k.startswith("v")]
        assert "Firefox" in values
        assert "Testing" in values
        assert "Raptor" in values

    def test_next_field_num_returned(self):
        q = build_component_query(["Firefox::Profiler"])
        assert "_next_field_num" in q
        assert isinstance(q["_next_field_num"], int)

    def test_next_field_num_advances_with_more_pairs(self):
        q1 = build_component_query(["A::B"])
        q2 = build_component_query(["A::B", "C::D"])
        assert q2["_next_field_num"] > q1["_next_field_num"]

    def test_additional_params_included(self):
        q = build_component_query(["A::B"], include_fields=["id"])
        assert q["include_fields"] == ["id"]

    def test_next_field_num_excluded_from_query_keys(self):
        q = build_component_query(["A::B"])
        q_copy = dict(q)
        q_copy.pop("_next_field_num")
        # All remaining keys should be valid query params (no private keys)
        assert all(not k.startswith("_") for k in q_copy)


# ---------------------------------------------------------------------------
# BugzillaClient._confirm
# ---------------------------------------------------------------------------


class TestBugzillaClientConfirm:
    def test_apply_mode_returns_true_and_sets_applied(self):
        b = make_bugz(mode="apply")
        assert b._confirm() is True
        assert b.applied is True

    def test_preview_mode_returns_false_does_not_set_applied(self):
        b = make_bugz(mode="preview")
        assert b._confirm() is False
        assert b.applied is False

    def test_prompt_y_returns_true_and_sets_applied(self):
        b = make_bugz(mode="prompt")
        with patch("builtins.input", return_value="y"):
            assert b._confirm() is True
        assert b.applied is True

    def test_prompt_n_returns_false(self):
        b = make_bugz(mode="prompt")
        with patch("builtins.input", return_value="n"):
            assert b._confirm() is False
        assert b.applied is False

    def test_prompt_q_raises(self):
        b = make_bugz(mode="prompt")
        with patch("builtins.input", return_value="q"):
            with pytest.raises(SystemExit):
                b._confirm()

    def test_prompt_eof_returns_false(self):
        b = make_bugz(mode="prompt")
        with patch("builtins.input", side_effect=EOFError):
            assert b._confirm() is False

    def test_prompt_invalid_then_valid(self):
        b = make_bugz(mode="prompt")
        with patch("builtins.input", side_effect=["x", "?", "y"]):
            assert b._confirm() is True


# ---------------------------------------------------------------------------
# BugzillaClient.get_valid_keywords
# ---------------------------------------------------------------------------


class TestBugzillaClientKeywords:
    def test_fetches_keywords_from_api(self):
        b = make_bugz()
        b.client._backend.bug_fields.return_value = {
            "fields": [{"values": [{"name": "perf"}, {"name": "regression"}]}]
        }
        keywords = b.get_valid_keywords()
        assert "perf" in keywords
        assert "regression" in keywords

    def test_caches_result_on_second_call(self):
        b = make_bugz()
        b.client._backend.bug_fields.return_value = {
            "fields": [{"values": [{"name": "perf"}]}]
        }
        b.get_valid_keywords()
        b.get_valid_keywords()
        assert b.client._backend.bug_fields.call_count == 1


# ---------------------------------------------------------------------------
# BugzillaClient.update_type (reverse issue-type sync)
# ---------------------------------------------------------------------------


class TestBugzillaClientUpdateType:
    def _bug_jira(self):
        bug = {"id": 1, "url": "https://bugzil.la/1", "status": "NEW",
               "product": "P", "component": "C", "type": "task"}
        jira = {"key": "FXP-1", "url": "https://j/FXP-1", "issuetype": "Bug"}
        return bug, jira

    def test_sets_type_field_on_apply(self):
        b = make_bugz(mode="apply")
        b.client.build_update.return_value = {}
        bug, jira = self._bug_jira()
        b.update_type(bug, jira, "task", "defect")
        b.client.update_bugs.assert_called_once_with([1], {"type": "defect"})

    def test_no_write_in_preview(self):
        b = make_bugz(mode="preview")
        bug, jira = self._bug_jira()
        b.update_type(bug, jira, "task", "defect")
        b.client.update_bugs.assert_not_called()

    def test_returns_empty_on_exception(self):
        b = make_bugz()
        b.client._backend.bug_fields.side_effect = Exception("network error")
        keywords = b.get_valid_keywords()
        assert keywords == []

    def test_pre_cached_keywords_returned_directly(self):
        b = make_bugz()
        b._valid_keywords = ["perf", "regression"]
        keywords = b.get_valid_keywords()
        assert keywords == ["perf", "regression"]
        b.client._backend.bug_fields.assert_not_called()

    def test_handles_missing_fields_key(self):
        b = make_bugz()
        b.client._backend.bug_fields.return_value = {}
        keywords = b.get_valid_keywords()
        assert keywords == []


# ---------------------------------------------------------------------------
# BugzillaClient update methods
# ---------------------------------------------------------------------------


class TestBugzillaClientUpdates:
    def _make_infos(self):
        bug = {
            "id": 111,
            "url": "https://bugzil.la/111",
            "status": "NEW",
            "product": "Firefox",
            "component": "General",
        }
        jira = {
            "key": "FXP-1",
            "url": "https://mozilla-hub.atlassian.net/browse/FXP-1",
            "summary": "Test",
        }
        return bug, jira

    def test_update_summary_calls_api_in_apply_mode(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        b.update_summary(bug, jira, "Old summary", "New summary")
        b.client.build_update.assert_called_once_with(summary="New summary")
        b.client.update_bugs.assert_called_once()

    def test_update_summary_skipped_in_log_mode(self):
        b = make_bugz(mode="preview")
        bug, jira = self._make_infos()
        b.update_summary(bug, jira, "Old summary", "New summary")
        b.client.update_bugs.assert_not_called()

    def test_update_summary_records_update(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        b.update_summary(bug, jira, "Old", "New")
        assert len(b.updates) == 1
        assert b.updates[0]["bug_field"] == "summary"

    def test_update_priority_calls_api(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        b.update_priority(bug, jira, "P3", "P2")
        b.client.build_update.assert_called_once_with(priority="P2")
        b.client.update_bugs.assert_called_once()

    def test_update_estimated_time_calls_api(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        jira["timeoriginalestimate"] = 7200  # 2h in seconds
        b.update_estimated_time(bug, jira, 1.0, 2.0)
        b.client.build_update.assert_called_once_with(estimated_time=2.0)

    def test_update_keywords_merges_and_calls_api(self):
        b = make_bugz(mode="apply")
        bug = {
            "id": 111,
            "url": "https://bugzil.la/111",
            "status": "NEW",
            "product": "Firefox",
            "component": "General",
            "keywords": ["existing"],
        }
        jira = {
            "key": "FXP-1",
            "url": "https://mozilla-hub.atlassian.net/browse/FXP-1",
            "labels": ["new-label"],
        }
        b.update_keywords(bug, jira, ["new-label"])
        b.client.build_update.assert_called_once_with(
            keywords_set=sorted({"existing", "new-label"})
        )
        # Bug info mutated with merged keywords
        assert "new-label" in bug["keywords"]

    def test_update_whiteboard_calls_api(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        jira["labels"] = []
        b.update_whiteboard(bug, jira, "[fxp]", "[fxp][perf]")
        b.client.build_update.assert_called_once_with(whiteboard="[fxp][perf]")

    def test_update_severity_calls_api(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        b.update_severity(bug, jira, "High", "S3", "S2")
        b.client.build_update.assert_called_once_with(severity="S2")

    def test_make_update_appends_to_updates(self):
        b = make_bugz(mode="preview")
        bug = {
            "id": 111,
            "url": "https://bugzil.la/111",
            "status": "NEW",
            "product": "Firefox",
            "component": "General",
        }
        jira = {
            "key": "FXP-1",
            "url": "https://mozilla-hub.atlassian.net/browse/FXP-1",
        }
        b._make_update(bug, jira, "priority", "P3", "P2")
        assert len(b.updates) == 1
        record = b.updates[0]
        assert record["direction"] == "jira→bugzilla"
        assert record["bug_url"] == bug["url"]
        assert record["jira_url"] == jira["url"]
        assert record["bug_field"] == "priority"
        assert record["bug_before"] == "P3"
        assert record["bug_after"] == "P2"

    def test_applied_reset_between_uses(self):
        b = make_bugz(mode="apply")
        bug, jira = self._make_infos()
        b.update_priority(bug, jira, "P3", "P2")
        assert b.applied is True
        b.applied = False
        assert b.applied is False

    def test_prompt_mode_output_goes_to_stderr(self, capsys):
        b = make_bugz(mode="prompt")
        with patch("builtins.input", side_effect=EOFError):
            b._confirm()
        captured = capsys.readouterr()
        assert "EOF" in captured.err or captured.err != ""

    def test_update_estimated_time_records_jira_hours(self):
        b = make_bugz(mode="preview")
        bug = {
            "id": 111,
            "url": "https://bugzil.la/111",
            "status": "NEW",
            "product": "Firefox",
            "component": "General",
        }
        jira = {
            "key": "FXP-1",
            "url": "https://mozilla-hub.atlassian.net/browse/FXP-1",
            "timeoriginalestimate": 7200,
        }
        b.update_estimated_time(bug, jira, 1.0, 2.0)
        assert len(b.updates) == 1
        # jira_before/after should be hour strings
        assert "h" in b.updates[0]["bug_before"]
        assert "h" in b.updates[0]["bug_after"]
