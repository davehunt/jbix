"""End-to-end tests of jbix.py's main(): a whole run against in-memory Bugzilla and Jira.

These pin down what main()'s per-tag loop does today (fetching, both caches, the
forward sync, the check, snapshots, remembered key moves, the site), so the
changes planned for it, such as fetching only updated Jira issues, are made with
tests around them. The real BugzillaClient and JiraClient are used, with only the
python-bugzilla and python-jira objects beneath them faked, and JBI's config is
a small one of the tests' own, read through the real jbix.config functions.
"""

import importlib.util
import json
import pathlib
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from jira.exceptions import JIRAError

import jbix.config as config
from tests.conftest import make_bugz, make_jira

_ROOT = pathlib.Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("jbix_main_e2e", _ROOT / "jbix.py")
jbix_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jbix_main)  # type: ignore[union-attr]

SITE = "https://mozilla-hub.atlassian.net/browse/"

CONFIG = [
    {"whiteboard_tag": "fxp", "parameters": {
        "jira_project_key": "FXP",
        "steps": {"existing": ["update_issue_summary", "maybe_update_issue_priority", "sync_see_also"]},
    }},
    # A second tag on the same Jira project, as DENG has four.
    {"whiteboard_tag": "perf", "parameters": {
        "jira_project_key": "FXP",
        "steps": {"existing": ["update_issue_summary", "maybe_update_issue_priority"]},
    }},
]


def _bug(bug_id, whiteboard, priority, summary, see_also=()):
    return SimpleNamespace(
        id=bug_id, whiteboard=whiteboard, priority=priority, summary=summary,
        see_also=[f"{SITE}{k}" for k in see_also], assigned_to="dev@example.com",
        blocks=[], depends_on=[], dupe_of=None, duplicates=[], regressed_by=[], regressions=[],
        component="Raptor", product="Testing", deadline=None, estimated_time=None, groups=[],
        keywords=[], resolution="", severity="S3", status="NEW", type="defect",
    )


def _issue(key, priority, summary, labels=("bugzilla", "fxp")):
    return SimpleNamespace(key=key, fields=SimpleNamespace(
        assignee=None, components=[], duedate=None, customfield_10441=None, labels=list(labels),
        issuelinks=[], priority=SimpleNamespace(name=priority), resolution=None,
        status=SimpleNamespace(name="Backlog"), issuetype=SimpleNamespace(name="Bug"),
        summary=summary, timeoriginalestimate=None,
    ))


class World:
    """Bugzilla and Jira as a run sees them, and a record of what the run asked."""

    def __init__(self):
        self.bugs = [
            _bug(1, "[fxp]", "P1", "In sync", ["FXP-1"]),
            _bug(2, "[fxp]", "P2", "Priority drifted", ["FXP-2"]),
            # Links an issue that has since moved project: GENAI-5 is now AIFE-5.
            _bug(3, "[fxp]", "P1", "Links a moved issue", ["FXP-3", "GENAI-5"]),
            _bug(4, "[perf]", "P1", "Perf tag, same project", ["FXP-4"]),
        ]
        self.issues = {
            "FXP": [
                _issue("FXP-1", "P1", "In sync"),
                _issue("FXP-2", "P3", "Priority drifted"),
                _issue("FXP-3", "P1", "Links a moved issue"),
                _issue("FXP-4", "P1", "Perf tag, same project", labels=("bugzilla", "perf")),
                # Labelled for fxp, but no bug links to it: an orphan.
                _issue("FXP-9", "P1", "Orphan"),
                _issue("FXP-8", "P1", "Orphan of a restricted bug"),
            ],
        }
        self.moved = {"GENAI-5": "AIFE-5"}
        # An orphan whose bug is restricted: anonymous Bugzilla cannot see bug 99,
        # and only FXP-8's remote link says it is the issue's bug.
        self.remote_links = {"FXP-8": [99]}
        self.restricted = {99}
        self.remote_link_lookups: list[str] = []
        self.live_other = {"AIFE-5"}  # issues outside the fetched projects that exist
        self.project_fetches: list[str] = []
        self.single_lookups: list[str] = []
        self.updated: list[tuple[str, dict]] = []
        self.bugzilla = None
        self.jira = None

    # -- python-bugzilla --------------------------------------------------------

    def bugzilla_client(self):
        client = MagicMock()
        client.build_query.side_effect = lambda **kw: dict(kw)

        def query(q):
            if "id" in q:  # a visibility check by bug id: restricted bugs are not returned
                ids = [int(x) for x in str(q["id"]).split(",")]
                return [SimpleNamespace(id=i, groups=[]) for i in ids if i not in self.restricted]
            if "status_whiteboard" not in q:
                return []  # see-also lookups for orphans: nothing references them
            pattern = re.compile(q["status_whiteboard"])
            hits = [b for b in self.bugs if pattern.search(b.whiteboard)]
            offset, limit = q.get("offset", 0), q.get("limit", len(hits))
            return hits[offset:offset + limit]

        client.query.side_effect = query
        client.getbugs.return_value = []

        def getbug(bug_id, **kw):
            from bugzilla.exceptions import BugzillaError
            raise BugzillaError("no", code=102 if bug_id in self.restricted else 101)

        client.getbug.side_effect = getbug
        counts = iter([10, 3] * 10)  # component total, then tagged, per count pair
        client._backend.bug_search.side_effect = lambda params: {"bug_count": next(counts)}
        return client

    # -- python-jira ------------------------------------------------------------

    def jira_client(self):
        client = MagicMock()
        client.project_components.return_value = []
        def remote_links(key):
            self.remote_link_lookups.append(key)
            return [SimpleNamespace(object=SimpleNamespace(
                url=f"https://bugzilla.mozilla.org/show_bug.cgi?id={b}")) for b in self.remote_links.get(key, [])]

        client.remote_links.side_effect = remote_links

        def search_issues(jql, **kw):
            if jql.startswith("project = "):
                project = jql.split("=", 1)[1].strip()
                self.project_fetches.append(project)
                return list(self.issues.get(project, []))
            if jql.startswith("key in ("):
                keys = jql[len("key in ("):-1].split(",")
                known = {i.key for issues in self.issues.values() for i in issues} | self.live_other
                return [SimpleNamespace(key=k) for k in keys if k in known]
            return []

        def issue(key, fields=None):
            self.single_lookups.append(key)
            current = self.moved.get(key, key)
            known = {i.key for issues in self.issues.values() for i in issues} | self.live_other
            if current not in known:
                raise JIRAError(status_code=404, text="Issue does not exist")
            found = SimpleNamespace(key=current)
            found.update = lambda fields=None, **kw: self.updated.append((current, fields))
            return found

        client.search_issues.side_effect = search_issues
        client.issue.side_effect = issue
        return client


@pytest.fixture(autouse=True)
def _restore_logging():
    """main() configures logging for a whole process: the root logger's handlers and
    level, and in check mode jbix's own loggers turned down to errors. Put them back,
    or later tests that expect a warning see none."""
    import logging
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    named = {n: logging.getLogger(n).level for n in ("jbix.jira", "jbix.bugzilla", "jbix.sync")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in named.items():
        logging.getLogger(name).setLevel(lvl)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World()
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "tags.yaml").write_text("fxp:\n  components:\n    - Testing::Raptor\n")
    (data / "groups.yaml").write_text("performance:\n  name: Performance\n  tags: [fxp, perf]\n")
    monkeypatch.setenv("JBIX_DATA_DIR", str(data))
    monkeypatch.delenv("PEOPLE_API_TOKEN", raising=False)
    monkeypatch.setattr(config, "get_config", lambda: CONFIG)
    monkeypatch.setattr(jbix_main, "BUGZILLA_API_KEY", "")  # anonymous unless a test says not

    def new_bugz(mode="preview"):
        w.bugzilla = make_bugz(mode)
        w.bugzilla.client = w.bugzilla_client()
        return w.bugzilla

    def new_jira(mode="preview"):
        w.jira = make_jira(mode)
        w.jira.client = w.jira_client()
        return w.jira

    monkeypatch.setattr(jbix_main, "BugzillaClient", new_bugz)
    monkeypatch.setattr(jbix_main, "JiraClient", new_jira)
    w.data = data
    return w


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["jbix.py", *argv])
    jbix_main.main()


def history(world, tag):
    path = world.data / "snapshots" / f"{tag}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def latest(world, tag):
    return json.loads((world.data / "latest" / f"{tag}.json").read_text())


# -- check mode -------------------------------------------------------------------

def test_check_run_records_the_drift_it_finds_and_writes_nothing(world, monkeypatch):
    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site")

    [line] = history(world, "fxp")
    assert line["jira_project"] == "FXP"
    assert line["diff"]["total_pairs"] == 3
    # Bug 2's priority, and bug 3's see-also: FXP-3 lacks its "relates" link to AIFE-5,
    # the moved issue bug 3 links.
    assert line["diff"]["drifted_pairs"] == 2
    assert line["diff"]["fields"]["priority"] == 1
    assert line["diff"]["fields"]["links"] == 1
    assert line["bugzilla"] == {"total": 10, "tagged": 3, "untagged": 7}

    detail = latest(world, "fxp")
    [row] = [r for r in detail["diff"]["rows"] if r["field"] == "priority"]
    assert (row["bug_url"], row["before"], row["after"]) == ("https://bugzil.la/2", "P3", "P2")
    orphans = [b for b in detail["broken_links"] if b["reason"] == "stale-label"]
    assert [b["jira_key"] for b in orphans] == ["FXP-9"]

    assert world.updated == []  # check mode never writes to Jira
    world.jira.client.create_issue_link.assert_not_called()
    assert not pathlib.Path("output", "site").exists()  # --no-site


def test_a_moved_issue_is_looked_up_once_and_remembered(world, monkeypatch):
    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site")
    assert "GENAI-5" in world.single_lookups
    moves = json.loads(pathlib.Path("cache", "jira_key_moves.json").read_text())
    assert moves["GENAI-5"]["key"] == "AIFE-5"

    world.single_lookups.clear()
    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site", "--refresh")
    assert "GENAI-5" not in world.single_lookups  # remembered, not looked up
    assert len(history(world, "fxp")) == 2


def test_a_warm_cache_fetches_nothing_and_records_nothing_new(world, monkeypatch):
    def tag_fetches():
        return [c for c in world.bugzilla.client.query.call_args_list
                if "status_whiteboard" in c.args[0]]

    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site")
    assert world.project_fetches == ["FXP"] and tag_fetches()

    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site")
    assert world.project_fetches == ["FXP"]  # no second Jira fetch
    assert tag_fetches() == []  # this run's Bugzilla client: tagged bugs from the cache
    assert len(history(world, "fxp")) == 1  # nothing fresh, so no new snapshot


def test_tags_sharing_a_project_fetch_it_once(world, monkeypatch):
    run(monkeypatch, "--tags", "fxp", "perf", "--mode", "check", "--no-site", "--refresh")
    assert world.project_fetches == ["FXP"]
    assert history(world, "fxp") and history(world, "perf")
    assert history(world, "perf")[0]["diff"]["total_pairs"] == 1


def test_check_run_builds_the_site(world, monkeypatch):
    run(monkeypatch, "--tags", "fxp", "--mode", "check")
    assert pathlib.Path("output", "site", "tags", "fxp.html").is_file()


# -- apply mode -------------------------------------------------------------------

def test_apply_writes_to_jira_and_drops_the_changed_project_from_the_cache(world, monkeypatch):
    monkeypatch.setattr(jbix_main, "BUGZILLA_API_KEY", "a-key")
    run(monkeypatch, "--tags", "fxp", "--mode", "apply", "--no-site")

    assert ("FXP-2", {"priority": {"name": "P2"}}) in world.updated
    world.jira.client.create_issue_link.assert_called_once()  # FXP-3 relates to AIFE-5
    assert not pathlib.Path("cache", "jira_FXP.pickle").exists()  # changed, so not reused
    assert pathlib.Path("output", "updates.csv").is_file()
    [line] = history(world, "fxp")
    assert line["diff"]["drifted_pairs"] == 2  # the drift found before applying


def test_modes_that_write_refuse_to_start_without_a_bugzilla_key(world, monkeypatch):
    with pytest.raises(SystemExit) as exit_:
        run(monkeypatch, "--tags", "fxp", "--mode", "apply")
    assert exit_.value.code == 1
    assert world.jira is None  # stopped before any client existed


# -- tags that cannot run ---------------------------------------------------------

def test_a_tag_jbi_does_not_know_is_skipped(world, monkeypatch):
    run(monkeypatch, "--tags", "fxp", "nosuchtag", "--mode", "check", "--no-site")
    assert history(world, "fxp") and not history(world, "nosuchtag")


def test_no_known_tags_exits(world, monkeypatch):
    with pytest.raises(SystemExit) as exit_:
        run(monkeypatch, "--tags", "nosuchtag", "--mode", "check")
    assert exit_.value.code == 1


def test_all_tags_less_the_excluded(world, monkeypatch):
    run(monkeypatch, "--all-tags", "--exclude-tags", "perf", "--mode", "check", "--no-site")
    assert history(world, "fxp") and not history(world, "perf")


def test_an_orphan_of_a_restricted_bug_is_not_a_broken_link(world, monkeypatch):
    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site")
    orphans = [b["jira_key"] for b in latest(world, "fxp")["broken_links"] if b["reason"] == "stale-label"]
    assert orphans == ["FXP-9"]  # FXP-8's bug is restricted, so it is not reported
    assert history(world, "fxp")[0]["broken_links"] == 1
    remembered = json.loads(pathlib.Path("cache", "jira_orphan_bugs.json").read_text())
    assert remembered["FXP-8"]["bugs"] == [99]

    world.remote_link_lookups.clear()
    run(monkeypatch, "--tags", "fxp", "--mode", "check", "--no-site", "--refresh")
    assert world.remote_link_lookups == []  # remembered, not looked up again
    assert [b["jira_key"] for b in latest(world, "fxp")["broken_links"]] == ["FXP-9"]
