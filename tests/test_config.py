"""Tests for jbix/config.py — config loading and helpers."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

import jbix.config as config
from jbix.constants import (
    DEFAULT_PRIORITY_MAP,
    DEFAULT_RESOLUTION_MAP,
    DEFAULT_SEVERITY_MAP,
    DEFAULT_STATUS_MAP,
)

SAMPLE_CONFIG = [
    {
        "whiteboard_tag": "fxp",
        "parameters": {
            "jira_project_key": "FXP",
            "steps": {
                "existing": [
                    "update_issue_summary",
                    "maybe_update_issue_priority",
                    "sync_whiteboard_labels",
                ]
            },
        },
    },
    {
        "whiteboard_tag": "fxpe",
        "parameters": {
            "jira_project_key": "FXPE",
            "steps": {"existing": []},
        },
    },
]

SAMPLE_YAML = yaml.dump(SAMPLE_CONFIG)


# ---------------------------------------------------------------------------
# _is_cache_fresh / get_config cache logic
# ---------------------------------------------------------------------------


class TestCacheFreshness:
    def test_missing_file_is_not_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_PATH", tmp_path / "missing.yaml")
        assert config._is_cache_fresh() is False

    def test_fresh_file_is_fresh(self, tmp_path, monkeypatch):
        p = tmp_path / "jbi_config.yaml"
        p.write_text(SAMPLE_YAML)
        monkeypatch.setattr(config, "CACHE_PATH", p)
        assert config._is_cache_fresh() is True

    def test_stale_file_is_not_fresh(self, tmp_path, monkeypatch):
        p = tmp_path / "jbi_config.yaml"
        p.write_text(SAMPLE_YAML)
        old_mtime = time.time() - 25 * 3600
        os.utime(p, (old_mtime, old_mtime))
        monkeypatch.setattr(config, "CACHE_PATH", p)
        assert config._is_cache_fresh() is False


class TestGetConfig:
    def test_loads_from_fresh_cache(self, tmp_path, monkeypatch):
        p = tmp_path / "jbi_config.yaml"
        p.write_text(SAMPLE_YAML)
        monkeypatch.setattr(config, "CACHE_PATH", p)

        with patch.object(config, "_fetch_raw") as mock_fetch:
            result = config.get_config()
        mock_fetch.assert_not_called()
        assert len(result) == 2
        assert result[0]["whiteboard_tag"] == "fxp"

    def test_fetches_and_caches_when_stale(self, tmp_path, monkeypatch):
        p = tmp_path / "jbi_config.yaml"
        p.write_text(SAMPLE_YAML)
        old_mtime = time.time() - 25 * 3600
        os.utime(p, (old_mtime, old_mtime))
        monkeypatch.setattr(config, "CACHE_PATH", p)

        new_config = [{"whiteboard_tag": "new-tag", "parameters": {}}]
        with patch.object(config, "_fetch_raw", return_value=yaml.dump(new_config)):
            result = config.get_config()
        assert result[0]["whiteboard_tag"] == "new-tag"
        assert p.read_text()  # cache was written

    def test_fetches_when_no_cache(self, tmp_path, monkeypatch):
        p = tmp_path / "missing.yaml"
        monkeypatch.setattr(config, "CACHE_PATH", p)

        with patch.object(config, "_fetch_raw", return_value=SAMPLE_YAML):
            result = config.get_config()
        assert result[0]["whiteboard_tag"] == "fxp"
        assert p.exists()  # cache was created

    def test_raises_on_non_list_yaml(self, tmp_path, monkeypatch):
        p = tmp_path / "bad.yaml"
        p.write_text("key: value\n")  # dict, not list
        monkeypatch.setattr(config, "CACHE_PATH", p)
        with pytest.raises(ValueError, match="expected a YAML list"):
            config.get_config()


# ---------------------------------------------------------------------------
# get_tag_config
# ---------------------------------------------------------------------------


class TestGetTagConfig:
    def test_returns_entry_when_found(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            result = config.get_tag_config("fxp")
        assert result["whiteboard_tag"] == "fxp"

    def test_returns_none_when_not_found(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            result = config.get_tag_config("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# get_jira_project
# ---------------------------------------------------------------------------


class TestGetJiraProject:
    def test_returns_project_key(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            assert config.get_jira_project("fxp") == "FXP"

    def test_returns_none_for_unknown_tag(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            assert config.get_jira_project("unknown") is None


# ---------------------------------------------------------------------------
# get_linked_project_excludes
# ---------------------------------------------------------------------------


class TestGetLinkedProjectExcludes:
    def test_defaults_to_bzffx_when_unset(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            assert config.get_linked_project_excludes("fxp") == ["BZFFX"]

    def test_defaults_to_bzffx_for_unknown_tag(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            assert config.get_linked_project_excludes("unknown") == ["BZFFX"]

    def test_honours_explicit_empty_list(self):
        cfg = [{"whiteboard_tag": "bzffx",
                "parameters": {"jira_project_key": "BZFFX", "linked_project_excludes": []}}]
        with patch.object(config, "get_config", return_value=cfg):
            assert config.get_linked_project_excludes("bzffx") == []

    def test_honours_custom_list(self):
        cfg = [{"whiteboard_tag": "fxp",
                "parameters": {"jira_project_key": "FXP", "linked_project_excludes": ["BZFFX", "FOO"]}}]
        with patch.object(config, "get_config", return_value=cfg):
            assert config.get_linked_project_excludes("fxp") == ["BZFFX", "FOO"]


# ---------------------------------------------------------------------------
# get_steps
# ---------------------------------------------------------------------------


class TestGetSteps:
    def test_returns_steps_list(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            steps = config.get_steps("fxp")
        assert "update_issue_summary" in steps
        assert "maybe_update_issue_priority" in steps

    def test_returns_empty_for_unknown_tag(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            assert config.get_steps("nonexistent") == []

    def test_returns_empty_when_steps_absent(self):
        cfg = [{"whiteboard_tag": "bare", "parameters": {}}]
        with patch.object(config, "get_config", return_value=cfg):
            assert config.get_steps("bare") == []


# ---------------------------------------------------------------------------
# get_enabled_flags
# ---------------------------------------------------------------------------


class TestGetEnabledFlags:
    def test_maps_steps_to_flags(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            flags = config.get_enabled_flags("fxp")
        assert "summary" in flags
        assert "priority" in flags
        assert "whiteboard_labels" in flags

    def test_fallback_to_summary_and_whiteboard(self):
        """No recognised steps → fall back to {summary, whiteboard_labels}."""
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            flags = config.get_enabled_flags("fxpe")  # empty steps list
        assert flags == {"summary", "whiteboard_labels"}

    def test_fallback_for_unknown_tag(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            flags = config.get_enabled_flags("nonexistent")
        assert flags == {"summary", "whiteboard_labels"}

    def test_unknown_steps_ignored(self):
        cfg = [
            {
                "whiteboard_tag": "t",
                "parameters": {
                    "steps": {"existing": ["some_unknown_step", "update_issue_summary"]}
                },
            }
        ]
        with patch.object(config, "get_config", return_value=cfg):
            flags = config.get_enabled_flags("t")
        assert "summary" in flags
        # unknown step should not appear
        assert len(flags) == 1

    def test_dependencies_step_maps_to_flag(self):
        # JBI's combined step → the single 'dependencies' flag.
        cfg = [{"whiteboard_tag": "t",
                "parameters": {"steps": {"existing": ["sync_dependencies"]}}}]
        with patch.object(config, "get_config", return_value=cfg):
            assert config.get_enabled_flags("t") == {"dependencies"}

    def test_old_dependency_steps_are_gone(self):
        # The pre-merge step names are no longer recognised.
        assert "sync_dependencies" in config.STEP_TO_FLAG
        assert config.STEP_TO_FLAG["sync_dependencies"] == "dependencies"
        assert "sync_depends_on_links" not in config.STEP_TO_FLAG
        assert "sync_blocks_links" not in config.STEP_TO_FLAG


# ---------------------------------------------------------------------------
# get_mappings
# ---------------------------------------------------------------------------


class TestGetMappings:
    def test_returns_default_maps_when_no_custom_mapping(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            mappings = config.get_mappings("fxp")
        assert mappings["priority_map"] == DEFAULT_PRIORITY_MAP
        assert mappings["severity_map"] == DEFAULT_SEVERITY_MAP
        assert mappings["status_map"] == DEFAULT_STATUS_MAP
        assert mappings["resolution_map"] == DEFAULT_RESOLUTION_MAP

    def test_returns_custom_maps_when_configured(self):
        custom_priority = {"P1": "Blocker", "P2": "Major"}
        cfg = [
            {
                "whiteboard_tag": "custom",
                "parameters": {
                    "jira_project_key": "CUS",
                    "priority_map": custom_priority,
                },
            }
        ]
        with patch.object(config, "get_config", return_value=cfg):
            mappings = config.get_mappings("custom")
        assert mappings["priority_map"] == custom_priority

    def test_returns_defaults_for_unknown_tag(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            mappings = config.get_mappings("unknown")
        assert mappings["priority_map"] == DEFAULT_PRIORITY_MAP

    def test_jira_components_defaults_when_absent(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            mappings = config.get_mappings("fxp")
        jc = mappings["jira_components"]
        assert jc["use_bug_component"] is True
        assert jc["use_bug_product"] is False
        assert jc["use_bug_component_with_product_prefix"] is False
        assert jc["set_custom_components"] == []
        assert jc["create_components"] is False

    def test_jira_components_reads_flags_from_config(self):
        cfg = [
            {
                "whiteboard_tag": "perf",
                "parameters": {
                    "jira_project_key": "PERF",
                    "jira_components": {
                        "use_bug_component": False,
                        "use_bug_component_with_product_prefix": True,
                        "create_components": True,
                    },
                },
            }
        ]
        with patch.object(config, "get_config", return_value=cfg):
            mappings = config.get_mappings("perf")
        jc = mappings["jira_components"]
        assert jc["use_bug_component"] is False
        assert jc["use_bug_component_with_product_prefix"] is True
        assert jc["create_components"] is True

    def test_jira_components_reads_set_custom_components(self):
        cfg = [
            {
                "whiteboard_tag": "dataplat",
                "parameters": {
                    "jira_project_key": "DP",
                    "jira_components": {
                        "set_custom_components": ["Data Platform Infrastructure"],
                    },
                },
            }
        ]
        with patch.object(config, "get_config", return_value=cfg):
            mappings = config.get_mappings("dataplat")
        assert mappings["jira_components"]["set_custom_components"] == [
            "Data Platform Infrastructure"
        ]

    def test_labels_brackets_defaults_to_no(self):
        with patch.object(config, "get_config", return_value=SAMPLE_CONFIG):
            mappings = config.get_mappings("fxp")
        assert mappings["labels_brackets"] == "no"

    def test_labels_brackets_reads_from_config(self):
        cfg = [
            {
                "whiteboard_tag": "both-tag",
                "parameters": {
                    "jira_project_key": "BT",
                    "labels_brackets": "both",
                },
            }
        ]
        with patch.object(config, "get_config", return_value=cfg):
            mappings = config.get_mappings("both-tag")
        assert mappings["labels_brackets"] == "both"


# ---------------------------------------------------------------------------
# _fetch_raw
# ---------------------------------------------------------------------------


class TestFetchRaw:
    def test_fetch_raw_calls_urlopen(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b"yaml content"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = config._fetch_raw()
        assert result == "yaml content"
