<img src="jbix/assets/wordmark.svg" alt="jbix" width="240">

# jbix

Bidirectional synchronisation tool that keeps Mozilla's Bugzilla and Jira instances consistent for specific projects, with drift health reporting. It extends [JBI](https://github.com/mozilla/jira-bugzilla-integration) with reverse sync, consistency checks, and an HTML drift report.

> **Status:** Internal Mozilla tooling, shared publicly as-is under MPL-2.0. It drives live Bugzilla and Jira instances and requires API credentials (see [Setup](#setup)).

## Usage

`--mode` chooses what a run does: **check** (default) — health check + drift report, no changes; **preview** — dry-run forward sync; **prompt** — confirm each change; **apply** — write changes.

```bash
# Health check + drift report (default mode)
python jbix.py --tags fxp

# Dry-run: show what a forward sync would change
python jbix.py --tags fxp --mode preview

# Multiple tags
python jbix.py --tags fxp fxpe --mode preview

# Every whiteboard tag in the JBI config
python jbix.py --all-tags --mode check

# Every tag except some (also works with --tags and --group)
python jbix.py --all-tags --exclude-tags bz-ffx-core bz-ffx-firefox --mode check

# A named tag group (see groups.yaml)
python jbix.py --group perf --mode check

# Apply changes
python jbix.py --tags fxp --mode apply

# Prompt before each change
python jbix.py --tags fxp --mode prompt

# Force re-fetch (bypass 1-hour cache)
python jbix.py --tags fxp --refresh

# Override JBI defaults: disable a field
python jbix.py --tags fxp --no-priority

# Add extension fields (not managed by JBI)
python jbix.py --tags fxp --time-tracking --remote-links

# Manual mode: ignore JBI config, sync only the fields you pass
python jbix.py --tags fxp --manual --priority --severity

# Reverse sync: Jira → Bugzilla
python jbix.py --tags fxp --reverse --priority --severity

# Health check: consistency metrics
python jbix.py --tags fxp --mode check

# Health check with fuzzy link candidate matching
python jbix.py --tags fxp --mode check --link-candidates
```

Reads [JBI's production config](https://github.com/mozilla/jira-bugzilla-integration/blob/main/config/config.prod.yaml) to auto-detect which fields to sync for each whiteboard tag.

Fields auto-detected from JBI config: `summary`, `assignee`, `components`, `whiteboard-labels`, `keyword-labels`, `priority`, `severity`, `status`, `resolution`, `type`, `dependencies`, `see-also`, `duplicates`, `regressions`. (`dependencies` covers both Bugzilla `depends_on` and `blocks`.) Disable any with `--no-<field>`, or pass `--manual` to ignore JBI config and sync only the fields you name.

Extension fields (opt-in, never from JBI config): `--time-tracking`, `--remote-links`.

Reverse sync (`--reverse`) supported for: `assignee`, `summary`, `priority`, `severity`, `whiteboard-labels`, `keyword-labels`, `type`.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
cp .env.example .env                          # then fill in API credentials
cp assignee_map.example.yaml assignee_map.yaml  # optional: Bugzilla→Jira assignee overrides
```

The Bugzilla/Jira/people-directory hostnames default to Mozilla's instances; override them via the
env vars below for other deployments.

## Configuration

### `.env`

```
BUGZILLA_URL=bugzilla.mozilla.org
BUGZILLA_API_KEY=...
JIRA_URL=https://mozilla-hub.atlassian.net
JIRA_API_USER=user@mozilla.com
JIRA_API_KEY=...

# Optional: pre-migration Jira host. see_also links using this host are treated
# as aliases of JIRA_URL (issue keys were preserved). Set empty to disable.
JIRA_LEGACY_URL=https://jira.mozilla.com

# Optional: enables automatic Bugzilla→LDAP email lookup for assignee sync
PEOPLE_API_TOKEN=...
```

`BUGZILLA_API_KEY` is optional in `--mode check`, the default. Without it Bugzilla is queried anonymously, which returns public bugs only, so restricted bugs never reach a snapshot or the drift report; the run says so at the start. Every other mode needs the key and refuses to start without it.

`JIRA_LEGACY_URL` (default `https://jira.mozilla.com`) makes the tool recognize legacy-host `see_also` links as linked, so they count toward linkage and aren't mis-reported as broken/orphaned. Set it empty to recognize only `JIRA_URL`.

`PEOPLE_API_TOKEN` is a Bearer token from [people.mozilla.org](https://people.mozilla.org). When set, `sync_assignee` looks up unmapped Bugzilla email addresses against Mozilla's people directory to find the canonical work email for Jira. Explicit overrides in `assignee_map.yaml` take precedence (see below).

> **Note:** the people-directory integration is currently a **non-functioning stub** pending people.mozilla.org API access. Lookups fail gracefully (returning nothing), so assignee resolution falls back to `assignee_map.yaml` and then the raw Bugzilla email. Use `assignee_map.yaml` for now.

### `assignee_map.yaml`

Optional Bugzilla-email → Jira-email overrides consulted first by `sync_assignee` (use `null` to mark "no Jira account — skip"). It is gitignored because it holds personal addresses; copy `assignee_map.example.yaml` to create your own. If absent, the map is empty and resolution falls back to the people-directory lookup and then the raw Bugzilla email.

### `tags.yaml`

Defines Bugzilla component lists used by the `--mode check` component counts, keyed by whiteboard tag:

```yaml
fxp:
  components:
    - Testing::Raptor
    - Testing::Performance
    - Core::Gecko Profiler
    - "Firefox Profiler::*"   # wildcard: all components in this product
```

`jbix.py` reads Jira project keys and sync steps from JBI's live config (cached 24h at `cache/jbi_config.yaml`). `tags.yaml` is only consulted for the component list in `--mode check`.

### `groups.yaml`

Named shortcuts for sets of tags, used by `jbix.py --group` and rendered as group views in the report:

```yaml
perf:
  name: Performance
  tags: [fp, fxp, fxpe, pcf]
genai:
  name: GenAI
  tags: [aife, aimodels, aiplatform, aiact, aiasst, aiaug, aidisc]
# A bare list also works (the key doubles as the display name):
deng: [dataplatform, dataquality, fog-migration, glean-sdk-jira]
```

`--group perf` expands to that group's tags. Multiple groups (`--group perf genai`) and combining with `--tags` are supported (de-duplicated union). After a capturing run the drift site is rebuilt in full, so every group in `groups.yaml` gets its page regardless of which tags were processed.

## Caching

Bugzilla and Jira data are cached separately and expire after 1 hour:

- `cache/<tag>_bugzilla.pickle` — raw Bugzilla bug data, per whiteboard tag
- `cache/jira_<PROJECT>.pickle` — Jira issue data, per Jira project

On each run, only the stale side is re-fetched. Several tags can map to one Jira project, so a project is fetched at most once per run and shared by every tag that uses it, shown as `shared (<tag>)` in the fetch summary. The cache is only invalidated after a run if changes were actually applied to that side — in `prompt` mode, declining all changes leaves the cache intact.

Use `--refresh` to bypass the cache and force a full re-fetch. The cache status line will show `forced (Xm old)` or `forced (no cache)` when `--refresh` is active.

## Cross-project linking

When `--dependencies`, `--duplicates`, or `--see-also` are enabled, bugs referenced by those fields that do not carry the whiteboard tag are fetched separately from Bugzilla and injected as lightweight lookup entries. This allows sync functions to create Jira issue links even when the dependency lives in a different Bugzilla component or Jira project.

`--see-also` also handles Jira URLs in a bug's `see_also` field that point to other Jira projects, creating "Relates" links directly.

## Output

All sync operations append to `output/updates.csv` for audit trail purposes.

`--mode check` prints per-field diff counts for all enabled fields, followed by a **drift score** — the percentage of linked bug/issue pairs that have at least one field out of sync.

`--mode check` also reports **broken Jira links** — Bugzilla `see_also` references whose Jira issue has been deleted or moved (verified, so renames aren't flagged) — and writes the actionable list to `output/broken_links_<tag>.csv` (`bug_id, bug_url, jira_key, jira_url`).

Fuzzy link candidate matching (`--link-candidates`) writes `output/link_candidates_<tag>.csv`; tune the match cutoff with `--threshold PCT` (default 85).

## Drift site

Every forward run records two things per tag in the data directory, then (unless `--no-site`) rebuilds the drift site in `output/site/`:

- `snapshots/<tag>.jsonl` — the history: one compact line of counts per run, a few hundred bytes, so a year of daily runs stays small enough to keep in git
- `latest/<tag>.json` — the latest run's detail, overwritten each run: the drifted fields with their current and expected values, the broken links, and links to live Bugzilla and Jira queries
- `latest/<tag>.csv` — every change the latest run found, uncapped: the tag page lists the first 500 and links to this as `tags/<tag>-changes.csv`, with a column saying what syncing from Bugzilla would change (`+ todo`, `P3 → P1`)

The site is plain static HTML with relative links, so it works from disk or any web server:

| Page | Shows |
|---|---|
| `index.html` | totals, drift over time, every group, and every tag ranked by drift |
| `groups/<group>.html` | the same for one group in `groups.yaml` |
| `tags/<tag>.html` | one tag: its trend, which fields drift, each bug and issue out of sync, its broken links |

Charts are inline SVG, and it has a light and a dark theme, following the system unless the reader picks one. Build it on its own, from any data directory:

```bash
uv run jbix-site                                    # current directory → output/site/
jbix-site --data-dir ../jbi-drift --out _site       # another repository's data
```

The package installs on its own (`pip install git+https://github.com/davehunt/jbix`), so a repository holding only data can build the site in CI. Each build replaces the output directory whole, so a tag that is gone leaves no stale page; it refuses to replace a directory it did not build.

## Testing

```bash
uv run pytest
uv run pytest --cov
uv run ruff check .
```

## Contributing

Issues and pull requests are welcome. Before opening a PR, please run `uv run ruff check .` and `uv run pytest` and keep both green. Note that the tool talks to live Bugzilla/Jira instances, so most behaviour is exercised through the test suite (which mocks both APIs) rather than against real services.

## Project layout

```
jbix.py               # CLI: tag-driven sync, reverse sync, health check
tags.yaml            # Component lists per whiteboard tag (for health check); data directory
groups.yaml          # Named tag groups (display name + tags) for --group; data directory
jbix/
  __init__.py
  config.py          # JBI config loader (24h cache)
  constants.py       # Colors, ASSIGNEE_MAP, default field mappings
  bugzilla.py        # BugzillaClient + build_component_query()
  jira.py            # JiraClient
  people.py          # PeopleClient: Bugzilla→LDAP email lookup via people.mozilla.org
  sync.py            # sync_* and reverse_sync_* functions
  health.py          # Health check + diff check with drift score
  fuzzy.py           # Fuzzy matching for link candidates
  snapshots.py       # Per-tag history + latest-run store, aggregate_metrics
  groups.py          # Named tag groups (groups.yaml loader)
  paths.py           # The data directory (JBIX_DATA_DIR) and the files in it
  site.py, site.css  # The static drift site (jbix-site)
  assets/            # The jbix mark (icon.svg, favicon.ico) and wordmark (wordmark.svg)
tests/               # pytest suite (one test_*.py per module)
```

Generated at runtime (all gitignored): `cache/` (data + JBI config), `snapshots/`
and `latest/` (drift history and latest detail), `output/` (`site/` + audit/broken-link/candidate CSVs).

## License

[Mozilla Public License 2.0](LICENSE).
