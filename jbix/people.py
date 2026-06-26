"""
PeopleClient: looks up Mozilla LDAP emails for Bugzilla users via people.mozilla.org.

Requires PEOPLE_API_TOKEN env var (OAuth Bearer token from people.mozilla.org).
API: GET https://people.mozilla.org/api/v4/search/simple/?q=<email>&w=all

NOTE: This integration is a **non-functioning stub** pending people.mozilla.org
API access. Without a valid PEOPLE_API_TOKEN every lookup() fails gracefully and
returns None, so assignee resolution falls back to assignee_map.yaml and then the
raw Bugzilla email. The request/parse logic below is the intended shape for when
API support is provided; it is exercised only by mocked tests today.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

PEOPLE_API_URL = os.getenv(
    "PEOPLE_API_URL", "https://people.mozilla.org/api/v4/search/simple/"
)


class PeopleClient:
    def __init__(self, token: str):
        self._token = token
        self._cache: dict[str, str | None] = {}

    def lookup(self, bugzilla_email: str) -> str | None:
        """Return the primaryEmail (Mozilla work email) for a Bugzilla address.

        Searches people.mozilla.org for the given email in all fields, then
        returns the primaryEmail of the first dino whose primaryEmail or
        secondaryEmail exactly matches. Returns None if not found or on error.
        """
        if bugzilla_email in self._cache:
            return self._cache[bugzilla_email]

        try:
            resp = requests.get(
                PEOPLE_API_URL,
                params={"q": bugzilla_email, "w": "all"},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f"PeopleClient lookup failed for {bugzilla_email!r}: {exc}")
            self._cache[bugzilla_email] = None
            return None

        for dino in data.get("dinos", []):
            primary = dino.get("primaryEmail") or ""
            secondary = dino.get("secondaryEmail") or ""
            if bugzilla_email in (primary, secondary):
                result = primary or None
                self._cache[bugzilla_email] = result
                return result

        logger.debug(f"PeopleClient: no match for {bugzilla_email!r}")
        self._cache[bugzilla_email] = None
        return None
