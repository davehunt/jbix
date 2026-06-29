"""Tests for jbix/sync.py — all forward and reverse sync functions."""

from unittest.mock import MagicMock

import pytest

from jbix.sync import (
    _whiteboard_as_labels,
    reverse_sync_assignee,
    reverse_sync_issue_type,
    reverse_sync_priority,
    reverse_sync_severity,
    reverse_sync_summary,
    reverse_sync_whiteboard_labels,
    sync_assignee,
    sync_blocks,
    sync_components,
    sync_dependencies,
    sync_depends_on,
    sync_duplicates,
    sync_issue_type,
    sync_keyword_labels,
    sync_priority,
    sync_regressions,
    sync_remote_links,
    sync_resolution,
    sync_see_also,
    sync_severity,
    sync_status,
    sync_summary,
    sync_time_tracking,
    sync_whiteboard_labels,
)

PRIORITY_MAP = {
    "--": "None",
    "P1": "P1",
    "P2": "P2",
    "P3": "P3",
    "P4": "P4",
    "P5": "P5",
}

SEVERITY_MAP = {
    "S1": "S1",
    "S2": "S2",
    "S3": "S3",
    "S4": "S4",
    "--": None,
    "": None,
}

STATUS_MAP = {
    "NEW": "Open",
    "ASSIGNED": "In Progress",
    "RESOLVED": "Done",
}

RESOLUTION_MAP = {
    "FIXED": "Fixed",
    "WONTFIX": "Won't Fix",
}


def _make_jira():
    """Return a MagicMock Jira client with a pre-initialised users dict."""
    j = MagicMock()
    j.users = {}
    return j


def _make_bugz():
    """Return a MagicMock Bugzilla client."""
    return MagicMock()


# ---------------------------------------------------------------------------
# sync_assignee
# ---------------------------------------------------------------------------


class TestSyncAssignee:
    @pytest.fixture(autouse=True)
    def _controlled_assignee_map(self, monkeypatch):
        # Use a synthetic map so tests don't depend on the gitignored assignee_map.yaml.
        monkeypatch.setattr("jbix.sync.ASSIGNEE_MAP", {
            "contributor@example.com": "contributor@example.org",
            "no-jira-account@example.com": None,
        })

    def test_nobody_skipped_when_jira_already_unassigned(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "nobody@mozilla.org"
        jira_info["assignee"] = None
        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.update_assignee.assert_not_called()

    def test_nobody_clears_jira_assignee(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "nobody@mozilla.org"
        # jira_info["assignee"] is a non-None mock by default
        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.update_assignee.assert_called_once_with(bug_info, jira_info, jira_info["assignee"], None)

    def test_mapped_to_none_in_assignee_map_skipped(self, bug_info, jira_info):
        """Email that maps to None in ASSIGNEE_MAP → no Jira account → skip."""
        j = _make_jira()
        bug_info["assignee"] = "no-jira-account@example.com"
        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.search_users.assert_not_called()
        j.update_assignee.assert_not_called()

    def test_updates_when_user_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "contributor@example.com"  # maps to contributor@example.org
        mock_user = MagicMock()
        mock_user.displayName = "Alex Contributor"
        j.search_users.return_value = [mock_user]
        jira_info["assignee"] = MagicMock()
        jira_info["assignee"].displayName = "Someone Else"

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.update_assignee.assert_called_once()

    def test_no_update_when_displayname_matches(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "contributor@example.com"
        mock_user = MagicMock()
        mock_user.displayName = "Alice Example"
        j.search_users.return_value = [mock_user]
        # str(jira_info["assignee"]) must equal str(mock_user.displayName) = "Alice Example"
        jira_info["assignee"] = "Alice Example"

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.update_assignee.assert_not_called()

    def test_user_not_found_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "newuser@example.com"
        j.search_users.return_value = []

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.update_assignee.assert_not_called()

    def test_multiple_users_found_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "ambiguous@example.com"
        j.search_users.return_value = [MagicMock(), MagicMock()]

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.update_assignee.assert_not_called()

    def test_cached_user_no_search(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "contributor@example.com"
        mock_user = MagicMock()
        mock_user.displayName = "Someone Else"
        j.users["contributor@example.org"] = mock_user  # pre-populate cache

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.search_users.assert_not_called()
        j.update_assignee.assert_called_once()

    def test_cached_none_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "contributor@example.com"
        j.users["contributor@example.org"] = None  # cached as no Jira account

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.search_users.assert_not_called()
        j.update_assignee.assert_not_called()

    def test_unmapped_email_used_directly(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["assignee"] = "direct@example.com"  # not in ASSIGNEE_MAP
        mock_user = MagicMock()
        mock_user.displayName = "New Person"
        j.search_users.return_value = [mock_user]

        sync_assignee(bug_info, jira_info, _make_bugz(), j)
        j.search_users.assert_called_once_with("direct@example.com")

    def test_uses_people_lookup_for_unmapped_email(self, bug_info, jira_info):
        """Email not in ASSIGNEE_MAP: PeopleClient.lookup() provides the work email."""
        j = _make_jira()
        bug_info["assignee"] = "personal@gmail.com"  # not in ASSIGNEE_MAP
        mock_user = MagicMock()
        mock_user.displayName = "Some Person"
        j.search_users.return_value = [mock_user]

        people = MagicMock()
        people.lookup.return_value = "person@example.org"

        sync_assignee(bug_info, jira_info, _make_bugz(), j, people=people)
        people.lookup.assert_called_once_with("personal@gmail.com")
        j.search_users.assert_called_once_with("person@example.org")

    def test_people_not_called_when_email_in_assignee_map(self, bug_info, jira_info):
        """Email present in ASSIGNEE_MAP: PeopleClient.lookup() must not be called."""
        j = _make_jira()
        bug_info["assignee"] = "contributor@example.com"  # in ASSIGNEE_MAP → contributor@example.org
        mock_user = MagicMock()
        mock_user.displayName = "Alex"
        j.search_users.return_value = [mock_user]

        people = MagicMock()
        sync_assignee(bug_info, jira_info, _make_bugz(), j, people=people)
        people.lookup.assert_not_called()
        j.search_users.assert_called_once_with("contributor@example.org")

    def test_people_lookup_returns_none_falls_back_to_bug_value(self, bug_info, jira_info):
        """If PeopleClient.lookup() returns None, fall back to the raw Bugzilla email."""
        j = _make_jira()
        bug_info["assignee"] = "unknown@gmail.com"  # not in ASSIGNEE_MAP
        mock_user = MagicMock()
        mock_user.displayName = "Unknown"
        j.search_users.return_value = [mock_user]

        people = MagicMock()
        people.lookup.return_value = None

        sync_assignee(bug_info, jira_info, _make_bugz(), j, people=people)
        j.search_users.assert_called_once_with("unknown@gmail.com")

    def test_people_is_none_uses_bug_value_directly(self, bug_info, jira_info):
        """With people=None, unmapped emails are used directly (current behaviour)."""
        j = _make_jira()
        bug_info["assignee"] = "direct@example.com"
        mock_user = MagicMock()
        mock_user.displayName = "Direct"
        j.search_users.return_value = [mock_user]

        sync_assignee(bug_info, jira_info, _make_bugz(), j, people=None)
        j.search_users.assert_called_once_with("direct@example.com")


# ---------------------------------------------------------------------------
# sync_components
# ---------------------------------------------------------------------------


def _jc(**overrides):
    """Return a jira_components dict with JBI defaults, optionally overridden."""
    base = {
        "use_bug_component": True,
        "use_bug_product": False,
        "use_bug_component_with_product_prefix": False,
        "set_custom_components": [],
        "create_components": False,
    }
    base.update(overrides)
    return base


class TestSyncComponents:
    def test_no_update_when_component_matches(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "Profiler"
        comp = MagicMock()
        comp.name = "Firefox::Profiler"
        jira_info["components"] = [comp]
        project_components = [comp]
        jc = _jc(use_bug_component=False, use_bug_component_with_product_prefix=True)

        sync_components("FXP", project_components, bug_info, jira_info, j, jc)
        j.update_components.assert_not_called()

    def test_updates_with_existing_project_component(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "Profiler"
        jira_info["components"] = []
        existing = MagicMock()
        existing.name = "Firefox::Profiler"
        project_components = [existing]
        jc = _jc(use_bug_component=False, use_bug_component_with_product_prefix=True)

        sync_components("FXP", project_components, bug_info, jira_info, j, jc)
        j.update_components.assert_called_once()

    def test_creates_component_when_not_in_project(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "NewComp"
        jira_info["components"] = []
        new_comp = MagicMock()
        new_comp.name = "Firefox::NewComp"
        j.create_component.return_value = new_comp
        jc = _jc(
            use_bug_component=False,
            use_bug_component_with_product_prefix=True,
            create_components=True,
        )

        sync_components("FXP", [], bug_info, jira_info, j, jc)
        j.create_component.assert_called_once_with("FXP", "Firefox::NewComp")
        j.update_components.assert_called_once()

    def test_skips_update_if_create_component_returns_none(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "NewComp"
        jira_info["components"] = []
        j.create_component.return_value = None
        jc = _jc(
            use_bug_component=False,
            use_bug_component_with_product_prefix=True,
            create_components=True,
        )

        sync_components("FXP", [], bug_info, jira_info, j, jc)
        j.update_components.assert_not_called()

    def test_use_bug_component_default_uses_bare_name(self, bug_info, jira_info):
        """Default config (use_bug_component=True) → candidate is bare component name."""
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "Profiler"
        jira_info["components"] = []
        existing = MagicMock()
        existing.name = "Profiler"  # bare name, not "Firefox::Profiler"
        jc = _jc()  # use_bug_component=True is default

        sync_components("FXP", [existing], bug_info, jira_info, j, jc)
        j.update_components.assert_called_once()

    def test_use_bug_product(self, bug_info, jira_info):
        """use_bug_product=True → candidate is the product name."""
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "Profiler"
        jira_info["components"] = []
        prod_comp = MagicMock()
        prod_comp.name = "Firefox"
        jc = _jc(use_bug_component=False, use_bug_product=True)

        sync_components("FXP", [prod_comp], bug_info, jira_info, j, jc)
        j.update_components.assert_called_once()

    def test_set_custom_components(self, bug_info, jira_info):
        """set_custom_components assigns a fixed component regardless of bug fields."""
        j = _make_jira()
        jira_info["components"] = []
        custom = MagicMock()
        custom.name = "My Team"
        jc = _jc(use_bug_component=False, set_custom_components=["My Team"])

        sync_components("FXP", [custom], bug_info, jira_info, j, jc)
        j.update_components.assert_called_once()

    def test_empty_candidates_noop(self, bug_info, jira_info):
        """All flags False and no custom components → nothing to sync."""
        j = _make_jira()
        jc = _jc(use_bug_component=False)  # all derivation flags off, no custom

        sync_components("FXP", [], bug_info, jira_info, j, jc)
        j.update_components.assert_not_called()

    def test_no_create_when_flag_false(self, bug_info, jira_info):
        """Missing component + create_components=False → create_component not called."""
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "NewComp"
        jira_info["components"] = []
        jc = _jc(
            use_bug_component=False,
            use_bug_component_with_product_prefix=True,
            create_components=False,
        )

        sync_components("FXP", [], bug_info, jira_info, j, jc)
        j.create_component.assert_not_called()
        j.update_components.assert_not_called()

    def test_prefix_mode_component_only_when_no_product(self, bug_info, jira_info):
        """use_bug_component_with_product_prefix with no product → bare component name."""
        j = _make_jira()
        bug_info["product"] = ""
        bug_info["component"] = "Profiler"
        jira_info["components"] = []
        comp = MagicMock()
        comp.name = "Profiler"
        jc = _jc(use_bug_component=False, use_bug_component_with_product_prefix=True)

        sync_components("FXP", [comp], bug_info, jira_info, j, jc)
        j.update_components.assert_called_once()

    def test_multiple_candidates_one_missing(self, bug_info, jira_info):
        """When two candidates are built but one already on issue, only missing one added."""
        j = _make_jira()
        bug_info["product"] = "Firefox"
        bug_info["component"] = "Profiler"
        already = MagicMock()
        already.name = "Profiler"
        jira_info["components"] = [already]  # bare "Profiler" already present
        prefix_comp = MagicMock()
        prefix_comp.name = "Firefox::Profiler"
        # both use_bug_component and use_bug_component_with_product_prefix
        jc = _jc(use_bug_component=True, use_bug_component_with_product_prefix=True)

        sync_components("FXP", [prefix_comp], bug_info, jira_info, j, jc)
        # "Profiler" already on issue, only "Firefox::Profiler" is missing
        j.update_components.assert_called_once()
        new_comps = j.update_components.call_args[0][3]
        assert prefix_comp in new_comps
        assert already not in new_comps


# ---------------------------------------------------------------------------
# sync_summary
# ---------------------------------------------------------------------------


class TestSyncSummary:
    def test_updates_when_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["summary"] = "New summary"
        jira_info["summary"] = "Old summary"
        sync_summary(bug_info, jira_info, j)
        j.update_summary.assert_called_once_with(
            bug_info, jira_info, "Old summary", "New summary"
        )

    def test_no_update_when_same(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["summary"] = "Same"
        jira_info["summary"] = "Same"
        sync_summary(bug_info, jira_info, j)
        j.update_summary.assert_not_called()


# ---------------------------------------------------------------------------
# sync_priority
# ---------------------------------------------------------------------------


class TestSyncPriority:
    def test_updates_when_mapped_and_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["priority"] = "P1"
        jira_info["priority"] = "P3"
        sync_priority(bug_info, jira_info, j, PRIORITY_MAP)
        j.update_priority.assert_called_once_with(bug_info, jira_info, "P3", "P1")

    def test_no_update_when_already_correct(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["priority"] = "P2"
        jira_info["priority"] = "P2"
        sync_priority(bug_info, jira_info, j, PRIORITY_MAP)
        j.update_priority.assert_not_called()

    def test_skips_when_priority_not_in_map(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["priority"] = "UNKNOWN"
        sync_priority(bug_info, jira_info, j, PRIORITY_MAP)
        j.update_priority.assert_not_called()

    def test_maps_double_dash_to_none_string(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["priority"] = "--"
        jira_info["priority"] = "P2"
        sync_priority(bug_info, jira_info, j, PRIORITY_MAP)
        j.update_priority.assert_called_once_with(bug_info, jira_info, "P2", "None")


# ---------------------------------------------------------------------------
# sync_severity
# ---------------------------------------------------------------------------


class TestSyncSeverity:
    def test_updates_when_mapped_and_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["severity"] = "S1"
        jira_sev = MagicMock()
        jira_sev.value = "S3"
        jira_info["severity"] = jira_sev
        sync_severity(bug_info, jira_info, j, SEVERITY_MAP)
        j.update_severity.assert_called_once_with(bug_info, jira_info, "S3", "S1")

    def test_no_update_when_same(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["severity"] = "S2"
        jira_sev = MagicMock()
        jira_sev.value = "S2"
        jira_info["severity"] = jira_sev
        sync_severity(bug_info, jira_info, j, SEVERITY_MAP)
        j.update_severity.assert_not_called()

    def test_skips_when_not_in_map(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["severity"] = "UNKNOWN"
        sync_severity(bug_info, jira_info, j, SEVERITY_MAP)
        j.update_severity.assert_not_called()

    def test_maps_blank_to_none(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["severity"] = "--"
        jira_sev = MagicMock()
        jira_sev.value = "S3"
        jira_info["severity"] = jira_sev
        sync_severity(bug_info, jira_info, j, SEVERITY_MAP)
        j.update_severity.assert_called_once_with(bug_info, jira_info, "S3", None)

    def test_handles_none_jira_severity(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["severity"] = "S2"
        jira_info["severity"] = None
        sync_severity(bug_info, jira_info, j, SEVERITY_MAP)
        j.update_severity.assert_called_once_with(bug_info, jira_info, None, "S2")


# ---------------------------------------------------------------------------
# sync_status
# ---------------------------------------------------------------------------


class TestSyncStatus:
    def test_skips_when_map_empty(self, bug_info, jira_info):
        j = _make_jira()
        sync_status(bug_info, jira_info, j, {})
        j.transition_issue.assert_not_called()

    def test_transitions_when_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "ASSIGNED"
        jira_info["status"] = "Open"
        sync_status(bug_info, jira_info, j, STATUS_MAP)
        j.transition_issue.assert_called_once_with(
            bug_info, jira_info, "Open", "In Progress", resolution=None
        )

    def test_transitions_with_resolution_when_both_provided(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "RESOLVED"
        bug_info["resolution"] = "FIXED"
        jira_info["status"] = "Open"
        jira_info["resolution"] = None
        sync_status(bug_info, jira_info, j, STATUS_MAP, resolution_map=RESOLUTION_MAP)
        j.transition_issue.assert_called_once_with(
            bug_info, jira_info, "Open", "Done", resolution="Fixed"
        )
        assert jira_info["resolution"] == "Fixed"

    def test_transitions_without_resolution_when_not_resolved(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "ASSIGNED"
        jira_info["status"] = "Open"
        sync_status(bug_info, jira_info, j, STATUS_MAP, resolution_map=RESOLUTION_MAP)
        j.transition_issue.assert_called_once_with(
            bug_info, jira_info, "Open", "In Progress", resolution=None
        )
        assert jira_info.get("resolution") is None

    def test_no_transition_when_already_correct(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "NEW"
        jira_info["status"] = "Open"
        sync_status(bug_info, jira_info, j, STATUS_MAP)
        j.transition_issue.assert_not_called()

    def test_skips_unmapped_bug_status(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "UNKNOWN_STATUS"
        sync_status(bug_info, jira_info, j, STATUS_MAP)
        j.transition_issue.assert_not_called()


# ---------------------------------------------------------------------------
# sync_resolution
# ---------------------------------------------------------------------------


class TestSyncResolution:
    def test_skips_when_map_empty(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "RESOLVED"
        sync_resolution(bug_info, jira_info, j, {})
        j.update_resolution.assert_not_called()

    def test_skips_when_not_resolved(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "NEW"
        bug_info["resolution"] = "FIXED"
        sync_resolution(bug_info, jira_info, j, RESOLUTION_MAP)
        j.update_resolution.assert_not_called()

    def test_updates_when_resolved_and_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "RESOLVED"
        bug_info["resolution"] = "FIXED"
        jira_info["resolution"] = None
        sync_resolution(bug_info, jira_info, j, RESOLUTION_MAP)
        j.update_resolution.assert_called_once_with(bug_info, jira_info, None, "Fixed")

    def test_no_update_when_already_matches(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "RESOLVED"
        bug_info["resolution"] = "FIXED"
        jira_info["resolution"] = "Fixed"
        sync_resolution(bug_info, jira_info, j, RESOLUTION_MAP)
        j.update_resolution.assert_not_called()

    def test_skips_unmapped_resolution(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["status"] = "RESOLVED"
        bug_info["resolution"] = "UNKNOWN"
        sync_resolution(bug_info, jira_info, j, RESOLUTION_MAP)
        j.update_resolution.assert_not_called()


# ---------------------------------------------------------------------------
# _whiteboard_as_labels helper
# ---------------------------------------------------------------------------


class TestWhiteboardAsLabels:
    def test_no_mode_returns_bare_labels(self):
        assert _whiteboard_as_labels("no", "[fxp]") == ["bugzilla", "fxp"]

    def test_yes_mode_returns_bracketed_labels(self):
        assert _whiteboard_as_labels("yes", "[fxp]") == ["bugzilla", "[fxp]"]

    def test_both_mode_returns_both_forms(self):
        assert _whiteboard_as_labels("both", "[fxp]") == ["bugzilla", "fxp", "[fxp]"]

    def test_space_converted_to_dot_no_mode(self):
        assert _whiteboard_as_labels("no", "[perf issue]") == ["bugzilla", "perf.issue"]

    def test_space_converted_to_dot_both_mode(self):
        assert _whiteboard_as_labels("both", "[perf issue]") == [
            "bugzilla",
            "perf.issue",
            "[perf.issue]",
        ]

    def test_multiple_tags_no_mode(self):
        assert _whiteboard_as_labels("no", "[fxp][perf]") == ["bugzilla", "fxp", "perf"]

    def test_multiple_tags_both_mode(self):
        assert _whiteboard_as_labels("both", "[fxp][perf]") == [
            "bugzilla",
            "fxp",
            "perf",
            "[fxp]",
            "[perf]",
        ]

    def test_empty_whiteboard_returns_bugzilla_only(self):
        assert _whiteboard_as_labels("no", "") == ["bugzilla"]

    def test_none_whiteboard_returns_bugzilla_only(self):
        assert _whiteboard_as_labels("no", None) == ["bugzilla"]


# ---------------------------------------------------------------------------
# sync_whiteboard_labels
# ---------------------------------------------------------------------------


class TestSyncWhiteboardLabels:
    def test_adds_label_from_whiteboard(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp][perf]"
        jira_info["labels"] = ["bugzilla"]
        sync_whiteboard_labels(bug_info, jira_info, j)
        j.add_labels.assert_called_once()
        args = j.add_labels.call_args[0]
        new_labels = args[3]
        assert "fxp" in new_labels
        assert "perf" in new_labels

    def test_no_update_when_all_present(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = ["fxp", "bugzilla"]
        sync_whiteboard_labels(bug_info, jira_info, j)
        j.add_labels.assert_not_called()

    def test_spaces_converted_to_dots(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[perf issue]"
        jira_info["labels"] = []
        sync_whiteboard_labels(bug_info, jira_info, j)
        j.add_labels.assert_called_once()
        new_labels = j.add_labels.call_args[0][3]
        assert "perf.issue" in new_labels

    def test_empty_whiteboard_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = ""
        sync_whiteboard_labels(bug_info, jira_info, j)
        j.add_labels.assert_not_called()

    def test_none_whiteboard_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = None
        sync_whiteboard_labels(bug_info, jira_info, j)
        j.add_labels.assert_not_called()

    @pytest.mark.parametrize(
        "whiteboard,expected",
        [
            ("[fxp]", ["fxp"]),
            ("[fxp][perf]", ["fxp", "perf"]),
            ("[fxp-special]", ["fxp-special"]),
        ],
    )
    def test_various_whiteboard_formats(self, bug_info, jira_info, whiteboard, expected):
        j = _make_jira()
        bug_info["whiteboard"] = whiteboard
        jira_info["labels"] = ["bugzilla"]  # bugzilla marker already present
        sync_whiteboard_labels(bug_info, jira_info, j)
        new_labels = j.add_labels.call_args[0][3]
        assert sorted(new_labels) == sorted(expected)

    def test_bugzilla_marker_added_when_absent(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = []  # "bugzilla" not present
        sync_whiteboard_labels(bug_info, jira_info, j)
        new_labels = j.add_labels.call_args[0][3]
        assert "bugzilla" in new_labels

    def test_labels_brackets_yes_adds_bracketed(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = ["bugzilla"]
        sync_whiteboard_labels(bug_info, jira_info, j, labels_brackets="yes")
        new_labels = j.add_labels.call_args[0][3]
        assert "[fxp]" in new_labels
        assert "fxp" not in new_labels

    def test_labels_brackets_yes_no_update_when_already_present(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = ["bugzilla", "[fxp]"]
        sync_whiteboard_labels(bug_info, jira_info, j, labels_brackets="yes")
        j.add_labels.assert_not_called()

    def test_labels_brackets_both_adds_both_forms(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = ["bugzilla"]
        sync_whiteboard_labels(bug_info, jira_info, j, labels_brackets="both")
        new_labels = j.add_labels.call_args[0][3]
        assert "fxp" in new_labels
        assert "[fxp]" in new_labels

    def test_labels_brackets_both_only_missing_form(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = ["bugzilla", "fxp"]  # bare form already present
        sync_whiteboard_labels(bug_info, jira_info, j, labels_brackets="both")
        new_labels = j.add_labels.call_args[0][3]
        assert "[fxp]" in new_labels
        assert "fxp" not in new_labels

    def test_labels_brackets_both_no_update_when_both_present(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["whiteboard"] = "[fxp]"
        jira_info["labels"] = ["bugzilla", "fxp", "[fxp]"]
        sync_whiteboard_labels(bug_info, jira_info, j, labels_brackets="both")
        j.add_labels.assert_not_called()


# ---------------------------------------------------------------------------
# sync_keyword_labels
# ---------------------------------------------------------------------------


class TestSyncKeywordLabels:
    def test_adds_missing_keywords(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["keywords"] = ["perf", "regression"]
        jira_info["labels"] = ["bugzilla"]
        sync_keyword_labels(bug_info, jira_info, j)
        j.add_labels.assert_called_once()
        new_labels = j.add_labels.call_args[0][3]
        assert "perf" in new_labels
        assert "regression" in new_labels

    def test_skips_already_present_keywords(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["keywords"] = ["perf"]
        jira_info["labels"] = ["perf", "bugzilla"]
        sync_keyword_labels(bug_info, jira_info, j)
        j.add_labels.assert_not_called()

    def test_empty_keywords_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["keywords"] = []
        sync_keyword_labels(bug_info, jira_info, j)
        j.add_labels.assert_not_called()

    def test_none_keywords_skipped(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["keywords"] = None
        sync_keyword_labels(bug_info, jira_info, j)
        j.add_labels.assert_not_called()


# ---------------------------------------------------------------------------
# sync_depends_on
# ---------------------------------------------------------------------------


class TestSyncDependsOn:
    def _make_dep_bug(self, jira_key="FXP-200"):
        return {"jira": {jira_key: {"key": jira_key}}}

    def test_skips_when_no_depends_on(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = []
        sync_depends_on(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_creates_blocks_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = [999]
        dep_bug = self._make_dep_bug("FXP-200")
        jira_info["links"] = []

        sync_depends_on(bug_info, jira_info, {999: dep_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Blocks", "FXP-200", jira_info["key"]
        )

    def test_skips_if_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = [999]
        dep_bug = self._make_dep_bug("FXP-200")

        existing_link = MagicMock()
        existing_link.type.name = "Blocks"
        existing_link.inwardIssue = MagicMock()
        existing_link.inwardIssue.key = "FXP-200"
        del existing_link.outwardIssue  # no outwardIssue
        jira_info["links"] = [existing_link]

        sync_depends_on(bug_info, jira_info, {999: dep_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_deletes_opposite_link_before_creating(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = [999]
        dep_bug = self._make_dep_bug("FXP-200")

        # An incorrect outward "Blocks" link exists (current bug blocks dep, not vice versa)
        wrong_link = MagicMock()
        wrong_link.type.name = "Blocks"
        wrong_link.outwardIssue = MagicMock()
        wrong_link.outwardIssue.key = "FXP-200"
        wrong_link.id = "link-42"
        del wrong_link.inwardIssue
        jira_info["links"] = [wrong_link]

        sync_depends_on(bug_info, jira_info, {999: dep_bug}, j)
        j.delete_issue_link.assert_called_once()
        j.create_issue_link.assert_called_once()

    def test_skips_dependency_not_in_bugzilla_bugs(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = [9999]  # not in the dict
        sync_depends_on(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def _make_inward_link(self, key, link_id="link-stale"):
        link = MagicMock()
        link.type.name = "Blocks"
        link.inwardIssue = MagicMock()
        link.inwardIssue.key = key
        link.id = link_id
        del link.outwardIssue
        return link

    def test_deletes_stale_inward_link(self, bug_info, jira_info):
        # Dep 999 was removed from depends_on; stale inward link in Jira should be deleted.
        j = _make_jira()
        bug_info["depends_on"] = []
        dep_bug = self._make_dep_bug("FXP-200")
        jira_info["links"] = [self._make_inward_link("FXP-200")]

        sync_depends_on(bug_info, jira_info, {999: dep_bug}, j)
        j.delete_issue_link.assert_called_once()
        j.create_issue_link.assert_not_called()

    def test_does_not_delete_stale_link_for_unknown_key(self, bug_info, jira_info):
        # Inward link to a key not in bugzilla_bugs → external; leave it alone.
        j = _make_jira()
        bug_info["depends_on"] = []
        jira_info["links"] = [self._make_inward_link("EXTERNAL-1")]

        sync_depends_on(bug_info, jira_info, {}, j)
        j.delete_issue_link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_blocks
# ---------------------------------------------------------------------------


class TestSyncBlocks:
    def test_skips_when_no_blocks(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["blocks"] = []
        sync_blocks(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_creates_blocks_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["blocks"] = [777]
        blocked_bug = {"jira": {"FXP-300": {}}}
        jira_info["links"] = []

        sync_blocks(bug_info, jira_info, {777: blocked_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Blocks", jira_info["key"], "FXP-300"
        )

    def test_skips_if_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["blocks"] = [777]
        blocked_bug = {"jira": {"FXP-300": {}}}

        link = MagicMock()
        link.type.name = "Blocks"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = "FXP-300"
        del link.inwardIssue
        jira_info["links"] = [link]

        sync_blocks(bug_info, jira_info, {777: blocked_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_skips_when_blocked_bug_has_reciprocal_depends_on(self, bug_info, jira_info):
        # Bug A (id=123456, FXP-100) blocks Bug B (777).
        # Bug B's depends_on includes 123456 — the reverse relationship.
        # With depends_on_enabled=True, sync_blocks defers to sync_depends_on.
        j = _make_jira()
        bug_info["blocks"] = [777]
        blocked_bug = {"jira": {"FXP-300": {}}, "depends_on": [123456]}
        jira_info["links"] = []

        sync_blocks(bug_info, jira_info, {777: blocked_bug}, j, depends_on_enabled=True)
        j.create_issue_link.assert_not_called()

    def test_creates_link_when_depends_on_not_enabled(self, bug_info, jira_info):
        # Same reciprocal scenario, but depends_on sync is disabled — link must be created.
        j = _make_jira()
        bug_info["blocks"] = [777]
        blocked_bug = {"jira": {"FXP-300": {}}, "depends_on": [123456]}
        jira_info["links"] = []

        sync_blocks(bug_info, jira_info, {777: blocked_bug}, j, depends_on_enabled=False)
        j.create_issue_link.assert_called_once()

    def _make_outward_link(self, key, link_id="link-stale"):
        link = MagicMock()
        link.type.name = "Blocks"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = key
        link.id = link_id
        del link.inwardIssue
        return link

    def test_deletes_stale_outward_link(self, bug_info, jira_info):
        # Bug 777 was removed from blocks; stale outward link in Jira should be deleted.
        j = _make_jira()
        bug_info["blocks"] = []
        removed_bug = {"jira": {"FXP-300": {}}}
        jira_info["links"] = [self._make_outward_link("FXP-300")]

        sync_blocks(bug_info, jira_info, {777: removed_bug}, j)
        j.delete_issue_link.assert_called_once()
        j.create_issue_link.assert_not_called()

    def test_does_not_delete_stale_link_for_unknown_key(self, bug_info, jira_info):
        # Outward link to a key not in bugzilla_bugs → external; leave it alone.
        j = _make_jira()
        bug_info["blocks"] = []
        jira_info["links"] = [self._make_outward_link("EXTERNAL-1")]

        sync_blocks(bug_info, jira_info, {}, j)
        j.delete_issue_link.assert_not_called()

    def test_does_not_delete_managed_link_when_depends_on_enabled(self, bug_info, jira_info):
        # Bug A (123456) blocks Bug B (777) and B depends_on A.
        # With depends_on_enabled, sync_depends_on owns the link — sync_blocks must not delete it.
        j = _make_jira()
        bug_info["blocks"] = [777]
        blocked_bug = {"jira": {"FXP-300": {}}, "depends_on": [123456]}
        jira_info["links"] = [self._make_outward_link("FXP-300")]

        sync_blocks(bug_info, jira_info, {777: blocked_bug}, j, depends_on_enabled=True)
        j.delete_issue_link.assert_not_called()
        j.create_issue_link.assert_not_called()

    def test_skips_stale_outward_delete_when_reciprocal_present_and_depends_on_enabled(self, bug_info, jira_info):
        # Reciprocal bug 777 maps to FXP-300; sync_depends_on on that bug will delete the link
        j = _make_jira()
        bug_info["blocks"] = []
        jira_info["links"] = [self._make_outward_link("FXP-300")]

        sync_blocks(bug_info, jira_info, {777: {"jira": {"FXP-300": {}}}}, j, depends_on_enabled=True)
        j.delete_issue_link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_dependencies (combined depends_on + blocks)
# ---------------------------------------------------------------------------


class TestSyncDependencies:
    def test_drives_both_directions_in_one_call(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = [999]
        bug_info["blocks"] = [888]
        jira_info["links"] = []
        bugzilla_bugs = {
            999: {"jira": {"FXP-200": {"key": "FXP-200"}}},
            888: {"jira": {"FXP-300": {"key": "FXP-300"}}},
        }
        sync_dependencies(bug_info, jira_info, bugzilla_bugs, j)
        # inward link for the depends_on bug + outward link for the blocks bug
        j.create_issue_link.assert_any_call(
            bug_info, jira_info, "Blocks", "FXP-200", jira_info["key"])
        j.create_issue_link.assert_any_call(
            bug_info, jira_info, "Blocks", jira_info["key"], "FXP-300")
        assert j.create_issue_link.call_count == 2

    def test_noop_when_no_dependencies(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["depends_on"] = []
        bug_info["blocks"] = []
        jira_info["links"] = []
        sync_dependencies(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_duplicates
# ---------------------------------------------------------------------------


class TestSyncDuplicates:
    def test_skips_when_no_dupe_of(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = []
        sync_duplicates(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_creates_duplicate_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = 555
        original_bug = {"jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_duplicates(bug_info, jira_info, {555: original_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Duplicate", "FXP-400", jira_info["key"]
        )

    def test_skips_if_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = 555
        original_bug = {"jira": {"FXP-400": {}}}

        existing = MagicMock()
        existing.type.name = "Duplicate"
        existing.inwardIssue = MagicMock()
        existing.inwardIssue.key = "FXP-400"
        del existing.outwardIssue
        jira_info["links"] = [existing]

        sync_duplicates(bug_info, jira_info, {555: original_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_skips_when_original_not_in_bugzilla_bugs(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = 555
        sync_duplicates(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_creates_duplicated_by_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = [555]
        dup_bug = {"jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_duplicates(bug_info, jira_info, {555: dup_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Duplicate", jira_info["key"], "FXP-400"
        )

    def test_skips_if_duplicated_by_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = [555]
        dup_bug = {"jira": {"FXP-400": {}}}

        existing = MagicMock()
        existing.type.name = "Duplicate"
        existing.outwardIssue = MagicMock()
        existing.outwardIssue.key = "FXP-400"
        del existing.inwardIssue
        jira_info["links"] = [existing]

        sync_duplicates(bug_info, jira_info, {555: dup_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_skips_when_duplicating_bug_not_in_bugzilla_bugs(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = [555]
        sync_duplicates(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_handles_both_dupe_of_and_duplicates(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = 444
        bug_info["duplicates"] = [555]
        original_bug = {"jira": {"FXP-300": {}}}
        dup_bug = {"jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_duplicates(bug_info, jira_info, {444: original_bug, 555: dup_bug}, j)
        assert j.create_issue_link.call_count == 2
        calls = j.create_issue_link.call_args_list
        assert any(c.args[3:] == ("FXP-300", jira_info["key"]) for c in calls)
        assert any(c.args[3:] == (jira_info["key"], "FXP-400") for c in calls)

    def test_skips_duplicates_entry_handled_by_dupe_of(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = [555]
        # dup bug has dupe_of pointing back to this bug — sync_duplicates on bug 555 handles creation
        dup_bug = {"dupe_of": bug_info["id"], "jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_duplicates(bug_info, jira_info, {555: dup_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_preserves_existing_link_when_dup_handled_by_dupe_of(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = [555]
        # dup bug has dupe_of pointing back — existing outward link must NOT be deleted
        dup_bug = {"dupe_of": bug_info["id"], "jira": {"FXP-400": {}}}
        existing = MagicMock()
        existing.type.name = "Duplicate"
        existing.outwardIssue = MagicMock()
        existing.outwardIssue.key = "FXP-400"
        del existing.inwardIssue
        jira_info["links"] = [existing]

        sync_duplicates(bug_info, jira_info, {555: dup_bug}, j)
        j.delete_issue_link.assert_not_called()
        j.create_issue_link.assert_not_called()

    def test_does_not_skip_external_dup_with_dupe_of(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = [555]
        # external bugs are never synced via dupe_of, so duplicates must handle them
        dup_bug = {"external": True, "dupe_of": bug_info["id"], "jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_duplicates(bug_info, jira_info, {555: dup_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Duplicate", jira_info["key"], "FXP-400"
        )

    def test_removes_stale_dupe_of_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = []
        stale = MagicMock()
        stale.type.name = "Duplicate"
        stale.inwardIssue = MagicMock()
        stale.inwardIssue.key = "FXP-400"
        stale.id = "link-1"
        del stale.outwardIssue
        jira_info["links"] = [stale]

        sync_duplicates(bug_info, jira_info, {999: {"jira": {"FXP-400": {}}}}, j)
        j.delete_issue_link.assert_called_once_with(
            bug_info, jira_info, "link-1", "removing stale duplicate FXP-400"
        )
        j.create_issue_link.assert_not_called()

    def test_removes_stale_duplicated_by_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = []
        stale = MagicMock()
        stale.type.name = "Duplicate"
        stale.outwardIssue = MagicMock()
        stale.outwardIssue.key = "FXP-400"
        stale.id = "link-2"
        del stale.inwardIssue
        jira_info["links"] = [stale]

        # External reciprocal owner — sync_duplicates won't run on it, so deletion must proceed
        sync_duplicates(bug_info, jira_info, {999: {"jira": {"FXP-400": {}}, "external": True}}, j)
        j.delete_issue_link.assert_called_once_with(
            bug_info, jira_info, "link-2", "removing stale duplicated-by FXP-400"
        )
        j.create_issue_link.assert_not_called()

    def test_skips_stale_duplicated_by_delete_when_reciprocal_present(self, bug_info, jira_info):
        # Reciprocal bug 999 maps to FXP-400 and will delete the same link via its dupe_of path
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = []
        stale = MagicMock()
        stale.type.name = "Duplicate"
        stale.outwardIssue = MagicMock()
        stale.outwardIssue.key = "FXP-400"
        stale.id = "link-2"
        del stale.inwardIssue
        jira_info["links"] = [stale]

        sync_duplicates(bug_info, jira_info, {999: {"jira": {"FXP-400": {}}}}, j)
        j.delete_issue_link.assert_not_called()

    def test_does_not_remove_unknown_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["dupe_of"] = None
        bug_info["duplicates"] = []
        unknown = MagicMock()
        unknown.type.name = "Duplicate"
        unknown.inwardIssue = MagicMock()
        unknown.inwardIssue.key = "EXTERNAL-999"
        del unknown.outwardIssue
        jira_info["links"] = [unknown]

        # EXTERNAL-999 is not in bugzilla_bugs, so not in known_jira_keys — must not be deleted
        sync_duplicates(bug_info, jira_info, {}, j)
        j.delete_issue_link.assert_not_called()
        j.create_issue_link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_regressions
# ---------------------------------------------------------------------------


class TestSyncRegressions:
    def test_skips_when_no_regressions(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressions"] = []
        bug_info["regressed_by"] = []
        sync_regressions(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_creates_regressed_by_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = [555]
        cause_bug = {"jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_regressions(bug_info, jira_info, {555: cause_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Problem/Incident", "FXP-400", jira_info["key"]
        )

    def test_creates_regressions_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressions"] = [555]
        effect_bug = {"jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_regressions(bug_info, jira_info, {555: effect_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Problem/Incident", jira_info["key"], "FXP-400"
        )

    def test_skips_if_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = [555]
        cause_bug = {"jira": {"FXP-400": {}}}

        existing = MagicMock()
        existing.type.name = "Problem/Incident"
        existing.inwardIssue = MagicMock()
        existing.inwardIssue.key = "FXP-400"
        del existing.outwardIssue
        jira_info["links"] = [existing]

        sync_regressions(bug_info, jira_info, {555: cause_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_skips_when_referenced_bug_not_present(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = [555]
        bug_info["regressions"] = [666]
        sync_regressions(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_handles_both_directions(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = [444]
        bug_info["regressions"] = [555]
        cause_bug = {"jira": {"FXP-300": {}}}
        effect_bug = {"jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_regressions(bug_info, jira_info, {444: cause_bug, 555: effect_bug}, j)
        assert j.create_issue_link.call_count == 2
        calls = j.create_issue_link.call_args_list
        assert any(c.args[3:] == ("FXP-300", jira_info["key"]) for c in calls)
        assert any(c.args[3:] == (jira_info["key"], "FXP-400") for c in calls)

    def test_skips_regressions_entry_handled_by_reciprocal_regressed_by(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressions"] = [555]
        # effect bug's regressed_by points back — sync_regressions on bug 555 will create the link
        effect_bug = {"regressed_by": [bug_info["id"]], "jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_regressions(bug_info, jira_info, {555: effect_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_preserves_existing_link_when_reciprocal(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressions"] = [555]
        effect_bug = {"regressed_by": [bug_info["id"]], "jira": {"FXP-400": {}}}
        existing = MagicMock()
        existing.type.name = "Problem/Incident"
        existing.outwardIssue = MagicMock()
        existing.outwardIssue.key = "FXP-400"
        del existing.inwardIssue
        jira_info["links"] = [existing]

        sync_regressions(bug_info, jira_info, {555: effect_bug}, j)
        j.delete_issue_link.assert_not_called()
        j.create_issue_link.assert_not_called()

    def test_does_not_skip_external_effect_with_reciprocal(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressions"] = [555]
        # external bugs don't run sync_regressions themselves, so reciprocal-skip must not apply
        effect_bug = {"external": True, "regressed_by": [bug_info["id"]], "jira": {"FXP-400": {}}}
        jira_info["links"] = []

        sync_regressions(bug_info, jira_info, {555: effect_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Problem/Incident", jira_info["key"], "FXP-400"
        )

    def test_removes_stale_regressed_by_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = []
        bug_info["regressions"] = []
        stale = MagicMock()
        stale.type.name = "Problem/Incident"
        stale.inwardIssue = MagicMock()
        stale.inwardIssue.key = "FXP-400"
        stale.id = "link-1"
        del stale.outwardIssue
        jira_info["links"] = [stale]

        sync_regressions(bug_info, jira_info, {999: {"jira": {"FXP-400": {}}}}, j)
        j.delete_issue_link.assert_called_once_with(
            bug_info, jira_info, "link-1", "removing stale caused-by FXP-400"
        )
        j.create_issue_link.assert_not_called()

    def test_removes_stale_regressions_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = []
        bug_info["regressions"] = []
        stale = MagicMock()
        stale.type.name = "Problem/Incident"
        stale.outwardIssue = MagicMock()
        stale.outwardIssue.key = "FXP-400"
        stale.id = "link-2"
        del stale.inwardIssue
        jira_info["links"] = [stale]

        # External reciprocal owner — sync_regressions won't run on it, so deletion must proceed
        sync_regressions(bug_info, jira_info, {999: {"jira": {"FXP-400": {}}, "external": True}}, j)
        j.delete_issue_link.assert_called_once_with(
            bug_info, jira_info, "link-2", "removing stale causes FXP-400"
        )
        j.create_issue_link.assert_not_called()

    def test_skips_stale_regressions_delete_when_reciprocal_present(self, bug_info, jira_info):
        # Reciprocal bug 999 maps to FXP-400 and will delete the same link via its regressed_by path
        j = _make_jira()
        bug_info["regressed_by"] = []
        bug_info["regressions"] = []
        stale = MagicMock()
        stale.type.name = "Problem/Incident"
        stale.outwardIssue = MagicMock()
        stale.outwardIssue.key = "FXP-400"
        stale.id = "link-2"
        del stale.inwardIssue
        jira_info["links"] = [stale]

        sync_regressions(bug_info, jira_info, {999: {"jira": {"FXP-400": {}}}}, j)
        j.delete_issue_link.assert_not_called()

    def test_does_not_remove_unknown_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["regressed_by"] = []
        bug_info["regressions"] = []
        unknown = MagicMock()
        unknown.type.name = "Problem/Incident"
        unknown.inwardIssue = MagicMock()
        unknown.inwardIssue.key = "EXTERNAL-999"
        del unknown.outwardIssue
        jira_info["links"] = [unknown]

        sync_regressions(bug_info, jira_info, {}, j)
        j.delete_issue_link.assert_not_called()
        j.create_issue_link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_see_also
# ---------------------------------------------------------------------------


class TestSyncSeeAlso:
    def test_skips_when_no_see_also_bugs(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = []
        bug_info["see_also_jira_keys"] = []
        jira_info["links"] = []
        sync_see_also(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()
        j.delete_issue_link.assert_not_called()

    def test_creates_relates_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = [999]
        related_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": []}
        jira_info["links"] = []

        # FXP-100 (jira_info key) sorts before FXP-500 → FXP-100 is inward
        sync_see_also(bug_info, jira_info, {999: related_bug}, j)
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Relates", "FXP-100", "FXP-500"
        )

    def test_normalised_link_direction_when_current_key_sorts_last(self, bug_info, jira_info):
        j = _make_jira()
        # jira_info["key"] is "FXP-100" but related key "AAA-1" sorts before it
        bug_info["see_also_bugs"] = [999]
        related_bug = {"id": 999, "jira": {"AAA-1": {}}, "see_also_bugs": []}
        jira_info["links"] = []

        sync_see_also(bug_info, jira_info, {999: related_bug}, j)
        # sorted(["FXP-100", "AAA-1"]) → ["AAA-1", "FXP-100"]
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Relates", "AAA-1", "FXP-100"
        )

    def test_skips_if_inward_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = [999]
        related_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": []}

        link = MagicMock()
        link.type.name = "Relates"
        link.inwardIssue = MagicMock()
        link.inwardIssue.key = "FXP-500"
        del link.outwardIssue
        jira_info["links"] = [link]

        sync_see_also(bug_info, jira_info, {999: related_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_skips_if_outward_link_already_exists(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = [999]
        related_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": []}

        link = MagicMock()
        link.type.name = "Relates"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = "FXP-500"
        del link.inwardIssue
        jira_info["links"] = [link]

        sync_see_also(bug_info, jira_info, {999: related_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_lower_id_bug_creates_link_when_mutual(self, bug_info, jira_info):
        j = _make_jira()
        # bug_info has id=123456 (from conftest); related bug has id=999 (lower)
        bug_info["see_also_bugs"] = [999]
        related_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": [123456]}
        jira_info["links"] = []

        # bug_info.id (123456) > related_bug.id (999) → skip, let 999 create it
        sync_see_also(bug_info, jira_info, {999: related_bug}, j)
        j.create_issue_link.assert_not_called()

    def test_higher_id_bug_creates_link_when_not_mutual(self, bug_info, jira_info):
        j = _make_jira()
        # bug_info has id=123456; related bug has id=999 but does NOT reference 123456
        bug_info["see_also_bugs"] = [999]
        related_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": []}
        jira_info["links"] = []

        # Relationship is one-way — bug_info should still create the link
        sync_see_also(bug_info, jira_info, {999: related_bug}, j)
        j.create_issue_link.assert_called_once()

    def test_skips_when_related_bug_not_in_bugzilla_bugs(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = [9999]
        jira_info["links"] = []
        sync_see_also(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_creates_relates_link_for_cross_project_jira_key(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_jira_keys"] = ["SPM-50"]
        jira_info["links"] = []
        sync_see_also(bug_info, jira_info, {}, j)
        # sorted(["FXP-100", "SPM-50"]) → ["FXP-100", "SPM-50"]
        j.create_issue_link.assert_called_once_with(
            bug_info, jira_info, "Relates", "FXP-100", "SPM-50"
        )

    def test_skips_cross_project_key_if_already_linked(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_jira_keys"] = ["SPM-50"]
        link = MagicMock()
        link.type.name = "Relates"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = "SPM-50"
        del link.inwardIssue
        jira_info["links"] = [link]
        sync_see_also(bug_info, jira_info, {}, j)
        j.create_issue_link.assert_not_called()

    def test_deletes_stale_relates_link(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = []
        bug_info["see_also_jira_keys"] = []
        stale_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": []}

        link = MagicMock()
        link.type.name = "Relates"
        link.id = "link-123"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = "FXP-500"
        del link.inwardIssue
        jira_info["links"] = [link]

        sync_see_also(bug_info, jira_info, {999: stale_bug}, j)
        j.delete_issue_link.assert_called_once_with(
            bug_info, jira_info, "link-123", "removing stale see-also FXP-500"
        )
        j.create_issue_link.assert_not_called()

    def test_does_not_delete_link_to_unknown_key(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = []
        bug_info["see_also_jira_keys"] = []

        link = MagicMock()
        link.type.name = "Relates"
        link.id = "link-456"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = "EXT-999"  # not in bugzilla_bugs
        del link.inwardIssue
        jira_info["links"] = [link]

        sync_see_also(bug_info, jira_info, {}, j)
        j.delete_issue_link.assert_not_called()

    def test_does_not_delete_link_created_from_other_side(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["see_also_bugs"] = []  # this bug no longer references 999
        bug_info["see_also_jira_keys"] = []
        # but bug 999 still references this bug (id=123456) → link should stay
        other_bug = {"id": 999, "jira": {"FXP-500": {}}, "see_also_bugs": [123456]}

        link = MagicMock()
        link.type.name = "Relates"
        link.id = "link-789"
        link.outwardIssue = MagicMock()
        link.outwardIssue.key = "FXP-500"
        del link.inwardIssue
        jira_info["links"] = [link]

        sync_see_also(bug_info, jira_info, {999: other_bug}, j)
        j.delete_issue_link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_remote_links
# ---------------------------------------------------------------------------


class TestSyncRemoteLinks:
    def test_adds_link_when_missing(self, bug_info, jira_info):
        j = _make_jira()
        jira_info["remote_links"] = []
        sync_remote_links(bug_info, jira_info, j)
        j.add_remote_link.assert_called_once()
        url_arg = j.add_remote_link.call_args[0][2]
        assert "123456" in url_arg

    def test_skips_when_link_already_present(self, bug_info, jira_info):
        j = _make_jira()
        existing = MagicMock()
        existing.object.url = "https://bugzilla.mozilla.org/show_bug.cgi?id=123456"
        jira_info["remote_links"] = [existing]
        sync_remote_links(bug_info, jira_info, j)
        j.add_remote_link.assert_not_called()

    def test_adds_when_key_absent(self, bug_info, jira_info):
        """No 'remote_links' key at all → treat as empty → add."""
        j = _make_jira()
        jira_info.pop("remote_links", None)
        sync_remote_links(bug_info, jira_info, j)
        j.add_remote_link.assert_called_once()


# ---------------------------------------------------------------------------
# sync_time_tracking
# ---------------------------------------------------------------------------


class TestSyncTimeTracking:
    def test_updates_when_estimate_differs(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["estimated_time"] = 8.0
        jira_info["timeoriginalestimate"] = 3600  # 1 hour
        sync_time_tracking(bug_info, jira_info, j)
        j.update_original_estimate.assert_called_once_with(
            bug_info, jira_info, 1.0, 8.0
        )

    def test_no_update_when_estimate_matches(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["estimated_time"] = 2.0
        jira_info["timeoriginalestimate"] = 7200  # 2 hours
        sync_time_tracking(bug_info, jira_info, j)
        j.update_original_estimate.assert_not_called()

    def test_skips_when_no_estimate(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["estimated_time"] = None
        sync_time_tracking(bug_info, jira_info, j)
        j.update_original_estimate.assert_not_called()

    def test_skips_when_estimate_is_zero(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["estimated_time"] = 0
        sync_time_tracking(bug_info, jira_info, j)
        j.update_original_estimate.assert_not_called()

    def test_updates_duedate_when_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["deadline"] = "2025-06-01"
        jira_info["duedate"] = "2025-01-01"
        sync_time_tracking(bug_info, jira_info, j)
        j.update.assert_called()
        args = j.update.call_args[0]
        assert args[0] == "duedate"
        assert args[4] == "2025-06-01"

    def test_clears_duedate_when_deadline_removed(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["deadline"] = None
        jira_info["duedate"] = "2025-01-01"
        sync_time_tracking(bug_info, jira_info, j)
        j.update.assert_called()
        args = j.update.call_args[0]
        assert args[4] is None

    def test_no_duedate_update_when_both_none(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["deadline"] = None
        jira_info["duedate"] = None
        sync_time_tracking(bug_info, jira_info, j)
        j.update.assert_not_called()


# ---------------------------------------------------------------------------
# reverse_sync_priority
# ---------------------------------------------------------------------------


class TestReverseSyncPriority:
    def test_updates_when_jira_priority_differs(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["priority"] = "P3"
        jira_info["priority"] = "P1"
        reverse_sync_priority(bug_info, jira_info, b, PRIORITY_MAP)
        b.update_priority.assert_called_once_with(bug_info, jira_info, "P3", "P1")

    def test_no_update_when_already_matches(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["priority"] = "P2"
        jira_info["priority"] = "P2"
        reverse_sync_priority(bug_info, jira_info, b, PRIORITY_MAP)
        b.update_priority.assert_not_called()

    def test_skips_when_jira_priority_not_in_reverse_map(self, bug_info, jira_info):
        b = _make_bugz()
        jira_info["priority"] = "None"  # maps to "" or "--" which are excluded
        reverse_sync_priority(bug_info, jira_info, b, PRIORITY_MAP)
        b.update_priority.assert_not_called()

    def test_skips_none_priority(self, bug_info, jira_info):
        b = _make_bugz()
        jira_info["priority"] = "(none)"
        reverse_sync_priority(bug_info, jira_info, b, PRIORITY_MAP)
        b.update_priority.assert_not_called()


class TestReverseSyncIssueType:
    def test_updates_when_jira_type_differs(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["type"] = "task"
        jira_info["issuetype"] = "Bug"      # inverts to "defect"
        reverse_sync_issue_type(bug_info, jira_info, b, ISSUE_TYPE_MAP)
        b.update_type.assert_called_once_with(bug_info, jira_info, "task", "defect")

    def test_no_update_when_already_matches(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["type"] = "defect"
        jira_info["issuetype"] = "Bug"
        reverse_sync_issue_type(bug_info, jira_info, b, ISSUE_TYPE_MAP)
        b.update_type.assert_not_called()

    def test_skips_when_jira_type_not_in_reverse_map(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["type"] = "defect"
        jira_info["issuetype"] = "Epic"     # not a value in ISSUE_TYPE_MAP
        reverse_sync_issue_type(bug_info, jira_info, b, ISSUE_TYPE_MAP)
        b.update_type.assert_not_called()


# ---------------------------------------------------------------------------
# reverse_sync_summary
# ---------------------------------------------------------------------------


class TestReverseSyncSummary:
    def test_updates_when_different(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["summary"] = "Bug title"
        jira_info["summary"] = "Jira title"
        reverse_sync_summary(bug_info, jira_info, b)
        b.update_summary.assert_called_once_with(
            bug_info, jira_info, "Bug title", "Jira title"
        )

    def test_no_update_when_same(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["summary"] = "Same"
        jira_info["summary"] = "Same"
        reverse_sync_summary(bug_info, jira_info, b)
        b.update_summary.assert_not_called()


# ---------------------------------------------------------------------------
# reverse_sync_severity
# ---------------------------------------------------------------------------


class TestReverseSyncSeverity:
    def test_updates_when_impact_maps_to_different_severity(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["severity"] = "S4"
        jira_info["estimated_impact"] = "High"
        reverse_sync_severity(bug_info, jira_info, b, SEVERITY_MAP)
        b.update_severity.assert_called_once_with(
            bug_info, jira_info, "High", "S4", "S2"
        )

    def test_no_update_when_severity_matches(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["severity"] = "S2"
        jira_info["estimated_impact"] = "High"
        reverse_sync_severity(bug_info, jira_info, b, SEVERITY_MAP)
        b.update_severity.assert_not_called()

    def test_skips_when_no_estimated_impact(self, bug_info, jira_info):
        b = _make_bugz()
        jira_info["estimated_impact"] = None
        reverse_sync_severity(bug_info, jira_info, b, SEVERITY_MAP)
        b.update_severity.assert_not_called()

    def test_skips_unknown_impact(self, bug_info, jira_info):
        b = _make_bugz()
        jira_info["estimated_impact"] = "Critical"  # not in IMPACT_TO_SEVERITY
        reverse_sync_severity(bug_info, jira_info, b, SEVERITY_MAP)
        b.update_severity.assert_not_called()

    @pytest.mark.parametrize(
        "impact,expected_severity",
        [("High", "S2"), ("Medium", "S3"), ("Low", "S4")],
    )
    def test_impact_to_severity_mapping(
        self, bug_info, jira_info, impact, expected_severity
    ):
        b = _make_bugz()
        bug_info["severity"] = "S1"  # different from expected
        jira_info["estimated_impact"] = impact
        reverse_sync_severity(bug_info, jira_info, b, SEVERITY_MAP)
        b.update_severity.assert_called_once()
        args = b.update_severity.call_args[0]
        assert args[4] == expected_severity


# ---------------------------------------------------------------------------
# reverse_sync_whiteboard_labels
# ---------------------------------------------------------------------------


class TestReverseSyncWhiteboardLabels:
    def test_adds_valid_keyword_to_keywords(self, bug_info, jira_info):
        b = _make_bugz()
        b.get_valid_keywords.return_value = ["regression", "perf"]
        jira_info["labels"] = ["regression"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_keywords.assert_called_once()
        b.update_whiteboard.assert_not_called()

    def test_adds_non_keyword_to_whiteboard(self, bug_info, jira_info):
        b = _make_bugz()
        b.get_valid_keywords.return_value = []
        jira_info["labels"] = ["custom-label"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_whiteboard.assert_called_once()
        new_wb = b.update_whiteboard.call_args[0][3]
        assert "[custom-label]" in new_wb

    def test_skips_bugzilla_label(self, bug_info, jira_info):
        b = _make_bugz()
        b.get_valid_keywords.return_value = []
        jira_info["labels"] = ["bugzilla"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_keywords.assert_not_called()
        b.update_whiteboard.assert_not_called()

    def test_skips_already_present_keyword(self, bug_info, jira_info):
        b = _make_bugz()
        b.get_valid_keywords.return_value = ["perf"]
        jira_info["labels"] = ["perf"]
        bug_info["keywords"] = ["perf"]
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_keywords.assert_not_called()

    def test_skips_already_present_whiteboard_item(self, bug_info, jira_info):
        b = _make_bugz()
        b.get_valid_keywords.return_value = []
        jira_info["labels"] = ["fxp"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = "[fxp]"
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_whiteboard.assert_not_called()

    def test_case_insensitive_label_comparison(self, bug_info, jira_info):
        b = _make_bugz()
        b.get_valid_keywords.return_value = ["Regression"]
        jira_info["labels"] = ["regression"]
        bug_info["keywords"] = ["Regression"]  # already present (case-insensitive)
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_keywords.assert_not_called()

    def test_bracketed_label_matches_whiteboard_item(self, bug_info, jira_info):
        """[fxp] in Jira labels matches existing [fxp] in whiteboard — no update."""
        b = _make_bugz()
        b.get_valid_keywords.return_value = []
        jira_info["labels"] = ["bugzilla", "[fxp]"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = "[fxp]"
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_whiteboard.assert_not_called()
        b.update_keywords.assert_not_called()

    def test_bracketed_label_added_to_keywords(self, bug_info, jira_info):
        """[regression] in Jira → stripped to 'regression' → added to keywords."""
        b = _make_bugz()
        b.get_valid_keywords.return_value = ["regression"]
        jira_info["labels"] = ["bugzilla", "[regression]"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_keywords.assert_called_once()
        keywords_added = b.update_keywords.call_args[0][2]
        assert "regression" in keywords_added

    def test_bracketed_label_added_to_whiteboard(self, bug_info, jira_info):
        """[custom] in Jira, not a keyword, not in whiteboard → add 'custom' to whiteboard."""
        b = _make_bugz()
        b.get_valid_keywords.return_value = []
        jira_info["labels"] = ["bugzilla", "[custom]"]
        bug_info["keywords"] = []
        bug_info["whiteboard"] = ""
        reverse_sync_whiteboard_labels(bug_info, jira_info, b)
        b.update_whiteboard.assert_called_once()
        new_wb = b.update_whiteboard.call_args[0][3]
        assert "[custom]" in new_wb


# ---------------------------------------------------------------------------
# sync_issue_type
# ---------------------------------------------------------------------------

ISSUE_TYPE_MAP = {"defect": "Bug", "task": "Task"}


class TestSyncIssueType:
    def test_updates_when_mapped_and_different(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["type"] = "defect"
        jira_info["issuetype"] = "Task"
        sync_issue_type(bug_info, jira_info, j, ISSUE_TYPE_MAP)
        j.update_issue_type.assert_called_once_with(bug_info, jira_info, "Task", "Bug")

    def test_no_update_when_already_correct(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["type"] = "defect"
        jira_info["issuetype"] = "Bug"
        sync_issue_type(bug_info, jira_info, j, ISSUE_TYPE_MAP)
        j.update_issue_type.assert_not_called()

    def test_defaults_to_task_when_type_not_in_map(self, bug_info, jira_info):
        # Unmapped bug types fall back to "Task" (matching JBI), not skipped.
        j = _make_jira()
        bug_info["type"] = "enhancement"
        jira_info["issuetype"] = "Bug"
        sync_issue_type(bug_info, jira_info, j, ISSUE_TYPE_MAP)
        j.update_issue_type.assert_called_once_with(bug_info, jira_info, "Bug", "Task")

    def test_no_update_when_unmapped_already_task(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["type"] = "enhancement"
        jira_info["issuetype"] = "Task"
        sync_issue_type(bug_info, jira_info, j, ISSUE_TYPE_MAP)
        j.update_issue_type.assert_not_called()

    def test_skips_when_bug_has_no_type(self, bug_info, jira_info):
        j = _make_jira()
        bug_info["type"] = None
        jira_info["issuetype"] = "Bug"
        sync_issue_type(bug_info, jira_info, j, ISSUE_TYPE_MAP)
        j.update_issue_type.assert_not_called()


# ---------------------------------------------------------------------------
# reverse_sync_assignee
# ---------------------------------------------------------------------------


# Synthetic mapping for reverse-sync tests, independent of the gitignored map.
_SAMPLE_BZ_EMAIL = "personal@example.com"
_SAMPLE_JIRA_EMAIL = "work@example.org"


class TestReverseSyncAssignee:
    @pytest.fixture(autouse=True)
    def _controlled_reverse_map(self, monkeypatch):
        monkeypatch.setattr(
            "jbix.sync._REVERSE_ASSIGNEE_MAP", {_SAMPLE_JIRA_EMAIL: _SAMPLE_BZ_EMAIL}
        )

    def _make_user(self, email):
        u = MagicMock()
        u.emailAddress = email
        return u

    def test_updates_via_reverse_map(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["assignee"] = "other@example.com"
        jira_info["assignee"] = self._make_user(_SAMPLE_JIRA_EMAIL)
        reverse_sync_assignee(bug_info, jira_info, b)
        b.update_assignee.assert_called_once_with(bug_info, jira_info, "other@example.com", _SAMPLE_BZ_EMAIL)

    def test_updates_with_raw_jira_email_when_not_in_map(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["assignee"] = "old@example.com"
        jira_info["assignee"] = self._make_user("unmapped@example.com")
        reverse_sync_assignee(bug_info, jira_info, b)
        b.update_assignee.assert_called_once_with(bug_info, jira_info, "old@example.com", "unmapped@example.com")

    def test_no_update_when_already_matches(self, bug_info, jira_info):
        b = _make_bugz()
        bug_info["assignee"] = _SAMPLE_BZ_EMAIL
        jira_info["assignee"] = self._make_user(_SAMPLE_JIRA_EMAIL)
        reverse_sync_assignee(bug_info, jira_info, b)
        b.update_assignee.assert_not_called()

    def test_skips_when_jira_unassigned(self, bug_info, jira_info):
        # Jira unassigned may mean contributor without Jira access — don't clear Bugzilla
        b = _make_bugz()
        bug_info["assignee"] = "someone@example.com"
        jira_info["assignee"] = None
        reverse_sync_assignee(bug_info, jira_info, b)
        b.update_assignee.assert_not_called()

    def test_skips_with_warning_when_no_email_address(self, bug_info, jira_info, caplog):
        import logging
        b = _make_bugz()
        user = MagicMock(spec=[])  # no emailAddress attribute
        jira_info["assignee"] = user
        with caplog.at_level(logging.WARNING):
            reverse_sync_assignee(bug_info, jira_info, b)
        b.update_assignee.assert_not_called()
        assert "no emailAddress" in caplog.text
