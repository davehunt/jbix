"""Named tag groups — `--group NAME` shortcuts shared by jbix.py and make_report.py.

Groups are defined in ``groups.yaml`` at the repo root. Each entry is either a
bare list of tags (``name: [tag, ...]``, key doubles as the label) or a mapping
``name: {name: "Display Name", tags: [tag, ...]}``.

Stdlib + pyyaml only: this module must not import credential-reading jbi modules
(``jbix.bugzilla``/``jbix.jira``/``jbix.people``) so that the standalone
``make_report.py`` can import it without ``.env`` (same rule as ``jbix.snapshots``).
"""

import pathlib
import sys

import yaml

GROUPS_PATH = pathlib.Path(__file__).resolve().parent.parent / "groups.yaml"


def _raw() -> dict:
    if GROUPS_PATH.exists():
        with open(GROUPS_PATH) as fh:
            return yaml.safe_load(fh) or {}
    return {}


def load_groups() -> dict[str, list[str]]:
    """Load group → tags from groups.yaml (empty dict if the file is absent).

    Accepts both the bare-list form and the ``{name, tags}`` mapping form.
    """
    out: dict[str, list[str]] = {}
    for key, val in _raw().items():
        out[key] = val["tags"] if isinstance(val, dict) else val
    return out


def group_display_names() -> dict[str, str]:
    """Load group key → display name (falls back to the key when unspecified)."""
    return {
        key: (val.get("name", key) if isinstance(val, dict) else key)
        for key, val in _raw().items()
    }


def expand_groups(names: list[str]) -> list[str]:
    """Expand group names to their tags (order-preserving union, de-duplicated).

    Exits with an error listing the available groups if a name is unknown.
    """
    groups = load_groups()
    tags: list[str] = []
    for name in names:
        if name not in groups:
            available = ", ".join(sorted(groups)) or "(none defined)"
            sys.exit(f"Unknown group {name!r}. Available groups: {available}")
        for tag in groups[name]:
            if tag not in tags:
                tags.append(tag)
    return tags


def resolve_tags(tags: list[str], group_names: list[str]) -> list[str]:
    """Order-preserving, de-duplicated union of explicit tags + expanded groups."""
    out: list[str] = []
    for tag in list(tags) + expand_groups(group_names):
        if tag not in out:
            out.append(tag)
    return out
