"""Tests for jbix/constants.py."""

import jbix.constants as constants
from jbix.constants import (
    ASSIGNEE_MAP,
    DEFAULT_PRIORITY_MAP,
    DEFAULT_RESOLUTION_MAP,
    DEFAULT_SEVERITY_MAP,
    DEFAULT_STATUS_MAP,
    JIRA_SEVERITY_FIELD,
    Colors,
)


class TestColors:
    def test_colors_are_non_empty_strings(self):
        for attr in ("RESET", "BOLD", "CYAN", "YELLOW", "WHITE", "BLUE"):
            value = getattr(Colors, attr)
            assert isinstance(value, str)
            assert value != ""

    def test_disable_clears_all_codes(self):
        # Save originals
        originals = {
            attr: getattr(Colors, attr)
            for attr in dir(Colors)
            if not attr.startswith("_") and attr != "disable"
        }
        Colors.disable()
        for attr in originals:
            assert getattr(Colors, attr) == ""
        # Restore for other tests
        for attr, val in originals.items():
            setattr(Colors, attr, val)

    def test_disable_does_not_affect_disable_method(self):
        Colors.disable()
        assert callable(Colors.disable)
        # Restore
        Colors.RESET = "\033[0m"
        Colors.BOLD = "\033[1m"
        Colors.UNDERLINE = "\033[4m"
        Colors.CYAN = "\033[36m"
        Colors.YELLOW = "\033[33m"
        Colors.WHITE = "\033[37m"
        Colors.BLUE = "\033[34m"


class TestDefaultMaps:
    def test_default_priority_map_has_p_levels(self):
        for p in ("P1", "P2", "P3", "P4", "P5"):
            assert p in DEFAULT_PRIORITY_MAP
            assert DEFAULT_PRIORITY_MAP[p] == p

    def test_default_priority_map_blank_maps_to_none_string(self):
        assert DEFAULT_PRIORITY_MAP[""] == "None"
        assert DEFAULT_PRIORITY_MAP["--"] == "None"

    def test_default_severity_map_has_s_levels(self):
        for s in ("S1", "S2", "S3", "S4"):
            assert s in DEFAULT_SEVERITY_MAP
            assert DEFAULT_SEVERITY_MAP[s] == s

    def test_default_severity_map_blank_maps_to_none(self):
        assert DEFAULT_SEVERITY_MAP[""] is None
        assert DEFAULT_SEVERITY_MAP["--"] is None

    def test_default_status_map_is_empty(self):
        assert DEFAULT_STATUS_MAP == {}

    def test_default_resolution_map_is_empty(self):
        assert DEFAULT_RESOLUTION_MAP == {}

    def test_jira_severity_field_is_customfield(self):
        assert JIRA_SEVERITY_FIELD.startswith("customfield_")


class TestAssigneeMap:
    def test_assignee_map_is_a_dict(self):
        # Loaded from the (gitignored) assignee_map.yaml; may be empty on a fresh
        # checkout, so only assert the type here.
        assert isinstance(ASSIGNEE_MAP, dict)

    def test_loader_parses_yaml_including_null(self, tmp_path, monkeypatch):
        p = tmp_path / "assignee_map.yaml"
        p.write_text(
            '"person@example.com": "work@example.org"\n'
            '"noaccount@example.com": null\n'
        )
        monkeypatch.setattr(constants, "_ASSIGNEE_MAP_PATH", p)
        loaded = constants._load_assignee_map()
        assert loaded == {
            "person@example.com": "work@example.org",
            "noaccount@example.com": None,
        }

    def test_loader_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, "_ASSIGNEE_MAP_PATH", tmp_path / "nope.yaml")
        assert constants._load_assignee_map() == {}
