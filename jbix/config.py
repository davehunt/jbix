"""
JBI config loader with 24-hour local cache.

Fetches config.prod.yaml from the mozilla/jira-bugzilla-integration GitHub repo
and parses each whiteboard tag entry to expose steps and field mappings.

Step → sync flag mapping:
  update_issue_summary          → (implicit, always done by JBI)
  maybe_assign_jira_user        → assignee
  maybe_update_issue_status     → status
  maybe_update_issue_resolution → resolution
  maybe_update_issue_priority   → priority
  maybe_update_issue_severity   → severity
  maybe_update_issue_type       → type
  maybe_update_components       → components
  sync_whiteboard_labels        → whiteboard_labels
  sync_keywords_labels          → keyword_labels
  sync_dependencies             → dependencies
  sync_see_also                 → see_also
  sync_duplicates               → duplicates
  sync_regressions              → regressions
"""

import logging
import pathlib
import time
import urllib.request

import yaml

from jbix.constants import (
    DEFAULT_ISSUE_TYPE_MAP,
    DEFAULT_PRIORITY_MAP,
    DEFAULT_RESOLUTION_MAP,
    DEFAULT_SEVERITY_MAP,
    DEFAULT_STATUS_MAP,
)

logger = logging.getLogger(__name__)

SOURCE_URL = (
    "https://raw.githubusercontent.com/mozilla/jira-bugzilla-integration"
    "/main/config/config.prod.yaml"
)
CACHE_PATH = pathlib.Path("cache") / "jbi_config.yaml"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Maps JBI step names to the sync flag names used in jbix.py
STEP_TO_FLAG: dict[str, str] = {
    "update_issue_summary": "summary",
    "maybe_assign_jira_user": "assignee",
    "maybe_update_issue_status": "status",
    "maybe_update_issue_resolution": "resolution",
    "maybe_update_issue_priority": "priority",
    "maybe_update_issue_severity": "severity",
    "maybe_update_issue_type": "type",
    "maybe_update_components": "components",
    "sync_whiteboard_labels": "whiteboard_labels",
    "sync_keywords_labels": "keyword_labels",
    "sync_dependencies": "dependencies",
    "sync_see_also": "see_also",
    "sync_duplicates": "duplicates",
    "sync_regressions": "regressions",
}


def _fetch_raw() -> str:
    """Download the raw YAML config from GitHub."""
    logger.debug(f"Fetching JBI config from {SOURCE_URL}")
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _is_cache_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    age = time.time() - CACHE_PATH.stat().st_mtime
    return age < CACHE_TTL_SECONDS


def _load_from_cache() -> str:
    return CACHE_PATH.read_text(encoding="utf-8")


def _save_to_cache(content: str) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(content, encoding="utf-8")


def get_config() -> list[dict]:
    """Return the full parsed JBI config, using cache when fresh."""
    if _is_cache_fresh():
        logger.debug("Loading JBI config from cache")
        raw = _load_from_cache()
    else:
        try:
            raw = _fetch_raw()
            _save_to_cache(raw)
            logger.info("JBI config: fetched from GitHub")
        except OSError as e:
            # Network failure: fall back to a stale cache if one exists.
            if CACHE_PATH.exists():
                logger.warning(f"JBI config fetch failed ({e}); using stale cache")
                raw = _load_from_cache()
            else:
                raise

    entries = yaml.safe_load(raw)
    if not isinstance(entries, list):
        raise ValueError("Unexpected JBI config format: expected a YAML list")
    return entries


def get_tag_config(whiteboard_tag: str) -> dict | None:
    """Return the full config entry for a whiteboard tag, or None if not found."""
    for entry in get_config():
        if entry.get("whiteboard_tag") == whiteboard_tag:
            return entry
    return None


def all_tags() -> list[str]:
    """Every whiteboard tag defined in the JBI config (sorted, de-duplicated)."""
    return sorted({
        tag for entry in get_config()
        if (tag := entry.get("whiteboard_tag"))
    })


def get_jira_project(whiteboard_tag: str) -> str | None:
    """Return the Jira project key for a whiteboard tag, e.g. 'FXP'."""
    entry = get_tag_config(whiteboard_tag)
    if not entry:
        return None
    return entry.get("parameters", {}).get("jira_project_key")


def get_steps(whiteboard_tag: str) -> list[str]:
    """Return the steps.existing list for a whiteboard tag, or [] if absent."""
    entry = get_tag_config(whiteboard_tag)
    if not entry:
        return []
    return entry.get("parameters", {}).get("steps", {}).get("existing", [])


def get_enabled_flags(whiteboard_tag: str) -> set[str]:
    """
    Return the set of sync flag names enabled by JBI steps for this tag.

    Only includes flags that correspond to a known STEP_TO_FLAG mapping.
    Extension flags (time_tracking, remote_links) are never included here.
    Falls back to {"whiteboard_labels"} when no steps are defined.
    """
    flags = {
        STEP_TO_FLAG[step]
        for step in get_steps(whiteboard_tag)
        if step in STEP_TO_FLAG
    }
    return flags or {"summary", "whiteboard_labels"}


def get_linked_project_excludes(whiteboard_tag: str) -> list[str]:
    """
    Return the projects whose cross-project links should be excluded for this tag.

    Mirrors JBI's ActionParams.linked_project_excludes, defaulting to ["BZFFX"]
    when the tag does not set it. BZFFX-style projects set this to [] in JBI's
    config so they can still link to their own tickets.
    """
    entry = get_tag_config(whiteboard_tag)
    params = entry.get("parameters", {}) if entry else {}
    return params.get("linked_project_excludes", ["BZFFX"])


def get_mappings(whiteboard_tag: str) -> dict:
    """
    Return field mappings for this tag, falling back to ActionParams defaults.

    Returns a dict with keys: priority_map, severity_map, status_map,
    resolution_map, labels_brackets, jira_components.
    """
    entry = get_tag_config(whiteboard_tag)
    params = entry.get("parameters", {}) if entry else {}

    jc_raw = params.get("jira_components", {})
    jira_components = {
        "use_bug_component": jc_raw.get("use_bug_component", True),
        "use_bug_product": jc_raw.get("use_bug_product", False),
        "use_bug_component_with_product_prefix": jc_raw.get(
            "use_bug_component_with_product_prefix", False
        ),
        "set_custom_components": jc_raw.get("set_custom_components", []),
        "create_components": jc_raw.get("create_components", False),
    }

    return {
        "priority_map": params.get("priority_map", DEFAULT_PRIORITY_MAP),
        "severity_map": params.get("severity_map", DEFAULT_SEVERITY_MAP),
        "status_map": params.get("status_map", DEFAULT_STATUS_MAP),
        "resolution_map": params.get("resolution_map", DEFAULT_RESOLUTION_MAP),
        "issue_type_map": params.get("issue_type_map", DEFAULT_ISSUE_TYPE_MAP),
        "labels_brackets": params.get("labels_brackets", "no"),
        "jira_components": jira_components,
    }
