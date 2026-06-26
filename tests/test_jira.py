"""Tests for jbix/jira.py — JiraClient."""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_jira

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_infos():
    bug = {
        "id": 123456,
        "url": "https://bugzil.la/123456",
        "status": "NEW",
        "product": "Firefox",
        "component": "General",
        "summary": "Test bug",
        "severity": "S3",
    }
    jira = {
        "key": "FXP-100",
        "url": "https://mozilla-hub.atlassian.net/browse/FXP-100",
        "status": "In Progress",
        "summary": "Test bug",
    }
    return bug, jira


# ---------------------------------------------------------------------------
# _confirm
# ---------------------------------------------------------------------------


class TestJiraClientConfirm:
    def test_apply_mode_returns_true_sets_applied(self):
        j = make_jira(mode="apply")
        assert j._confirm() is True
        assert j.applied is True

    def test_preview_mode_returns_false_no_applied(self):
        j = make_jira(mode="preview")
        assert j._confirm() is False
        assert j.applied is False

    def test_prompt_y_returns_true_sets_applied(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", return_value="y"):
            assert j._confirm() is True
        assert j.applied is True

    def test_prompt_n_returns_false(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", return_value="n"):
            assert j._confirm() is False
        assert j.applied is False

    def test_prompt_q_raises_system_exit(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", return_value="q"):
            with pytest.raises(SystemExit):
                j._confirm()

    def test_prompt_eof_returns_q_then_raises(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", side_effect=EOFError):
            # _ask returns "q" on EOFError, _confirm raises SystemExit
            with pytest.raises(SystemExit):
                j._confirm()

    def test_prompt_invalid_then_valid(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", side_effect=["x", "?", "n"]):
            assert j._confirm() is False


# ---------------------------------------------------------------------------
# _ask
# ---------------------------------------------------------------------------


class TestJiraClientAsk:
    def test_valid_answer_returned(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", return_value="y"):
            assert j._ask("Confirm?") == "y"

    def test_eof_returns_q(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", side_effect=EOFError):
            assert j._ask("Confirm?") == "q"

    def test_custom_valid_chars(self):
        j = make_jira(mode="prompt")
        with patch("builtins.input", return_value="a"):
            assert j._ask("Pick", valid="abc") == "a"


# ---------------------------------------------------------------------------
# update methods
# ---------------------------------------------------------------------------


class TestJiraClientUpdates:
    def test_update_summary_calls_issue_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_summary(bug, jira, "Old", "New summary")
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"summary": "New summary"}, notify=False
        )

    def test_update_summary_skipped_in_log(self):
        j = make_jira(mode="preview")
        bug, jira = _make_infos()
        j.update_summary(bug, jira, "Old", "New")
        j.client.issue.return_value.update.assert_not_called()

    def test_update_summary_records_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_summary(bug, jira, "Old", "New")
        assert len(j.updates) == 1
        assert j.updates[0]["jira_field"] == "summary"

    def test_update_priority_calls_issue_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_priority(bug, jira, "P3", "P2")
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"priority": {"name": "P2"}}, notify=False
        )

    def test_update_resolution_wraps_name(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_resolution(bug, jira, None, "Done")
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"resolution": {"name": "Done"}}, notify=False
        )

    def test_update_severity_with_value(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_severity(bug, jira, "S3", "S2")
        from jbix.constants import JIRA_SEVERITY_FIELD

        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={JIRA_SEVERITY_FIELD: {"value": "S2"}}, notify=False
        )

    def test_update_severity_clear_passes_none(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_severity(bug, jira, "S3", None)
        from jbix.constants import JIRA_SEVERITY_FIELD

        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={JIRA_SEVERITY_FIELD: None}, notify=False
        )

    def test_update_original_estimate(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update_original_estimate(bug, jira, 1.0, 4.0)
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"timetracking": {"originalEstimate": "4.0h"}}, notify=False
        )

    def test_update_original_estimate_skipped_in_log(self):
        j = make_jira(mode="preview")
        bug, jira = _make_infos()
        j.update_original_estimate(bug, jira, 1.0, 4.0)
        j.client.issue.assert_not_called()

    def test_update_assignee_calls_issue_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        new_user = MagicMock()
        new_user.displayName = "Bob"
        new_user.accountId = "acc-123"
        j.update_assignee(bug, jira, "Alice", new_user)
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"assignee": {"id": "acc-123"}}, notify=False
        )

    def test_update_components_calls_issue_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        comp = MagicMock()
        comp.name = "Firefox::General"
        j.update_components(bug, jira, [], [comp])
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"components": [{"name": "Firefox::General"}]}, notify=False
        )

    def test_add_labels_calls_issue_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.add_labels(bug, jira, ["bugzilla"], ["perf"])
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"labels": ["bugzilla", "perf"]}, notify=False
        )

    def test_add_remote_link_calls_add_remote_link(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        url = "https://bugzil.la/123456"
        j.add_remote_link(bug, jira, url)
        j.client.add_remote_link.assert_called_once_with(
            jira["key"], {"url": url, "title": url}
        )

    def test_add_remote_link_skipped_in_log(self):
        j = make_jira(mode="preview")
        bug, jira = _make_infos()
        j.add_remote_link(bug, jira, "https://bugzil.la/1")
        j.client.add_remote_link.assert_not_called()

    def test_create_issue_link_calls_api(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.create_issue_link(bug, jira, "Blocks", "FXP-200", jira["key"])
        j.client.create_issue_link.assert_called_once_with(
            "Blocks", inwardIssue="FXP-200", outwardIssue=jira["key"]
        )

    def test_delete_issue_link_calls_api(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.delete_issue_link(bug, jira, "link-id-99", "some link")
        j.client.delete_issue_link.assert_called_once_with("link-id-99")

    def test_delete_issue_link_deduplicates_same_link(self):
        # The same link reached from both sides is only deleted/recorded once.
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.delete_issue_link(bug, jira, "link-id-99", "stale from A side")
        j.delete_issue_link(bug, jira, "link-id-99", "stale from B side")
        j.client.delete_issue_link.assert_called_once_with("link-id-99")
        assert len(j.updates) == 1

    def test_delete_issue_link_dedup_in_preview_mode(self):
        # In preview nothing is deleted, but a duplicate is still suppressed.
        j = make_jira(mode="preview")
        bug, jira = _make_infos()
        j.delete_issue_link(bug, jira, "link-1", "stale from A side")
        j.delete_issue_link(bug, jira, "link-1", "stale from B side")
        j.client.delete_issue_link.assert_not_called()
        assert len(j.updates) == 1

    def test_search_users_delegates_to_client(self):
        j = make_jira()
        j.client.search_users.return_value = [MagicMock()]
        result = j.search_users("alice@example.com")
        j.client.search_users.assert_called_once_with(query="alice@example.com")
        assert len(result) == 1

    def test_generic_update_calls_issue_update(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        j.update("duedate", bug, jira, None, "2025-12-31")
        j.client.issue(jira["key"]).update.assert_called_once_with(
            fields={"duedate": "2025-12-31"}, notify=False
        )


# ---------------------------------------------------------------------------
# transition_issue
# ---------------------------------------------------------------------------


class TestJiraClientTransition:
    def test_transitions_to_matching_status(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()

        mock_issue = MagicMock()
        j.client.issue.return_value = mock_issue
        j.client.transitions.return_value = [
            {"id": "31", "to": {"name": "Done"}},
            {"id": "41", "to": {"name": "In Progress"}},
        ]

        j.transition_issue(bug, jira, "In Progress", "Done")
        j.client.transition_issue.assert_called_once_with(
            mock_issue, transition="31", fields={}
        )

    def test_transition_with_resolution(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()

        mock_issue = MagicMock()
        j.client.issue.return_value = mock_issue
        j.client.transitions.return_value = [
            {"id": "31", "to": {"name": "Done"}},
        ]

        j.transition_issue(bug, jira, "In Progress", "Done", resolution="Fixed")
        j.client.transition_issue.assert_called_once_with(
            mock_issue, transition="31", fields={"resolution": {"name": "Fixed"}}
        )

    def test_transition_with_resolution_records_combined_format(self):
        j = make_jira(mode="preview")
        bug, jira = _make_infos()
        jira["resolution"] = None

        j.transition_issue(bug, jira, "In Progress", "Done", resolution="Fixed")
        assert len(j.updates) == 1
        rec = j.updates[0]
        assert rec["jira_field"] == "status (resolution)"
        assert rec["jira_before"] == "In Progress (none)"
        assert rec["jira_after"] == "Done (Fixed)"

    def test_transition_without_resolution_records_plain_status(self):
        j = make_jira(mode="preview")
        bug, jira = _make_infos()

        j.transition_issue(bug, jira, "Open", "In Progress")
        assert len(j.updates) == 1
        rec = j.updates[0]
        assert rec["jira_field"] == "status"
        assert rec["jira_before"] == "Open"
        assert rec["jira_after"] == "In Progress"

    def test_transition_resolution_fallback_on_400(self):
        from jira.exceptions import JIRAError

        j = make_jira(mode="apply")
        bug, jira = _make_infos()

        mock_issue = MagicMock()
        j.client.issue.return_value = mock_issue
        j.client.transitions.return_value = [{"id": "31", "to": {"name": "Done"}}]

        err = JIRAError(status_code=400, text="Field 'resolution' cannot be set. It is not on the appropriate screen, or unknown.")
        j.client.transition_issue.side_effect = [err, None]

        j.transition_issue(bug, jira, "In Progress", "Done", resolution="Fixed")

        assert j.client.transition_issue.call_count == 2
        j.client.transition_issue.assert_called_with(mock_issue, transition="31", fields={})
        mock_issue.update.assert_called_once_with(
            fields={"resolution": {"name": "Fixed"}}, notify=False
        )

    def test_transition_reraises_non_resolution_400(self):
        from jira.exceptions import JIRAError

        j = make_jira(mode="apply")
        bug, jira = _make_infos()

        mock_issue = MagicMock()
        j.client.issue.return_value = mock_issue
        j.client.transitions.return_value = [{"id": "31", "to": {"name": "Done"}}]

        err = JIRAError(status_code=400, text="Some other error")
        j.client.transition_issue.side_effect = err

        with pytest.raises(JIRAError):
            j.transition_issue(bug, jira, "In Progress", "Done", resolution="Fixed")

    def test_no_matching_transition_skips(self):
        j = make_jira(mode="apply")
        bug, jira = _make_infos()

        j.client.transitions.return_value = [
            {"id": "31", "to": {"name": "Done"}},
        ]

        j.transition_issue(bug, jira, "In Progress", "Nonexistent Status")
        j.client.transition_issue.assert_not_called()

    def test_transition_skipped_in_log_mode(self):
        j = make_jira(mode="preview")
        bug, jira = _make_infos()

        j.client.transitions.return_value = [
            {"id": "31", "to": {"name": "Done"}},
        ]

        j.transition_issue(bug, jira, "In Progress", "Done")
        j.client.transition_issue.assert_not_called()


# ---------------------------------------------------------------------------
# create_component
# ---------------------------------------------------------------------------


class TestJiraClientCreateComponent:
    def test_creates_new_component(self):
        j = make_jira(mode="apply")
        mock_comp = MagicMock()
        j.client.create_component.return_value = mock_comp

        result = j.create_component("FXP", "Firefox::General")
        assert result == mock_comp
        j.client.create_component.assert_called_once_with(
            name="Firefox::General", project="FXP", description=""
        )

    def test_returns_existing_component_on_conflict(self):
        j = make_jira(mode="apply")
        j.client.create_component.side_effect = Exception("already exists")

        existing_comp = MagicMock()
        existing_comp.name = "Firefox::General"
        j.client.project_components.return_value = [existing_comp]

        result = j.create_component("FXP", "Firefox::General")
        assert result == existing_comp

    def test_skipped_in_log_mode(self):
        j = make_jira(mode="preview")
        result = j.create_component("FXP", "Firefox::General")
        j.client.create_component.assert_not_called()
        # Returns a stub with .name so sync_components can still call update_components
        assert result.name == "Firefox::General"


# ---------------------------------------------------------------------------
# _make_update records
# ---------------------------------------------------------------------------


class TestJiraClientMakeUpdate:
    def test_make_update_appends_record(self):
        j = make_jira(mode="preview")
        bug, jira_dict = _make_infos()
        j._make_update(bug, jira_dict, "priority", "P3", "P2")
        assert len(j.updates) == 1
        rec = j.updates[0]
        assert rec["direction"] == "bugzilla→jira"
        assert rec["jira_field"] == "priority"
        assert rec["jira_before"] == "P3"
        assert rec["jira_after"] == "P2"
        assert rec["bug_url"] == bug["url"]
        assert rec["jira_url"] == jira_dict["url"]

    def test_make_update_with_bug_field(self):
        j = make_jira(mode="preview")
        bug, jira_dict = _make_infos()
        j._make_update(bug, jira_dict, "status", "Open", "Done", "status", "NEW")
        rec = j.updates[0]
        assert rec["bug_field"] == "status"
        assert rec["bug_after"] == "NEW"

    def test_make_update_same_before_and_after(self):
        """When before == after, _make_update still records the update."""
        j = make_jira(mode="preview")
        bug, jira_dict = _make_infos()
        j._make_update(bug, jira_dict, "priority", "P2", "P2")
        assert len(j.updates) == 1
        rec = j.updates[0]
        assert rec["jira_before"] == "P2"
        assert rec["jira_after"] == "P2"


# ---------------------------------------------------------------------------
# create_component edge cases
# ---------------------------------------------------------------------------


class TestJiraClientCreateComponentEdgeCases:
    def test_conflict_but_component_not_found_returns_none(self):
        """Conflict exception but component name not in project_components → None."""
        j = make_jira(mode="apply")
        j.client.create_component.side_effect = Exception("already exists")

        # project_components returns a component with a DIFFERENT name
        other_comp = MagicMock()
        other_comp.name = "Firefox::Other"
        j.client.project_components.return_value = [other_comp]

        result = j.create_component("FXP", "Firefox::General")
        assert result is None

    def test_non_conflict_exception_reraised(self):
        """Exception that is not an 'already exists' conflict is re-raised."""
        j = make_jira(mode="apply")
        j.client.create_component.side_effect = Exception("network timeout")

        with pytest.raises(Exception, match="network timeout"):
            j.create_component("FXP", "Firefox::General")


# ---------------------------------------------------------------------------
# create_issue_link change descriptions
# ---------------------------------------------------------------------------


class TestJiraClientIssueLinkDescriptions:
    def test_duplicate_outward_change_description(self):
        """link_type=Duplicate and jira.key == outward_issue → 'duplicated by ...' message."""
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        # outward_issue is the current issue key → describes "this issue is a duplicate of inward"
        j.create_issue_link(bug, jira, "Duplicate", "FXP-999", jira["key"])
        j.client.create_issue_link.assert_called_once_with(
            "Duplicate", inwardIssue="FXP-999", outwardIssue=jira["key"]
        )

    def test_blocks_inward_change_description(self):
        """link_type=Blocks and jira.key == inward_issue → 'blocks ...' message."""
        j = make_jira(mode="apply")
        bug, jira = _make_infos()
        # inward_issue is the current issue key → the current issue is blocked by outward
        j.create_issue_link(bug, jira, "Blocks", jira["key"], "FXP-200")
        j.client.create_issue_link.assert_called_once_with(
            "Blocks", inwardIssue=jira["key"], outwardIssue="FXP-200"
        )
