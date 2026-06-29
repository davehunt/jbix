"""
Constants shared across the jbi package.

Includes Colors for console output, ASSIGNEE_MAP for Bugzilla→Jira user mapping,
and default field mappings matching JBI's ActionParams defaults from models.py.
"""

import pathlib

import yaml


class Colors:
    """ANSI color codes for console output - subtle scheme"""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

    # Subtle colors (standard, not bright)
    CYAN = '\033[36m'
    YELLOW = '\033[33m'
    WHITE = '\033[37m'
    BLUE = '\033[34m'

    @classmethod
    def disable(cls):
        """Disable colors if output is piped or redirected"""
        for attr in dir(cls):
            if not attr.startswith('_') and attr != 'disable':
                setattr(cls, attr, '')


# Mapping from Bugzilla email to Jira email (None means no Jira account available).
# Loaded from assignee_map.yaml at the repo root (gitignored — it holds personal
# addresses). Copy assignee_map.example.yaml to get started; absent file → empty map.
_ASSIGNEE_MAP_PATH = pathlib.Path(__file__).resolve().parent.parent / "assignee_map.yaml"


def _load_assignee_map() -> dict[str, str | None]:
    """Load the Bugzilla→Jira email map from assignee_map.yaml (empty if missing)."""
    if _ASSIGNEE_MAP_PATH.exists():
        with open(_ASSIGNEE_MAP_PATH) as fh:
            return yaml.safe_load(fh) or {}
    return {}


ASSIGNEE_MAP: dict[str, str | None] = _load_assignee_map()

# Default field mappings matching JBI ActionParams defaults from models.py.
# These are used when a tag's config does not define its own mapping.

DEFAULT_PRIORITY_MAP: dict[str, str] = {
    "": "None",
    "--": "None",
    "P1": "P1",
    "P2": "P2",
    "P3": "P3",
    "P4": "P4",
    "P5": "P5",
}

DEFAULT_SEVERITY_MAP: dict[str, str | None] = {
    "": None,
    "--": None,
    "S1": "S1",
    "S2": "S2",
    "S3": "S3",
    "S4": "S4",
    "N/A": "N/A",
}

# These are intentionally empty per ActionParams — each tag must configure its own.
DEFAULT_STATUS_MAP: dict[str, str] = {}
DEFAULT_RESOLUTION_MAP: dict[str, str] = {}

DEFAULT_ISSUE_TYPE_MAP: dict[str, str] = {
    "defect": "Bug",
    "task": "Task",
}

# Bug types not present in issue_type_map fall back to this Jira issue type
# (matches JBI, where unmapped types default to "Task").
DEFAULT_ISSUE_TYPE = "Task"

# Jira custom field IDs (from ActionParams)
JIRA_SEVERITY_FIELD = "customfield_10319"
JIRA_ESTIMATED_IMPACT_FIELD = "customfield_10441"
