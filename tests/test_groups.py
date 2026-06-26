"""Tests for jbix/groups.py — named tag groups."""

import pytest

import jbix.groups as groups
from jbix.groups import (
    expand_groups,
    group_display_names,
    load_groups,
    resolve_tags,
)

_GROUPS = "performance: [fp, fxp, fxpe, pcf]\ngenai: [aife, aimodels]\n"


@pytest.fixture(autouse=True)
def _groups_file(tmp_path, monkeypatch):
    p = tmp_path / "groups.yaml"
    p.write_text(_GROUPS)
    monkeypatch.setattr(groups, "GROUPS_PATH", p)
    return p


def test_load_groups():
    assert load_groups() == {"performance": ["fp", "fxp", "fxpe", "pcf"],
                             "genai": ["aife", "aimodels"]}


def test_load_groups_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(groups, "GROUPS_PATH", tmp_path / "nope.yaml")
    assert load_groups() == {}


def test_load_groups_mapping_form():
    # The {name, tags} mapping form yields the same tag lists as the bare form.
    groups.GROUPS_PATH.write_text(
        "performance:\n  name: Performance\n  tags: [fp, fxp]\n"
        "deng: [dataplatform, dataquality]\n")
    assert load_groups() == {"performance": ["fp", "fxp"],
                             "deng": ["dataplatform", "dataquality"]}
    assert expand_groups(["performance"]) == ["fp", "fxp"]


def test_group_display_names():
    groups.GROUPS_PATH.write_text(
        "performance:\n  name: Performance\n  tags: [fp]\n"
        "deng: [dataplatform]\n")  # bare list → key is the label
    assert group_display_names() == {"performance": "Performance", "deng": "deng"}


def test_expand_single_group():
    assert expand_groups(["performance"]) == ["fp", "fxp", "fxpe", "pcf"]


def test_expand_multiple_groups_dedups_and_orders():
    # two groups that overlap on fp → fp not repeated, order preserved
    groups.GROUPS_PATH.write_text("performance: [fp, fxp]\nextra: [fp, zz]\n")
    assert expand_groups(["performance", "extra"]) == ["fp", "fxp", "zz"]


def test_expand_unknown_group_exits():
    with pytest.raises(SystemExit) as e:
        expand_groups(["bogus"])
    assert "bogus" in str(e.value) and "performance" in str(e.value)


def test_resolve_tags_union_no_dupes():
    # explicit fp + performance group (which also has fp) → fp once, sp3 appended
    assert resolve_tags(["fp", "sp3"], ["performance"]) == ["fp", "sp3", "fxp", "fxpe", "pcf"]


def test_resolve_tags_only_tags():
    assert resolve_tags(["fp", "fxp"], []) == ["fp", "fxp"]
