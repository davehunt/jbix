"""Tests for jbix/site.py — the static drift site."""

import json
import re
from datetime import date, datetime
from html.parser import HTMLParser

import pytest

import jbix.snapshots as snapshots
from jbix import site
from jbix.config import compare_steps


def _health(pairs_total=100, linked=40):
    return {
        "bugzilla": {"total": 50, "tagged": 20, "untagged": 30,
                     "bugs_url": "https://bugzilla.mozilla.org/buglist.cgi?all",
                     "untagged_url": "https://bugzilla.mozilla.org/buglist.cgi?untagged"},
        "jira": {"total": pairs_total, "linked": linked, "unlinked": pairs_total - linked,
                 "project_url": "https://mozilla-hub.atlassian.net/browse/FP",
                 "unlinked_url": "https://mozilla-hub.atlassian.net/issues/?jql=x"},
    }


def _diff(pairs=40, drifted=4, rows=None, fields=None):
    return {
        "total_pairs": pairs, "drifted_pairs": drifted, "drift_pct": drifted / pairs,
        "fields": [{"name": k, "n": v, "pct": 0} for k, v in (fields or {"priority": drifted, "summary": 0}).items()],
        "rows": rows if rows is not None else [
            {"bug_url": "https://bugzil.la/1", "jira_url": "https://mozilla-hub.atlassian.net/browse/FP-1",
             "field": "priority", "before": "P3", "after": "P1"}],
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", d / "snapshots")
    monkeypatch.setattr(snapshots, "LATEST_DIR", d / "latest")
    (d).mkdir()
    (d / "groups.yaml").write_text("perf:\n  name: Performance\n  tags: [fp, fxp]\nempty: [nothing]\n")
    return d


def _record(tag, day, project="FP", broken=None, **kw):
    snapshots.record_snapshot(tag, project, _health(), _diff(**kw),
                              datetime.combine(day, datetime.min.time()), broken_links=broken)


def _build(data_dir, tmp_path):
    return site.build_site(tmp_path / "out", data_dir)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ("href", "src"):
                self.hrefs.append(v)


# -- loading -------------------------------------------------------------------

def test_history_keeps_the_last_run_of_each_day(data_dir):
    _record("fp", date(2026, 11, 1), drifted=4)
    snapshots.record_snapshot("fp", "FP", _health(), _diff(drifted=2), datetime(2026, 11, 1, 18, 0))
    _record("fp", date(2026, 11, 2), drifted=1)
    tag = site.load_tags(data_dir)["fp"]
    assert [(p.day.day, p.drifted) for p in tag.history] == [(1, 2), (2, 1)]
    assert tag.project == "FP"
    assert tag.latest["diff"]["rows_total"] == 1


def test_combine_carries_a_tag_forward_on_days_it_missed():
    a = site.Tag("a", "A", [site.Point(date(2026, 1, 1), pairs=10, drifted=1),
                            site.Point(date(2026, 1, 3), pairs=10, drifted=3)])
    b = site.Tag("b", "B", [site.Point(date(2026, 1, 2), pairs=10, drifted=5)])
    series = site.combine([a, b])
    assert [(p.day.day, p.pairs, p.drifted) for p in series] == [(1, 10, 1), (2, 20, 6), (3, 20, 8)]


@pytest.mark.parametrize("ratio, text", [(0, "0%"), (0.00005, "<0.01%"), (0.0087, "0.87%"),
                                         (0.586, "58.6%"), (1, "100.0%")])
def test_pct(ratio, text):
    assert site.pct(ratio) == text


# -- the built site ------------------------------------------------------------

def test_builds_every_page_and_every_link_resolves(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    _record("fxp", date(2026, 11, 2), project="FXP")
    _record("omc", date(2026, 11, 2), project="OMC")
    out = _build(data_dir, tmp_path)
    pages = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.html"))
    assert pages == ["groups/perf.html", "index.html", "tags/fp.html", "tags/fxp.html", "tags/omc.html"]
    assert (out / "style.css").is_file() and (out / site.MARKER).is_file()
    for page in out.rglob("*.html"):
        parser = _Links()
        parser.feed(page.read_text())
        for href in parser.hrefs:
            if href.startswith(("http://", "https://", "#")):
                continue
            target = (page.parent / href.split("#")[0]).resolve()
            assert target.is_file(), f"{page.name} links to missing {href}"


def test_empty_groups_get_no_page_and_no_nav_entry(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    assert not (out / "groups" / "empty.html").exists()
    assert "empty" not in (out / "index.html").read_text()


def test_tag_page_shows_detail_and_flags_a_stale_tag(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    _record("fxp", date(2026, 11, 3), project="FXP")
    out = _build(data_dir, tmp_path)
    fp = (out / "tags" / "fp.html").read_text()
    assert "Last captured on 1 Nov 2026" in fp
    assert "https://bugzil.la/1" in fp and "FP-1" in fp
    assert "Matching everywhere: summary." in fp
    assert "Last captured" not in (out / "tags" / "fxp.html").read_text()


def test_shared_project_counts_the_other_tags_links(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    _record("fp-extra", date(2026, 11, 1))  # same FP project, 40 linked of its own
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert "other tags link 40 more" in doc


def test_theme_control_and_head_script_are_on_every_page(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    for page in out.rglob("*.html"):
        doc = page.read_text()
        assert 'id="theme-toggle"' in doc
        assert doc.index('localStorage.getItem("theme")') < doc.index("</head>")
        assert '<meta name="color-scheme" content="light dark">' in doc


def test_data_is_escaped_and_only_web_links_are_followed(data_dir, tmp_path):
    rows = [{"bug_url": "javascript:alert(1)", "jira_url": "https://j/FP-2",
             "field": "summary", "before": "<script>x()</script>", "after": "a & b"}]
    _record("fp", date(2026, 11, 1), rows=rows)
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert "<script>x()</script>" not in doc and "&lt;script&gt;x()&lt;/script&gt;" in doc
    assert 'href="javascript:' not in doc
    assert "a &amp; b" in doc


def test_capped_rows_say_how_many_there_are(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "_DRIFT_ROWS_CAP", 2)
    rows = [{"bug_url": f"https://bugzil.la/{i}", "jira_url": f"https://j/FP-{i}",
             "field": "priority", "before": "P3", "after": "P1"} for i in range(5)]
    _record("fp", date(2026, 11, 1), rows=rows)
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert "Listing 2 of 4 drifted pairs." in doc
    assert '<a href="fp-changes.csv" download>Download all 5 changes (CSV)</a>' in doc


def test_rebuild_replaces_the_site_and_drops_stale_pages(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    _record("omc", date(2026, 11, 1), project="OMC")
    out = _build(data_dir, tmp_path)
    assert (out / "tags" / "omc.html").exists()
    (data_dir / "snapshots" / "omc.jsonl").unlink()
    _build(data_dir, tmp_path)
    assert not (out / "tags" / "omc.html").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temp dirs left


def test_refuses_to_replace_a_directory_it_did_not_build(data_dir, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "precious.txt").write_text("keep me")
    with pytest.raises(SystemExit, match="refusing"):
        _build(data_dir, tmp_path)
    assert (tmp_path / "out" / "precious.txt").exists()


def test_no_snapshots_builds_a_placeholder(tmp_path):
    out = site.build_site(tmp_path / "out", tmp_path)
    assert "No snapshots yet" in (out / "index.html").read_text()


def test_cli_reads_data_dir_from_the_environment(data_dir, tmp_path, monkeypatch):
    _record("fp", date(2026, 11, 1))
    monkeypatch.setenv("JBIX_DATA_DIR", str(data_dir))
    site.main(["--out", str(tmp_path / "cli-out")])
    assert (tmp_path / "cli-out" / "tags" / "fp.html").is_file()


# -- charts --------------------------------------------------------------------

def test_line_chart_with_one_point_draws_a_dot_and_no_line():
    svg = site.line_chart([site.Point(date(2026, 11, 1), pairs=10, drifted=1)], "Drift")
    assert 'class="dot"' in svg and 'class="line"' not in svg
    assert "10.0%" in svg


def test_line_chart_all_zero_has_a_sensible_axis():
    pts = [site.Point(date(2026, 11, d), pairs=10, drifted=0) for d in (1, 2, 3)]
    svg = site.line_chart(pts, "Drift")
    assert ">1%</text>" in svg and 'class="line"' in svg


def test_line_chart_hover_data_matches_points():
    pts = [site.Point(date(2026, 11, d), pairs=10, drifted=d) for d in (1, 2)]
    svg = site.line_chart(pts, "Drift")
    data = json.loads(re.search(r'data-points="([^"]+)"', svg).group(1).replace("&quot;", '"'))
    assert [d[3] for d in data] == ["10.0%", "20.0%"]
    assert "1 of 10 pairs" in data[0][2]


def test_sparkline_needs_two_points():
    assert "—" in site.sparkline([site.Point(date(2026, 11, 1))])
    assert "<svg" in site.sparkline([site.Point(date(2026, 11, d), pairs=1) for d in (1, 2)])


# -- review feedback: diffs, highlights, groups, nav -----------------------------

def test_change_shows_list_additions_and_removals():
    html = site.change("labels", ["bugzilla", "fxp", "stale"], ["bugzilla", "fxp", "todo"])
    assert "<ins>+ todo</ins>" in html and "<del>− stale</del>" in html
    assert "bugzilla" not in html  # unchanged values are not repeated


def test_change_shows_a_link_to_create_or_remove():
    assert site.change("links", "", "duplicates FXP-4641") == "<ins>+ duplicates FXP-4641</ins>"
    assert site.change("links", "relates to FXP-1", "") == "<del>− relates to FXP-1</del>"


def test_change_shows_the_value_a_scalar_replaces():
    html = site.change("priority", "", "P2")
    assert "<del>none</del>" in html and "<ins>P2</ins>" in html


def test_drift_table_has_one_row_per_pair(data_dir, tmp_path):
    """And explains how to read the changes above them."""
    rows = [{"bug_url": "https://bugzil.la/1", "jira_url": "https://j/FP-1", "field": f,
             "before": "", "after": "x"} for f in ("priority", "severity")]
    rows.append({"bug_url": "https://bugzil.la/2", "jira_url": "https://j/FP-2",
                 "field": "priority", "before": "P3", "after": "P1"})
    _record("fp", date(2026, 11, 1), rows=rows)
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    table = doc[doc.index('<table class="drift">'):]
    table = table[:table.index("</table>")]
    assert table.count("<tr") == 3  # the header row and two pairs
    assert table.count('class="change-field"') == 3
    assert "Bugzilla is the authority" in doc


def test_problem_counts_are_highlighted_only_when_not_zero(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), broken=[{"bug_id": 1, "jira_key": "FP-9", "reason": "dead-jira"}])
    _record("fxp", date(2026, 11, 1), project="FXP", drifted=0, rows=[])
    out = _build(data_dir, tmp_path)
    fp = (out / "tags" / "fp.html").read_text()
    assert 'class="tile warn"' in fp and 'class="tile crit"' in fp
    fxp = (out / "tags" / "fxp.html").read_text()
    assert 'class="tile warn"' not in fxp and 'class="tile crit"' not in fxp
    # Zero is good news: both problem tiles turn green with a check.
    assert fxp.count('class="tile good"') == 2


def test_zero_drift_with_no_pairs_is_not_good_news(data_dir, tmp_path):
    # A tag with no linked pairs records no drift at all, as jbix.health does.
    snapshots.record_snapshot("empty", "EMPTY", _health(), None, datetime(2026, 11, 1))
    doc = (_build(data_dir, tmp_path) / "tags" / "empty.html").read_text()
    assert doc.count('class="tile good"') == 1  # broken links only


def test_groups_are_sorted_by_pairs_out_of_sync_and_have_sparklines(data_dir, tmp_path):
    (data_dir / "groups.yaml").write_text("calm: [fp]\nbusy: [fxp]\n")
    for day in (1, 2):
        _record("fp", date(2026, 11, day), drifted=1)
        _record("fxp", date(2026, 11, day), project="FXP", drifted=9)
    doc = (_build(data_dir, tmp_path) / "index.html").read_text()
    groups = doc[doc.index('id="groups"'):doc.index('id="tags"')]
    assert groups.index("busy") < groups.index("calm")
    assert groups.count('class="spark"') == 2


def test_groups_are_in_a_menu_and_the_lede_carries_no_date(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    index = (out / "index.html").read_text()
    header = index[index.index('<header class="site">'):index.index("</header>")]
    assert '<details class="menu"><summary>Groups<svg class="chevron"' in header
    assert 'href="groups/perf.html"' in header
    lede = index[index.index('<p class="lede">'):]
    assert "2026" not in lede[:lede.index("</p>")]
    group = (out / "groups" / "perf.html").read_text()
    assert '<summary class="here">Groups<svg class="chevron"' in group
    assert 'href="../groups/perf.html" aria-current="page"' in group


def test_overview_introduces_jbi_and_drift_uses_one_term(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    index = (out / "index.html").read_text()
    assert f'<a href="{site.JBI_URL}">Jira Bugzilla Integration</a> (JBI)' in index
    lede = index[index.index('<p class="lede">'):]
    assert f'It is built by <a href="{site.REPO_URL}">JBIX</a>' in lede[:lede.index("</p>")]
    assert "<title>JBIX Drift Report</title>" in index
    for page in out.rglob("*.html"):
        assert "out of sync" not in page.read_text().lower()


def test_tables_are_sortable_with_drift_as_the_starting_order(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), drifted=1)
    _record("fxp", date(2026, 11, 1), project="FXP", drifted=9)
    index = (_build(data_dir, tmp_path) / "index.html").read_text()
    tags = index[index.index('<table class="tags sortable">'):]
    tags = tags[:tags.index("</table>")]
    assert '<th scope="col" class="n" aria-sort="descending" data-sort="num"><button type="button">Drift</button>' in tags
    assert tags.index(">fxp<") < tags.index(">fp<")
    assert 'data-v="0.225"' in tags  # 9 of 40 pairs, raw for sorting
    assert "flag" not in tags and "<svg class=\"icon\"" not in tags  # plain counts in tables


def test_drifted_pairs_can_be_filtered_by_field(data_dir, tmp_path):
    rows = [{"bug_url": "https://bugzil.la/1", "jira_url": "https://j/FP-1", "field": "assignee",
             "before": "", "after": "a"},
            {"bug_url": "https://bugzil.la/1", "jira_url": "https://j/FP-1", "field": "remote links",
             "before": "", "after": "x"},
            {"bug_url": "https://bugzil.la/2", "jira_url": "https://j/FP-2", "field": "assignee",
             "before": "", "after": "b"}]
    # The menu counts come from the field totals, which cover every pair, not
    # only the ones listed.
    _record("fp", date(2026, 11, 1), rows=rows, drifted=30, fields={"assignee": 25, "remote links": 9})
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert '<select id="field-filter"><option value="">All fields (30)</option>' in doc
    assert '<option value="assignee">assignee (25)</option>' in doc
    assert '<option value="remote links">remote links (9)</option>' in doc
    assert 'data-fields="assignee|remote links"' in doc
    assert '<li data-field="remote links">' in doc
    assert 'href="#field=' not in doc  # the field bars are plain text


def test_field_bar_shows_count_then_share():
    html = site.field_bars([{"name": "priority", "n": 10}, {"name": "summary", "n": 0}], 2416)
    assert '<span class="bar-value">10 <span class="muted">(0.41%)</span></span>' in html


def test_theme_toggle_has_a_sliding_highlight(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    doc = (_build(data_dir, tmp_path) / "index.html").read_text()
    assert '<div id="theme-toggle" role="group" aria-label="Theme"><span class="thumb" aria-hidden="true"></span>' in doc
    assert "toggle.dataset.current = t" in doc


def test_every_change_is_downloadable_beside_the_tag_page(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "_DRIFT_ROWS_CAP", 1)
    rows = [{"bug_url": f"https://bugzil.la/{i}", "jira_url": f"https://j/FP-{i}",
             "field": "labels", "before": ["a"], "after": ["a", f"x{i}"]} for i in range(3)]
    _record("fp", date(2026, 11, 1), rows=rows)
    out = _build(data_dir, tmp_path)
    lines = (out / "tags" / "fp-changes.csv").read_text().splitlines()
    assert lines[0] == "bug,jira_issue,field,change,jira_value,bugzilla_value"
    assert len(lines) == 4  # every change, though the page lists one
    assert lines[1] == "https://bugzil.la/0,https://j/FP-0,labels,+ x0,a,a; x0"


def test_no_drift_means_no_download(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), drifted=0, rows=[])
    out = _build(data_dir, tmp_path)
    assert not (out / "tags" / "fp-changes.csv").exists()
    assert "Download" not in (out / "tags" / "fp.html").read_text()


def test_the_mark_is_in_the_header_and_the_favicon(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    assert (out / "icon.svg").read_text().startswith("<svg")
    assert (out / "favicon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"  # an ICO header
    tag = (out / "tags" / "fp.html").read_text()
    brand = tag[tag.index('<a class="brand"'):tag.index("</a>", tag.index('<a class="brand"'))]
    assert 'class="wordmark" role="img" aria-label="JBIX"' in brand
    assert '<g class="letters" fill="currentColor">' in brand
    assert "<style>" not in brand and "<title" not in brand
    assert brand.endswith("<span>Drift Report</span>")
    assert '<link rel="icon" href="../favicon.ico" sizes="32x32">' in tag


def test_overview_is_titled_overview_and_the_wordmark_is_only_in_the_header(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    index = (out / "index.html").read_text()
    assert "<h1>Overview</h1>" in index
    assert "<title>JBIX Drift Report</title>" in index
    assert index.count('class="wordmark"') == 1
    assert 'id="green-underpass-header"' in index and 'id="green-underpass"' not in index
    # Its colours are the theme's, so dark mode can lighten them.
    brand = index[index.index('<a class="brand"'):index.index("</a>", index.index('<a class="brand"'))]
    assert "var(--mark-blue)" in brand and "var(--mark-green)" in brand


def test_fixing_section_offers_only_a_preview(data_dir, tmp_path):
    _record("fxdroid", date(2026, 11, 1), project="FXDROID",
            fields={"assignee": 3, "labels": 2, "links": 1, "summary": 0})
    doc = (_build(data_dir, tmp_path) / "tags" / "fxdroid.html").read_text()
    section = doc[doc.index('id="fixing"'):doc.index('id="broken"')]
    code = section[section.index('<code id="fix-command"'):section.index("</code>")]
    assert code.endswith(">python jbix.py --tags fxdroid --mode preview")
    by_field = json.loads(re.search(r'data-by-field="([^"]+)"', code).group(1).replace("&quot;", '"'))
    assert by_field == {
        "assignee": "python jbix.py --tags fxdroid --manual --assignee --mode preview",
        "labels": "python jbix.py --tags fxdroid --manual --whiteboard-labels --keyword-labels --mode preview",
        "links": "python jbix.py --tags fxdroid --manual --dependencies --duplicates --see-also"
                 " --regressions --mode preview",
    }
    # Nothing that can be copied whole changes anything: prompt and apply are named, not given.
    assert all("--mode preview" in c for c in by_field.values())
    assert "jbix.py --tags fxdroid --mode apply" not in doc and "--mode prompt</code> to approve" in section
    assert "after its next run is deployed" in section
    assert "python jbix.py --tags fxdroid --mode check</code>, which rebuilds the report" in section
    assert f'Once it is <a href="{site.REPO_URL}#setup">set up</a>, start with a preview' in section
    assert "its README" not in section
    callout = section[section.index('<aside class="callout warn"'):section.index("</aside>")]
    assert "Before you apply" in callout and callout.count("<li>") == 3
    assert "<code>--manual</code>" in section


def test_no_drift_means_no_fixing_section(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), drifted=0, rows=[])
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert 'id="fixing"' not in doc
    drift = doc[doc.index('id="drift"'):doc.index('id="broken"')]
    assert '<p class="all-clear good">' in drift and "None.</p>" in drift
    assert "Bugzilla is the authority" not in drift


# -- broken links: the trend, and every link in a group -------------------------

def _broken(n, reason="dead-jira", key="FP"):
    return [{"direction": "bug→jira", "bug_id": i, "bug_url": f"https://bugzil.la/{i}",
             "jira_key": f"{key}-{i}", "jira_url": f"https://j/{key}-{i}", "reason": reason}
            for i in range(1, n + 1)]


def test_every_page_charts_broken_links_over_time(data_dir, tmp_path):
    for day, n in ((1, 3), (2, 5)):
        _record("fp", date(2026, 11, day), broken=_broken(n))
    out = _build(data_dir, tmp_path)
    for page in ("index.html", "groups/perf.html", "tags/fp.html"):
        doc = (out / page).read_text()
        assert "<figcaption>Broken links</figcaption>" in doc, page
        assert "5 broken links" in doc  # the hover readout for the last day


def test_a_count_axis_tops_out_at_a_round_number_with_a_whole_half():
    assert [site._nice_count_max(v) for v in (0.5, 3, 5, 11, 39, 41, 950)] == [2, 4, 10, 20, 40, 100, 1000]
    pts = [site.Point(date(2026, 11, d), broken=b) for d, b in ((1, 3), (2, 7))]
    svg = site.line_chart(pts, "Broken links", "broken")
    assert ">10</text>" in svg and ">5</text>" in svg and "%" not in svg


def test_a_group_lists_its_tags_broken_links_with_a_csv(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), broken=_broken(2))
    _record("fxp", date(2026, 11, 1), project="FXP", broken=_broken(1, "stale-label", "FXP"))
    out = _build(data_dir, tmp_path)
    doc = (out / "groups" / "perf.html").read_text()
    import html as html_mod
    section = html_mod.unescape(doc[doc.index('<h2 id="broken">'):])
    assert section.count('<a href="../tags/fp.html#broken">fp</a>') == 2
    assert '<a href="../tags/fxp.html#broken">fxp</a>' in section
    assert '<a href="perf-broken-links.csv" download>Download all 3 (CSV)</a>' in section
    lines = (out / "groups" / "perf-broken-links.csv").read_text().splitlines()
    assert lines[0] == "tag,direction,bug_id,bug_url,jira_key,jira_url,why"
    assert len(lines) == 4
    # How to fix each kind present, and only those
    assert "Remove or correct the see-also link in Bugzilla." in section
    assert "Remove the tag's label from the Jira issue" in section
    assert "Put the whiteboard tag back" not in section


def test_the_overview_charts_broken_links_but_does_not_list_them(data_dir, tmp_path):
    """Nobody fixes another team's broken links, so the overview has no list or CSV of them."""
    _record("fp", date(2026, 11, 1), broken=_broken(2))
    out = _build(data_dir, tmp_path)
    doc = (out / "index.html").read_text()
    assert '<figure class="chart broken"' in doc
    assert 'id="broken"' not in doc
    assert not (out / "broken-links.csv").exists()


def test_the_two_charts_are_told_apart(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), broken=_broken(1))
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert '<figure class="chart drift"' in doc and '<figure class="chart broken"' in doc


def test_how_to_fix_is_an_info_callout(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1), broken=_broken(1))
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    callout = doc[doc.index('<aside class="callout info"'):]
    callout = callout[:callout.index("</aside>")]
    assert "How to fix these" in callout and "Remove or correct the see-also link in Bugzilla." in callout


def test_no_broken_links_anywhere_is_all_clear(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    out = _build(data_dir, tmp_path)
    for page in ("groups/perf.html", "tags/fp.html"):
        doc = (out / page).read_text()
        assert '<p class="all-clear good">' in doc[doc.index('<h2 id="broken">'):]


# -- how this was compared ----------------------------------------------------

SHA = "0123456789abcdef0123456789abcdef01234567"
DEFAULTS_URL = f"https://github.com/mozilla/jira-bugzilla-integration/blob/{SHA}/jbi/models.py"
CONFIG_URL = f"https://github.com/mozilla/jira-bugzilla-integration/blob/{SHA}/config/config.prod.yaml#L7-L16"


def _jbi(**kw):
    jbi = {
        "description": "Performance Team",
        "source": {"commit": SHA, "url": CONFIG_URL, "lines": [7, 16], "defaults_url": DEFAULTS_URL},
        "steps": {"existing": ["update_issue_summary", "maybe_assign_jira_user",
                               "maybe_update_issue_priority", "sync_see_also"], "new": None},
        "fields": ["assignee", "priority", "see_also", "summary"],
        "compared": ["priority", "see_also", "summary"],
        "linked_project_excludes": {"value": ["BZFFX"], "default": True},
        "maps": {"priority": {"value": {"P1": "P1", "--": "(none)", "": "None"}, "default": False},
                 "severity": {"value": {"S1": "S1"}, "default": True},
                 "status": {"value": {}, "default": True},
                 "resolution": {"value": {}, "default": True},
                 "type": {"value": {"defect": "Bug"}, "default": True}},
        "labels_brackets": {"value": "no", "default": True},
    }
    jbi = {**jbi, **kw}
    # As jbix.config.describe_tag records it, unless a test gives its own.
    jbi.setdefault("step_check", compare_steps(jbi["steps"]["new"], jbi["steps"]["existing"]))
    return jbi


def _config_section(data_dir, tmp_path, jbi):
    snapshots.record_snapshot("fp", "FP", _health(), _diff(), datetime(2026, 11, 1), jbi=jbi)
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    return doc, doc[doc.index('<h2 id="config">'):doc.index('<h2 id="queries">')]


def test_the_config_section_links_to_the_tags_lines_at_the_commit(data_dir, tmp_path):
    doc, section = _config_section(data_dir, tmp_path, _jbi())
    assert f'<a href="{CONFIG_URL}" target="_blank" rel="noopener">JBI&#x27;s configuration</a> at commit <code>{SHA[:12]}</code>, lines 7 to 16' in section
    assert "“Performance Team”" in section
    assert '<a href="#config">How this was compared</a>' in doc
    # The fixing section's advice about recurring drift points at the same lines.
    fixing = doc[doc.index('<h2 id="fixing">'):doc.index('<h2 id="broken">')]
    assert (f'<a href="{CONFIG_URL}" target="_blank" rel="noopener">JBI&#x27;s configuration</a> '
            'rather than another sync') in fixing


def test_a_field_jbi_syncs_but_the_run_left_out_is_called_out(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi())
    assert '<aside class="callout info" aria-labelledby="what-was-compared">' in section
    assert "<strong>Compared:</strong> Summary, Priority, See also." in section
    assert ("<strong>Left out of this run:</strong> Assignee. JBI syncs it, but the run that made "
            "this report was told not to compare it, so its drift is not counted.") in section
    _, section = _config_section(data_dir, tmp_path, _jbi(compared=["see_also", "summary"]))
    assert "Assignee, Priority. JBI syncs them" in section and "so their drift" in section


def test_fields_jbi_does_not_sync_are_listed_but_jbixs_own_are_not(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi())
    unsynced = section[section.index("Not compared, as JBI does not sync them"):]
    unsynced = unsynced[:unsynced.index("</li>")]
    assert "Severity" in unsynced and "Components" in unsynced
    assert "Time tracking" not in unsynced and "Priority" not in unsynced


def test_the_maps_of_compared_fields_are_listed(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi())
    maps = section[section.index('<dl class="mappings">'):section.index("</dl>")]
    assert '<dt>Priority <span class="muted">(set for this tag)</span></dt>' in maps
    # Values kept as they are share a line; each translation has its own.
    assert ('<ul class="translations"><li><span class="muted">Unchanged:</span> <ul class="codes">'
            '<li><code>P1</code></li></ul></li><li><code>--</code> → <code>(none)</code></li>') in maps
    assert '<li><span class="muted">(empty)</span> → <code>None</code></li>' in maps
    assert "<code>S1</code>" not in maps  # severity is not compared, so its map is not listed


def test_a_mapped_field_that_is_not_compared_says_why(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi())
    maps = section[section.index('<dl class="mappings">'):section.index("</dl>")]
    assert ('<dt>Resolution</dt><dd class="muted">Not compared: not synced for this tag, so JBI '
            "leaves Jira's resolution as it is.</dd>") in maps
    _, section = _config_section(data_dir, tmp_path, _jbi(fields=["severity", "priority"],
                                                          compared=["priority"]))
    assert "<dt>Severity</dt><dd class=\"muted\">Not compared: JBI syncs it, but this run left it out.</dd>" in section


def test_link_excludes_are_noted_when_links_are_compared(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi())
    assert "Links to issues in BZFFX are never made (JBI&#39;s default)" in section
    _, section = _config_section(data_dir, tmp_path, _jbi(compared=["priority"]))
    assert "BZFFX" not in section


# glean-sdk-jira's steps on 24 Nov 2026: labels synced for existing bugs only.
GLEAN_STEPS = {
    "new": ["create_issue", "maybe_delete_duplicate", "maybe_update_components", "add_link_to_bugzilla",
            "add_link_to_jira", "maybe_assign_jira_user", "maybe_update_issue_status"],
    "existing": ["update_issue_summary", "maybe_assign_jira_user", "maybe_update_components",
                 "maybe_update_issue_status", "sync_keywords_labels", "sync_whiteboard_labels"],
}


def _steps_rows(section):
    table = section[section.index('<table class="steps">'):]
    return re.findall(r"<tr(?: class=\"(\w+)\")?><td><code>([\w_]+)</code>", table[:table.index("</table>")])


def test_steps_are_side_by_side_grouped_by_the_field_they_sync(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=GLEAN_STEPS,
                                                          compared=["components", "keyword_labels",
                                                                    "status", "summary", "whiteboard_labels"]))
    assert "<summary>JBI's steps, for new and existing bugs</summary>" in section
    rows = _steps_rows(section)
    # In the order fields are listed, then the steps that sync no field.
    assert [step for _, step in rows] == [
        "update_issue_summary", "maybe_assign_jira_user", "maybe_update_components",
        "sync_whiteboard_labels", "sync_keywords_labels", "maybe_update_issue_status",
        "create_issue", "maybe_delete_duplicate", "add_link_to_bugzilla", "add_link_to_jira"]
    assert [step for cls, step in rows if cls == "mismatch"] == ["sync_whiteboard_labels", "sync_keywords_labels"]
    assert ('<td><code>sync_whiteboard_labels</code></td><td>Whiteboard labels ' in section
            and '<td class="muted">–</td><td>✓</td></tr>' in section)


def test_labels_synced_for_existing_bugs_only_are_warned_about(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=GLEAN_STEPS))
    callout = section[section.index('<aside class="callout warn" aria-labelledby="steps-differ">'):]
    callout = callout[:callout.index("</aside>")]
    assert "New and existing bugs are synced differently" in callout
    assert ("<strong>Synced for existing bugs but not new ones:</strong> Whiteboard labels, "
            "Keyword labels. An issue JBI creates starts without these labels, and the label steps only "
            "add what changes afterwards, so they stay missing and every new issue drifts.") in callout
    # It comes before what was compared, so it is seen first.
    assert section.index("steps-differ") < section.index("what-was-compared")


def test_a_non_label_field_on_existing_bugs_only_is_not_said_to_drift_on_every_issue(data_dir, tmp_path):
    steps = {"new": ["create_issue"], "existing": ["update_issue_summary", "sync_regressions"]}
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=steps))
    assert ("Synced for existing bugs but not new ones:</strong> Regressions. An issue JBI creates starts "
            "without it, which shows when the bug already has a value at that point, for example when the "
            "tag is added to an older bug. A later change in Bugzilla brings it into line.") in section
    assert "every new issue drifts" not in section


def test_labels_and_other_fields_on_existing_bugs_only_get_a_bullet_each(data_dir, tmp_path):
    steps = {"new": ["create_issue"], "existing": ["sync_whiteboard_labels", "sync_regressions"]}
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=steps))
    assert section.count("<strong>Synced for existing bugs but not new ones:</strong>") == 2


def test_a_field_synced_for_new_bugs_only_says_later_changes_are_not_copied(data_dir, tmp_path):
    steps = {"new": ["create_issue", "maybe_update_components"], "existing": ["update_issue_summary"]}
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=steps, compared=["summary"]))
    assert ("<strong>Synced for new bugs but not existing ones:</strong> Components. It is set when JBI "
            "creates the issue, but a later change in Bugzilla is not copied to Jira. This report compares "
            "the existing-bug steps, so it does not count this either.") in section


def test_matching_steps_raise_no_warning(data_dir, tmp_path):
    steps = {"new": ["create_issue", "sync_whiteboard_labels"],
             "existing": ["update_issue_summary", "sync_whiteboard_labels"]}
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=steps))
    assert "steps-differ" not in section and 'class="mismatch"' not in section


def test_steps_left_to_jbis_defaults_link_to_them_rather_than_copy_them(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi())  # new steps unset
    assert '<td class="muted">default</td>' in section
    assert ("Not set for new bugs, so "
            f'<a href="{DEFAULTS_URL}" target="_blank" rel="noopener">JBI&#x27;s defaults</a> apply there, '
            "and steps synced on one side only cannot be found.") in section
    assert "steps-differ" not in section
    _, section = _config_section(data_dir, tmp_path, _jbi(steps={"new": None, "existing": None}))
    assert "JBI's steps: not set for this tag, so " in section and '<table class="steps">' not in section


def test_a_latest_file_from_before_steps_were_compared_shows_no_table(data_dir, tmp_path):
    jbi = _jbi()
    del jbi["step_check"]
    _, section = _config_section(data_dir, tmp_path, jbi)
    assert '<table class="steps">' not in section and "steps-differ" not in section


def test_without_a_commit_the_config_is_said_to_be_from_main(data_dir, tmp_path):
    url = "https://github.com/mozilla/jira-bugzilla-integration/blob/main/config/config.prod.yaml"
    _, section = _config_section(data_dir, tmp_path, _jbi(source={"commit": None, "url": url, "lines": None}))
    assert f'<a href="{url}" target="_blank" rel="noopener">JBI&#x27;s configuration</a>, read from its main branch.' in section


def test_config_values_are_escaped(data_dir, tmp_path):
    jbi = _jbi(description="<script>x</script>",
               maps={"priority": {"value": {"<b>": "&"}, "default": False}})
    _, section = _config_section(data_dir, tmp_path, jbi)
    assert "<script>" not in section and "&lt;script&gt;" in section
    assert "<code>&lt;b&gt;</code> → <code>&amp;</code>" in section


def test_a_latest_file_without_config_detail_has_no_section(data_dir, tmp_path):
    _record("fp", date(2026, 11, 1))
    doc = (_build(data_dir, tmp_path) / "tags" / "fp.html").read_text()
    assert 'id="config"' not in doc and 'href="#config"' not in doc
    fixing = doc[doc.index('<h2 id="fixing">'):doc.index('<h2 id="broken">')]
    assert f'<a href="{site.JBI_URL}" target="_blank" rel="noopener">JBI&#x27;s configuration</a>' in fixing


def test_a_field_create_issue_sets_is_not_shown_as_missing_for_a_new_bug(data_dir, tmp_path):
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=GLEAN_STEPS))
    assert ('<td><code>update_issue_summary</code></td><td>Summary</td>'
            '<td class="muted">by <code>create_issue</code></td><td>✓</td>') in section


def test_a_field_synced_for_new_bugs_only_is_not_also_listed_as_not_synced(data_dir, tmp_path):
    steps = {"new": ["create_issue", "maybe_update_components"], "existing": ["update_issue_summary"]}
    _, section = _config_section(data_dir, tmp_path, _jbi(steps=steps, compared=["summary"]))
    unsynced = section[section.index("Not compared, as JBI does not sync them"):]
    assert "Components" not in unsynced[:unsynced.index("</li>")]
