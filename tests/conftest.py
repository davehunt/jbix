"""Shared pytest fixtures for the jbix test suite."""

from unittest.mock import MagicMock

import pytest

from jbix.bugzilla import BugzillaClient
from jbix.jira import JiraClient


def make_bugz(mode: str = "apply") -> BugzillaClient:
    """BugzillaClient with a mocked backend — no real API connection."""
    client: BugzillaClient = object.__new__(BugzillaClient)
    client.client = MagicMock()
    client.mode = mode
    client.updates = []
    client.applied = False
    client._valid_keywords = None
    return client


def make_jira(mode: str = "apply") -> JiraClient:
    """JiraClient with a mocked backend — no real API connection."""
    client: JiraClient = object.__new__(JiraClient)
    client.client = MagicMock()
    client.mode = mode
    client.updates = []
    client.applied = False
    client.users = {}
    client.handled_link_ids = set()
    return client


@pytest.fixture
def bugz() -> BugzillaClient:
    return make_bugz()


@pytest.fixture
def jira() -> JiraClient:
    return make_jira()


@pytest.fixture
def bug_info() -> dict:
    """Minimal Bugzilla bug_info dict matching the structure built by fetch_data."""
    return {
        "id": 123456,
        "assignee": "user@example.com",
        "blocks": [],
        "component": "General",
        "deadline": None,
        "depends_on": [],
        "dupe_of": None,
        "duplicates": [],
        "estimated_time": None,
        "jira": {"FXP-100": {}},
        "keywords": [],
        "regressed_by": [],
        "regressions": [],
        "priority": "P2",
        "product": "Firefox",
        "resolution": "",
        "severity": "S3",
        "status": "NEW",
        "summary": "Test bug summary",
        "url": "https://bugzil.la/123456",
        "whiteboard": "[fxp]",
    }


@pytest.fixture
def jira_info() -> dict:
    """Minimal Jira jira_info dict matching the structure built by _fetch_jira."""
    assignee = MagicMock()
    assignee.displayName = "Alice Example"
    return {
        "assignee": assignee,
        "components": [],
        "duedate": None,
        "estimated_impact": None,
        "key": "FXP-100",
        "labels": ["bugzilla"],
        "links": [],
        "priority": "P2",
        "resolution": None,
        "severity": None,
        "status": "In Progress",
        "summary": "Test bug summary",
        "timeoriginalestimate": None,
        "url": "https://mozilla-hub.atlassian.net/browse/FXP-100",
    }
