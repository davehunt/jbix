"""Where jbix keeps its data: one directory, set by ``JBIX_DATA_DIR``.

The data directory holds what a run records and what the drift site is built
from, so that another repository can keep them and point jbix at itself:

- ``snapshots/<tag>.jsonl`` — compact per-tag history, one line per run
- ``latest/<tag>.json`` — the latest run's per-bug detail for each tag
- ``groups.yaml`` and ``tags.yaml`` — named tag groups and per-tag components

It defaults to the current directory, which is the repository root when jbix is
run from a checkout. Paths are resolved when called, not at import, so a test or
``--data-dir`` can change the variable first.

Stdlib only: imported by the site builder, which must run without credentials.
"""

import os
import pathlib

DATA_DIR_ENV = "JBIX_DATA_DIR"


def data_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(DATA_DIR_ENV) or ".")


def snapshots_dir() -> pathlib.Path:
    return data_dir() / "snapshots"


def latest_dir() -> pathlib.Path:
    return data_dir() / "latest"


def groups_file() -> pathlib.Path:
    return data_dir() / "groups.yaml"


def tags_file() -> pathlib.Path:
    return data_dir() / "tags.yaml"
