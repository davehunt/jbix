"""
Fuzzy matching utilities for finding potential links between
unlinked Bugzilla bugs and Jira issues.
"""

import csv
import logging
import os
import re
from collections import defaultdict

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

JIRA_URL = os.getenv("JIRA_URL", "https://mozilla-hub.atlassian.net")


def fetch_unlinked_jira_issues(jira_client, project_key: str) -> list[dict]:
    """Query Jira for issues WITHOUT the 'bugzilla' label.

    The ``labels is EMPTY`` clause is required: in JQL ``labels != "bugzilla"``
    silently excludes issues whose labels field is empty/null (inequality only
    matches issues where the field is present), which would drop every
    label-less issue from the result.
    """
    jql = f'project = {project_key} AND (labels is EMPTY OR labels != "bugzilla")'
    logger.info(f"Querying Jira: {jql}")
    unlinked = []
    for issue in jira_client.search_issues(jql, maxResults=False):
        unlinked.append({
            "key": issue.key,
            "summary": issue.fields.summary,
            "components": [c.name for c in issue.fields.components],
            "url": f"{JIRA_URL}/browse/{issue.key}",
        })
    return unlinked


def fetch_all_jira_issues(jira_client, project_key: str) -> list[dict]:
    """Query Jira for all issues in a project."""
    jql = f'project = {project_key}'
    logger.info(f"Querying Jira: {jql}")
    issues = []
    for issue in jira_client.search_issues(jql, maxResults=False):
        issues.append({
            "key": issue.key,
            "summary": issue.fields.summary,
            "components": [c.name for c in issue.fields.components],
            "url": f"{JIRA_URL}/browse/{issue.key}",
            "has_bugzilla_label": "bugzilla" in (issue.fields.labels or []),
        })
    return issues


def preprocess_text(text: str) -> str:
    """Normalize text for fuzzy comparison: lowercase, strip special chars."""
    text = text.lower()
    text = re.sub(r'[^\w\s-]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def find_candidate_matches(
    bugs: list[dict],
    issues: list[dict],
    threshold: int = 85,
    component_filter: bool = True,
) -> list[tuple[dict, dict, int]]:
    """
    Find candidate matches between unlinked Bugzilla bugs and Jira issues.

    Uses a multi-stage approach to reduce comparisons:
    1. Group issues by component (reduces search space ~10×)
    2. Token pre-filter: require ≥2 shared tokens of length ≥3
    3. Fuzzy match using token_sort_ratio

    Returns a list of (bug, issue, score) tuples sorted by score descending.
    """
    matches = []

    if component_filter:
        issues_by_comp: dict[str, list] = defaultdict(list)
        for issue in issues:
            for comp in issue["components"]:
                issues_by_comp[comp].append(issue)

        for bug in bugs:
            comp = f"{bug['product']}::{bug['component']}"
            candidates = issues_by_comp.get(comp, [])
            if not candidates:
                continue

            bug_summary = preprocess_text(bug["summary"])
            bug_tokens = {w for w in bug_summary.split() if len(w) >= 3}

            for issue in candidates:
                issue_summary = preprocess_text(issue["summary"])
                issue_tokens = {w for w in issue_summary.split() if len(w) >= 3}
                if len(bug_tokens & issue_tokens) >= 2:
                    score = fuzz.token_sort_ratio(bug_summary, issue_summary)
                    if score >= threshold:
                        matches.append((bug, issue, score))
    else:
        logger.warning(
            f"Running without component filter: "
            f"{len(bugs)} × {len(issues)} = {len(bugs) * len(issues):,} comparisons"
        )
        for bug in bugs:
            bug_summary = preprocess_text(bug["summary"])
            for issue in issues:
                score = fuzz.token_sort_ratio(bug_summary, preprocess_text(issue["summary"]))
                if score >= threshold:
                    matches.append((bug, issue, score))

    matches.sort(key=lambda x: x[2], reverse=True)
    return matches


def export_matches_to_csv(matches: list[tuple[dict, dict, int]], output_file: str) -> None:
    """Export match candidates to CSV for manual review."""
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'bug_id', 'bug_summary', 'bug_component', 'bug_url',
            'jira_key', 'jira_summary', 'jira_components', 'jira_url',
            'similarity_score',
        ])
        writer.writeheader()
        for bug, issue, score in matches:
            writer.writerow({
                'bug_id': bug['id'],
                'bug_summary': bug['summary'],
                'bug_component': f"{bug['product']}::{bug['component']}",
                'bug_url': bug['url'],
                'jira_key': issue['key'],
                'jira_summary': issue['summary'],
                'jira_components': '|'.join(issue['components']),
                'jira_url': issue['url'],
                'similarity_score': score,
            })
