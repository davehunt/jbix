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
import os
import pathlib
import re
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

JBI_REPO = "mozilla/jira-bugzilla-integration"
CONFIG_FILE = "config/config.prod.yaml"
SOURCE_URL = f"https://raw.githubusercontent.com/{JBI_REPO}/main/{CONFIG_FILE}"
# The commit at the tip of main, as a bare sha (the ``.sha`` media type), so the
# config can be read at that commit and the report can link to the lines it used.
COMMIT_URL = f"https://api.github.com/repos/{JBI_REPO}/commits/main"
CACHE_PATH = pathlib.Path("cache") / "jbi_config.yaml"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# What the last get_config() read: the raw text and the commit it came from (None
# when unknown), for describe_tag()'s line numbers and permalink.
_loaded: dict = {"raw": None, "commit": None}

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


def _fetch_commit() -> str | None:
    """The sha at the tip of JBI's main branch, or None if GitHub will not say.

    Unauthenticated, GitHub's API allows 60 requests an hour per address, which a
    shared CI runner can exhaust, so ``GITHUB_TOKEN`` is sent when set. Failing
    only costs the permalink: the config is then read from main.
    """
    req = urllib.request.Request(COMMIT_URL, headers={"Accept": "application/vnd.github.sha"})
    if token := os.getenv("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            sha = resp.read().decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"JBI config: could not find main's commit ({e}); reading main")
        return None
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _fetch_raw(commit: str | None = None) -> str:
    """Download the raw YAML config from GitHub, at ``commit`` if given, else main."""
    url = (f"https://raw.githubusercontent.com/{JBI_REPO}/{commit}/{CONFIG_FILE}"
           if commit else SOURCE_URL)
    logger.debug(f"Fetching JBI config from {url}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _commit_path() -> pathlib.Path:
    """Beside the cached config: the commit it was read at."""
    return CACHE_PATH.with_suffix(".commit")


def _cached_commit() -> str | None:
    path = _commit_path()
    sha = path.read_text().strip() if path.is_file() else ""
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _is_cache_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    age = time.time() - CACHE_PATH.stat().st_mtime
    return age < CACHE_TTL_SECONDS


def _load_from_cache() -> str:
    return CACHE_PATH.read_text(encoding="utf-8")


def _save_to_cache(content: str, commit: str | None = None) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(content, encoding="utf-8")
    # Always rewritten, so a config read from main never keeps an older commit.
    _commit_path().write_text(commit or "", encoding="utf-8")


def get_config() -> list[dict]:
    """Return the full parsed JBI config, using cache when fresh."""
    if _is_cache_fresh():
        logger.debug("Loading JBI config from cache")
        raw, commit = _load_from_cache(), _cached_commit()
    else:
        try:
            commit = _fetch_commit()
            raw = _fetch_raw(commit)
            _save_to_cache(raw, commit)
            logger.info(f"JBI config: fetched from GitHub at {commit[:12] if commit else 'main'}")
        except OSError as e:
            # Network failure: fall back to a stale cache if one exists.
            if CACHE_PATH.exists():
                logger.warning(f"JBI config fetch failed ({e}); using stale cache")
                raw, commit = _load_from_cache(), _cached_commit()
            else:
                raise

    entries = yaml.safe_load(raw)
    if not isinstance(entries, list):
        raise ValueError("Unexpected JBI config format: expected a YAML list")
    _loaded.update(raw=raw, commit=commit)
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


# Where JBI defines the steps a tag gets when it sets none. Linked rather than
# copied, so the report never shows a stale copy of JBI's defaults.
MODELS_FILE = "jbi/models.py"
# The kinds of steps the report lists: for a bug JBI already has an issue for,
# and for one it creates an issue for.
STEP_KINDS = ("existing", "new")
# Fields JBI's create_issue sets itself (jbi/jira/service.py create_jira_issue),
# so a new bug has them even without the step that updates them later.
CREATE_ISSUE_SETS = frozenset({"summary", "type"})
# Fields a newly filed bug cannot have yet, so syncing them for existing bugs only
# is not a mismatch. A tag added to an older bug that already has duplicates is
# the exception, and too rare to flag on every tag that leaves them out.
NEVER_ON_NEW = frozenset({"duplicates"})


def compare_steps(new: list | None, existing: list | None) -> dict:
    """Each step for a new and an existing bug side by side, and the fields synced on one only.

    A step is listed with the field it syncs (``STEP_TO_FLAG``) or None, and
    whether each kind runs it; a kind the tag leaves to JBI's defaults is None
    throughout, as those defaults are not copied here, so nothing is flagged.

    A field-syncing step on one side only causes drift that no amount of syncing
    clears for long: synced only for existing bugs, a new issue starts without
    the value; synced only for new bugs, a later change in Bugzilla is never
    copied. Steps that do not sync a field (``create_issue``, the links, the
    comments) belong to one kind by design, so are never flagged.
    """
    names = list(dict.fromkeys([*(new or []), *(existing or [])]))
    rows = [{"step": s, "field": STEP_TO_FLAG.get(s),
             "new": None if new is None else s in new,
             "existing": None if existing is None else s in existing} for s in names]
    if new is None or existing is None:
        return {"rows": rows, "existing_only": [], "new_only": []}

    on_new = {STEP_TO_FLAG[s] for s in new if s in STEP_TO_FLAG}
    on_existing = {STEP_TO_FLAG[s] for s in existing if s in STEP_TO_FLAG}
    # create_issue covers the summary and type for a new bug. Not flagged the other
    # way either: JBI sets the type at creation and, by default, never updates it.
    created = CREATE_ISSUE_SETS if "create_issue" in new else set()
    for r in rows:
        if r["field"] in created and not r["new"]:
            r["new_via"] = "create_issue"  # so the page does not show it as missing
    order = list(STEP_TO_FLAG.values())
    return {
        "rows": rows,
        "existing_only": [f for f in order if f in on_existing - on_new - created - NEVER_ON_NEW],
        "new_only": [f for f in order if f in on_new - on_existing],
    }

# The maps a tag can set, by the field they translate, in the order the report lists them.
_MAPS = {
    "priority": "priority_map",
    "severity": "severity_map",
    "status": "status_map",
    "resolution": "resolution_map",
    "type": "issue_type_map",
}


def _tag_lines(raw: str, whiteboard_tag: str) -> tuple[int, int] | None:
    """The 1-based first and last lines of the tag's entry in the raw config.

    An entry runs from its ``- `` at column 0 to the line before the next one,
    less any blank lines and column-0 comments that belong to what follows.
    """
    lines = raw.splitlines()
    pattern = re.compile(rf"^-?\s*whiteboard_tag:\s*['\"]?{re.escape(whiteboard_tag)}['\"]?\s*(#.*)?$")
    found = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if found is None:
        return None
    start = next((i for i in range(found, -1, -1) if lines[i].startswith("- ")), found)
    end = next((i for i in range(found + 1, len(lines)) if lines[i].startswith("- ")), len(lines))
    end -= 1
    while end > found and (not lines[end].strip() or lines[end].startswith("#")):
        end -= 1
    return start + 1, end + 1


def _plain(value):
    """A YAML value as something JSON can hold, with every mapping key a string."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def describe_tag(whiteboard_tag: str) -> dict:
    """What JBI's config says about a tag, as the drift report shows it.

    Each map and list says whether the tag sets it or JBI's default applies, since
    a reader asking "why does this field drift" usually needs to know which. The
    source names the commit the config was read at and links to the tag's lines
    there, so the link keeps pointing at what this run used after JBI changes.
    """
    entry = get_tag_config(whiteboard_tag) or {}
    params = entry.get("parameters") or {}
    commit = _loaded["commit"]
    lines = _tag_lines(_loaded["raw"], whiteboard_tag) if _loaded["raw"] else None
    if commit:
        url = f"https://github.com/{JBI_REPO}/blob/{commit}/{CONFIG_FILE}"
        if lines:
            url += f"#L{lines[0]}-L{lines[1]}"
    else:
        # Line numbers on main would drift as JBI changes, so link the file alone.
        url = f"https://github.com/{JBI_REPO}/blob/main/{CONFIG_FILE}"
    mappings = get_mappings(whiteboard_tag)
    set_steps = params.get("steps") or {}
    # A kind the tag does not set is None: JBI's default applies, linked to, not copied.
    steps = {kind: _plain(set_steps[kind]) if kind in set_steps else None for kind in STEP_KINDS}
    return {
        "description": entry.get("description"),
        "source": {"commit": commit, "url": url, "lines": list(lines) if lines and commit else None,
                   "defaults_url": f"https://github.com/{JBI_REPO}/blob/{commit or 'main'}/{MODELS_FILE}"},
        "steps": steps,
        "step_check": compare_steps(steps["new"], steps["existing"]),
        "fields": sorted(get_enabled_flags(whiteboard_tag)),
        "linked_project_excludes": {
            "value": get_linked_project_excludes(whiteboard_tag),
            "default": "linked_project_excludes" not in params,
        },
        "maps": {
            field: {"value": _plain(mappings[key]), "default": key not in params}
            for field, key in _MAPS.items()
        },
        "labels_brackets": {"value": mappings["labels_brackets"], "default": "labels_brackets" not in params},
    }
