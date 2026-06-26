"""
Health check logic for jbix.py.

Reports consistency between Bugzilla and Jira for a given whiteboard tag:
- Bugs in configured components with/without the whiteboard tag
- Jira issues linked/unlinked to Bugzilla
- Optional fuzzy link candidates
"""

import logging
import pathlib
import urllib.parse
from collections import defaultdict

from jbix.bugzilla import (
    BUGZILLA_URL,
    BZ_FETCH_BATCH_SIZE,
    BugzillaClient,
    build_component_query,
)
from jbix.constants import JIRA_SEVERITY_FIELD, Colors
from jbix.fuzzy import (
    export_matches_to_csv,
    fetch_all_jira_issues,
    fetch_unlinked_jira_issues,
    find_candidate_matches,
)
from jbix.jira import JIRA_URL
from jbix.snapshots import (
    aggregate_metrics,  # noqa: F401 — re-exported for callers/tests
)

logger = logging.getLogger(__name__)


def run_health_check(
    tag: str,
    jira_project: str,
    bugz: BugzillaClient,
    jira_client,
    components: list[str] | None = None,
    link_candidates: bool = False,
    threshold: int = 85,
    jira_issues: dict | None = None,
    bugzilla_bugs: dict | None = None,
) -> dict:
    """
    Print a health report for the given whiteboard tag / Jira project pair.

    Args:
        tag: Whiteboard tag, e.g. "fxp"
        jira_project: Jira project key, e.g. "FXP"
        bugz: BugzillaClient instance
        jira_client: Raw JIRA client (jira.JIRA)
        components: List of "Product::Component" strings for Bugzilla queries.
                    If None or empty, the Bugzilla section is skipped.
        link_candidates: If True, run fuzzy matching and export CSV.
        threshold: Fuzzy match similarity threshold (0-100).
        jira_issues: Pre-fetched Jira issues dict (keyed by issue key).
                     If provided, avoids a fresh Jira query in _check_jira.
        bugzilla_bugs: Merged bug dict; when provided, the Jira "linked" count is
                       derived from actual bug→issue links (per-tag accurate)
                       rather than Jira labels.

    Returns:
        The computed metrics: ``{"bugzilla": <dict or None>, "jira": <dict>}``.
    """
    print("─" * 60)

    # --- Bugzilla section (requires component list) ---
    if components:
        bugzilla_metrics = _check_bugzilla(tag, components, bugz)
    else:
        bugzilla_metrics = None
        print(f"\n{Colors.YELLOW}Bugzilla:{Colors.RESET} No components configured in tags.yaml — skipping")

    # --- Jira section ---
    linked = (count_linked_jira_issues(bugzilla_bugs, jira_issues)
              if bugzilla_bugs is not None else None)
    jira_metrics = _check_jira(jira_project, jira_client, jira_issues=jira_issues, linked=linked)

    # --- Fuzzy link candidates ---
    if link_candidates:
        _find_link_candidates(tag, jira_project, components, bugz, jira_client, threshold)

    return {"bugzilla": bugzilla_metrics, "jira": jira_metrics}


def compute_health_metrics(
    tag: str,
    jira_project: str,
    components: list[str] | None,
    bugz: BugzillaClient,
    jira_client,
    jira_issues: dict | None = None,
    bugzilla_bugs: dict | None = None,
) -> dict:
    """Silent variant of :func:`run_health_check` for non-check modes.

    Returns ``{"bugzilla": <dict|None>, "jira": <dict>}`` without printing a
    health report. The Bugzilla section runs its count queries only when
    ``components`` is non-empty. The Jira "linked" count is derived from
    ``bugzilla_bugs`` (actual bug→issue links) when provided.
    """
    linked = (count_linked_jira_issues(bugzilla_bugs, jira_issues)
              if bugzilla_bugs is not None else None)
    return {
        "bugzilla": _bugzilla_metrics(tag, components, bugz) if components else None,
        "jira": _jira_metrics(jira_project, jira_client, jira_issues, linked=linked),
    }


def _bz_count(bugz: BugzillaClient, query: dict) -> int:
    """Return the total number of bugs matching query, paginating past the 10k limit."""
    total = 0
    offset = 0
    while True:
        batch = bugz.client.query({**query, "limit": BZ_FETCH_BATCH_SIZE, "offset": offset})
        total += len(batch)
        if len(batch) < BZ_FETCH_BATCH_SIZE:
            break
        offset += BZ_FETCH_BATCH_SIZE
    return total


def _bugzilla_metrics(
    tag: str,
    components: list[str],
    bugz: BugzillaClient,
) -> dict:
    """Compute Bugzilla component counts for the given tag (no printing)."""
    # Total bugs in components
    query_all = build_component_query(
        components,
        include_fields=["id"],
    )
    query_all.pop('_next_field_num')
    total = _bz_count(bugz, query_all)

    # Count tagged bugs scoped to the same components as the total
    query_tagged = build_component_query(components, include_fields=["id"])
    next_num = query_tagged.pop("_next_field_num")
    query_tagged[f"f{next_num}"] = "status_whiteboard"
    query_tagged[f"o{next_num}"] = "regexp"
    query_tagged[f"v{next_num}"] = rf"\[{tag}(-[^\]]+)?\]"
    tagged = _bz_count(bugz, query_tagged)

    untagged = total - tagged

    bugs_url = f"https://{BUGZILLA_URL}/buglist.cgi?{urllib.parse.urlencode(query_all)}"

    untagged_url = None
    if untagged > 0:
        url_query = build_component_query(components)
        next_num = url_query.pop("_next_field_num")
        url_query[f"f{next_num}"] = "status_whiteboard"
        url_query[f"o{next_num}"] = "notregexp"
        url_query[f"v{next_num}"] = f"\\[{tag}"
        untagged_url = f"https://{BUGZILLA_URL}/buglist.cgi?{urllib.parse.urlencode(url_query)}"

    return {
        "total": total,
        "tagged": tagged,
        "untagged": untagged,
        "pct_tagged": tagged / total * 100 if total else 0,
        "pct_untagged": untagged / total * 100 if total else 0,
        "bugs_url": bugs_url,
        "untagged_url": untagged_url,
    }


def _check_bugzilla(
    tag: str,
    components: list[str],
    bugz: BugzillaClient,
) -> dict:
    print(f"\n{Colors.BOLD}Bugzilla{Colors.RESET} (components):")

    m = _bugzilla_metrics(tag, components, bugz)
    total, tagged, untagged = m["total"], m["tagged"], m["untagged"]
    pct_tagged, pct_untagged = m["pct_tagged"], m["pct_untagged"]

    labels = ["Total:", f"With [{tag}] tag:", "Without tag:"]
    lw = max(len(lbl) for lbl in labels)
    cw = len(f"{total:,}")

    print(f"  {'Total:':<{lw}}  {Colors.WHITE}{total:>{cw},}{Colors.RESET}")
    print(f"  {f'With [{tag}] tag:':<{lw}}  {Colors.CYAN}{tagged:>{cw},}{Colors.RESET}  ({Colors.CYAN}{pct_tagged:>5.1f}%{Colors.RESET})")
    print(f"  {'Without tag:':<{lw}}  {Colors.YELLOW}{untagged:>{cw},}{Colors.RESET}  ({Colors.YELLOW}{pct_untagged:>5.1f}%{Colors.RESET})")

    if m["untagged_url"]:
        print(f"  Untagged: {Colors.BLUE}{Colors.UNDERLINE}{m['untagged_url']}{Colors.RESET}")

    return m


def count_linked_jira_issues(bugzilla_bugs: dict, jira_issues: dict | None = None) -> int:
    """Count distinct Jira issues linked to (non-external) Bugzilla bugs.

    This is the authoritative, per-tag linkage: it reads the Jira keys recorded
    on each bug rather than relying on Jira labels (which are not reliably set to
    the whiteboard tag). When ``jira_issues`` is given, the count is restricted to
    issues that actually exist in this project — excluding cross-project or stale
    links — so it never exceeds the project total.
    """
    keys = {
        k for b in bugzilla_bugs.values() if not b.get("external")
        for k, v in (b.get("jira") or {}).items() if isinstance(v, dict)
    }
    if jira_issues is not None:
        keys &= set(jira_issues)
    return len(keys)


def _jira_metrics(
    jira_project: str,
    jira_client,
    jira_issues: dict | None = None,
    linked: int | None = None,
) -> dict:
    """Compute Jira project link counts (no printing).

    ``linked`` is the count of issues linked to Bugzilla (from
    :func:`count_linked_jira_issues`). When omitted, it falls back to counting the
    generic ``bugzilla`` label — used by callers that lack the bug data (e.g. the
    ``fetch_all_jira_issues`` path when ``jira_issues`` is not supplied).
    """
    if jira_issues is not None:
        total = len(jira_issues)
        if linked is None:
            linked = sum(
                1 for v in jira_issues.values()
                if any("bugzilla" in lbl.lower() for lbl in v.get("labels", []))
            )
    else:
        all_issues = fetch_all_jira_issues(jira_client, jira_project)
        total = len(all_issues)
        linked = sum(1 for i in all_issues if i["has_bugzilla_label"])

    unlinked = total - linked

    project_url = f"{JIRA_URL}/browse/{jira_project}"
    issues_url = (
        f"{JIRA_URL}/issues/?jql="
        f"{urllib.parse.quote(f'project = {jira_project} ORDER BY created DESC')}"
    )

    unlinked_url = None
    if unlinked > 0:
        # `labels is EMPTY OR` is required: `NOT labels = bugzilla` alone drops
        # issues whose labels field is empty/null (JQL inequality only matches
        # issues where the field is present), undercounting label-less issues.
        jql = (
            f"project = {jira_project} "
            f"AND (labels is EMPTY OR labels != bugzilla) ORDER BY created DESC"
        )
        unlinked_url = f"{JIRA_URL}/issues/?jql={urllib.parse.quote(jql)}"

    return {
        "total": total,
        "linked": linked,
        "unlinked": unlinked,
        "pct_linked": linked / total * 100 if total else 0,
        "pct_unlinked": unlinked / total * 100 if total else 0,
        "project_url": project_url,
        "issues_url": issues_url,
        "unlinked_url": unlinked_url,
    }


def _check_jira(
    jira_project: str,
    jira_client,
    jira_issues: dict | None = None,
    linked: int | None = None,
) -> dict:
    print(f"\n{Colors.BOLD}Jira{Colors.RESET} (project {jira_project}):")

    m = _jira_metrics(jira_project, jira_client, jira_issues=jira_issues, linked=linked)
    total, linked, unlinked = m["total"], m["linked"], m["unlinked"]
    pct_linked, pct_unlinked = m["pct_linked"], m["pct_unlinked"]

    lw = len("Linked to Bugzilla:")
    cw = len(f"{total:,}")

    print(f"  {'Total:':<{lw}}  {Colors.WHITE}{total:>{cw},}{Colors.RESET}")
    print(f"  {'Linked to Bugzilla:':<{lw}}  {Colors.CYAN}{linked:>{cw},}{Colors.RESET}  ({Colors.CYAN}{pct_linked:>5.1f}%{Colors.RESET})")
    print(f"  {'Not linked:':<{lw}}  {Colors.YELLOW}{unlinked:>{cw},}{Colors.RESET}  ({Colors.YELLOW}{pct_unlinked:>5.1f}%{Colors.RESET})")

    if m["unlinked_url"]:
        print(f"  Unlinked: {Colors.BLUE}{Colors.UNDERLINE}{m['unlinked_url']}{Colors.RESET}")

    return m


def _find_link_candidates(
    tag: str,
    jira_project: str,
    components: list[str] | None,
    bugz: BugzillaClient,
    jira_client,
    threshold: int,
) -> None:
    print(f"\n{Colors.BOLD}Link Candidates{Colors.RESET} (threshold={threshold}%):")

    if not components:
        print(f"  {Colors.YELLOW}Skipped — no components configured{Colors.RESET}")
        return

    # Fetch unlinked bugs from Bugzilla
    query = build_component_query(
        components,
        include_fields=["id", "summary", "product", "component", "see_also"],
        chfield="[Bug creation]",
        chfieldfrom="2019-12-01",
    )
    next_field = query.pop('_next_field_num')
    query[f"f{next_field}"] = "see_also"
    query[f"o{next_field}"] = "notsubstring"
    query[f"v{next_field}"] = "hub"
    raw_bugs = bugz.client.query(query)

    bugs = [
        {
            "id": b.id,
            "summary": b.summary,
            "product": b.product,
            "component": b.component,
            "url": f"https://bugzil.la/{b.id}",
        }
        for b in raw_bugs
    ]

    unlinked_issues = fetch_unlinked_jira_issues(jira_client, jira_project)

    if not bugs or not unlinked_issues:
        print("  No unlinked bugs or issues to compare")
        return

    logger.info(f"Comparing {len(bugs)} unlinked bugs with {len(unlinked_issues)} unlinked issues...")
    matches = find_candidate_matches(bugs, unlinked_issues, threshold=threshold)

    if matches:
        out_dir = pathlib.Path("output")
        out_dir.mkdir(parents=True, exist_ok=True)
        output_file = str(out_dir / f"link_candidates_{tag}.csv")
        export_matches_to_csv(matches, output_file)
        print(f"  Found {len(matches)} candidate matches above {threshold}% similarity")
        print(f"  {Colors.CYAN}→ Exported to {output_file}{Colors.RESET}")
    else:
        print(f"  No candidate matches found above {threshold}% similarity")


# Maps jira_field keys (as they appear in updates) to display names
_FIELD_DISPLAY: dict[str, str] = {
    "summary": "summary",
    "priority": "priority",
    "assignee": "assignee",
    "components": "components",
    "labels": "labels",
    "status": "status",
    "resolution": "resolution",
    "severity": "severity",
    "issue_links": "issue_links",
    "remote_links": "remote_links",
    "issuetype": "type",
    "timeoriginalestimate": "time_tracking",
    "duedate": "deadline",
    JIRA_SEVERITY_FIELD: "severity",
}

# Maps enabled flag names to the jira_field keys they produce in updates
_FLAG_TO_JIRA_FIELDS: dict[str, list[str]] = {
    "summary": ["summary"],
    "priority": ["priority"],
    "assignee": ["assignee"],
    "components": ["components"],
    "whiteboard_labels": ["labels"],
    "keyword_labels": ["labels"],
    "status": ["status"],
    "resolution": ["resolution"],
    "type": ["issuetype"],
    "severity": ["severity"],
    "time_tracking": ["timeoriginalestimate", "duedate"],
    "dependencies": ["issue_links"],
    "duplicates": ["issue_links"],
    "see_also": ["issue_links"],
    "remote_links": ["remote_links"],
}


def _diff_metrics(updates: list[dict], bugzilla_bugs: dict, enabled: set[str]) -> dict | None:
    """Compute per-field diff counts and drift score (no printing).

    Returns None when there are no linked pairs or no fields to show.
    """
    total_pairs = sum(
        1 for bug in bugzilla_bugs.values()
        if not bug.get("external")
        for jira in bug["jira"].values()
        if isinstance(jira, dict)
    )
    if not total_pairs:
        return None

    # Build the ordered list of jira fields to display from enabled flags
    jira_fields_to_show: set[str] = set()
    for flag in enabled:
        jira_fields_to_show.update(_FLAG_TO_JIRA_FIELDS.get(flag, []))
    # Also include any unexpected fields that actually appeared in updates
    for u in updates:
        jira_fields_to_show.add(u["jira_field"])

    if not jira_fields_to_show:
        return None

    # Order: follow _FIELD_DISPLAY order, then any remainder alphabetically
    ordered = [f for f in _FIELD_DISPLAY if f in jira_fields_to_show]
    ordered += sorted(jira_fields_to_show - set(_FIELD_DISPLAY))

    # Tally distinct (bug_url, jira_url) pairs that differed, per jira_field
    seen: dict[str, set] = defaultdict(set)
    for u in updates:
        seen[u["jira_field"]].add((u["bug_url"], u["jira_url"]))

    fields = []
    for field in ordered:
        n = len(seen.get(field, set()))
        fields.append({
            "field": field,
            "name": _FIELD_DISPLAY.get(field, field),
            "n": n,
            "pct": n / total_pairs,
        })

    drifted_pairs = len({pair for pairs in seen.values() for pair in pairs})

    # Per-field repair rows (one per update) for the per-tag detail report.
    rows = [
        {
            "bug_url": u["bug_url"],
            "jira_url": u["jira_url"],
            "field": _FIELD_DISPLAY.get(u["jira_field"], u["jira_field"]),
            "before": "" if u["jira_before"] is None else str(u["jira_before"]),
            "after": "" if u["jira_after"] is None else str(u["jira_after"]),
        }
        for u in updates
    ]

    return {
        "total_pairs": total_pairs,
        "fields": fields,
        "drifted_pairs": drifted_pairs,
        "drift_pct": drifted_pairs / total_pairs,
        "rows": rows,
    }


def run_diff_check(updates: list[dict], bugzilla_bugs: dict, enabled: set[str]) -> dict | None:
    """Print per-field diff counts from a completed log-mode forward sync.

    Returns the computed metrics dict (or None when there is nothing to show),
    so callers can reuse it for the snapshot file and cross-tag totals.
    """
    m = _diff_metrics(updates, bugzilla_bugs, enabled)
    if m is None:
        return None

    _print_diff_metrics(m)
    return m


def compute_diff_metrics(updates: list[dict], bugzilla_bugs: dict, enabled: set[str]) -> dict | None:
    """Silent variant of :func:`run_diff_check` — returns metrics without printing."""
    return _diff_metrics(updates, bugzilla_bugs, enabled)


def _print_diff_metrics(m: dict) -> None:
    """Print the colorized field-comparison + drift-score block for one metrics dict."""
    total_pairs = m["total_pairs"]
    name_width = max(len(f["name"]) for f in m["fields"])
    count_width = len(f"{total_pairs:,}")
    pair_word = "pair" if total_pairs == 1 else "pairs"

    print(f"\n  {Colors.BOLD}Field comparison — {total_pairs:,} linked {pair_word}:{Colors.RESET}")
    for f in m["fields"]:
        n = f["n"]
        color = Colors.CYAN if n == 0 else Colors.YELLOW
        pct_str = f"{f['pct']:>7.2%}"
        print(
            f"    {f['name']:<{name_width}}  {color}{n:>{count_width},}{Colors.RESET}"
            f" / {total_pairs:,}  ({color}{pct_str}{Colors.RESET})"
        )

    drifted_pairs = m["drifted_pairs"]
    drift_color = Colors.CYAN if drifted_pairs == 0 else Colors.YELLOW
    print(
        f"\n  {Colors.BOLD}Drift score:{Colors.RESET}"
        f"  {drift_color}{drifted_pairs:>{count_width},} / {total_pairs:,}"
        f"  ({m['drift_pct']:.2%}) pairs have at least one field out of sync{Colors.RESET}"
    )


def print_totals(totals: dict, num_tags: int) -> None:
    """Print a colorized cross-tag totals section to the console."""
    print(f"\n{'═' * 60}")
    print(f"{Colors.BOLD}Totals ({num_tags} tags){Colors.RESET}")

    bz = totals.get("bugzilla")
    if bz:
        print(f"\n{Colors.BOLD}Bugzilla{Colors.RESET} (components):")
        print(f"  {'Total:':<20}  {Colors.WHITE}{bz['total']:>9,}{Colors.RESET}")
        print(f"  {'Tagged:':<20}  {Colors.CYAN}{bz['tagged']:>9,}{Colors.RESET}  ({Colors.CYAN}{bz['pct_tagged']:>5.1f}%{Colors.RESET})")
        print(f"  {'Without tag:':<20}  {Colors.YELLOW}{bz['untagged']:>9,}{Colors.RESET}  ({Colors.YELLOW}{bz['pct_untagged']:>5.1f}%{Colors.RESET})")

    jr = totals.get("jira")
    if jr:
        print(f"\n{Colors.BOLD}Jira{Colors.RESET}:")
        print(f"  {'Total:':<20}  {Colors.WHITE}{jr['total']:>9,}{Colors.RESET}")
        print(f"  {'Linked to Bugzilla:':<20}  {Colors.CYAN}{jr['linked']:>9,}{Colors.RESET}  ({Colors.CYAN}{jr['pct_linked']:>5.1f}%{Colors.RESET})")
        print(f"  {'Not linked:':<20}  {Colors.YELLOW}{jr['unlinked']:>9,}{Colors.RESET}  ({Colors.YELLOW}{jr['pct_unlinked']:>5.1f}%{Colors.RESET})")

    if totals.get("diff"):
        _print_diff_metrics(totals["diff"])


