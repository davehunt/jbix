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

# A named tag group (see groups.yaml) — regenerates the whole reports/ site
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

Reverse sync (`--reverse`) supported for: `assignee`, `summary`, `priority`, `severity`, `whiteboard-labels`, `keyword-labels`.

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

Named shortcuts for sets of tags, used by `--group` in both `jbix.py` and `make_report.py`:

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

`--group perf` expands to that group's tags. Multiple groups (`--group perf genai`) and combining with `--tags` are supported (de-duplicated union). After a capturing run the whole `reports/` site is regenerated, including a scoped `reports/<group>.html` for every group.

## Caching

Bugzilla and Jira data are cached separately and expire after 1 hour:

- `cache/<tag>_bugzilla.pickle` — raw Bugzilla bug data
- `cache/<tag>_jira.pickle` — Jira issue data (includes a `has_remote_links` flag)

On each run, only the stale side is re-fetched. The cache is only invalidated after a run if changes were actually applied to that side — in `prompt` mode, declining all changes leaves the cache intact.

Use `--refresh` to bypass the cache and force a full re-fetch. The cache status line will show `forced (Xm old)` or `forced (no cache)` when `--refresh` is active.

## Cross-project linking

When `--dependencies`, `--duplicates`, or `--see-also` are enabled, bugs referenced by those fields that do not carry the whiteboard tag are fetched separately from Bugzilla and injected as lightweight lookup entries. This allows sync functions to create Jira issue links even when the dependency lives in a different Bugzilla component or Jira project.

`--see-also` also handles Jira URLs in a bug's `see_also` field that point to other Jira projects, creating "Relates" links directly.

## Output

All sync operations append to `output/updates.csv` for audit trail purposes.

`--mode check` prints per-field diff counts for all enabled fields, followed by a **drift score** — the percentage of linked bug/issue pairs that have at least one field out of sync.

`--mode check` also reports **broken Jira links** — Bugzilla `see_also` references whose Jira issue has been deleted or moved (verified, so renames aren't flagged) — and writes the actionable list to `output/broken_links_<tag>.csv` (`bug_id, bug_url, jira_key, jira_url`).

Fuzzy link candidate matching (`--link-candidates`) writes `output/link_candidates_<tag>.csv`; tune the match cutoff with `--threshold PCT` (default 85).

## Drift report

Every forward run records a compact per-tag snapshot to `snapshots/<tag>.jsonl` and (unless `--no-report`) regenerates the self-contained `reports/` site covering the latest snapshot of every tag:

- `reports/index.html` — full report, with a "Group views" nav to each group summary
- `reports/<group>.html` — one summary per group in `groups.yaml`
- `reports/tags/<tag>.html` — per-tag drilldown: every drifted field (bug/Jira links + current → should-be values, for hand repairs) and the full broken-links table

Summary cards show coverage and drift over time; the "fields out of sync" and "broken links" numbers link into the tag page's detail sections. Rebuild on demand with:

```bash
python make_report.py                          # whole site → reports/
python make_report.py --tags fxp fxpe          # scoped reports/index.html (overwrites the full index)
python make_report.py --group perf             # group → reports/perf.html
python make_report.py --group perf genai        # one group file each
```

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
make_report.py       # Build the reports/ site from the snapshots/ store
tags.yaml            # Component lists per whiteboard tag (for health check)
groups.yaml          # Named tag groups (display name + tags) for --group
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
  snapshots.py       # Per-tag drift snapshot store + aggregate_metrics
  groups.py          # Named tag groups (groups.yaml loader)
tests/               # pytest suite (one test_*.py per module)
```

Generated at runtime (all gitignored): `cache/` (data + JBI config), `snapshots/`
(drift history), `reports/` (HTML site), `output/` (audit/broken-link/candidate CSVs).

## License

[Mozilla Public License 2.0](LICENSE).
