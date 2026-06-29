"""
Jira client class for the jbi package.
"""

import logging
import os
import sys

from jira import JIRA
from jira.exceptions import JIRAError

from jbix.constants import JIRA_SEVERITY_FIELD, Colors

logger = logging.getLogger(__name__)

JIRA_URL = os.getenv("JIRA_URL", "https://mozilla-hub.atlassian.net")
JIRA_API_USER = os.getenv("JIRA_API_USER")
JIRA_API_KEY = os.getenv("JIRA_API_KEY")


class JiraClient:
    """
    Wraps the python-jira client with update helpers.

    All updates are routed through _confirm() so that log/prompt/apply
    modes are respected.
    """

    def __init__(self, mode: str = "preview"):
        self.client = JIRA(JIRA_URL, basic_auth=(JIRA_API_USER, JIRA_API_KEY))
        self.mode = mode
        self.updates: list[dict] = []
        self.applied: bool = False
        self.users: dict = {}
        # A Jira issue link has one global ID shared by both linked issues, so a
        # stale link can be reached from either side. Track IDs already handled
        # this run to consolidate the removal to a single record/API call.
        self.handled_link_ids: set[str] = set()

    def _make_update(
        self,
        bug_info: dict,
        jira_info: dict,
        jira_field: str,
        jira_before,
        jira_after,
        bug_field: str | None = None,
        bug_value=None,
    ) -> None:
        bug_id = bug_info['id']
        jira_key = jira_info['key']

        logger.info(
            f"{Colors.CYAN}[BUG {bug_id}]{Colors.RESET} "
            f"{Colors.YELLOW}→{Colors.RESET} "
            f"{Colors.CYAN}[{jira_key}]{Colors.RESET}"
        )
        logger.info(
            f"  {Colors.BOLD}{Colors.WHITE}Field:{Colors.RESET} {jira_field}"
        )
        logger.info(
            f"  {Colors.BOLD}{Colors.WHITE}Change:{Colors.RESET} "
            f"{jira_before} {Colors.YELLOW}→{Colors.RESET} {jira_after}"
        )
        logger.info(
            f"  {Colors.BOLD}{Colors.WHITE}URLs:{Colors.RESET} "
            f"{Colors.BLUE}{Colors.UNDERLINE}{bug_info['url']}{Colors.RESET} | "
            f"{Colors.BLUE}{Colors.UNDERLINE}{jira_info['url']}{Colors.RESET}"
        )
        logger.info("")

        self.updates.append({
            "direction": "bugzilla→jira",
            "bug_url": bug_info["url"],
            "bug_status": bug_info["status"],
            "bug_product": bug_info["product"],
            "bug_component": bug_info["component"],
            "bug_field": bug_field or "",
            "bug_before": "",
            "bug_after": str(bug_value) if bug_value is not None else "",
            "jira_url": jira_info["url"],
            "jira_field": jira_field,
            "jira_before": jira_before,
            "jira_after": jira_after,
        })

    def _ask(self, question: str, valid: str = "ynq") -> str:
        prompt = f"{question} [{'/'.join(valid)}]: "
        while True:
            try:
                ans = input(prompt).strip().lower()
            except EOFError:
                print("\n! EOF received, aborting.", file=sys.stderr)
                return "q"
            if ans in valid:
                return ans
            print(f"Please choose one of: {', '.join(valid)}")

    def _confirm(self) -> bool:
        if self.mode == "apply":
            self.applied = True
            return True
        if self.mode == "preview":
            return False
        ans = self._ask("Apply this change? (y=apply, n=skip, q=abort)")
        if ans == "y":
            self.applied = True
            return True
        if ans == "n":
            return False
        raise SystemExit("Aborted by user")

    def _update(self, key: str, field: str, value) -> None:
        logger.debug(f"[_update] key:{key} field:{field} value:{value}")
        if self._confirm():
            self.client.issue(key).update(fields={field: value}, notify=False)

    def add_labels(self, bug: dict, jira: dict, current: list, new: list) -> None:
        value = current + new
        self._make_update(bug, jira, "labels", current, value, "whiteboard", bug.get("whiteboard", ""))
        self._update(jira["key"], "labels", value)

    def add_remote_link(self, bug: dict, jira: dict, url: str) -> None:
        self._make_update(bug, jira, "remote_links", "", url)
        if self._confirm():
            # Use the bug id as the remote link's globalId — the same value JBI uses
            # (global_id=str(bug.id)) — so this is an idempotent upsert against the
            # same link object rather than a second, parallel link.
            self.client.add_remote_link(
                jira["key"], {"url": url, "title": url}, globalId=str(bug["id"])
            )

    def create_component(self, project_key: str, component_name: str):
        from types import SimpleNamespace

        logger.debug(f"[create_component] {project_key} / {component_name}")
        if self._confirm():
            logger.info(f"[create_component] [{component_name}] in [{project_key}]")
            try:
                return self.client.create_component(
                    name=component_name, project=project_key, description=""
                )
            except Exception as e:
                if "already exists" in str(e).lower():
                    logger.warning(f"[create_component] {component_name!r} already exists, fetching it")
                    for comp in self.client.project_components(project_key):
                        if comp.name == component_name:
                            return comp
                    logger.error(f"[create_component] {component_name!r} should exist but wasn't found")
                    return None
                raise
        # In log/prompt mode when declined: return a stub so sync_components can
        # still call update_components and generate an audit log entry.
        return SimpleNamespace(name=component_name)

    def create_issue_link(self, bug: dict, jira: dict, link_type: str, inward_issue: str, outward_issue: str) -> None:
        change = f"{outward_issue} → ({link_type.lower()}) → {inward_issue}"
        if link_type == "Duplicate" and jira["key"] == outward_issue:
            change = f"duplicated by {inward_issue}"
        elif link_type == "Blocks":
            if jira["key"] == inward_issue:
                change = f"blocks {outward_issue}"
            else:
                change = f"depends on {inward_issue}"
        elif link_type == "Relates":
            other = inward_issue if jira["key"] == outward_issue else outward_issue
            change = f"relates to {other}"
        elif link_type == "Problem/Incident":
            if jira["key"] == inward_issue:
                change = f"causes {outward_issue}"
            else:
                change = f"caused by {inward_issue}"

        self._make_update(bug, jira, "issue_links", "", change)
        if self._confirm():
            self.client.create_issue_link(link_type, inwardIssue=inward_issue, outwardIssue=outward_issue)

    def delete_issue_link(self, bug: dict, jira: dict, link_id: str, description: str) -> None:
        # The same link is reachable from both issues it connects; only handle it
        # once per run regardless of which side reports it stale.
        if link_id in self.handled_link_ids:
            return
        self.handled_link_ids.add(link_id)
        self._make_update(bug, jira, "issue_links", description, "")
        if self._confirm():
            self.client.delete_issue_link(link_id)

    def search_users(self, user: str) -> list:
        logger.debug(f"Searching for user {user!r} in Jira")
        return self.client.search_users(query=user)

    def transition_issue(self, bug: dict, jira: dict, current: str, new: str, resolution: str | None = None) -> None:
        logger.debug(f"[transition_issue] {bug['url']} {current!r} → {new!r}")
        if resolution:
            cur_res = jira.get("resolution") or "none"
            field = "status (resolution)"
            before = f"{current} ({cur_res})"
            after = f"{new} ({resolution})"
            bug_value = f"{bug.get('status', '')} ({bug.get('resolution', '')})"
            self._make_update(bug, jira, field, before, after, "status (resolution)", bug_value)
        else:
            self._make_update(bug, jira, "status", current, new, "status", bug.get("status"))
        if self._confirm():
            issue = self.client.issue(jira["key"])
            transitions = self.client.transitions(issue, expand="transitions.fields")
            target = next(
                (t for t in transitions
                 if (t.get("to") or {}).get("name", "").lower() == new.lower()),
                None,
            )
            if target:
                fields = {"resolution": {"name": resolution}} if resolution else {}
                try:
                    self.client.transition_issue(issue, transition=target["id"], fields=fields)
                except JIRAError as e:
                    if resolution and e.status_code == 400 and "resolution" in str(e).lower():
                        logger.warning(
                            f"[transition_issue] resolution field not on transition screen "
                            f"for {jira['key']}; transitioning without it then setting separately"
                        )
                        self.client.transition_issue(issue, transition=target["id"], fields={})
                        issue.update(fields={"resolution": {"name": resolution}}, notify=False)
                    else:
                        raise

    def update(self, field: str, bug: dict, jira: dict, current, new, bug_field: str | None = None, bug_value=None) -> None:
        self._make_update(bug, jira, field, current, new, bug_field, bug_value)
        self._update(jira["key"], field, new)

    def update_issue_type(self, bug: dict, jira: dict, current, new) -> None:
        self._make_update(bug, jira, "issuetype", current, new, "type", bug.get("type", ""))
        self._update(jira["key"], "issuetype", {"name": new})

    def update_assignee(self, bug: dict, jira: dict, current, new) -> None:
        new_label = "(unassigned)" if new is None else new.displayName
        self._make_update(bug, jira, "assignee", current, new_label, "assigned_to", bug.get("assigned_to"))
        self._update(jira["key"], "assignee", None if new is None else {"id": new.accountId})

    def update_components(self, bug: dict, jira: dict, current, new) -> None:
        names = [c.name for c in new]
        value = [{"name": n} for n in names]
        self._make_update(bug, jira, "components", current, names)
        self._update(jira["key"], "components", value)

    def update_summary(self, bug: dict, jira: dict, current, new) -> None:
        self._make_update(bug, jira, "summary", current, new, "summary", bug.get("summary", ""))
        self._update(jira["key"], "summary", new)

    def update_priority(self, bug: dict, jira: dict, current, new) -> None:
        self._make_update(bug, jira, "priority", current, new, "priority", bug.get("priority", ""))
        self._update(jira["key"], "priority", {"name": new})

    def update_resolution(self, bug: dict, jira: dict, current, new) -> None:
        self._make_update(bug, jira, "resolution", current, new, "resolution", bug.get("resolution", ""))
        self._update(jira["key"], "resolution", {"name": new})

    def update_severity(self, bug: dict, jira: dict, current, new) -> None:
        self._make_update(bug, jira, "severity", current, new, "severity", bug.get("severity"))
        # To clear a custom select field, pass None directly; otherwise wrap in dict
        value = None if new is None else {"value": new}
        self._update(jira["key"], JIRA_SEVERITY_FIELD, value)

    def update_original_estimate(self, bug: dict, jira: dict, current, new) -> None:
        self._make_update(
            bug, jira, "timeoriginalestimate",
            f"{current}h", f"{new}h", "estimated_time", bug.get("estimated_time")
        )
        if self._confirm():
            issue = self.client.issue(jira["key"])
            issue.update(fields={"timetracking": {"originalEstimate": f"{new}h"}}, notify=False)
