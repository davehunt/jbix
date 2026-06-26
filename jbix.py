#!/usr/bin/env python3
"""
jbix.py — JBI-aware sync, reverse sync, and health check for Bugzilla ↔ Jira.

Reads JBI's production config to auto-determine which fields to sync for each
whiteboard tag. Supports extension fields (time-tracking, remote-links) and
reverse sync (Jira → Bugzilla) for supported fields.

Usage:
    python jbix.py --tags fxp --mode preview
    python jbix.py --tags fxp fxpe --mode apply
    python jbix.py --tags fxp --time-tracking --dependencies
    python jbix.py --tags fxp --no-priority
    python jbix.py --tags fxp --reverse --priority --severity
    python jbix.py --tags fxp --mode check --link-candidates
"""

import argparse
import csv
import logging
import os
import pathlib
import pickle
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# load_dotenv() must run before importing jbi modules: jbix/jira.py and
# jbix/bugzilla.py read credentials via os.getenv() at module-import time.
from dotenv import load_dotenv

load_dotenv()

import yaml
from jira.exceptions import JIRAError

import jbix.sync as sync_fns
from jbix.bugzilla import (
    BZ_FETCH_BATCH_SIZE,
    BZ_ID_BATCH_SIZE,
    BugzillaClient,
    retry_on_transient,
)
from jbix.config import CACHE_PATH as JBI_CONFIG_CACHE
from jbix.config import (
    all_tags,
    get_enabled_flags,
    get_jira_project,
    get_linked_project_excludes,
    get_mappings,
)
from jbix.constants import Colors
from jbix.groups import resolve_tags
from jbix.health import (
    aggregate_metrics,
    compute_diff_metrics,
    compute_health_metrics,
    print_totals,
    run_diff_check,
    run_health_check,
)
from jbix.jira import JiraClient
from jbix.people import PeopleClient
from jbix.snapshots import has_snapshot, record_snapshot

logger = logging.getLogger(__name__)

JIRA_URL = os.getenv("JIRA_URL", "https://mozilla-hub.atlassian.net")
JIRA_LINK_PREFIX = f"{JIRA_URL}/browse/"
# Pre-migration Jira host. Issue keys were preserved during the migration to
# JIRA_URL, so a legacy-host see_also link is an alias for the same issue. Many
# bugs still reference it; recognizing it keeps linked-detection and orphan
# resolution accurate. Set JIRA_LEGACY_URL empty to disable.
JIRA_LEGACY_URL = os.getenv("JIRA_LEGACY_URL", "https://jira.mozilla.com")
# Browse-URL prefixes recognized when parsing a Jira key OUT of a see_also link.
# (Display/link construction always uses the canonical JIRA_LINK_PREFIX.)
JIRA_BROWSE_PREFIXES = tuple(f"{u}/browse/" for u in (JIRA_URL, JIRA_LEGACY_URL) if u)


def _jira_key_from_link(link) -> str | None:
    """Return the Jira issue key if ``link`` is a recognized browse URL, else None.

    Recognizes both the canonical (``JIRA_URL``) and legacy (``JIRA_LEGACY_URL``)
    hosts — issue keys were preserved across the migration, so a legacy-host URL
    refers to the same issue.
    """
    if not isinstance(link, str):
        return None
    for prefix in JIRA_BROWSE_PREFIXES:
        if link.startswith(prefix):
            return link[len(prefix):]
    return None
BUGZILLA_URL = os.getenv("BUGZILLA_URL", "bugzilla.mozilla.org")
_BUGZILLA_BUG_URL_RE = re.compile(rf"https?://{re.escape(BUGZILLA_URL)}/show_bug\.cgi\?id=(\d+)")
TAGS_YAML = pathlib.Path(__file__).parent / "tags.yaml"
OUTPUT_DIR = pathlib.Path("output")  # dir for updates / broken-link / link-candidate CSVs
CACHE_TTL = 60 * 60  # seconds — cached data expires after 1 hour

# Max Jira keys per `key in (...)` query — keeps the GET URL under CloudFront's
# ~8 KB limit (otherwise the request is rejected with HTTP 414 URI Too Long).
JIRA_KEY_BATCH_SIZE = 100

# Max see_also URLs per OR'd Bugzilla query (orphan resolution). Each condition is
# a full browse URL (~60 chars encoded), so keep the batch small enough that the
# GET URL stays under CloudFront's ~8 KB limit.
BZ_SEE_ALSO_BATCH_SIZE = 50

# Jira fields fetched for each issue — limits response payload
JIRA_FIELDS = ",".join([
    "assignee", "components", "customfield_10319", "customfield_10441",
    "duedate", "issuetype", "issuelinks", "labels", "priority", "resolution",
    "status", "summary", "timeoriginalestimate",
])

# JBI-managed sync flags (can be enabled/disabled via --[no-]flag)
JBI_FLAGS = [
    "summary",
    "assignee",
    "components",
    "whiteboard_labels",
    "keyword_labels",
    "priority",
    "severity",
    "status",
    "resolution",
    "type",
    "dependencies",
    "see_also",
    "duplicates",
    "regressions",
]

# Extension flags: not managed by JBI, always opt-in via CLI
EXTENSION_FLAGS = ["time_tracking", "remote_links"]

# Maps each link-type flag to the bug_info field(s) it reads
_LINK_FLAGS_TO_FIELD: dict[str, str | list[str]] = {
    "dependencies": ["depends_on", "blocks"],  # list[int], list[int]
    "duplicates": ["dupe_of", "duplicates"], # int | False, list[int]
    "see_also":   "see_also_bugs",           # list[int]
    "regressions": ["regressions", "regressed_by"],  # list[int], list[int]
}

ALL_FLAGS = JBI_FLAGS + EXTENSION_FLAGS

# Fields that support reverse sync (Jira → Bugzilla)
REVERSE_SUPPORTED: frozenset[str] = frozenset({"assignee", "summary", "priority", "severity", "whiteboard_labels", "keyword_labels"})


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def load_tags() -> dict:
    """Load tags.yaml (component lists keyed by whiteboard tag)."""
    if not TAGS_YAML.exists():
        return {}
    with open(TAGS_YAML) as f:
        return yaml.safe_load(f) or {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jbix.py",
        description=(
            "JBI-aware sync, reverse sync, and health check for Bugzilla ↔ Jira. "
            "Reads JBI's production config to auto-determine which fields to sync."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python jbix.py --tags fxp --mode preview\n"
            "  python jbix.py --tags fxp --no-priority --time-tracking\n"
            "  python jbix.py --tags fxp --reverse --priority\n"
            "  python jbix.py --tags fxp --mode check --link-candidates\n"
        ),
    )

    parser.add_argument(
        "--tags", nargs="+", metavar="TAG",
        help="Whiteboard tag(s) to process (e.g. fxp fxpe)",
    )
    parser.add_argument(
        "--group", nargs="+", metavar="GROUP",
        help="Tag group(s) from groups.yaml to process (e.g. perf); "
             "expands to its tags and also writes reports/<group>.html",
    )
    parser.add_argument(
        "--all-tags", action="store_true", default=False,
        help="Process every whiteboard tag in the JBI config",
    )
    parser.add_argument(
        "--mode", choices=["preview", "prompt", "apply", "check"], default="check",
        help="check=health+diff (default), preview=dry-run, prompt=confirm each, apply=make changes",
    )

    # JBI-managed flags with --no-X counterparts (BooleanOptionalAction adds --no-X automatically)
    for flag in JBI_FLAGS:
        cli_name = flag.replace("_", "-")
        parser.add_argument(
            f"--{cli_name}",
            dest=flag,
            action=argparse.BooleanOptionalAction,
            default=None,
            help=(
                f"Enable/disable {flag.replace('_', ' ')} sync "
                f"(default: follows JBI config for this tag)"
            ),
        )

    # Extension flags (opt-in only)
    parser.add_argument(
        "--time-tracking", action="store_true", default=False,
        help="Sync time tracking + due dates (extension: not managed by JBI)",
    )
    parser.add_argument(
        "--remote-links", action="store_true", default=False,
        help="Ensure each Jira issue has a remote link back to its Bugzilla bug (extension: not managed by JBI)",
    )

    # Direction
    parser.add_argument(
        "--reverse", action="store_true", default=False,
        help="Reverse sync direction: Jira → Bugzilla (supported fields only)",
    )

    parser.add_argument(
        "--link-candidates", action="store_true", default=False,
        help="Include fuzzy link candidate matching in health check output (slow)",
    )
    parser.add_argument(
        "--threshold", type=int, default=85, metavar="PCT",
        help="Fuzzy match similarity threshold for --link-candidates (default: 85)",
    )

    # Misc
    parser.add_argument(
        "--refresh", action="store_true", default=False,
        help="Bypass the local data cache and fetch fresh data from Bugzilla and Jira",
    )
    parser.add_argument(
        "--debug", action="store_true", default=False,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--manual", action="store_true", default=False,
        help="Ignore JBI config defaults; only sync fields explicitly passed on the command line",
    )
    parser.add_argument(
        "--no-report", action="store_true", default=False,
        help="Skip regenerating report.html after a run that recorded snapshots",
    )
    return parser


def effective_flags(tag: str, args: argparse.Namespace) -> set[str]:
    """
    Compute the effective set of enabled sync flags for a tag.

    JBI-managed flags start from JBI config defaults; CLI --flag / --no-flag
    overrides win. Extension flags are always opt-in via CLI.
    """
    enabled = set() if args.manual else get_enabled_flags(tag)

    for flag in JBI_FLAGS:
        cli_value = getattr(args, flag, None)
        if cli_value is True:
            enabled.add(flag)
        elif cli_value is False:
            enabled.discard(flag)
        # None → keep JBI default

    for flag in EXTENSION_FLAGS:
        if getattr(args, flag, False):
            enabled.add(flag)

    return enabled


# ---------------------------------------------------------------------------
# Sync plan display
# ---------------------------------------------------------------------------

def show_sync_plan(args: argparse.Namespace, tag_configs: list[tuple]) -> None:
    """Display the tag list before running."""
    print(f"\n{Colors.BOLD}Tags:{Colors.RESET}")

    for tag, jira_project, enabled in tag_configs:
        extras = []
        if args.mode == "check" and args.link_candidates:
            extras.append("link-candidates")
        if args.reverse:
            extras.append("reverse")
        extra_str = f"  ({', '.join(extras)})" if extras else ""
        print(f"\n  {Colors.BOLD}[{tag}]{Colors.RESET} → {Colors.CYAN}{jira_project}{Colors.RESET}{extra_str}")

        if not enabled:
            print(f"    {Colors.YELLOW}(no sync fields enabled){Colors.RESET}")
            continue

        if args.reverse and args.mode != "check":
            supported = enabled & REVERSE_SUPPORTED
            skipped = enabled - REVERSE_SUPPORTED
            if supported:
                flag_str = ", ".join(sorted(f.replace("_", "-") for f in supported))
                print(f"    {Colors.CYAN}✓{Colors.RESET} {flag_str}")
            if skipped:
                flag_str = ", ".join(sorted(f.replace("_", "-") for f in skipped))
                print(f"    {Colors.YELLOW}⚠ skipped (no reverse support): {flag_str}{Colors.RESET}")
        else:
            flag_str = ", ".join(sorted(f.replace("_", "-") for f in enabled))
            print(f"    {Colors.CYAN}✓{Colors.RESET} {flag_str}")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_is_fresh(path: pathlib.Path) -> bool:
    return path.is_file() and (time.time() - path.stat().st_mtime) < CACHE_TTL


def _cache_status(path: pathlib.Path, forced: bool = False) -> str:
    """Human-readable cache state for logging, e.g. 'hit (23m old)' or 'miss'."""
    if not path.is_file():
        return "forced (no cache)" if forced else "miss"
    age_secs = time.time() - path.stat().st_mtime
    if forced:
        return f"forced ({int(age_secs / 60)}m old)"
    label = "hit" if age_secs < CACHE_TTL else "expired"
    return f"{label} ({int(age_secs / 60)}m old)"


def _cache_status_short(status: str) -> str:
    """Strip ' old' suffix from cache status for compact display."""
    return status.replace(" old)", ")")


def _print_fetch_summary(
    tag: str,
    bugz_status: str,
    jira_status: str,
    bugz_data: dict,
    jira_issues: dict,
    external_bugs: dict,
) -> None:
    """Print a compact 2–3 line summary of fetched data for a tag."""
    def _status_colored(s: str) -> str:
        color = Colors.YELLOW if any(w in s for w in ("forced", "expired", "miss")) else ""
        return f"{color}{s}{Colors.RESET}" if color else s

    bz_short = _cache_status_short(bugz_status)
    ji_short = _cache_status_short(jira_status)
    bz_count = f"{len(bugz_data):,}"
    ji_count = f"{len(jira_issues):,}"

    print(f"  Bugzilla  {_status_colored(bz_short):<14}  {bz_count:>6} bugs")
    print(f"  Jira      {_status_colored(ji_short):<14}  {ji_count:>6} issues")
    if external_bugs:
        print(f"  Cross-project bugs: {len(external_bugs):,} loaded")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------



def _fetch_bugzilla(tag: str, bugz: BugzillaClient) -> list:
    """Query Bugzilla for all bugs carrying the given whiteboard tag.

    Fetches in batches of BZ_FETCH_BATCH_SIZE to avoid server-side timeouts
    when a tag has a large number of bugs.
    """
    logger.debug(f"Querying Bugzilla for [{tag}] bugs...")
    base_query = bugz.client.build_query(
        status_whiteboard=rf"\[{tag}(-.*)?\]",
        status_whiteboard_type="regexp",
        include_fields=[
            "assigned_to",
            "blocks",
            "component",
            "deadline",
            "depends_on",
            "dupe_of",
            "duplicates",
            "estimated_time",
            "keywords",
            "priority",
            "product",
            "regressed_by",
            "regressions",
            "resolution",
            "see_also",
            "severity",
            "status",
            "summary",
            "type",
            "whiteboard",
        ],
    )
    all_results: list = []
    offset = 0
    while True:
        batch = retry_on_transient(
            lambda: bugz.client.query(
                {**base_query, "limit": BZ_FETCH_BATCH_SIZE, "offset": offset}
            ),
            description=f"Bugzilla query for [{tag}] (offset {offset})",
        )
        all_results.extend(batch)
        if len(batch) < BZ_FETCH_BATCH_SIZE:
            break
        offset += BZ_FETCH_BATCH_SIZE
        logger.info(f"  Fetched {len(all_results)} bugs, continuing...")
    logger.debug(f"Found {len(all_results)} Bugzilla bugs")
    return all_results


def _fetch_jira(jira_project: str, jira: JiraClient) -> dict:
    """Query Jira for all issues in the project."""
    logger.info(f"Querying Jira for project {jira_project}...")
    jql = f"project = {jira_project}"
    jira_issues: dict = {}
    for issue in jira.client.search_issues(jql, maxResults=False, fields=JIRA_FIELDS):
        estimated_impact = getattr(issue.fields, "customfield_10441", None)
        jira_issues[issue.key] = {
            "assignee": issue.fields.assignee,
            "components": issue.fields.components,
            "duedate": getattr(issue.fields, "duedate", None),
            "estimated_impact": (
                estimated_impact.value
                if estimated_impact and hasattr(estimated_impact, "value")
                else None
            ),
            "key": issue.key,
            "labels": issue.fields.labels,
            "links": issue.fields.issuelinks,
            "priority": getattr(issue.fields.priority, "name", None),
            "resolution": getattr(issue.fields.resolution, "name", None),
            "severity": issue.fields.customfield_10319,
            "status": getattr(issue.fields.status, "name", None),
            "issuetype": issue.fields.issuetype.name if issue.fields.issuetype else None,
            "summary": issue.fields.summary,
            "timeoriginalestimate": getattr(issue.fields, "timeoriginalestimate", None),
            "url": f"{JIRA_URL}/browse/{issue.key}",
        }
    logger.debug(f"Found {len(jira_issues)} Jira issues")
    return jira_issues


def _exclude_projects(keys: list[str], excludes: list[str]) -> list[str]:
    """Drop Jira keys whose project prefix (before the hyphen) is in excludes.

    Mirrors JBI's linked_project_excludes filter so we never create cross-project
    links to projects (e.g. BZFFX) that JBI deliberately omits.
    """
    if not excludes:
        return keys
    return [k for k in keys if k.split("-")[0] not in excludes]


def _bugzilla_results_to_dict(bugzilla_results: list, jira_project: str, excludes: list[str]) -> dict:
    """Convert raw Bugzilla bug objects to a cacheable pre-merge dict.

    bug_info["jira"] holds {jira_key: {}} placeholders for each linked key.
    Call _merge_bug_data() afterwards to populate those placeholders.
    """
    bugzilla_bugs: dict = {}
    for b in bugzilla_results:
        jira_keys: dict = {}
        see_also_bugs: list = []
        see_also_jira_keys: list = []
        for link in b.see_also:
            key = _jira_key_from_link(link)
            if key:
                if key.startswith(jira_project):
                    jira_keys.setdefault(key, {})
                else:
                    see_also_jira_keys.append(key)
            elif isinstance(link, str):
                m = _BUGZILLA_BUG_URL_RE.search(link)
                if m:
                    see_also_bugs.append(int(m.group(1)))
        bugzilla_bugs[b.id] = {
            "assignee": b.assigned_to,
            "blocks": b.blocks,
            "component": b.component,
            "deadline": b.deadline if hasattr(b, "deadline") else None,
            "depends_on": b.depends_on,
            "dupe_of": hasattr(b, "dupe_of") and b.dupe_of,
            "duplicates": list(b.duplicates) if hasattr(b, "duplicates") else [],
            "estimated_time": b.estimated_time if hasattr(b, "estimated_time") else None,
            "id": b.id,
            "jira": jira_keys,
            "keywords": b.keywords,
            "regressed_by": list(b.regressed_by) if hasattr(b, "regressed_by") else [],
            "regressions": list(b.regressions) if hasattr(b, "regressions") else [],
            "see_also_bugs": see_also_bugs,
            "see_also_jira_keys": _exclude_projects(see_also_jira_keys, excludes),
            "priority": b.priority,
            "product": b.product,
            "resolution": b.resolution,
            "severity": b.severity,
            "status": b.status,
            "summary": b.summary,
            "type": getattr(b, "type", None),
            "url": f"https://bugzil.la/{b.id}",
            "whiteboard": b.whiteboard,
        }
    return bugzilla_bugs


def _merge_bug_data(bugz_data: dict, jira_issues: dict, dropped: list | None = None) -> dict:
    """Populate bug_info["jira"] with full jira_info dicts from jira_issues.

    Modifies bugz_data in-place and returns it. Keys that are not present in
    jira_issues are removed from bug_info["jira"]. When ``dropped`` is provided,
    each removed (same-project) key is appended as ``(bug_id, bug_url, jira_key)``
    so callers can flag broken see-also links to deleted/moved Jira issues.
    """
    for bug_info in bugz_data.values():
        for jira_key in list(bug_info["jira"]):
            if jira_key in jira_issues:
                bug_info["jira"][jira_key] = jira_issues[jira_key]
            else:
                if dropped is not None:
                    dropped.append((bug_info["id"], bug_info["url"], jira_key))
                del bug_info["jira"][jira_key]
    return bugz_data


def _collect_external_ids(bugzilla_bugs: dict, enabled: set) -> set[int]:
    """Return bug IDs referenced by link fields that are not already in bugzilla_bugs."""
    active_fields = []
    for flag, f in _LINK_FLAGS_TO_FIELD.items():
        if flag in enabled:
            if isinstance(f, list):
                active_fields.extend(f)
            else:
                active_fields.append(f)
    if not active_fields:
        return set()
    external: set[int] = set()
    for bug_info in bugzilla_bugs.values():
        if bug_info.get("external"):
            continue
        for field in active_fields:
            val = bug_info.get(field)
            if not val:
                continue
            if isinstance(val, list):
                external.update(v for v in val if v not in bugzilla_bugs)
            elif val not in bugzilla_bugs:   # dupe_of is a single int
                external.add(val)
    return external


# Sentinel stored in the resolution cache for keys confirmed not to exist in Jira.
_KEY_MISSING = object()


def _resolve_jira_keys(keys: set[str], jira_client, cache: dict) -> dict[str, str]:
    """Map each Jira key to its current key, following project renames.

    Returns ``{input_key: current_key}`` for keys that resolve to a live issue;
    keys that do not exist are omitted (so this doubles as existence verification).
    Jira keeps old keys after a project rename and redirects them, so a key not
    returned verbatim by a batch search is resolved with a direct GET (e.g. a
    renamed project: GENAI-1608 → AIFE-1608).

    ``cache`` is consulted/populated first (value = current key, or ``_KEY_MISSING``)
    so no key is looked up more than once per invocation.
    """
    mapping: dict[str, str] = {}
    todo: list[str] = []
    for k in keys:
        cached = cache.get(k, None)
        if cached is _KEY_MISSING:
            continue
        if cached is not None:
            mapping[k] = cached
        else:
            todo.append(k)
    if not todo:
        return mapping

    todo.sort()
    found_current: set[str] = set()
    for i in range(0, len(todo), JIRA_KEY_BATCH_SIZE):
        batch = todo[i:i + JIRA_KEY_BATCH_SIZE]
        try:
            found_current.update(
                issue.key for issue in jira_client.search_issues(
                    f"key in ({','.join(batch)})", maxResults=False, fields="key",
                )
            )
        except JIRAError:
            pass  # an unknown/renamed project prefix can fail the batch; resolve per-key below

    for k in todo:
        if k in found_current:
            cache[k] = mapping[k] = k
            continue
        try:
            current = jira_client.issue(k, fields="key").key
        except JIRAError:
            logger.warning(f"Jira key {k} not found (renamed or deleted); skipping")
            cache[k] = _KEY_MISSING
            continue
        cache[k] = mapping[k] = current
    return mapping


def _resolve_see_also_keys(bugzilla_bugs: dict, jira_client, cache: dict) -> None:
    """Rewrite see_also_jira_keys to current keys in place, handling project renames.

    Unresolved keys are kept as-is (conservative); only actual renames mutate the data.
    """
    keys = {k for b in bugzilla_bugs.values() for k in (b.get("see_also_jira_keys") or [])}
    mapping = _resolve_jira_keys(keys, jira_client, cache)
    renamed = {k: v for k, v in mapping.items() if k != v}
    if not renamed:
        return
    for b in bugzilla_bugs.values():
        sa = b.get("see_also_jira_keys")
        if sa:
            b["see_also_jira_keys"] = [mapping.get(k, k) for k in sa]
    logger.info(f"Resolved renamed Jira see-also keys: {renamed}")


# Broken-link directions. "bug→jira": a Bugzilla see_also points at a Jira issue
# that no longer exists. "jira→bug": a Jira issue still carries the tag's labels
# but no fetched bug links it (e.g. the bug's whiteboard tag was removed).
_DIR_BUG_TO_JIRA = "bug→jira"
_DIR_JIRA_TO_BUG = "jira→bug"


def _label_matches_tag(labels, tag: str) -> bool:
    """True if any label belongs to ``tag``'s whiteboard family.

    JBI derives the Jira label from the whole whiteboard bracket content
    (``[fxpe]`` → ``fxpe``, ``[fxpe-moco]`` → ``fxpe-moco``), and matches the
    tag as ``\\[tag(-[^\\]]*)*\\]`` — boundary ``-`` or end, never ``:``. So a
    label belongs to the tag iff (bracket-stripped, lowercased) it equals the
    tag or starts with ``tag-``.
    """
    t = tag.lower()
    for raw in labels or []:
        norm = raw.strip("[]").lower()
        if norm == t or norm.startswith(f"{t}-"):
            return True
    return False


def _whiteboard_has_tag(whiteboard: str | None, tag: str) -> bool:
    """True if ``whiteboard`` still carries ``[tag]`` per JBI's match semantics."""
    if not whiteboard:
        return False
    return re.search(rf"\[{re.escape(tag)}(-[^\]]*)*\]", whiteboard) is not None


def _see_also_exact_match(bug, jira_key: str) -> bool:
    """True if ``bug``'s see_also contains the exact Jira issue URL for ``jira_key``.

    ``substring`` queries narrow the server-side scan but over-match
    (``browse/FXP-140`` is a substring of ``browse/FXP-1407``), so candidates are
    exact-matched here on the key after the prefix. Both the canonical and legacy
    Jira hosts are recognized (via ``_jira_key_from_link``).
    """
    return any(
        _jira_key_from_link(link) == jira_key for link in (bug.see_also or [])
    )


def _find_bugs_via_see_also(jira_keys: list[str], bugz: BugzillaClient) -> dict[str, dict]:
    """Resolve each Jira key to the lowest-id Bugzilla bug whose see_also references it.

    Returns ``{jira_key: {"id", "whiteboard"}}`` for keys that have a referencing
    bug (others are omitted). Queries are batched: each batch OR's together up to
    ``BZ_SEE_ALSO_BATCH_SIZE`` ``see_also substring`` conditions into one query
    (vs. one query per key), then exact-matches each key in Python. This keeps the
    cost ~``len(keys) / BZ_SEE_ALSO_BATCH_SIZE`` queries instead of one per key —
    essential for tags on large shared projects (e.g. proton → FIDEFE).
    """
    keys = sorted(set(jira_keys))
    found: dict[str, dict] = {}
    for i in range(0, len(keys), BZ_SEE_ALSO_BATCH_SIZE):
        chunk = keys[i:i + BZ_SEE_ALSO_BATCH_SIZE]
        query: dict = {"include_fields": ["id", "whiteboard", "see_also"]}
        n = 1
        query[f"f{n}"], query[f"j{n}"] = "OP", "OR"
        n += 1
        for key in chunk:
            query[f"f{n}"] = "see_also"
            query[f"o{n}"] = "substring"
            # Host-agnostic substring matches both the canonical and legacy Jira
            # hosts; _see_also_exact_match() then confirms the exact key.
            query[f"v{n}"] = f"/browse/{key}"
            n += 1
        query[f"f{n}"] = "CP"
        bugs = retry_on_transient(
            lambda q=query: bugz.client.query(q),
            description=f"Bugzilla see_also lookup ({len(chunk)} keys)",
        ) or []
        for key in chunk:
            matches = [b for b in bugs if _see_also_exact_match(b, key)]
            if matches:
                b = min(matches, key=lambda x: x.id)
                found[key] = {"id": b.id, "whiteboard": b.whiteboard}
    return found


def find_orphaned_issues(
    tag: str, bugzilla_bugs: dict, jira_issues: dict, bugz: BugzillaClient
) -> list[dict]:
    """Return Jira issues labelled for ``tag`` that no fetched bug links to.

    These are the mirror image of broken see-also links: the Jira issue keeps
    its sticky ``bugzilla`` + tag labels, but the originating bug no longer
    carries the ``[tag]`` whiteboard (so it falls out of the fetch). Each orphan
    is resolved back to its bug via ``see_also`` to record the bug id and reason
    (``whiteboard-removed`` if the bug still references it but dropped the tag;
    ``stale-label`` if nothing on the Bugzilla side references it any more).
    """
    linked = {
        k for b in bugzilla_bugs.values() if not b.get("external")
        for k, v in (b.get("jira") or {}).items() if isinstance(v, dict)
    }
    candidates = sorted(
        key for key, info in (jira_issues or {}).items()
        if key not in linked
        and any(lbl.lower() == "bugzilla" for lbl in info.get("labels", []))
        and _label_matches_tag(info.get("labels"), tag)
    )

    found = _find_bugs_via_see_also(candidates, bugz)
    rows = []
    for key in candidates:
        bug = found.get(key)
        if bug is None:
            bug_id, bug_url, reason = "", "", "stale-label"
        else:
            bug_id = bug["id"]
            bug_url = f"https://bugzil.la/{bug['id']}"
            reason = (
                "whiteboard-removed"
                if not _whiteboard_has_tag(bug["whiteboard"], tag)
                else "unlinked"
            )
        rows.append({
            "direction": _DIR_JIRA_TO_BUG,
            "bug_id": bug_id,
            "bug_url": bug_url,
            "jira_key": key,
            "jira_url": f"{JIRA_LINK_PREFIX}{key}",
            "reason": reason,
        })
    return rows


def find_broken_links(
    bugzilla_bugs: dict, dropped: list, jira_client, cache: dict,
    *, tag: str, jira_issues: dict, bugz: BugzillaClient,
) -> list[dict]:
    """Return broken links in both directions, tagged with a ``direction`` field.

    ``bug→jira`` rows: a Bugzilla see-also pointing at a non-existent Jira issue.
    Candidates are (a) ``dropped`` same-project keys removed by ``_merge_bug_data``
    (issue absent from the project fetch) and (b) each non-external bug's
    ``see_also_jira_keys`` (cross-project). Every candidate is GET-verified via
    ``_resolve_jira_keys``; a key that still resolves (e.g. a rename, or a loose
    prefix match like ``FXPE-1`` on an ``FXP`` bug) is NOT reported.

    ``jira→bug`` rows: orphaned Jira issues from :func:`find_orphaned_issues`.
    """
    key_to_bugs: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for bug_id, bug_url, key in dropped:
        key_to_bugs[key].append((bug_id, bug_url))
    for bug_info in bugzilla_bugs.values():
        if bug_info.get("external"):
            continue
        for key in bug_info.get("see_also_jira_keys") or []:
            key_to_bugs[key].append((bug_info["id"], bug_info["url"]))

    rows = []
    if key_to_bugs:
        mapping = _resolve_jira_keys(set(key_to_bugs), jira_client, cache)
        for key, bugs in key_to_bugs.items():
            if key in mapping:
                continue  # resolves to a live issue → not broken
            for bug_id, bug_url in bugs:
                rows.append({
                    "direction": _DIR_BUG_TO_JIRA,
                    "bug_id": bug_id,
                    "bug_url": bug_url,
                    "jira_key": key,
                    "jira_url": f"{JIRA_LINK_PREFIX}{key}",
                    "reason": "dead-jira",
                })
        rows.sort(key=lambda r: (r["bug_id"], r["jira_key"]))

    rows.extend(find_orphaned_issues(tag, bugzilla_bugs, jira_issues, bugz))
    return rows


def print_broken_links(rows: list[dict]) -> None:
    """Print the broken-links section (both directions) for a tag's health check."""
    print(f"\n{Colors.BOLD}Broken Jira links{Colors.RESET} "
          f"(see_also → dead issue, or orphaned issue → untagged bug):")
    if not rows:
        print(f"  {Colors.CYAN}None{Colors.RESET}")
        return
    print(f"  {Colors.YELLOW}{len(rows)}{Colors.RESET} broken link(s) found:")
    for r in rows[:20]:
        if r.get("direction") == _DIR_JIRA_TO_BUG:
            bug = f"bug {r['bug_id']}" if r["bug_id"] != "" else "no bug"
            print(f"    {Colors.YELLOW}{r['jira_key']}{Colors.RESET} → {bug} "
                  f"({r['reason']})  "
                  f"({Colors.BLUE}{Colors.UNDERLINE}{r['jira_url']}{Colors.RESET})")
        else:
            print(f"    bug {r['bug_id']} → {Colors.YELLOW}{r['jira_key']}{Colors.RESET}  "
                  f"({Colors.BLUE}{Colors.UNDERLINE}{r['bug_url']}{Colors.RESET})")
    if len(rows) > 20:
        print(f"    … and {len(rows) - 20} more")


def write_broken_links_csv(tag: str, rows: list[dict]) -> None:
    """Write broken_links_<tag>.csv (nothing written when there are no broken links)."""
    if not rows:
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"broken_links_{tag}.csv"
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["direction", "bug_id", "bug_url", "jira_key", "jira_url", "reason"],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {Colors.CYAN}→ Wrote {output_path}{Colors.RESET}")


def _fetch_external_bugs(
    external_ids: set[int], bugz: BugzillaClient, jira_client, excludes: list[str], cache: dict
) -> dict:
    """Fetch Bugzilla+Jira data for bugs referenced by link fields but not in bugzilla_bugs."""
    if not external_ids:
        return {}
    logger.debug(f"Fetching {len(external_ids)} external referenced bugs from Bugzilla...")
    id_list = list(external_ids)
    raw_bugs = []
    for i in range(0, len(id_list), BZ_ID_BATCH_SIZE):
        batch_ids = id_list[i:i + BZ_ID_BATCH_SIZE]
        raw_bugs += retry_on_transient(
            lambda: bugz.client.getbugs(batch_ids, include_fields=["id", "see_also"]),
            description=f"Bugzilla getbugs ({len(batch_ids)} external ids)",
        )

    bug_to_keys: dict[int, list[str]] = {}
    all_jira_keys: set[str] = set()
    for b in raw_bugs:
        keys = []
        for link in b.see_also:
            key = _jira_key_from_link(link)
            if key:
                keys.append(key)
        keys = _exclude_projects(keys, excludes)
        all_jira_keys.update(keys)
        bug_to_keys[b.id] = keys

    # Resolve to current keys (and verify existence); renamed keys map old → new.
    mapping = _resolve_jira_keys(all_jira_keys, jira_client, cache)

    return {
        b.id: {
            "id": b.id,
            "external": True,
            "jira": {mapping[k]: {} for k in bug_to_keys[b.id] if k in mapping},
        }
        for b in raw_bugs
    }


def _fetch_remote_links_parallel(
    jira_issues: dict, bugz_data: dict, jira: JiraClient
) -> None:
    """Fetch and attach remote links for paired Jira issues (in-place).

    Only fetches for keys that appear in a bug's see_also AND exist in jira_issues,
    avoiding unnecessary calls for unlinked issues. Fetched sequentially with a small
    delay to avoid Jira rate limiting (350-token bucket).
    """
    paired_keys = sorted(
        key
        for bug_info in bugz_data.values()
        for key in bug_info["jira"]
        if key in jira_issues
    )
    total = len(paired_keys)
    logger.info(f"Fetching remote links for {total} paired Jira issues...")
    for i, key in enumerate(paired_keys, 1):
        if i % 50 == 0:
            logger.info(f"  [{i}/{total}] fetching remote links...")
        jira_issues[key]["remote_links"] = jira.client.remote_links(key)
        time.sleep(0.15)


def fetch_data(
    tag: str,
    jira_project: str,
    bugz: BugzillaClient,
    jira: JiraClient,
    need_remote_links: bool = False,
) -> dict:
    """
    Query Bugzilla and Jira in parallel, then merge into a unified dict.

    Bugzilla is queried by whiteboard tag; Jira by project + 'bugzilla' label.
    The merge links each Bugzilla bug to any Jira issues found in its see_also.

    Returns:
        bugzilla_bugs: {bug_id: bug_info} where bug_info["jira"] maps
                       jira_key → jira_info dict.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        bz_future = pool.submit(_fetch_bugzilla, tag, bugz)
        jira_future = pool.submit(_fetch_jira, jira_project, jira)
        bugzilla_results = bz_future.result()
        jira_issues = jira_future.result()
    bugz_data = _bugzilla_results_to_dict(
        bugzilla_results, jira_project, get_linked_project_excludes(tag)
    )
    if need_remote_links:
        _fetch_remote_links_parallel(jira_issues, bugz_data, jira)
    return _merge_bug_data(bugz_data, jira_issues)


# ---------------------------------------------------------------------------
# Sync runners
# ---------------------------------------------------------------------------

def run_forward_sync(
    bugzilla_bugs: dict,
    enabled: set[str],
    mappings: dict,
    jira_project: str,
    bugz: BugzillaClient,
    jira: JiraClient,
    project_components=None,
    people=None,
) -> None:
    """Run forward sync (Bugzilla → Jira) for enabled fields."""
    # Each forward-sync pass starts a fresh link-dedup scope so a stale link is
    # only reported once even though both linked bugs reference it.
    jira.handled_link_ids.clear()
    for bug_info in bugzilla_bugs.values():
        if bug_info.get("external"):
            continue
        for jira_info in bug_info["jira"].values():
            if not isinstance(jira_info, dict):
                continue

            if "assignee" in enabled:
                sync_fns.sync_assignee(bug_info, jira_info, bugz, jira, people=people)
            if "components" in enabled and project_components is not None:
                sync_fns.sync_components(
                    jira_project, project_components, bug_info, jira_info, jira,
                    mappings["jira_components"]
                )
            if "priority" in enabled:
                sync_fns.sync_priority(bug_info, jira_info, jira, mappings["priority_map"])
            if "severity" in enabled:
                sync_fns.sync_severity(bug_info, jira_info, jira, mappings["severity_map"])
            if "status" in enabled:
                resolution_map = mappings.get("resolution_map") if "resolution" in enabled else None
                sync_fns.sync_status(bug_info, jira_info, jira, mappings["status_map"], resolution_map=resolution_map)
            if "resolution" in enabled:
                sync_fns.sync_resolution(bug_info, jira_info, jira, mappings["resolution_map"])
            if "type" in enabled:
                sync_fns.sync_issue_type(bug_info, jira_info, jira, mappings["issue_type_map"])
            if "summary" in enabled:
                sync_fns.sync_summary(bug_info, jira_info, jira)
            if "whiteboard_labels" in enabled:
                sync_fns.sync_whiteboard_labels(bug_info, jira_info, jira, mappings["labels_brackets"])
            if "keyword_labels" in enabled:
                sync_fns.sync_keyword_labels(bug_info, jira_info, jira)
            if "time_tracking" in enabled:
                sync_fns.sync_time_tracking(bug_info, jira_info, jira)
            if "dependencies" in enabled:
                sync_fns.sync_dependencies(bug_info, jira_info, bugzilla_bugs, jira)
            if "duplicates" in enabled:
                sync_fns.sync_duplicates(bug_info, jira_info, bugzilla_bugs, jira)
            if "regressions" in enabled:
                sync_fns.sync_regressions(bug_info, jira_info, bugzilla_bugs, jira)
            if "see_also" in enabled:
                sync_fns.sync_see_also(bug_info, jira_info, bugzilla_bugs, jira)
            if "remote_links" in enabled:
                sync_fns.sync_remote_links(bug_info, jira_info, jira)


def run_reverse_sync(
    bugzilla_bugs: dict,
    enabled: set[str],
    mappings: dict,
    bugz: BugzillaClient,
) -> None:
    """Run reverse sync (Jira → Bugzilla) for supported fields."""
    active = enabled & REVERSE_SUPPORTED
    skipped = enabled - REVERSE_SUPPORTED

    if skipped:
        logger.warning(
            "Reverse sync not supported for: %s — skipped",
            ", ".join(sorted(skipped)),
        )
    if not active:
        logger.warning("No supported fields for reverse sync")
        return

    for bug_info in bugzilla_bugs.values():
        if bug_info.get("external"):
            continue
        for jira_info in bug_info["jira"].values():
            if not isinstance(jira_info, dict):
                continue
            if "assignee" in active:
                sync_fns.reverse_sync_assignee(bug_info, jira_info, bugz)
            if "priority" in active:
                sync_fns.reverse_sync_priority(bug_info, jira_info, bugz, mappings["priority_map"])
            if "severity" in active:
                sync_fns.reverse_sync_severity(bug_info, jira_info, bugz, mappings["severity_map"])
            if "summary" in active:
                sync_fns.reverse_sync_summary(bug_info, jira_info, bugz)
            if "whiteboard_labels" in active or "keyword_labels" in active:
                sync_fns.reverse_sync_whiteboard_labels(bug_info, jira_info, bugz)


# ---------------------------------------------------------------------------
# CSV audit trail
# ---------------------------------------------------------------------------

def write_updates_csv(updates: list[dict]) -> None:
    """Overwrite updates.csv with the current run's audit trail."""
    if not updates:
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "updates.csv"
    fieldnames = [
        "direction",
        "bug_url", "bug_status", "bug_product", "bug_component",
        "jira_url",
        "field", "before", "after",
    ]
    rows = []
    for u in updates:
        if u.get("direction") == "jira→bugzilla":
            field, before, after = u["bug_field"], u["bug_before"], u["bug_after"]
        else:
            field, before, after = u["jira_field"], u["jira_before"], u["jira_after"]
        rows.append({
            "direction": u.get("direction", "bugzilla→jira"),
            "bug_url": u["bug_url"],
            "bug_status": u["bug_status"],
            "bug_product": u["bug_product"],
            "bug_component": u["bug_component"],
            "jira_url": u["jira_url"],
            "field": field,
            "before": before,
            "after": after,
        })
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote {len(updates)} update(s) to {output_path}")


# ---------------------------------------------------------------------------
# Health check helper
# ---------------------------------------------------------------------------

def _get_components_for_tag(tag: str, tags: dict) -> list[str] | None:
    """
    Look up the component list from tags.yaml for a given whiteboard tag.

    Returns None if not found (health check will skip the Bugzilla section).
    """
    entry = tags.get(tag)
    if entry:
        comps = entry.get("components")
        if isinstance(comps, (list, set)):
            return list(comps)
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
        force=True,
    )

    if not sys.stdout.isatty():
        Colors.disable()

    if args.refresh and JBI_CONFIG_CACHE.exists():
        JBI_CONFIG_CACHE.unlink()

    tags = load_tags()

    explicit_tags = list(args.tags or [])
    if args.all_tags:
        explicit_tags += all_tags()
    run_tags = resolve_tags(explicit_tags, args.group or [])
    if not run_tags:
        logger.error("No tags to process — pass --tags, --group, and/or --all-tags.")
        sys.exit(1)

    # Resolve each tag to its Jira project + effective flags
    tag_configs: list[tuple[str, str, set[str]]] = []
    for tag in run_tags:
        jira_project = get_jira_project(tag)
        if not jira_project:
            logger.warning(f"Tag {tag!r} not found in JBI config — skipping")
            continue
        enabled = effective_flags(tag, args)
        tag_configs.append((tag, jira_project, enabled))

    if not tag_configs:
        logger.error("No valid tags found in JBI config. Exiting.")
        sys.exit(1)

    show_sync_plan(args, tag_configs)

    is_check = args.mode == "check"
    bugz = BugzillaClient(mode="preview" if is_check else args.mode)
    jira = JiraClient(mode="preview" if is_check else args.mode)
    people = PeopleClient(token) if (token := os.getenv("PEOPLE_API_TOKEN")) else None
    if people:
        logger.info("People directory lookup enabled")
    if is_check:
        for _mod in ("jbix.jira", "jbix.bugzilla", "jbix.sync"):
            logging.getLogger(_mod).setLevel(logging.ERROR)
    all_updates: list[dict] = []
    per_tag_metrics: list[dict] = []
    run_ts = datetime.now()       # shared by every snapshot recorded this run
    captured = False              # True once any tag records a snapshot
    # Shared across tags so a renamed key is resolved (and missing keys recorded) only once.
    resolved_key_cache: dict = {}

    for tag, jira_project, enabled in tag_configs:
        print(f"\n{Colors.BOLD}[{tag}]{Colors.RESET} → {Colors.CYAN}{jira_project}{Colors.RESET}")
        print("─" * 60)

        mappings = get_mappings(tag)
        excludes = get_linked_project_excludes(tag)

        # ── Check mode: pre-compute components (health check runs after fetch) ─
        components = None
        if is_check:
            components = _get_components_for_tag(tag, tags)

        # ── Sync ────────────────────────────────────────────────────────────
        cache_bugz_path = pathlib.Path("cache", f"{tag}_bugzilla.pickle")
        cache_jira_path = pathlib.Path("cache", f"{tag}_jira.pickle")
        cache_bugz_path.parent.mkdir(parents=True, exist_ok=True)
        need_remote_links = "remote_links" in enabled

        # Pre-fetch project components before bulk Jira queries — the session
        # can become stale after many search_issues calls if fetched afterwards.
        project_components = None
        if not args.reverse and "components" in enabled:
            project_components = jira.client.project_components(jira_project)

        # Determine freshness of each cache independently
        bugz_fresh = not args.refresh and _cache_is_fresh(cache_bugz_path)
        jira_fresh = not args.refresh and _cache_is_fresh(cache_jira_path)
        # Record a snapshot only when fresh data was fetched (cache busted), or when
        # this tag has no history yet — avoids redundant identical rows and the extra
        # Bugzilla count queries on warm-cache runs.
        should_record = (not bugz_fresh) or (not jira_fresh) or not has_snapshot(tag)
        jira_meta: dict | None = None

        # Peek at the Jira cache to verify remote links were included if needed
        if jira_fresh:
            with cache_jira_path.open("rb") as f:
                jira_meta = pickle.load(f)
            if need_remote_links and not jira_meta.get("has_remote_links"):
                logger.info(f"[{tag}] Jira cache lacks remote links — re-fetching")
                jira_fresh = False

        bugz_cache_status = _cache_status(cache_bugz_path, forced=args.refresh)
        jira_cache_status = _cache_status(cache_jira_path, forced=args.refresh)

        if bugz_fresh and jira_fresh:
            with cache_bugz_path.open("rb") as f:
                bugz_data = pickle.load(f)
            jira_issues = jira_meta["issues"]

        elif not bugz_fresh and not jira_fresh:
            # Both stale — fetch in parallel
            with ThreadPoolExecutor(max_workers=2) as pool:
                bz_future = pool.submit(_fetch_bugzilla, tag, bugz)
                jira_future = pool.submit(_fetch_jira, jira_project, jira)
                bugzilla_results = bz_future.result()
                jira_issues = jira_future.result()
            bugz_data = _bugzilla_results_to_dict(bugzilla_results, jira_project, excludes)
            if need_remote_links:
                _fetch_remote_links_parallel(jira_issues, bugz_data, jira)
            with cache_bugz_path.open("wb") as f:
                pickle.dump(bugz_data, f)
            with cache_jira_path.open("wb") as f:
                pickle.dump({"issues": jira_issues, "has_remote_links": need_remote_links}, f)

        elif not bugz_fresh:
            # Only Bugzilla stale
            bugzilla_results = _fetch_bugzilla(tag, bugz)
            bugz_data = _bugzilla_results_to_dict(bugzilla_results, jira_project, excludes)
            with cache_bugz_path.open("wb") as f:
                pickle.dump(bugz_data, f)
            jira_issues = jira_meta["issues"]

        else:
            # Only Jira stale
            with cache_bugz_path.open("rb") as f:
                bugz_data = pickle.load(f)
            jira_issues = _fetch_jira(jira_project, jira)
            if need_remote_links:
                _fetch_remote_links_parallel(jira_issues, bugz_data, jira)
            with cache_jira_path.open("wb") as f:
                pickle.dump({"issues": jira_issues, "has_remote_links": need_remote_links}, f)

        dropped_jira_keys: list = []
        bugzilla_bugs = _merge_bug_data(bugz_data, jira_issues, dropped=dropped_jira_keys)

        external_bugs: dict = {}
        if enabled & set(_LINK_FLAGS_TO_FIELD):
            external_ids = _collect_external_ids(bugzilla_bugs, enabled)
            if external_ids:
                external_bugs = _fetch_external_bugs(external_ids, bugz, jira.client, excludes, resolved_key_cache)
                bugzilla_bugs.update(external_bugs)

        # Resolve renamed cross-project see-also keys so existing links are recognised as correct.
        if "see_also" in enabled:
            _resolve_see_also_keys(bugzilla_bugs, jira.client, resolved_key_cache)

        _print_fetch_summary(tag, bugz_cache_status, jira_cache_status, bugz_data, jira_issues, external_bugs)

        if is_check:
            health_metrics = run_health_check(
                tag=tag,
                jira_project=jira_project,
                bugz=bugz,
                jira_client=jira.client,
                components=components,
                link_candidates=args.link_candidates,
                threshold=args.threshold,
                jira_issues=jira_issues,
                bugzilla_bugs=bugzilla_bugs,
            )
            diff_metrics = None
            if enabled:
                run_forward_sync(bugzilla_bugs, enabled, mappings, jira_project, bugz, jira,
                                 project_components=project_components, people=people)
                diff_metrics = run_diff_check(jira.updates, bugzilla_bugs, enabled)
            broken_links = find_broken_links(
                bugzilla_bugs, dropped_jira_keys, jira.client, resolved_key_cache,
                tag=tag, jira_issues=jira_issues, bugz=bugz,
            )
            print_broken_links(broken_links)
            write_broken_links_csv(tag, broken_links)
            if should_record:
                record_snapshot(tag, jira_project, health_metrics, diff_metrics, run_ts,
                                broken_links=broken_links)
                captured = True
            per_tag_metrics.append({
                "jira_project": jira_project,
                "bugzilla": health_metrics["bugzilla"],
                "jira": health_metrics["jira"],
                "diff": diff_metrics,
            })
            jira.updates = []
            bugz.updates = []
            continue

        print("─" * 60)
        print()
        try:
            if args.reverse:
                run_reverse_sync(bugzilla_bugs, enabled, mappings, bugz)
            else:
                run_forward_sync(bugzilla_bugs, enabled, mappings, jira_project, bugz, jira,
                                 project_components=project_components, people=people)
        finally:
            # Record a snapshot for forward runs (apply/prompt/preview). The updates
            # gathered above are the pre-apply drift; capture before they are cleared.
            if not args.reverse and should_record:
                snap_components = _get_components_for_tag(tag, tags)
                health_metrics = compute_health_metrics(
                    tag, jira_project, snap_components, bugz, jira.client,
                    jira_issues=jira_issues, bugzilla_bugs=bugzilla_bugs,
                )
                diff_metrics = (
                    compute_diff_metrics(jira.updates, bugzilla_bugs, enabled)
                    if enabled else None
                )
                broken_links = find_broken_links(
                    bugzilla_bugs, dropped_jira_keys, jira.client, resolved_key_cache,
                    tag=tag, jira_issues=jira_issues, bugz=bugz,
                )
                record_snapshot(tag, jira_project, health_metrics, diff_metrics, run_ts,
                                broken_links=broken_links)
                captured = True
            all_updates.extend(bugz.updates)
            all_updates.extend(jira.updates)
            if bugz.applied:
                cache_bugz_path.unlink(missing_ok=True)
                logger.info(f"[{tag}] Bugzilla cache cleared (data changed)")
            if jira.applied:
                cache_jira_path.unlink(missing_ok=True)
                logger.info(f"[{tag}] Jira cache cleared (data changed)")
            bugz.updates = []
            jira.updates = []
            bugz.applied = False
            jira.applied = False

    if is_check:
        # Cross-tag totals to the console (only meaningful with more than one tag)
        if len(per_tag_metrics) > 1:
            totals = aggregate_metrics(per_tag_metrics)
            print_totals(totals, len(per_tag_metrics))
    else:
        write_updates_csv(all_updates)

    # Regenerate the whole HTML report site from the snapshot store after any
    # run that recorded at least one snapshot. build_site() writes
    # reports/index.html, reports/<group>.html for every group, and
    # reports/tags/<tag>.html — so no per-group loop is needed here.
    if captured and not args.no_report:
        import make_report

        make_report.build_site()
        print(f"\n{Colors.CYAN}→ Wrote reports/ site (index, group, and per-tag pages){Colors.RESET}")


if __name__ == "__main__":
    main()
