"""
Bugzilla client class and query helpers for the jbi package.
"""

import logging
import os
import sys
import time

import bugzilla as bugzilla_lib
from bugzilla.exceptions import BugzillaHTTPError

from jbix.constants import Colors

logger = logging.getLogger(__name__)

BUGZILLA_URL = os.getenv("BUGZILLA_URL", "bugzilla.mozilla.org")
BUGZILLA_API_KEY = os.getenv("BUGZILLA_API_KEY")
BZ_FETCH_BATCH_SIZE = 1000
BZ_ID_BATCH_SIZE = 200  # REST API sends IDs as GET params; keep URLs short

# Transient HTTP statuses worth retrying. Bugzilla's gateway intermittently
# returns these for expensive queries (e.g. regexp whiteboard scans over large
# products) when the backend is slow to respond.
_TRANSIENT_STATUSES = frozenset({502, 503, 504})
BZ_QUERY_MAX_RETRIES = 4
BZ_QUERY_BACKOFF_BASE = 2.0  # seconds; doubled each attempt


def _transient_status(exc: BugzillaHTTPError) -> int | None:
    """Return the HTTP status code if exc is a retryable gateway error, else None."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if status in _TRANSIENT_STATUSES else None


def retry_on_transient(func, *, description="Bugzilla request", _sleep=time.sleep):
    """Call func(), retrying on transient gateway errors with exponential backoff.

    Non-transient BugzillaHTTPErrors (e.g. 400/401/404) are re-raised immediately;
    only 502/503/504 are retried. Raises the last error if all attempts fail.
    """
    for attempt in range(1, BZ_QUERY_MAX_RETRIES + 1):
        try:
            return func()
        except BugzillaHTTPError as e:
            status = _transient_status(e)
            if status is None or attempt == BZ_QUERY_MAX_RETRIES:
                raise
            delay = BZ_QUERY_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.warning(
                f"{description} failed with HTTP {status} "
                f"(attempt {attempt}/{BZ_QUERY_MAX_RETRIES}); "
                f"retrying in {delay:.0f}s..."
            )
            _sleep(delay)


def build_component_query(component_list: list[str], **additional_params) -> dict:
    """
    Build a Bugzilla query for specific product/component pairs using boolean logic.

    Avoids the cartesian product problem: instead of matching
    (any product) × (any component), creates proper pairs:
    (product=A AND component=X) OR (product=B AND component=Y) OR ...

    Supports wildcard notation "Product::*" to match all components in a product.

    Args:
        component_list: List of "Product::Component" strings.
                        Use "Product::*" to match all components in a product.
        **additional_params: Extra query parameters (e.g. include_fields, chfield).

    Returns:
        dict: Query dict for bugzilla.Bugzilla.query(). Includes '_next_field_num'
              for callers that need to append additional boolean conditions.
    """
    if not component_list:
        raise ValueError("component_list cannot be empty")

    pairs = []
    for comp_str in component_list:
        if '::' not in comp_str:
            raise ValueError(
                f"Invalid component format: {comp_str!r}. Expected 'Product::Component'"
            )
        product, component = comp_str.split('::', 1)
        pairs.append((product, component))

    query: dict = {}
    field_num = 1

    # Outer OR group
    query[f'f{field_num}'] = 'OP'
    query[f'j{field_num}'] = 'OR'
    field_num += 1

    for product, component in pairs:
        if component == '*':
            # Wildcard: match all components — just product condition, no inner group
            query[f'f{field_num}'] = 'product'
            query[f'o{field_num}'] = 'equals'
            query[f'v{field_num}'] = product
            field_num += 1
        else:
            query[f'f{field_num}'] = 'OP'
            field_num += 1
            query[f'f{field_num}'] = 'product'
            query[f'o{field_num}'] = 'equals'
            query[f'v{field_num}'] = product
            field_num += 1
            query[f'f{field_num}'] = 'component'
            query[f'o{field_num}'] = 'equals'
            query[f'v{field_num}'] = component
            field_num += 1
            query[f'f{field_num}'] = 'CP'
            field_num += 1

    # Close outer OR group
    query[f'f{field_num}'] = 'CP'
    field_num += 1

    query.update(additional_params)
    query['_next_field_num'] = field_num
    return query


class BugzillaClient:
    """
    Wraps the python-bugzilla client with update helpers.

    All updates are routed through _confirm() so that log/prompt/apply
    modes are respected.
    """

    def __init__(self, mode: str = "preview"):
        self.client = bugzilla_lib.Bugzilla(BUGZILLA_URL, api_key=BUGZILLA_API_KEY, force_rest=True)
        self.mode = mode
        self.updates: list[dict] = []
        self.applied: bool = False
        self._valid_keywords: list[str] | None = None

    def _make_update(
        self,
        bug_info: dict,
        jira_info: dict,
        bug_field: str,
        bug_before,
        bug_after,
        jira_field: str = "",
        jira_value: str = "",
    ) -> None:
        bug_id = bug_info['id']
        jira_key = jira_info['key']

        logger.info(
            f"{Colors.CYAN}[BUG {bug_id}]{Colors.RESET} "
            f"{Colors.YELLOW}←{Colors.RESET} "
            f"{Colors.CYAN}[{jira_key}]{Colors.RESET}"
        )
        logger.info(
            f"  {Colors.BOLD}{Colors.WHITE}Field:{Colors.RESET} {bug_field}"
        )
        logger.info(
            f"  {Colors.BOLD}{Colors.WHITE}Change:{Colors.RESET} "
            f"{bug_before} {Colors.YELLOW}→{Colors.RESET} {bug_after}"
        )
        logger.info(
            f"  {Colors.BOLD}{Colors.WHITE}URLs:{Colors.RESET} "
            f"{Colors.BLUE}{Colors.UNDERLINE}{bug_info['url']}{Colors.RESET} | "
            f"{Colors.BLUE}{Colors.UNDERLINE}{jira_info['url']}{Colors.RESET}"
        )
        logger.info("")

        self.updates.append({
            "direction": "jira→bugzilla",
            "bug_url": bug_info["url"],
            "bug_status": bug_info["status"],
            "bug_product": bug_info["product"],
            "bug_component": bug_info["component"],
            "bug_field": bug_field,
            "bug_before": bug_before,
            "bug_after": bug_after,
            "jira_url": jira_info["url"],
            "jira_field": jira_field,
            "jira_before": jira_value,
            "jira_after": jira_value,
        })

    def _confirm(self) -> bool:
        if self.mode == "apply":
            self.applied = True
            return True
        if self.mode == "preview":
            return False
        # prompt mode
        prompt = "Apply this change? [y/n/q]: "
        while True:
            try:
                ans = input(prompt).strip().lower()
            except EOFError:
                print("\n! EOF received, aborting.", file=sys.stderr)
                return False
            if ans == "y":
                self.applied = True
                return True
            if ans == "n":
                return False
            if ans == "q":
                raise SystemExit("Aborted by user")
            print("Please choose one of: y, n, q")

    def get_valid_keywords(self) -> list[str]:
        """Fetch and cache valid Bugzilla keywords."""
        if self._valid_keywords is not None:
            return self._valid_keywords
        try:
            result = self.client._backend.bug_fields({"names": ["keywords"]})
            if 'fields' in result and result['fields']:
                field = result['fields'][0]
                if 'values' in field:
                    self._valid_keywords = [v['name'] for v in field['values'] if 'name' in v]
                    logger.debug(f"Fetched {len(self._valid_keywords)} valid keywords")
                    return self._valid_keywords
        except Exception as e:
            logger.warning(f"Failed to fetch valid keywords: {e}")
        self._valid_keywords = []
        return self._valid_keywords

    def update_summary(self, bug_info: dict, jira_info: dict, current, new) -> None:
        self._make_update(bug_info, jira_info, "summary", current, new,
                          "summary", jira_info.get("summary", ""))
        if self._confirm():
            update = self.client.build_update(summary=new)
            self.client.update_bugs([bug_info["id"]], update)

    def update_assignee(self, bug_info: dict, jira_info: dict, current, new) -> None:
        self._make_update(bug_info, jira_info, "assignee", current, new)
        if self._confirm():
            update = self.client.build_update(assigned_to=new)
            self.client.update_bugs([bug_info["id"]], update)

    def update_priority(self, bug_info: dict, jira_info: dict, current, new) -> None:
        self._make_update(bug_info, jira_info, "priority", current, new)
        if self._confirm():
            update = self.client.build_update(priority=new)
            self.client.update_bugs([bug_info["id"]], update)

    def update_type(self, bug_info: dict, jira_info: dict, current, new) -> None:
        self._make_update(bug_info, jira_info, "type", current, new,
                          "issuetype", jira_info.get("issuetype", ""))
        if self._confirm():
            # build_update() has no `type=` kwarg; set the Bugzilla `type` field directly.
            update = self.client.build_update()
            update["type"] = new
            self.client.update_bugs([bug_info["id"]], update)

    def update_estimated_time(self, bug_info: dict, jira_info: dict, current, new) -> None:
        jira_estimate = jira_info.get("timeoriginalestimate")
        jira_hours = f"{jira_estimate / 3600:.1f}h" if jira_estimate else ""
        self._make_update(
            bug_info, jira_info, "estimated_time",
            f"{current}h", f"{new}h", "timeoriginalestimate", jira_hours
        )
        if self._confirm():
            update = self.client.build_update(estimated_time=new)
            self.client.update_bugs([bug_info["id"]], update)

    def update_keywords(self, bug_info: dict, jira_info: dict, keywords_to_add: list[str]) -> None:
        current_keywords = bug_info.get("keywords", [])
        new_keywords = sorted(set(current_keywords + keywords_to_add))
        jira_labels = jira_info.get("labels", [])
        self._make_update(
            bug_info, jira_info,
            "keywords",
            ", ".join(current_keywords),
            ", ".join(new_keywords),
            "labels",
            ", ".join(jira_labels),
        )
        if self._confirm():
            update = self.client.build_update(keywords_set=new_keywords)
            self.client.update_bugs([bug_info["id"]], update)
            bug_info["keywords"] = new_keywords

    def update_whiteboard(self, bug_info: dict, jira_info: dict, current: str, new: str) -> None:
        jira_labels = jira_info.get("labels", [])
        self._make_update(bug_info, jira_info, "whiteboard", current, new, "labels", ", ".join(jira_labels))
        if self._confirm():
            update = self.client.build_update(whiteboard=new)
            self.client.update_bugs([bug_info["id"]], update)

    def update_severity(self, bug_info: dict, jira_info: dict, jira_impact, current, new) -> None:
        self._make_update(bug_info, jira_info, "severity", current, new, "estimated_impact", jira_impact)
        if self._confirm():
            update = self.client.build_update(severity=new)
            self.client.update_bugs([bug_info["id"]], update)
