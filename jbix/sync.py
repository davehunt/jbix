"""
Sync functions for the jbi package.

Each function compares a Bugzilla bug_info dict with a Jira jira_info dict and
calls the appropriate client update method when a difference is found.

Mapping dicts (priority, severity, status, resolution) are passed in explicitly
rather than read from module-level constants, so each tag's JBI config mappings
are applied.
"""

import logging
import os

from jbix.bugzilla import BugzillaClient
from jbix.constants import ASSIGNEE_MAP
from jbix.jira import JiraClient

logger = logging.getLogger(__name__)

BUGZILLA_URL = os.getenv("BUGZILLA_URL", "bugzilla.mozilla.org")


def _has_reciprocal_owner(bugzilla_bugs: dict, key: str, current_bug_id: int) -> bool:
    """True if some other non-external bug in bugzilla_bugs maps to `key`.

    Used by outward-path link sync to skip stale-link deletion when the
    inward-path on the reciprocal bug will delete the same Jira link.
    """
    for bug_id, bug in bugzilla_bugs.items():
        if bug_id == current_bug_id or bug.get("external"):
            continue
        if key in bug.get("jira", {}):
            return True
    return False

_REVERSE_ASSIGNEE_MAP: dict[str, str] = {
    v: k for k, v in ASSIGNEE_MAP.items() if v is not None
}


# ---------------------------------------------------------------------------
# Bugzilla → Jira sync functions
# ---------------------------------------------------------------------------

def sync_assignee(
    bug_info: dict,
    jira_info: dict,
    bugz: BugzillaClient,
    jira: JiraClient,
    people=None,
) -> None:
    bug_value = bug_info["assignee"]
    jira_value = jira_info["assignee"]
    if bug_value == "nobody@mozilla.org":
        if jira_value is not None:
            jira.update_assignee(bug_info, jira_info, jira_value, None)
        return

    # Explicit ASSIGNEE_MAP takes priority (handles None = no account, known overrides)
    if bug_value in ASSIGNEE_MAP:
        assignee_ldap = ASSIGNEE_MAP[bug_value]
    elif people is not None:
        assignee_ldap = people.lookup(bug_value) or bug_value
    else:
        assignee_ldap = bug_value

    logger.debug(f"[sync_assignee] bug:{bug_value} jira:{jira_value} ldap:{assignee_ldap}")
    if not assignee_ldap:
        return

    if assignee_ldap not in jira.users:
        users = jira.search_users(assignee_ldap)
        jira.users[assignee_ldap] = users[0] if users and len(users) == 1 else None
        if not jira.users[assignee_ldap]:
            logger.warning(f"Unable to find Jira user for bugzilla user {bug_value!r}")
            return

    if jira.users[assignee_ldap] is None:
        return

    if str(jira_value) != str(jira.users[assignee_ldap].displayName):
        jira.update_assignee(bug_info, jira_info, jira_value, jira.users[assignee_ldap])


def sync_components(
    project_key: str,
    project_components,
    bug_info: dict,
    jira_info: dict,
    jira: JiraClient,
    jira_components: dict,
) -> None:
    candidate_components: set[str] = set(jira_components.get("set_custom_components", []))
    if jira_components.get("use_bug_component", True) and bug_info.get("component"):
        candidate_components.add(bug_info["component"])
    if jira_components.get("use_bug_product", False) and bug_info.get("product"):
        candidate_components.add(bug_info["product"])
    if jira_components.get("use_bug_component_with_product_prefix", False):
        product = bug_info.get("product", "")
        component = bug_info.get("component", "")
        if product and component:
            candidate_components.add(f"{product}::{component}")
        elif component:
            candidate_components.add(component)

    if not candidate_components:
        logger.debug(
            f"[sync_components] bug {bug_info['id']}: no candidate components (all flags off), skipping"
        )
        return

    jira_values = [c.name for c in jira_info["components"]]
    missing = [name for name in candidate_components if name not in jira_values]
    logger.debug(
        f"[sync_components] bug {bug_info['id']} {jira_info['key']}: "
        f"candidates={sorted(candidate_components)} jira={sorted(jira_values)} missing={sorted(missing)}"
    )
    if not missing:
        return

    new_components = []
    for name in missing:
        found = [c for c in project_components if c.name == name]
        if found:
            new_components.extend(found)
        elif jira_components.get("create_components", False):
            created = jira.create_component(project_key, name)
            if created:
                new_components.append(created)
        else:
            logger.warning(
                f"[sync_components] Component {name!r} not found in project {project_key!r}; "
                "set create_components: true to auto-create"
            )

    if new_components:
        jira.update_components(bug_info, jira_info, jira_values, new_components)


def sync_summary(bug_info: dict, jira_info: dict, jira: JiraClient) -> None:
    bug_value = bug_info["summary"]
    jira_value = jira_info["summary"]
    if bug_value != jira_value:
        jira.update_summary(bug_info, jira_info, jira_value, bug_value)


def sync_priority(bug_info: dict, jira_info: dict, jira: JiraClient, priority_map: dict) -> None:
    bug_value = bug_info["priority"]
    jira_value = jira_info["priority"]
    mapped = priority_map.get(bug_value)
    if mapped is None:
        logger.debug(f"[sync_priority] No mapping for bug priority {bug_value!r}, skipping")
        return
    if jira_value != mapped:
        jira.update_priority(bug_info, jira_info, jira_value, mapped)


def sync_severity(bug_info: dict, jira_info: dict, jira: JiraClient, severity_map: dict) -> None:
    bug_value = bug_info["severity"]
    jira_value = jira_info["severity"] and getattr(jira_info["severity"], "value", None)
    if bug_value not in severity_map:
        logger.debug(f"[sync_severity] No mapping for bug severity {bug_value!r}, skipping")
        return
    mapped = severity_map[bug_value]
    if jira_value != mapped:
        jira.update_severity(bug_info, jira_info, jira_value, mapped)


def sync_status(
    bug_info: dict,
    jira_info: dict,
    jira: JiraClient,
    status_map: dict,
    resolution_map: dict | None = None,
) -> None:
    if not status_map:
        logger.debug("[sync_status] No status_map configured for this tag, skipping")
        return
    bug_value = bug_info["status"]
    jira_value = jira_info["status"]
    mapped = status_map.get(bug_value)
    if mapped is None:
        logger.debug(f"[sync_status] No mapping for bug status {bug_value!r}, skipping")
        return
    if jira_value != mapped:
        resolution = None
        if resolution_map and bug_info.get("status") == "RESOLVED":
            res = bug_info.get("resolution")
            if res:
                resolution = resolution_map.get(res)
        jira.transition_issue(bug_info, jira_info, jira_value, mapped, resolution=resolution)
        if resolution:
            jira_info["resolution"] = resolution


def sync_resolution(bug_info: dict, jira_info: dict, jira: JiraClient, resolution_map: dict) -> None:
    if not resolution_map:
        logger.debug("[sync_resolution] No resolution_map configured for this tag, skipping")
        return
    if bug_info["status"] != "RESOLVED":
        return
    bug_value = bug_info["resolution"]
    jira_value = jira_info["resolution"]
    mapped = resolution_map.get(bug_value)
    if mapped is None:
        logger.debug(f"[sync_resolution] No mapping for bug resolution {bug_value!r}, skipping")
        return
    if not jira_value or jira_value != mapped:
        jira.update_resolution(bug_info, jira_info, jira_value, mapped)


def sync_issue_type(bug_info: dict, jira_info: dict, jira: JiraClient, issue_type_map: dict) -> None:
    bug_value = bug_info.get("type")
    jira_value = jira_info["issuetype"]
    mapped = issue_type_map.get(bug_value)
    if mapped is None:
        logger.debug(f"[sync_issue_type] No mapping for bug type {bug_value!r}, skipping")
        return
    if jira_value != mapped:
        jira.update_issue_type(bug_info, jira_info, jira_value, mapped)


def _whiteboard_as_labels(labels_brackets: str, whiteboard: str | None) -> list[str]:
    """Return the full expected Jira label list for a given whiteboard string.

    Mirrors JBI's _whiteboard_as_labels so behaviour stays consistent.
    """
    splitted = whiteboard.replace("[", "").split("]") if whiteboard else []
    stripped = [x.strip() for x in splitted if x not in ("", " ")]
    nospace = [wb.replace(" ", ".") for wb in stripped]
    with_brackets = [f"[{wb}]" for wb in nospace]

    if labels_brackets == "yes":
        labels = with_brackets
    elif labels_brackets == "both":
        labels = nospace + with_brackets
    else:  # "no"
        labels = nospace

    return ["bugzilla"] + labels


def sync_whiteboard_labels(
    bug_info: dict, jira_info: dict, jira: JiraClient, labels_brackets: str = "no"
) -> None:
    jira_value = jira_info["labels"]
    expected = _whiteboard_as_labels(labels_brackets, bug_info.get("whiteboard"))
    labels = [e for e in expected if e not in jira_value]
    if labels:
        jira.add_labels(bug_info, jira_info, jira_value, labels)


def sync_keyword_labels(bug_info: dict, jira_info: dict, jira: JiraClient) -> None:
    bug_value = bug_info.get("keywords")
    jira_value = jira_info["labels"]
    if bug_value:
        labels = [b for b in bug_value if b not in jira_value]
        if labels:
            jira.add_labels(bug_info, jira_info, jira_value, labels)


def sync_depends_on(bug_info: dict, jira_info: dict, bugzilla_bugs: dict, jira: JiraClient) -> None:
    depends_on = bug_info.get("depends_on") or []
    inward_links = {
        link.inwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Blocks" and hasattr(link, "inwardIssue")
    }
    opposite_links = {
        link.outwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Blocks" and hasattr(link, "outwardIssue")
    }
    if not depends_on and not inward_links:
        return
    if depends_on:
        logger.debug(f"Bug {bug_info['id']} depends on {depends_on}")

    expected_keys = {key for b in depends_on if b in bugzilla_bugs for key in bugzilla_bugs[b]["jira"]}
    known_jira_keys = {key for bug in bugzilla_bugs.values() for key in bug.get("jira", {})}

    for key, link_id in inward_links.items():
        if key not in expected_keys and key in known_jira_keys:
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale depends-on {key}")

    for bug in [bugzilla_bugs[b] for b in depends_on if b in bugzilla_bugs]:
        for key in [k for k in bug["jira"] if k not in inward_links]:
            if key in opposite_links:
                jira.delete_issue_link(
                    bug_info, jira_info, opposite_links[key],
                    f"removing incorrect {jira_info['key']} blocks {key}",
                )
            jira.create_issue_link(bug_info, jira_info, "Blocks", key, jira_info["key"])


def sync_blocks(
    bug_info: dict,
    jira_info: dict,
    bugzilla_bugs: dict,
    jira: JiraClient,
    depends_on_enabled: bool = False,
) -> None:
    blocks = bug_info.get("blocks") or []
    outward_links = {
        link.outwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Blocks" and hasattr(link, "outwardIssue")
    }
    if not blocks and not outward_links:
        return
    if blocks:
        logger.debug(f"Bug {bug_info['id']} blocks {blocks}")

    known_jira_keys = {key for bug in bugzilla_bugs.values() for key in bug.get("jira", {})}
    # Build expected outward keys; include managed pairs so they are never deleted
    expected_keys: set[str] = set()
    for b in blocks:
        if b in bugzilla_bugs:
            expected_keys.update(bugzilla_bugs[b]["jira"])

    for key, link_id in outward_links.items():
        if key not in expected_keys and key in known_jira_keys:
            if depends_on_enabled and _has_reciprocal_owner(bugzilla_bugs, key, bug_info["id"]):
                continue  # sync_depends_on on the reciprocal bug will delete this link
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale blocks {key}")

    for b in blocks:
        if b not in bugzilla_bugs:
            continue
        bug = bugzilla_bugs[b]
        if depends_on_enabled and bug_info["id"] in bug.get("depends_on", []):
            continue  # sync_depends_on handles this pair (avoids duplicate link)
        for key in [k for k in bug["jira"] if k not in outward_links]:
            jira.create_issue_link(bug_info, jira_info, "Blocks", jira_info["key"], key)


def sync_dependencies(bug_info: dict, jira_info: dict, bugzilla_bugs: dict, jira: JiraClient) -> None:
    """Sync both dependency directions for a bug (JBI's combined ``sync_dependencies`` step).

    A Bugzilla ``depends_on`` becomes an inward Jira "Blocks" link and a ``blocks`` an outward one.
    The two are always enabled together, so ``sync_blocks`` runs with ``depends_on_enabled=True`` to
    coordinate the reciprocal pair (avoiding duplicate or self-deleting links).
    """
    sync_depends_on(bug_info, jira_info, bugzilla_bugs, jira)
    sync_blocks(bug_info, jira_info, bugzilla_bugs, jira, depends_on_enabled=True)


def sync_duplicates(bug_info: dict, jira_info: dict, bugzilla_bugs: dict, jira: JiraClient) -> None:
    dupe_of = bug_info.get("dupe_of")
    duplicates = bug_info.get("duplicates") or []

    inward_links = {
        link.inwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Duplicate" and hasattr(link, "inwardIssue")
    }
    outward_links = {
        link.outwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Duplicate" and hasattr(link, "outwardIssue")
    }
    if not dupe_of and not duplicates and not inward_links and not outward_links:
        return

    known_jira_keys = {key for bug in bugzilla_bugs.values() for key in bug.get("jira", {})}

    # --- dupe_of: current issue "duplicates" original ---
    expected_inward: set[str] = set()
    if dupe_of and dupe_of in bugzilla_bugs:
        logger.debug(f"Bug {bug_info['id']} duplicates {dupe_of}")
        expected_inward.update(bugzilla_bugs[dupe_of]["jira"])

    for key, link_id in inward_links.items():
        if key not in expected_inward and key in known_jira_keys:
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale duplicate {key}")

    for key in [k for k in expected_inward if k not in inward_links]:
        jira.create_issue_link(bug_info, jira_info, "Duplicate", key, jira_info["key"])

    # --- duplicates: current issue "is duplicated by" others ---
    # Build expected_outward from ALL duplicates for deletion protection; creation
    # skips bugs whose dupe_of path already handles the link (avoids double-creation).
    expected_outward: set[str] = set()
    for dup_id in duplicates:
        if dup_id in bugzilla_bugs:
            expected_outward.update(bugzilla_bugs[dup_id]["jira"])
    if duplicates:
        logger.debug(f"Bug {bug_info['id']} is duplicated by {duplicates}")

    for key, link_id in outward_links.items():
        if key not in expected_outward and key in known_jira_keys:
            if _has_reciprocal_owner(bugzilla_bugs, key, bug_info["id"]):
                continue  # sync_duplicates on the reciprocal bug will delete this link
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale duplicated-by {key}")

    for dup_id in duplicates:
        if dup_id not in bugzilla_bugs:
            continue
        dup_bug = bugzilla_bugs[dup_id]
        if not dup_bug.get("external") and dup_bug.get("dupe_of") == bug_info["id"]:
            continue  # sync_duplicates on dup_id handles this link (avoids duplicate link)
        for key in [k for k in dup_bug["jira"] if k not in outward_links]:
            jira.create_issue_link(bug_info, jira_info, "Duplicate", jira_info["key"], key)


def sync_regressions(bug_info: dict, jira_info: dict, bugzilla_bugs: dict, jira: JiraClient) -> None:
    regressions = bug_info.get("regressions") or []
    regressed_by = bug_info.get("regressed_by") or []

    inward_links = {
        link.inwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Problem/Incident" and hasattr(link, "inwardIssue")
    }
    outward_links = {
        link.outwardIssue.key: link.id
        for link in jira_info["links"]
        if link.type.name == "Problem/Incident" and hasattr(link, "outwardIssue")
    }
    if not regressions and not regressed_by and not inward_links and not outward_links:
        return

    known_jira_keys = {key for bug in bugzilla_bugs.values() for key in bug.get("jira", {})}

    # --- regressed_by: current issue is the effect, other is the cause (inward on current) ---
    expected_inward: set[str] = set()
    for cause_id in regressed_by:
        if cause_id in bugzilla_bugs:
            expected_inward.update(bugzilla_bugs[cause_id]["jira"])
    if regressed_by:
        logger.debug(f"Bug {bug_info['id']} regressed by {regressed_by}")

    for key, link_id in inward_links.items():
        if key not in expected_inward and key in known_jira_keys:
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale caused-by {key}")

    for key in [k for k in expected_inward if k not in inward_links]:
        jira.create_issue_link(bug_info, jira_info, "Problem/Incident", key, jira_info["key"])

    # --- regressions: current issue is the cause, others are the effects (outward on current) ---
    # Build expected_outward from ALL regressions for deletion protection; creation skips
    # bugs whose regressed_by path already handles the link (avoids double-creation).
    expected_outward: set[str] = set()
    for effect_id in regressions:
        if effect_id in bugzilla_bugs:
            expected_outward.update(bugzilla_bugs[effect_id]["jira"])
    if regressions:
        logger.debug(f"Bug {bug_info['id']} caused regressions in {regressions}")

    for key, link_id in outward_links.items():
        if key not in expected_outward and key in known_jira_keys:
            if _has_reciprocal_owner(bugzilla_bugs, key, bug_info["id"]):
                continue  # sync_regressions on the reciprocal bug will delete this link
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale causes {key}")

    for effect_id in regressions:
        if effect_id not in bugzilla_bugs:
            continue
        effect_bug = bugzilla_bugs[effect_id]
        if not effect_bug.get("external") and bug_info["id"] in effect_bug.get("regressed_by", []):
            continue  # sync_regressions on effect_id handles this link (avoids duplicate link)
        for key in [k for k in effect_bug["jira"] if k not in outward_links]:
            jira.create_issue_link(bug_info, jira_info, "Problem/Incident", jira_info["key"], key)


def sync_see_also(bug_info: dict, jira_info: dict, bugzilla_bugs: dict, jira: JiraClient) -> None:
    see_also_bugs = bug_info.get("see_also_bugs") or []
    see_also_jira_keys = bug_info.get("see_also_jira_keys") or []

    # "Relates" is symmetric — collect both directions with link IDs for deletion
    all_relates: dict[str, str] = {}  # {other_key: link_id}
    for link in jira_info["links"]:
        if link.type.name != "Relates":
            continue
        if hasattr(link, "inwardIssue"):
            all_relates[link.inwardIssue.key] = link.id
        if hasattr(link, "outwardIssue"):
            all_relates[link.outwardIssue.key] = link.id

    if not see_also_bugs and not see_also_jira_keys and not all_relates:
        return

    if see_also_bugs or see_also_jira_keys:
        logger.debug(f"Bug {bug_info['id']} see_also_bugs={see_also_bugs} see_also_jira_keys={see_also_jira_keys}")

    expected_keys: set[str] = set()
    for b in see_also_bugs:
        if b in bugzilla_bugs:
            expected_keys.update(bugzilla_bugs[b]["jira"])
    expected_keys.update(see_also_jira_keys)
    # Include keys from bugs that reference this bug so we don't remove links created from the other side
    for other in bugzilla_bugs.values():
        if bug_info["id"] in (other.get("see_also_bugs") or []):
            expected_keys.update(other.get("jira", {}).keys())

    known_jira_keys = {k for bug in bugzilla_bugs.values() for k in bug.get("jira", {})}

    for key, link_id in all_relates.items():
        if key not in expected_keys and key in known_jira_keys:
            jira.delete_issue_link(bug_info, jira_info, link_id, f"removing stale see-also {key}")

    for bug in [bugzilla_bugs[b] for b in see_also_bugs if b in bugzilla_bugs]:
        # Lower-ID bug creates the link when the relationship is mutual
        if bug_info["id"] > bug["id"] and bug_info["id"] in (bug.get("see_also_bugs") or []):
            continue
        for key in [k for k in bug["jira"] if k not in all_relates]:
            inward, outward = sorted([jira_info["key"], key])
            jira.create_issue_link(bug_info, jira_info, "Relates", inward, outward)
    for key in see_also_jira_keys:
        if key not in all_relates:
            inward, outward = sorted([jira_info["key"], key])
            jira.create_issue_link(bug_info, jira_info, "Relates", inward, outward)


def sync_remote_links(bug_info: dict, jira_info: dict, jira: JiraClient) -> None:
    bug_value = f"https://{BUGZILLA_URL}/show_bug.cgi?id={bug_info['id']}"
    jira_value = [link.object.url for link in jira_info.get("remote_links", [])]
    if bug_value not in jira_value:
        jira.add_remote_link(bug_info, jira_info, bug_value)


def sync_time_tracking(bug_info: dict, jira_info: dict, jira: JiraClient) -> None:
    bug_estimate = bug_info.get("estimated_time")
    jira_estimate = jira_info.get("timeoriginalestimate")
    logger.debug(f"[sync_time_tracking] bug:{bug_estimate} jira:{jira_estimate}")

    if bug_estimate and bug_estimate > 0:
        jira_estimate_hours = jira_estimate / 3600 if jira_estimate else 0
        if bug_estimate != jira_estimate_hours:
            jira.update_original_estimate(bug_info, jira_info, jira_estimate_hours, bug_estimate)

    bug_deadline = bug_info.get("deadline")
    jira_duedate = jira_info.get("duedate")

    if bug_deadline:
        if jira_duedate != bug_deadline:
            jira.update("duedate", bug_info, jira_info, jira_duedate, bug_deadline, "deadline", bug_deadline)
    elif jira_duedate:
        jira.update("duedate", bug_info, jira_info, jira_duedate, None, "deadline", None)


# ---------------------------------------------------------------------------
# Jira → Bugzilla (reverse) sync functions
# ---------------------------------------------------------------------------

def reverse_sync_priority(bug_info: dict, jira_info: dict, bugz: BugzillaClient, priority_map: dict) -> None:
    """Sync priority from Jira to Bugzilla when they differ."""
    bug_priority = bug_info["priority"]
    jira_priority = jira_info["priority"]
    # Only update if Jira priority maps to a valid Bugzilla value and differs
    reverse_map = {v: k for k, v in priority_map.items() if k not in ("", "--")}
    mapped = reverse_map.get(jira_priority)
    if mapped and jira_priority != "(none)" and bug_priority != mapped:
        bugz.update_priority(bug_info, jira_info, bug_priority, mapped)


def reverse_sync_summary(bug_info: dict, jira_info: dict, bugz: BugzillaClient) -> None:
    """Sync summary from Jira to Bugzilla when they differ."""
    jira_value = jira_info["summary"]
    bug_value = bug_info["summary"]
    if jira_value != bug_value:
        bugz.update_summary(bug_info, jira_info, bug_value, jira_value)


def reverse_sync_severity(bug_info: dict, jira_info: dict, bugz: BugzillaClient, severity_map: dict) -> None:
    """Sync severity from Jira to Bugzilla using estimated_impact."""
    IMPACT_TO_SEVERITY = {"High": "S2", "Medium": "S3", "Low": "S4"}
    estimated_impact = jira_info.get("estimated_impact")
    current_severity = bug_info.get("severity")
    if not estimated_impact:
        return
    new_severity = IMPACT_TO_SEVERITY.get(estimated_impact)
    if new_severity and current_severity != new_severity:
        bugz.update_severity(bug_info, jira_info, estimated_impact, current_severity, new_severity)


def reverse_sync_whiteboard_labels(bug_info: dict, jira_info: dict, bugz: BugzillaClient) -> None:
    """Sync Jira labels back to Bugzilla whiteboard/keywords."""
    jira_labels = jira_info.get("labels", [])
    bug_keywords = bug_info.get("keywords", [])
    bug_whiteboard = bug_info.get("whiteboard", "")

    valid_keywords = bugz.get_valid_keywords()
    valid_keywords_lower = {kw.lower() for kw in valid_keywords}

    splitted = bug_whiteboard.replace("[", "").split("]") if bug_whiteboard else []
    whiteboard_items = [x.strip() for x in splitted if x.strip()]
    whiteboard_normalized = [item.replace(" ", "-") for item in whiteboard_items]

    keywords_to_add = []
    whiteboard_to_add = []

    for raw_label in jira_labels:
        label = raw_label.strip("[]")  # "[fxp]" → "fxp"; "fxp" → "fxp"
        if label.lower() == "bugzilla":
            continue
        label_lower = label.lower()
        if any(kw.lower() == label_lower for kw in bug_keywords):
            continue
        if any(wb.lower() == label_lower for wb in whiteboard_normalized):
            continue
        if label_lower in valid_keywords_lower:
            keywords_to_add.append(label)
        else:
            whiteboard_to_add.append(label)

    if keywords_to_add:
        bugz.update_keywords(bug_info, jira_info, keywords_to_add)

    if whiteboard_to_add:
        new_whiteboard = bug_whiteboard
        for label in whiteboard_to_add:
            new_whiteboard += f"[{label}]"
        bugz.update_whiteboard(bug_info, jira_info, bug_whiteboard, new_whiteboard)


def reverse_sync_assignee(bug_info: dict, jira_info: dict, bugz: BugzillaClient) -> None:
    """Sync assignee from Jira to Bugzilla when they differ."""
    jira_user = jira_info.get("assignee")
    current = bug_info.get("assignee") or ""

    if jira_user is None:
        return  # Jira unassigned likely means contributor without Jira access — don't clear Bugzilla
    jira_email = getattr(jira_user, "emailAddress", None)
    if not jira_email:
        logger.warning(
            f"[reverse_sync_assignee] no emailAddress on Jira user {jira_user!r}"
            f" for bug {bug_info['id']}"
        )
        return
    new_email = _REVERSE_ASSIGNEE_MAP.get(jira_email, jira_email)

    if current != new_email:
        bugz.update_assignee(bug_info, jira_info, current, new_email)
