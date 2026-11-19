"""Named tag groups — `--group NAME` shortcuts shared by jbix.py and the drift site.

Groups are defined in ``groups.yaml`` in the data directory (see ``jbix.paths``),
which defaults to the current directory. Each entry is either a
bare list of tags (``name: [tag, ...]``, key doubles as the label) or a mapping
``name: {name: "Display Name", tags: [tag, ...]}``.

Stdlib + pyyaml only: this module must not import credential-reading jbi modules
(``jbix.bugzilla``/``jbix.jira``/``jbix.people``) so that the site
builder (``jbix.site``) can import it without ``.env`` (same rule as ``jbix.snapshots``).
"""

import pathlib
import sys
from collections.abc import Sequence

import yaml

from jbix import paths

# None means the data directory's groups.yaml; tests set a path directly.
GROUPS_PATH: pathlib.Path | None = None


def _raw(path: pathlib.Path | None = None) -> dict:
    path = path or GROUPS_PATH or paths.groups_file()
    if path.exists():
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    return {}


def load_groups(path: pathlib.Path | None = None) -> dict[str, list[str]]:
    """Load group → tags from groups.yaml (empty dict if the file is absent).

    Accepts both the bare-list form and the ``{name, tags}`` mapping form.
    """
    out: dict[str, list[str]] = {}
    for key, val in _raw(path).items():
        out[key] = val["tags"] if isinstance(val, dict) else val
    return out


def group_display_names(path: pathlib.Path | None = None) -> dict[str, str]:
    """Load group key → display name (falls back to the key when unspecified)."""
    return {
        key: (val.get("name", key) if isinstance(val, dict) else key)
        for key, val in _raw(path).items()
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


def resolve_tags(tags: list[str], group_names: list[str], exclude: Sequence[str] = ()) -> list[str]:
    """Order-preserving, de-duplicated union of explicit tags + expanded groups,
    less any tag in exclude."""
    out: list[str] = []
    for tag in list(tags) + expand_groups(group_names):
        if tag not in out and tag not in exclude:
            out.append(tag)
    return out
