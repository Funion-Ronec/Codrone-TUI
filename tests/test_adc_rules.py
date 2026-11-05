import pytest

from codrone_tui.adc import (
    LANDINGS,
    MATCH_SECONDS,
    MAX_ATTEMPTS,
    PILOTING_PHASES,
    TASKS,
    Scorecard,
    max_score,
)
from codrone_tui.adc.rules import PILOTING_TAKEOFF_POINTS, SPIRAL_KEY


def run(card, *keys):
    return [card.credit(k, t=float(i)) for i, k in enumerate(keys)]


def test_constants_and_theoretical_max():
    assert MATCH_SECONDS == 180 and MAX_ATTEMPTS == 3
    assert max_score() == 290


def test_scoring_table_matches_manual():
    expected = {
        "takeoff": (5, 1), "red_arch": (5, 2), "blue_arch": (5, 2), "green_keyhole": (10, 2),
        "yellow_keyhole": (20, 2), "large_cube": (25, 2), "mini_1": (15, 2), "mini_2": (15, 2),
        "mini_3": (15, 2), "spiral_bonus": (10, 2), "tunnel": (15, 2),
    }
    assert list(TASKS) == list(expected)  # same order as the manual's table
    assert {k: (t.points, t.max_count) for k, t in TASKS.items()} == expected
    assert {k: t.points for k, t in LANDINGS.items()} == {"landing_pad": 5, "landing_cube": 10, "landing_bullseye": 15}


def test_piloting_phases_reference_data():
    assert PILOTING_TAKEOFF_POINTS == 5
    assert [p.points for p in PILOTING_PHASES] == [5, 10, 15, 10]
    assert PILOTING_PHASES[2].steps == ("blue_arch", "yellow_loop", "tunnel", "blue_arch")


def test_credit_and_total():
    card = Scorecard()
    res = card.credit("takeoff")
    assert res.ok and res.points == 5 and card.total == 5
    assert card.credit("yellow_keyhole").points == 20
    assert card.total == 25
    assert card.counts["takeoff"] == 1 and card.remaining("yellow_keyhole") == 1


def test_caps_are_enforced_without_raising():
    card = Scorecard()
    assert card.credit("takeoff").ok
    res = card.credit("takeoff")
    assert not res.ok and "max" in res.message and card.total == 5
    run(card, "tunnel", "tunnel")
    assert not card.credit("tunnel").ok
    assert card.remaining("tunnel") == 0


def test_unknown_key_and_manual_spiral_are_rejected():
    card = Scorecard()
    assert not card.credit("nonsense").ok
    res = card.credit(SPIRAL_KEY)
    assert not res.ok and "automatically" in res.message
    assert card.total == 0
    assert card.remaining("nonsense") == 0


def test_spiral_bonus_awarded_for_ordered_minis():
    card = Scorecard()
    r1, r2, r3 = run(card, "mini_1", "mini_2", "mini_3")
    assert r1.auto == [] and r2.auto == []
    assert r3.auto == ["spiral_bonus"] and r3.auto_points == 10 and r3.total_points == 25
    assert card.total == 15 * 3 + 10
    assert card.count("spiral_bonus") == 1


@pytest.mark.parametrize(
    "keys",
    [
        ("mini_2", "mini_1", "mini_3"),  # wrong order
        ("mini_1", "mini_3"),  # skipped one
        ("mini_1", "mini_2", "tunnel", "mini_3"),  # another scoring task in between
        ("mini_1", "red_arch", "mini_2", "mini_3"),
        ("mini_3",),
    ],
)
def test_spiral_needs_strict_order_without_interruption(keys):
    card = Scorecard()
    run(card, *keys)
    assert card.count("spiral_bonus") == 0


def test_spiral_ignores_earlier_unrelated_tasks_and_repeats_up_to_cap():
    card = Scorecard()
    run(card, "takeoff", "red_arch", "mini_1", "mini_2", "mini_3")  # tasks BEFORE the run are fine
    assert card.count("spiral_bonus") == 1
    run(card, "mini_1", "mini_2", "mini_3")  # second pass, bonus not yet maxed
    assert card.count("spiral_bonus") == 2
    run(card, "mini_1")  # minis are capped at 2 each now
    assert not card.credit("mini_3").ok
    assert card.count("spiral_bonus") == 2


def test_spiral_bonus_does_not_count_as_task_in_between():
    card = Scorecard()
    run(card, "mini_1", "mini_2", "mini_3", "mini_1", "mini_2", "mini_3")
    assert card.count("spiral_bonus") == 2
    assert card.total == 6 * 15 + 2 * 10


def test_second_spiral_is_the_last_possible_one():
    card = Scorecard()
    run(card, "mini_1", "mini_2", "mini_3", "mini_1", "mini_2")
    res = card.credit("mini_3")
    assert res.ok and res.auto == ["spiral_bonus"] and "+10 spiral bonus" in res.message
    # the minis are capped at 2 each, so a third spiral cannot even start
    assert [card.credit(k).ok for k in ("mini_1", "mini_2", "mini_3")] == [False, False, False]
    assert card.count("spiral_bonus") == 2


def test_undo_removes_trigger_and_auto_bonus_together():
    card = Scorecard()
    run(card, "mini_1", "mini_2", "mini_3")
    res = card.undo()
    assert res.ok and res.points == -25 and res.auto == ["spiral_bonus"]
    assert card.total == 30 and card.count("mini_3") == 0 and card.count("spiral_bonus") == 0
    # redoing mini_3 earns the bonus again
    assert card.credit("mini_3").auto == ["spiral_bonus"]


def test_undo_plain_credit_and_empty():
    card = Scorecard()
    assert card.undo() is None
    card.credit("red_arch")
    assert card.undo().points == -5 and card.total == 0
    assert card.undo() is None


def test_landing_is_one_replaceable_slot():
    card = Scorecard()
    assert card.set_landing("landing_pad").points == 5
    res = card.set_landing("landing_bullseye")
    assert res.points == 10  # 15 - 5: replaced, not added
    assert card.total == 15 and card.landing == "landing_bullseye"
    assert card.remaining("landing_pad") == 0
    assert card.set_landing(None).points == -15 and card.total == 0
    assert card.remaining("landing_pad") == 1
    assert not card.set_landing("landing_roof").ok
    assert card.credit("landing_cube").ok and card.total == 10  # credit() routes landing keys


def test_perfect_match_is_exactly_290():
    card = Scorecard()
    run(card, "takeoff")
    for _ in range(2):
        run(card, "red_arch", "green_keyhole", "large_cube", "mini_1", "mini_2", "mini_3",
            "blue_arch", "yellow_keyhole", "tunnel")
    card.set_landing("landing_bullseye")
    assert card.total == max_score() == 290
    assert all(card.remaining(k) == 0 for k in TASKS if k != SPIRAL_KEY)


def test_rows_cover_every_task_and_landing():
    card = Scorecard()
    run(card, "red_arch", "red_arch")
    card.set_landing("landing_pad")
    rows = {r.key: r for r in card.rows()}
    assert list(rows)[: len(TASKS)] == list(TASKS)
    assert rows["red_arch"].count == 2 and rows["red_arch"].earned == 10 and rows["red_arch"].max_count == 2
    assert rows["landing_pad"].earned == 5 and rows["landing_bullseye"].earned == 0
    assert sum(r.earned for r in rows.values()) == card.total


def test_serialisation_round_trip_regenerates_the_bonus():
    card = Scorecard()
    run(card, "takeoff", "mini_1", "mini_2", "mini_3")
    card.set_landing("landing_cube")
    data = card.to_dict()
    assert any(e["key"] == "spiral_bonus" for e in data["events"])
    clone = Scorecard.from_dict(data)
    assert clone.total == card.total == 5 + 45 + 10 + 10
    assert [e.key for e in clone.events] == [e.key for e in card.events]
    assert clone.landing == "landing_cube"


def test_from_dict_drops_bad_and_over_cap_entries():
    data = {
        "events": [{"key": "takeoff", "t": 1}, {"key": "takeoff", "t": 2}, {"key": "bogus"}, "junk",
                   {"key": "spiral_bonus", "t": 3}],
        "landing": "nowhere",
    }
    card = Scorecard.from_dict(data)
    assert card.total == 5 and card.landing is None


def test_reset_clears_everything():
    card = Scorecard()
    run(card, "takeoff")
    card.set_landing("landing_pad")
    card.reset()
    assert card.total == 0 and card.events == [] and card.landing is None
