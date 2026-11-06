from codrone_tui.widgets.graph import BRAILLE, braille_rows, gradient, lerp_color, levels


def test_levels_clamp_and_none():
    assert levels([None, 0, 50, 100, 500], 0, 100, rows=2) == [0, 0, 4, 8, 8]


def test_empty_history_is_blank():
    rows = braille_rows([], width=4, height=2, vmin=0, vmax=100)
    assert len(rows) == 2 and all(ch == " " for row in rows for ch, _ in row)


def test_full_scale_fills_every_dot_of_the_cell():
    rows = braille_rows([100.0, 100.0], width=1, height=1, vmin=0, vmax=100)
    assert rows[0][0][0] == chr(BRAILLE + 0xFF)


def test_area_fills_from_the_bottom():
    rows = braille_rows([25.0, 0.0], width=1, height=1, vmin=0, vmax=100)
    assert rows[0][0][0] == chr(BRAILLE + 0x40)
    rows = braille_rows([0.0, 25.0], width=1, height=1, vmin=0, vmax=100)
    assert rows[0][0][0] == chr(BRAILLE + 0x80)


def test_two_row_graph_stacks_correctly():
    rows = braille_rows([100.0, 100.0], width=1, height=2, vmin=0, vmax=100)
    assert rows[0][0][0] == rows[1][0][0] == chr(BRAILLE + 0xFF)
    rows = braille_rows([50.0, 50.0], width=1, height=2, vmin=0, vmax=100)
    assert rows[0][0][0] == " " and rows[1][0][0] == chr(BRAILLE + 0xFF)


def test_newest_sample_is_at_the_right_edge():
    rows = braille_rows([100.0, 100.0], width=3, height=1, vmin=0, vmax=100)
    assert [c for c, _ in rows[0]] == [" ", " ", chr(BRAILLE + 0xFF)]


def test_gradient_endpoints():
    assert gradient(0, "#000000", "#808080", "#ffffff") == "#000000"
    assert gradient(1, "#000000", "#808080", "#ffffff") == "#ffffff"
    assert lerp_color("#000000", "#ffffff", 0.5) == "#808080"
