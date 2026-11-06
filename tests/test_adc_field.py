import tomllib
from pathlib import Path

import pytest

from codrone_tui.adc import default_field, load_field
from codrone_tui.adc.field import DEFAULT_FIELD_PATH, REQUIRED_ELEMENTS, Element, FieldConfig

GOOD = """
calibrated = true
mat_origin = "test origin"
notes = "measured on 2026-11-04"
spiral_order = ["mini_1", "mini_2", "mini_3"]

[elements.red_arch]
label = "Red arch gate"
x = 100
y = -5.5
z = 90
heading = 0
width = 30
height = 40
kind = "arch"
"""


def test_missing_file_gives_placeholder_defaults(tmp_path):
    cfg = load_field(tmp_path / "nope.toml")
    assert not cfg.calibrated
    assert set(REQUIRED_ELEMENTS) <= set(cfg.elements)
    assert any("not found" in e for e in cfg.load_errors)
    assert any("NOT calibrated" in w for w in cfg.warnings())


def test_shipped_field_toml_is_uncalibrated_and_complete():
    cfg = load_field(DEFAULT_FIELD_PATH)
    assert cfg.calibrated is False
    assert set(REQUIRED_ELEMENTS) <= set(cfg.elements)
    assert cfg.load_errors == []
    assert any("NOT calibrated" in w for w in cfg.warnings())


def test_shipped_field_toml_matches_builtin_defaults():
    shipped, builtin = load_field(DEFAULT_FIELD_PATH), default_field()
    assert shipped.elements == builtin.elements
    assert shipped.spiral_order == builtin.spiral_order


def test_layout_matches_the_manual_geometry():
    """Figure 3.2.2 is an L: a long leg up +x, then a turn to the RIGHT (heading 90, -y). Sizes are 1.5.2's."""
    f = default_field()
    e = f.elements
    assert e["red_arch"].x < e["landing_pad"].x < e["green_keyhole"].x < e["large_cube"].x < e["blue_arch"].x
    assert {e[n].heading for n in ("blue_arch", "yellow_keyhole")} == {90.0}
    assert e["blue_arch"].y > e["yellow_keyhole"].y > e["tunnel"].y
    assert e["mini_1"].heading == e["mini_2"].heading == e["mini_3"].heading == 270.0
    assert [e[f"mini_{i}"].z for i in (1, 2, 3)] == sorted(e[f"mini_{i}"].z for i in (1, 2, 3))
    assert e["tunnel"].vertical and not any(el.vertical for n, el in e.items() if n != "tunnel")
    assert (e["red_arch"].width, e["red_arch"].height) == (pytest.approx(152.4, abs=0.1), pytest.approx(160.0, abs=0.1))
    assert e["green_keyhole"].width == pytest.approx(61.0, abs=0.1)
    assert (e["large_cube"].width, e["large_cube"].height) == (pytest.approx(43.2, abs=0.1), pytest.approx(50.8, abs=0.1))
    assert (e["landing_pad"].width, e["bullseye"].width) == (pytest.approx(76.2, abs=0.1), pytest.approx(25.4, abs=0.1))
    assert (e["tunnel"].width, e["tunnel"].height) == (pytest.approx(59.7, abs=0.1), pytest.approx(48.3, abs=0.1))


def test_vertical_flag_round_trips_and_must_be_a_real_boolean(tmp_path):
    cfg = default_field()
    path = tmp_path / "f.toml"
    path.write_text(cfg.to_toml(), encoding="utf-8")
    assert load_field(path).elements["tunnel"].vertical is True
    assert load_field(path).elements["red_arch"].vertical is False
    path.write_text(GOOD.replace('kind = "arch"', 'kind = "arch"\nvertical = "yes"'), encoding="utf-8")
    assert load_field(path).elements["red_arch"].vertical is False


def test_shipped_header_says_placeholder_and_unofficial():
    head = Path(DEFAULT_FIELD_PATH).read_text(encoding="utf-8")[:1500].lower()
    assert "placeholder" in head and "not official" in head and "403" in head


def test_load_good_file(tmp_path):
    path = tmp_path / "field.toml"
    path.write_text(GOOD, encoding="utf-8")
    cfg = load_field(path)
    el = cfg.get("red_arch")
    assert cfg.calibrated and cfg.mat_origin == "test origin" and cfg.notes.startswith("measured")
    assert (el.x, el.y, el.z, el.heading, el.width, el.height, el.kind) == (100.0, -5.5, 90.0, 0.0, 30.0, 40.0, "arch")
    assert cfg.source == str(path)
    assert not any("calibrated" in w for w in cfg.warnings())
    assert any("missing" in w and "tunnel" in w for w in cfg.warnings())


def test_calibrated_must_be_a_real_boolean(tmp_path):
    path = tmp_path / "f.toml"
    path.write_text('calibrated = "yes"\n', encoding="utf-8")
    assert load_field(path).calibrated is False


def test_toml_round_trip(tmp_path):
    cfg = default_field()
    cfg.calibrated = True
    cfg.notes = 'quote " and \\ backslash and ünï'
    cfg.elements["odd name"] = Element("odd name", "Odd", 1.5, -2.5, 3.0, 45.0, 6.0, 7.0, "x")
    path = tmp_path / "round.toml"
    path.write_text(cfg.to_toml(), encoding="utf-8")
    again = load_field(path)
    assert again.calibrated and again.notes == cfg.notes
    assert again.elements == cfg.elements
    assert again.spiral_order == cfg.spiral_order
    tomllib.loads(cfg.to_toml())


def test_bad_toml_falls_back_with_a_reason(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("calibrated = [oops", encoding="utf-8")
    cfg = load_field(path)
    assert not cfg.calibrated and set(REQUIRED_ELEMENTS) <= set(cfg.elements)
    assert any("could not read" in e for e in cfg.load_errors)


def test_bad_element_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "f.toml"
    path.write_text(GOOD + '\n[elements.broken]\nx = "far"\ny = 1\n\n[elements.nopos]\nlabel = "no x"\n', encoding="utf-8")
    cfg = load_field(path)
    assert "red_arch" in cfg.elements and "broken" not in cfg.elements and "nopos" not in cfg.elements
    assert len([e for e in cfg.load_errors if "ignored" in e]) == 2


def test_get_unknown_element_lists_known_ones():
    with pytest.raises(KeyError, match="red_arch"):
        default_field().get("sky_gate")


def test_field_config_defaults_are_uncalibrated():
    assert FieldConfig().calibrated is False
