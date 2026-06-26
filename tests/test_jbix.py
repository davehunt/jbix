"""Tests for functions in jbix.py — loaded via importlib to avoid name clash with jbix/ package."""

import importlib.util
import os
import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest
from jira.exceptions import JIRAError

# ---------------------------------------------------------------------------
# Load jbix.py as "jbix_main" so it doesn't shadow the jbix/ package
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("jbix_main", _ROOT / "jbix.py")
jbix_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jbix_main)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# load_tags
# ---------------------------------------------------------------------------


class TestLoadTags:
    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jbix_main, "TAGS_YAML", tmp_path / "nonexistent.yaml")
        assert jbix_main.load_tags() == {}

    def test_returns_parsed_yaml(self, tmp_path, monkeypatch):
        yaml_content = "fxp:\n  components:\n    - Firefox::Profiler\n"
        p = tmp_path / "tags.yaml"
        p.write_text(yaml_content)
        monkeypatch.setattr(jbix_main, "TAGS_YAML", p)
        result = jbix_main.load_tags()
        assert "fxp" in result
        assert result["fxp"]["components"] == ["Firefox::Profiler"]

    def test_empty_yaml_returns_empty_dict(self, tmp_path, monkeypatch):
        p = tmp_path / "tags.yaml"
        p.write_text("")
        monkeypatch.setattr(jbix_main, "TAGS_YAML", p)
        assert jbix_main.load_tags() == {}


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_tags_not_required_at_parse(self):
        # --tags is no longer argparse-required (a --group may supply tags);
        # the "neither provided" case is rejected in main(), not by argparse.
        parser = jbix_main.build_parser()
        args = parser.parse_args([])
        assert args.tags is None and args.group is None

    def test_group_flag_parses(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--group", "performance", "genai"])
        assert args.group == ["performance", "genai"]

    def test_all_tags_flag(self):
        parser = jbix_main.build_parser()
        assert parser.parse_args(["--all-tags"]).all_tags is True
        assert parser.parse_args(["--tags", "fxp"]).all_tags is False

    def test_default_mode_is_check(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp"])
        assert args.mode == "check"

    def test_refresh_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--refresh"])
        assert args.refresh is True

    def test_no_cache_flag(self):
        """The old --cache flag should be gone."""
        parser = jbix_main.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--tags", "fxp", "--cache"])

    def test_manual_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--manual"])
        assert args.manual is True

    def test_reverse_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--reverse"])
        assert args.reverse is True

    def test_boolean_optional_flags_default_none(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp"])
        for flag in jbix_main.JBI_FLAGS:
            assert getattr(args, flag) is None

    def test_no_priority_disables_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--no-priority"])
        assert args.priority is False

    def test_priority_enables_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--priority"])
        assert args.priority is True

    def test_dependencies_flag_parses(self):
        parser = jbix_main.build_parser()
        assert parser.parse_args(["--tags", "fxp", "--dependencies"]).dependencies is True
        assert parser.parse_args(["--tags", "fxp", "--no-dependencies"]).dependencies is False

    def test_old_depends_on_blocks_flags_removed(self):
        parser = jbix_main.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--tags", "fxp", "--depends-on"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--tags", "fxp", "--blocks"])

    def test_extension_flags_default_false(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp"])
        for flag in jbix_main.EXTENSION_FLAGS:
            assert getattr(args, flag) is False

    def test_remote_links_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--remote-links"])
        assert args.remote_links is True

    def test_check_mode(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--mode", "check"])
        assert args.mode == "check"

    def test_debug_flag(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "--debug"])
        assert args.debug is True

    def test_mode_choices(self):
        parser = jbix_main.build_parser()
        for mode in ("preview", "prompt", "apply", "check"):
            args = parser.parse_args(["--tags", "fxp", "--mode", mode])
            assert args.mode == mode

    def test_multiple_tags(self):
        parser = jbix_main.build_parser()
        args = parser.parse_args(["--tags", "fxp", "fxpe", "nightly"])
        assert args.tags == ["fxp", "fxpe", "nightly"]


# ---------------------------------------------------------------------------
# effective_flags
# ---------------------------------------------------------------------------


class TestEffectiveFlags:
    def _parse(self, extra_args=None):
        parser = jbix_main.build_parser()
        return parser.parse_args(["--tags", "fxp"] + (extra_args or []))

    def test_starts_from_jbi_config_defaults(self):
        args = self._parse()
        with patch.object(
            jbix_main, "get_enabled_flags", return_value={"priority", "summary"}
        ):
            flags = jbix_main.effective_flags("fxp", args)
        assert "priority" in flags
        assert "summary" in flags

    def test_manual_ignores_jbi_config(self):
        args = self._parse(["--manual"])
        with patch.object(
            jbix_main, "get_enabled_flags", return_value={"priority", "summary"}
        ) as mock_get:
            flags = jbix_main.effective_flags("fxp", args)
        mock_get.assert_not_called()
        # Only explicitly added flags — none were added
        assert flags == set()

    def test_cli_flag_adds_to_defaults(self):
        args = self._parse(["--priority"])
        with patch.object(jbix_main, "get_enabled_flags", return_value=set()):
            flags = jbix_main.effective_flags("fxp", args)
        assert "priority" in flags

    def test_no_flag_removes_from_defaults(self):
        args = self._parse(["--no-priority"])
        with patch.object(
            jbix_main, "get_enabled_flags", return_value={"priority", "summary"}
        ):
            flags = jbix_main.effective_flags("fxp", args)
        assert "priority" not in flags
        assert "summary" in flags

    def test_extension_flag_added_when_set(self):
        args = self._parse(["--time-tracking"])
        with patch.object(jbix_main, "get_enabled_flags", return_value=set()):
            flags = jbix_main.effective_flags("fxp", args)
        assert "time_tracking" in flags

    def test_extension_flags_not_added_by_default(self):
        args = self._parse()
        with patch.object(jbix_main, "get_enabled_flags", return_value=set()):
            flags = jbix_main.effective_flags("fxp", args)
        for ext in jbix_main.EXTENSION_FLAGS:
            assert ext not in flags

    def test_manual_with_explicit_flag(self):
        args = self._parse(["--manual", "--severity"])
        with patch.object(jbix_main, "get_enabled_flags", return_value=set()):
            flags = jbix_main.effective_flags("fxp", args)
        assert flags == {"severity"}


# ---------------------------------------------------------------------------
# _cache_is_fresh
# ---------------------------------------------------------------------------


class TestCacheIsFresh:
    def test_missing_file_not_fresh(self, tmp_path):
        assert jbix_main._cache_is_fresh(tmp_path / "nonexistent.pkl") is False

    def test_fresh_file(self, tmp_path):
        p = tmp_path / "data.pkl"
        p.write_bytes(b"data")
        assert jbix_main._cache_is_fresh(p) is True

    def test_stale_file(self, tmp_path):
        p = tmp_path / "data.pkl"
        p.write_bytes(b"data")
        old_time = time.time() - jbix_main.CACHE_TTL - 60
        os.utime(p, (old_time, old_time))
        assert jbix_main._cache_is_fresh(p) is False


# ---------------------------------------------------------------------------
# _cache_status
# ---------------------------------------------------------------------------


class TestCacheStatus:
    def test_miss_for_missing_file(self, tmp_path):
        assert jbix_main._cache_status(tmp_path / "missing.pkl") == "miss"

    def test_hit_for_fresh_file(self, tmp_path):
        p = tmp_path / "data.pkl"
        p.write_bytes(b"data")
        status = jbix_main._cache_status(p)
        assert status.startswith("hit (")
        assert "m old)" in status

    def test_expired_for_stale_file(self, tmp_path):
        p = tmp_path / "data.pkl"
        p.write_bytes(b"data")
        old_time = time.time() - jbix_main.CACHE_TTL - 60
        os.utime(p, (old_time, old_time))
        status = jbix_main._cache_status(p)
        assert status.startswith("expired (")

    def test_forced_with_existing_file(self, tmp_path):
        p = tmp_path / "data.pkl"
        p.write_bytes(b"data")
        status = jbix_main._cache_status(p, forced=True)
        assert status.startswith("forced (")
        assert "m old)" in status

    def test_forced_with_missing_file(self, tmp_path):
        status = jbix_main._cache_status(tmp_path / "missing.pkl", forced=True)
        assert status == "forced (no cache)"


# ---------------------------------------------------------------------------
# _jira_key_from_link
# ---------------------------------------------------------------------------


class TestJiraKeyFromLink:
    def test_canonical_host(self):
        assert jbix_main._jira_key_from_link(
            "https://mozilla-hub.atlassian.net/browse/FXP-100") == "FXP-100"

    def test_legacy_host(self):
        assert jbix_main._jira_key_from_link(
            "https://jira.mozilla.com/browse/FIDEFE-5") == "FIDEFE-5"

    def test_non_jira_link(self):
        assert jbix_main._jira_key_from_link(
            "https://github.com/some/repo/issues/42") is None

    def test_bugzilla_link(self):
        assert jbix_main._jira_key_from_link(
            "https://bugzilla.mozilla.org/show_bug.cgi?id=999") is None

    def test_non_string(self):
        assert jbix_main._jira_key_from_link(None) is None


# ---------------------------------------------------------------------------
# _bugzilla_results_to_dict
# ---------------------------------------------------------------------------


class TestBugzillaResultsToDict:
    def _make_raw_bug(self, bug_id=111, jira_key="FXP-100"):
        b = MagicMock()
        b.id = bug_id
        b.assigned_to = "user@example.com"
        b.see_also = [f"https://mozilla-hub.atlassian.net/browse/{jira_key}"]
        b.blocks = [222]
        b.component = "General"
        b.deadline = None
        b.depends_on = []
        b.dupe_of = None
        b.estimated_time = 4.0
        b.keywords = ["perf"]
        b.priority = "P2"
        b.product = "Firefox"
        b.resolution = ""
        b.severity = "S3"
        b.status = "NEW"
        b.summary = "Test bug"
        b.whiteboard = "[fxp]"
        return b

    def test_converts_single_bug(self):
        raw = self._make_raw_bug(111, "FXP-100")
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert 111 in result
        bug = result[111]
        assert bug["summary"] == "Test bug"
        assert bug["url"] == "https://bugzil.la/111"
        assert bug["id"] == 111

    def test_extracts_jira_keys_from_see_also(self):
        raw = self._make_raw_bug(111, "FXP-100")
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert "FXP-100" in result[111]["jira"]
        assert result[111]["jira"]["FXP-100"] == {}

    def test_ignores_see_also_for_wrong_project(self):
        raw = self._make_raw_bug()
        raw.see_also = ["https://mozilla-hub.atlassian.net/browse/OTHER-100"]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert result[raw.id]["jira"] == {}

    def test_ignores_non_jira_see_also(self):
        raw = self._make_raw_bug()
        raw.see_also = ["https://github.com/some/repo/issues/42"]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert result[raw.id]["jira"] == {}
        assert result[raw.id]["see_also_bugs"] == []

    def test_extracts_bugzilla_bug_ids_from_see_also(self):
        raw = self._make_raw_bug()
        raw.see_also = ["https://bugzilla.mozilla.org/show_bug.cgi?id=999"]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert result[raw.id]["see_also_bugs"] == [999]
        assert result[raw.id]["jira"] == {}

    def test_extracts_both_jira_and_bugzilla_from_see_also(self):
        raw = self._make_raw_bug()
        raw.see_also = [
            "https://mozilla-hub.atlassian.net/browse/FXP-100",
            "https://bugzilla.mozilla.org/show_bug.cgi?id=999",
        ]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert "FXP-100" in result[raw.id]["jira"]
        assert result[raw.id]["see_also_bugs"] == [999]

    def test_extracts_cross_project_jira_keys_from_see_also(self):
        raw = self._make_raw_bug()
        raw.see_also = ["https://mozilla-hub.atlassian.net/browse/SPM-50"]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert result[raw.id]["jira"] == {}
        assert result[raw.id]["see_also_jira_keys"] == ["SPM-50"]

    def test_extracts_jira_keys_from_legacy_host_see_also(self):
        raw = self._make_raw_bug()
        raw.see_also = ["https://jira.mozilla.com/browse/FXP-100"]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert "FXP-100" in result[raw.id]["jira"]

    def test_extracts_cross_project_legacy_host_see_also(self):
        raw = self._make_raw_bug()
        raw.see_also = ["https://jira.mozilla.com/browse/SPM-50"]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", [])
        assert result[raw.id]["jira"] == {}
        assert result[raw.id]["see_also_jira_keys"] == ["SPM-50"]

    def test_converts_multiple_bugs(self):
        raw1 = self._make_raw_bug(111, "FXP-100")
        raw2 = self._make_raw_bug(222, "FXP-200")
        raw2.see_also = ["https://mozilla-hub.atlassian.net/browse/FXP-200"]
        result = jbix_main._bugzilla_results_to_dict([raw1, raw2], "FXP", [])
        assert 111 in result
        assert 222 in result

    def test_excludes_cross_project_keys_in_excluded_list(self):
        raw = self._make_raw_bug()
        raw.see_also = [
            "https://mozilla-hub.atlassian.net/browse/FXP-100",   # primary (same project)
            "https://mozilla-hub.atlassian.net/browse/BZFFX-7",   # excluded cross-project
            "https://mozilla-hub.atlassian.net/browse/SPM-50",    # allowed cross-project
        ]
        result = jbix_main._bugzilla_results_to_dict([raw], "FXP", ["BZFFX"])
        # Primary same-project link is always kept
        assert "FXP-100" in result[raw.id]["jira"]
        # Excluded project dropped, other cross-project key retained
        assert result[raw.id]["see_also_jira_keys"] == ["SPM-50"]


# ---------------------------------------------------------------------------
# _merge_bug_data
# ---------------------------------------------------------------------------


class TestMergeBugData:
    def test_populates_jira_sub_dict(self):
        bugz_data = {
            111: {"jira": {"FXP-100": {}}, "summary": "test"},
        }
        jira_issues = {"FXP-100": {"key": "FXP-100", "status": "Open"}}
        result = jbix_main._merge_bug_data(bugz_data, jira_issues)
        assert result[111]["jira"]["FXP-100"] == {"key": "FXP-100", "status": "Open"}

    def test_removes_unmatched_jira_keys(self):
        bugz_data = {
            111: {"jira": {"FXP-100": {}, "FXP-999": {}}, "summary": "test"},
        }
        jira_issues = {"FXP-100": {"key": "FXP-100"}}
        result = jbix_main._merge_bug_data(bugz_data, jira_issues)
        assert "FXP-100" in result[111]["jira"]
        assert "FXP-999" not in result[111]["jira"]

    def test_returns_the_dict(self):
        bugz_data = {111: {"jira": {}, "summary": "x"}}
        result = jbix_main._merge_bug_data(bugz_data, {})
        assert result is bugz_data

    def test_empty_bugzilla_bugs(self):
        result = jbix_main._merge_bug_data({}, {"FXP-100": {}})
        assert result == {}

    def test_dropped_collects_missing_same_project_keys(self):
        bugz_data = {
            111: {"id": 111, "url": "https://bugzil.la/111",
                  "jira": {"FXP-100": {}, "FXP-999": {}}},
        }
        jira_issues = {"FXP-100": {"key": "FXP-100"}}
        dropped: list = []
        jbix_main._merge_bug_data(bugz_data, jira_issues, dropped=dropped)
        assert dropped == [(111, "https://bugzil.la/111", "FXP-999")]
        assert "FXP-100" in bugz_data[111]["jira"]  # present key kept


# ---------------------------------------------------------------------------
# find_broken_links / write_broken_links_csv
# ---------------------------------------------------------------------------


class TestFindBrokenLinks:
    def _client(self, found_keys):
        c = MagicMock()
        c.search_issues.return_value = [MagicMock(key=k) for k in found_keys]
        c.issue.side_effect = JIRAError("not found")
        return c

    def test_reports_only_unresolved_keys(self):
        bugzilla_bugs = {
            111: {"id": 111, "url": "https://bugzil.la/111",
                  "see_also_jira_keys": ["SPM-50"]},  # cross-project, exists
            222: {"id": 222, "url": "https://bugzil.la/222",
                  "see_also_jira_keys": ["DEAD-9"]},   # cross-project, gone
        }
        dropped = [(111, "https://bugzil.la/111", "FXP-999")]  # same-project, gone
        c = self._client(["SPM-50"])  # only SPM-50 resolves; others raise on GET
        rows = jbix_main.find_broken_links(
            bugzilla_bugs, dropped, c, {}, tag="fxp", jira_issues={}, bugz=MagicMock())
        keys = {(r["bug_id"], r["jira_key"]) for r in rows}
        assert keys == {(111, "FXP-999"), (222, "DEAD-9")}
        assert all(r["jira_url"].endswith(r["jira_key"]) for r in rows)
        assert all(r["direction"] == "bug→jira" for r in rows)

    def test_rename_or_prefix_collision_not_reported(self):
        # A same-project drop that actually resolves via GET (e.g. FXPE-1 on an FXP
        # bug, or a renamed key) must NOT be reported as broken.
        bugzilla_bugs = {111: {"id": 111, "url": "https://bugzil.la/111",
                               "see_also_jira_keys": []}}
        dropped = [(111, "https://bugzil.la/111", "FXPE-1")]
        c = self._client([])           # batch finds nothing...
        c.issue.side_effect = None     # ...but the GET resolves it
        c.issue.return_value = MagicMock(key="FXPE-1")
        assert jbix_main.find_broken_links(
            bugzilla_bugs, dropped, c, {}, tag="fxp", jira_issues={}, bugz=MagicMock()) == []

    def test_skips_external_bugs_and_clean_returns_empty(self):
        bugzilla_bugs = {
            999: {"id": 999, "external": True, "jira": {"X-1": {}}},
        }
        assert jbix_main.find_broken_links(
            bugzilla_bugs, [], MagicMock(), {},
            tag="fxp", jira_issues={}, bugz=MagicMock()) == []


class TestLabelMatchesTag:
    def test_exact_and_prefix_and_brackets(self):
        m = jbix_main._label_matches_tag
        assert m(["fxpe"], "fxpe")
        assert m(["fxpe-moco"], "fxpe")          # dash boundary
        assert m(["[fxpe]"], "fxpe")             # bracket form
        assert m(["[fxpe-moco]"], "fxpe")
        assert m(["FXPE"], "fxpe")               # case-insensitive

    def test_non_matches(self):
        m = jbix_main._label_matches_tag
        assert not m(["fxpefoo"], "fxpe")        # no delimiter
        assert not m(["fxpe:hello"], "fxpe")     # colon is not a JBI delimiter
        assert not m(["bugzilla", "other"], "fxpe")
        assert not m([], "fxpe")
        assert not m(None, "fxpe")


class TestFindOrphanedIssues:
    _PREFIX = "https://mozilla-hub.atlassian.net/browse/"

    def _bug(self, bug_id, whiteboard, see_also_keys):
        """Mock bug whose see_also holds full browse URLs for the given keys."""
        return MagicMock(
            id=bug_id,
            whiteboard=whiteboard,
            see_also=[f"{self._PREFIX}{k}" for k in see_also_keys],
        )

    def test_whiteboard_removed_orphan(self):
        bugzilla_bugs = {1: {"id": 1, "jira": {"FXPE-5": {}}}}  # linked, not orphan
        jira_issues = {
            "FXPE-5": {"labels": ["bugzilla", "fxpe"]},          # linked → skip
            "FXPE-10": {"labels": ["bugzilla", "fxpe"]},         # orphan
            "FXPE-99": {"labels": []},                            # no bugzilla label
        }
        bugz = MagicMock()
        bugz.client.query.return_value = [
            self._bug(2008619, "[necko-triaged]", ["FXPE-10"])]
        rows = jbix_main.find_orphaned_issues("fxpe", bugzilla_bugs, jira_issues, bugz)
        assert len(rows) == 1
        r = rows[0]
        assert r["direction"] == "jira→bug"
        assert r["jira_key"] == "FXPE-10"
        assert r["bug_id"] == 2008619
        assert r["reason"] == "whiteboard-removed"

    def test_stale_label_when_no_bug_references_it(self):
        jira_issues = {"FXPE-21": {"labels": ["bugzilla", "fxpe"]}}
        bugz = MagicMock()
        bugz.client.query.return_value = []   # nothing references it any more
        rows = jbix_main.find_orphaned_issues("fxpe", {}, jira_issues, bugz)
        assert rows[0]["reason"] == "stale-label"
        assert rows[0]["bug_id"] == ""

    def test_substring_key_collision_not_matched(self):
        # Orphan FXPE-140; the only see_also hit is a bug linking FXPE-1407.
        # The substring query matches it server-side, but exact key matching must
        # reject it → stale-label, no bug attributed.
        jira_issues = {"FXPE-140": {"labels": ["bugzilla", "fxpe"]}}
        bugz = MagicMock()
        bugz.client.query.return_value = [self._bug(1672210, "[fxpe]", ["FXPE-1407"])]
        rows = jbix_main.find_orphaned_issues("fxpe", {}, jira_issues, bugz)
        assert rows[0]["jira_key"] == "FXPE-140"
        assert rows[0]["bug_id"] == ""
        assert rows[0]["reason"] == "stale-label"

    def test_external_bugs_do_not_count_as_linked(self):
        bugzilla_bugs = {9: {"id": 9, "external": True, "jira": {"FXPE-10": {}}}}
        jira_issues = {"FXPE-10": {"labels": ["bugzilla", "fxpe"]}}
        bugz = MagicMock()
        bugz.client.query.return_value = [self._bug(123, "", ["FXPE-10"])]
        rows = jbix_main.find_orphaned_issues("fxpe", bugzilla_bugs, jira_issues, bugz)
        assert [r["jira_key"] for r in rows] == ["FXPE-10"]

    def test_legacy_host_see_also_resolves_orphan(self):
        # A referencing bug whose see_also uses the legacy jira.mozilla.com host
        # is still matched → not reported as stale-label.
        jira_issues = {"FXPE-10": {"labels": ["bugzilla", "fxpe"]}}
        bugz = MagicMock()
        bugz.client.query.return_value = [
            MagicMock(id=2008619, whiteboard="[necko-triaged]",
                      see_also=["https://jira.mozilla.com/browse/FXPE-10"])]
        rows = jbix_main.find_orphaned_issues("fxpe", {}, jira_issues, bugz)
        assert rows[0]["jira_key"] == "FXPE-10"
        assert rows[0]["bug_id"] == 2008619
        assert rows[0]["reason"] == "whiteboard-removed"

    def test_lookups_are_batched(self, monkeypatch):
        # 3 candidates with batch size 2 → 2 batched queries (not 3 per-key ones),
        # and each key is mapped to its referencing bug from the right batch.
        monkeypatch.setattr(jbix_main, "BZ_SEE_ALSO_BATCH_SIZE", 2)
        jira_issues = {
            "FXPE-1": {"labels": ["bugzilla", "fxpe"]},
            "FXPE-2": {"labels": ["bugzilla", "fxpe"]},
            "FXPE-3": {"labels": ["bugzilla", "fxpe"]},
        }
        bugz = MagicMock()
        # chunk 1 = [FXPE-1, FXPE-2], chunk 2 = [FXPE-3] (sorted)
        bugz.client.query.side_effect = [
            [self._bug(11, "[fxpe]", ["FXPE-1"]), self._bug(12, "", ["FXPE-2"])],
            [self._bug(13, "[fxpe]", ["FXPE-3"])],
        ]
        rows = jbix_main.find_orphaned_issues("fxpe", {}, jira_issues, bugz)
        assert bugz.client.query.call_count == 2  # batched, not 3 per-key calls
        by_key = {r["jira_key"]: r["bug_id"] for r in rows}
        assert by_key == {"FXPE-1": 11, "FXPE-2": 12, "FXPE-3": 13}

    def test_batch_query_is_an_or_chart(self):
        bugz = MagicMock()
        bugz.client.query.return_value = []
        jira_issues = {"FXPE-1": {"labels": ["bugzilla", "fxpe"]}}
        jbix_main.find_orphaned_issues("fxpe", {}, jira_issues, bugz)
        q = bugz.client.query.call_args[0][0]
        assert q["f1"] == "OP" and q["j1"] == "OR"
        assert q["f2"] == "see_also" and q["o2"] == "substring"
        assert q["v2"] == "/browse/FXPE-1"  # host-agnostic: matches either host
        assert q["f3"] == "CP"
        assert q["include_fields"] == ["id", "whiteboard", "see_also"]


class TestWriteBrokenLinksCsv:
    def test_writes_rows(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rows = [{"direction": "bug→jira", "bug_id": 111,
                 "bug_url": "https://bugzil.la/111",
                 "jira_key": "FXP-9", "jira_url": "https://j/FXP-9",
                 "reason": "dead-jira"}]
        jbix_main.write_broken_links_csv("fxp", rows)
        out = (tmp_path / "output" / "broken_links_fxp.csv").read_text()
        assert "direction,bug_id,bug_url,jira_key,jira_url,reason" in out
        assert "bug→jira,111,https://bugzil.la/111,FXP-9,https://j/FXP-9,dead-jira" in out

    def test_no_file_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jbix_main.write_broken_links_csv("fxp", [])
        assert not (tmp_path / "output" / "broken_links_fxp.csv").exists()


# ---------------------------------------------------------------------------
# _collect_external_ids
# ---------------------------------------------------------------------------


class TestCollectExternalIds:
    def _bugs(self, entries: dict) -> dict:
        """Build a minimal bugzilla_bugs dict from {bug_id: extra_fields}."""
        return {
            bid: {"id": bid, "depends_on": [], "blocks": [], "dupe_of": False,
                  "see_also_bugs": [], **fields}
            for bid, fields in entries.items()
        }

    def test_returns_empty_when_no_link_flags(self):
        bugs = self._bugs({111: {"depends_on": [999]}})
        assert jbix_main._collect_external_ids(bugs, {"summary"}) == set()

    def test_dependencies_collects_depends_on_ids(self):
        bugs = self._bugs({111: {"depends_on": [999]}})
        assert jbix_main._collect_external_ids(bugs, {"dependencies"}) == {999}

    def test_dependencies_collects_blocks_ids(self):
        bugs = self._bugs({111: {"blocks": [999]}})
        assert jbix_main._collect_external_ids(bugs, {"dependencies"}) == {999}

    def test_dependencies_collects_both_fields(self):
        bugs = self._bugs({111: {"depends_on": [998], "blocks": [997]}})
        assert jbix_main._collect_external_ids(bugs, {"dependencies"}) == {997, 998}

    def test_collects_dupe_of_id(self):
        bugs = self._bugs({111: {"dupe_of": 999}})
        assert jbix_main._collect_external_ids(bugs, {"duplicates"}) == {999}

    def test_collects_see_also_bug_ids(self):
        bugs = self._bugs({111: {"see_also_bugs": [999]}})
        assert jbix_main._collect_external_ids(bugs, {"see_also"}) == {999}

    def test_collects_regressions_ids(self):
        bugs = self._bugs({111: {"regressions": [998], "regressed_by": [997]}})
        assert jbix_main._collect_external_ids(bugs, {"regressions"}) == {997, 998}

    def test_skips_ids_already_in_bugzilla_bugs(self):
        bugs = self._bugs({111: {"depends_on": [222]}, 222: {}})
        assert jbix_main._collect_external_ids(bugs, {"dependencies"}) == set()

    def test_skips_external_entries(self):
        bugs = {
            111: {"id": 111, "depends_on": [999], "blocks": [], "dupe_of": False,
                  "see_also_bugs": [], "external": True},
        }
        assert jbix_main._collect_external_ids(bugs, {"dependencies"}) == set()

    def test_collects_across_multiple_flags(self):
        bugs = self._bugs({111: {"dupe_of": 998, "see_also_bugs": [997]}})
        result = jbix_main._collect_external_ids(bugs, {"duplicates", "see_also"})
        assert result == {997, 998}


# ---------------------------------------------------------------------------
# _fetch_external_bugs
# ---------------------------------------------------------------------------


class TestFetchExternalBugs:
    def _make_raw_bug(self, bug_id, see_also):
        b = MagicMock()
        b.id = bug_id
        b.see_also = see_also
        return b

    def _make_bugz(self, raw_bugs):
        bugz = MagicMock()
        bugz.client.getbugs.return_value = raw_bugs
        return bugz

    def _make_jira_client(self, found_keys):
        jira_client = MagicMock()
        jira_client.search_issues.return_value = [
            MagicMock(key=k) for k in found_keys
        ]
        # By default a key not returned by the batch search doesn't exist — the
        # per-key GET fallback raises so it is dropped.
        jira_client.issue.side_effect = JIRAError("not found")
        return jira_client

    def test_returns_empty_when_no_ids(self):
        bugz = MagicMock()
        jira_client = MagicMock()
        result = jbix_main._fetch_external_bugs(set(), bugz, jira_client, [], {})
        assert result == {}
        bugz.client.getbugs.assert_not_called()

    def test_fetches_and_parses_jira_keys(self):
        raw = self._make_raw_bug(999, [f"{jbix_main.JIRA_LINK_PREFIX}SPM-50"])
        bugz = self._make_bugz([raw])
        jira_client = self._make_jira_client(["SPM-50"])
        result = jbix_main._fetch_external_bugs({999}, bugz, jira_client, [], {})
        assert result == {999: {"id": 999, "external": True, "jira": {"SPM-50": {}}}}

    def test_skips_jira_key_not_found_in_jira(self):
        raw = self._make_raw_bug(999, [f"{jbix_main.JIRA_LINK_PREFIX}SPM-50"])
        bugz = self._make_bugz([raw])
        jira_client = self._make_jira_client([])  # Jira confirms nothing
        result = jbix_main._fetch_external_bugs({999}, bugz, jira_client, [], {})
        assert result[999]["jira"] == {}

    def test_resolves_renamed_key_to_current(self):
        raw = self._make_raw_bug(999, [f"{jbix_main.JIRA_LINK_PREFIX}GENAI-1608"])
        bugz = self._make_bugz([raw])
        jira_client = self._make_jira_client([])  # batch search doesn't return the old key
        jira_client.issue.side_effect = None
        jira_client.issue.return_value = MagicMock(key="AIFE-1608")  # GET follows the rename
        result = jbix_main._fetch_external_bugs({999}, bugz, jira_client, [], {})
        assert result[999]["jira"] == {"AIFE-1608": {}}

    def test_handles_external_bug_with_no_jira_links(self):
        raw = self._make_raw_bug(999, [])
        bugz = self._make_bugz([raw])
        jira_client = MagicMock()
        result = jbix_main._fetch_external_bugs({999}, bugz, jira_client, [], {})
        assert result[999]["jira"] == {}
        jira_client.search_issues.assert_not_called()

    def test_skips_non_jira_see_also(self):
        raw = self._make_raw_bug(999, ["https://github.com/some/repo/issues/42"])
        bugz = self._make_bugz([raw])
        jira_client = MagicMock()
        result = jbix_main._fetch_external_bugs({999}, bugz, jira_client, [], {})
        assert result[999]["jira"] == {}
        jira_client.search_issues.assert_not_called()

    def test_excluded_project_keys_never_become_placeholders(self):
        # Applies to all link types: external-bug keys feed depends_on/blocks/etc.
        raw = self._make_raw_bug(999, [
            f"{jbix_main.JIRA_LINK_PREFIX}BZFFX-7",
            f"{jbix_main.JIRA_LINK_PREFIX}SPM-50",
        ])
        bugz = self._make_bugz([raw])
        jira_client = self._make_jira_client(["SPM-50"])
        result = jbix_main._fetch_external_bugs({999}, bugz, jira_client, ["BZFFX"], {})
        assert result[999]["jira"] == {"SPM-50": {}}
        # The excluded key must not even be verified against Jira
        verified = jira_client.search_issues.call_args[0][0]
        assert "BZFFX-7" not in verified
        assert "SPM-50" in verified


# ---------------------------------------------------------------------------
# _resolve_jira_keys / _resolve_see_also_keys
# ---------------------------------------------------------------------------


class TestResolveJiraKeys:
    def _client(self, found_keys):
        c = MagicMock()
        c.search_issues.return_value = [MagicMock(key=k) for k in found_keys]
        c.issue.side_effect = JIRAError("not found")
        return c

    def test_current_keys_map_to_themselves_without_get(self):
        c = self._client(["FXP-1", "SPM-2"])
        mapping = jbix_main._resolve_jira_keys({"FXP-1", "SPM-2"}, c, {})
        assert mapping == {"FXP-1": "FXP-1", "SPM-2": "SPM-2"}
        c.issue.assert_not_called()

    def test_renamed_key_resolved_via_get(self):
        c = self._client(["FXP-1"])  # batch returns only the current key
        c.issue.side_effect = None
        c.issue.return_value = MagicMock(key="AIFE-1608")
        mapping = jbix_main._resolve_jira_keys({"FXP-1", "GENAI-1608"}, c, {})
        assert mapping == {"FXP-1": "FXP-1", "GENAI-1608": "AIFE-1608"}

    def test_missing_key_omitted(self):
        c = self._client([])  # nothing found, GET raises
        mapping = jbix_main._resolve_jira_keys({"DEAD-9"}, c, {})
        assert mapping == {}

    def test_batch_error_falls_back_to_per_key(self):
        c = self._client([])
        c.search_issues.side_effect = JIRAError("bad JQL")
        c.issue.side_effect = None
        c.issue.return_value = MagicMock(key="AIFE-1608")
        mapping = jbix_main._resolve_jira_keys({"GENAI-1608"}, c, {})
        assert mapping == {"GENAI-1608": "AIFE-1608"}

    def test_cache_prevents_repeat_lookups(self):
        c = self._client(["FXP-1"])
        cache: dict = {}
        jbix_main._resolve_jira_keys({"FXP-1"}, c, cache)
        c.search_issues.reset_mock()
        # Second call for the same key uses the cache — no further API calls.
        mapping = jbix_main._resolve_jira_keys({"FXP-1"}, c, cache)
        assert mapping == {"FXP-1": "FXP-1"}
        c.search_issues.assert_not_called()

    def test_cache_remembers_missing_keys(self):
        c = self._client([])
        cache: dict = {}
        jbix_main._resolve_jira_keys({"DEAD-9"}, c, cache)
        c.issue.reset_mock()
        jbix_main._resolve_jira_keys({"DEAD-9"}, c, cache)
        c.issue.assert_not_called()


class TestResolveSeeAlsoKeys:
    def _client_renaming(self, old, new):
        c = MagicMock()
        c.search_issues.return_value = []  # old key not returned verbatim
        c.issue.return_value = MagicMock(key=new)
        c.issue.side_effect = None
        return c

    def test_rewrites_renamed_see_also_key(self):
        bugs = {
            1: {"id": 1, "see_also_jira_keys": ["GENAI-1608"], "jira": {}},
            2: {"id": 2, "see_also_jira_keys": [], "jira": {}},
        }
        c = self._client_renaming("GENAI-1608", "AIFE-1608")
        jbix_main._resolve_see_also_keys(bugs, c, {})
        assert bugs[1]["see_also_jira_keys"] == ["AIFE-1608"]
        assert bugs[2]["see_also_jira_keys"] == []

    def test_no_mutation_when_keys_current(self):
        bugs = {1: {"id": 1, "see_also_jira_keys": ["SPM-50"], "jira": {}}}
        c = MagicMock()
        c.search_issues.return_value = [MagicMock(key="SPM-50")]
        jbix_main._resolve_see_also_keys(bugs, c, {})
        assert bugs[1]["see_also_jira_keys"] == ["SPM-50"]
        c.issue.assert_not_called()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_cache_ttl_is_one_hour(self):
        assert jbix_main.CACHE_TTL == 3600

    def test_jbi_flags_contains_expected(self):
        for flag in ("summary", "priority", "severity", "status", "resolution", "see_also"):
            assert flag in jbix_main.JBI_FLAGS

    def test_extension_flags_listed(self):
        for flag in ("time_tracking", "remote_links"):
            assert flag in jbix_main.EXTENSION_FLAGS

    def test_duplicates_is_jbi_flag(self):
        assert "duplicates" in jbix_main.JBI_FLAGS
        assert "duplicates" not in jbix_main.EXTENSION_FLAGS

    def test_regressions_is_jbi_flag(self):
        assert "regressions" in jbix_main.JBI_FLAGS
        assert "regressions" not in jbix_main.EXTENSION_FLAGS

    def test_dependencies_replaces_depends_on_and_blocks(self):
        assert "dependencies" in jbix_main.JBI_FLAGS
        assert "depends_on" not in jbix_main.JBI_FLAGS
        assert "blocks" not in jbix_main.JBI_FLAGS
        # the combined flag reads both Bugzilla fields for external-id collection
        assert jbix_main._LINK_FLAGS_TO_FIELD["dependencies"] == ["depends_on", "blocks"]

    def test_reverse_supported_contains_expected(self):
        for flag in ("summary", "priority", "severity"):
            assert flag in jbix_main.REVERSE_SUPPORTED


# ---------------------------------------------------------------------------
# _fetch_bugzilla (pagination)
# ---------------------------------------------------------------------------


class TestFetchBugzilla:
    def _make_bugz(self, batches: list[list]) -> MagicMock:
        """Return a mock BugzillaClient whose query() yields successive batches."""
        bugz = MagicMock()
        bugz.client.build_query.return_value = {"status_whiteboard": "[fxp]"}
        bugz.client.query.side_effect = batches
        return bugz

    def test_single_page_when_results_fit(self):
        bugs = [MagicMock() for _ in range(3)]
        bugz = self._make_bugz([bugs])
        with patch.object(jbix_main, "BZ_FETCH_BATCH_SIZE", 500):
            result = jbix_main._fetch_bugzilla("fxp", bugz)
        assert result == bugs
        assert bugz.client.query.call_count == 1

    def test_paginates_when_first_batch_is_full(self):
        batch1 = [MagicMock() for _ in range(2)]
        batch2 = [MagicMock() for _ in range(1)]
        bugz = self._make_bugz([batch1, batch2])
        with patch.object(jbix_main, "BZ_FETCH_BATCH_SIZE", 2):
            result = jbix_main._fetch_bugzilla("fxp", bugz)
        assert result == batch1 + batch2
        assert bugz.client.query.call_count == 2

    def test_passes_limit_and_offset(self):
        batch1 = [MagicMock() for _ in range(2)]
        batch2 = [MagicMock()]
        bugz = self._make_bugz([batch1, batch2])
        with patch.object(jbix_main, "BZ_FETCH_BATCH_SIZE", 2):
            jbix_main._fetch_bugzilla("fxp", bugz)
        calls = bugz.client.query.call_args_list
        assert calls[0][0][0]["limit"] == 2
        assert calls[0][0][0]["offset"] == 0
        assert calls[1][0][0]["limit"] == 2
        assert calls[1][0][0]["offset"] == 2

    def test_three_full_pages_then_partial(self):
        batches = [[MagicMock()] * 2, [MagicMock()] * 2, [MagicMock()] * 2, [MagicMock()]]
        bugz = self._make_bugz(batches)
        with patch.object(jbix_main, "BZ_FETCH_BATCH_SIZE", 2):
            result = jbix_main._fetch_bugzilla("fxp", bugz)
        assert len(result) == 7
        assert bugz.client.query.call_count == 4

    def test_empty_result(self):
        bugz = self._make_bugz([[]])
        with patch.object(jbix_main, "BZ_FETCH_BATCH_SIZE", 500):
            result = jbix_main._fetch_bugzilla("fxp", bugz)
        assert result == []
        assert bugz.client.query.call_count == 1
