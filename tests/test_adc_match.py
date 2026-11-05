import json

import pytest

from codrone_tui.adc import MATCH_SECONDS, MAX_ATTEMPTS, MatchController, MatchError


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def match(clock, tmp_path):
    return MatchController(clock=clock, store_path=tmp_path / "attempts.json")


def test_idle_defaults(match):
    assert match.state == "idle"
    assert match.elapsed == 0 and match.remaining == MATCH_SECONDS
    assert match.attempts_left == MAX_ATTEMPTS and match.best_score == 0 and match.skills_total == 0
    assert not match.expired and match.tick() is None


def test_clock_runs_and_finish_records_attempt(match, clock):
    card = match.start()
    assert match.state == "running" and match.scorecard is card
    clock.advance(42.5)
    assert match.elapsed == pytest.approx(42.5) and match.remaining == pytest.approx(137.5)
    assert match.credit("takeoff").ok
    assert card.events[0].t == pytest.approx(42.5)  # credits are stamped with match time
    result = match.finish(landing="landing_bullseye")
    assert (result.number, result.score, result.resets, result.expired) == (1, 20, 0, False)
    assert result.duration == pytest.approx(42.5)
    assert result.breakdown == {"takeoff": 5, "landing_bullseye": 15} and result.landing == "landing_bullseye"
    assert match.state == "finished" and match.attempts_left == 2
    clock.advance(500)
    assert match.elapsed == pytest.approx(42.5)  # frozen after finish


def test_reset_does_not_stop_or_rewind_the_clock(match, clock):
    match.start()
    clock.advance(30)
    assert match.reset_drone() == 1
    clock.advance(30)
    assert match.reset_drone() == 2
    assert match.elapsed == pytest.approx(60)
    assert match.finish().resets == 2


def test_reset_requires_a_running_match(match):
    with pytest.raises(MatchError):
        match.reset_drone()
    with pytest.raises(MatchError):
        match.finish()


def test_expiry_auto_finishes_on_tick(match, clock):
    match.start()
    match.credit("red_arch")
    clock.advance(MATCH_SECONDS - 1)
    assert not match.expired and match.tick() is None
    clock.advance(1)
    assert match.expired
    result = match.tick()
    assert result.expired and result.duration == MATCH_SECONDS and result.score == 5
    assert match.state == "finished" and match.remaining == 0
    assert match.tick() is None  # only fires once


def test_clock_overshoot_is_capped_at_match_length(match, clock):
    match.start()
    clock.advance(400)
    assert match.remaining == 0
    assert match.tick().duration == MATCH_SECONDS


def test_three_attempts_then_refuse(match, clock):
    for n in range(MAX_ATTEMPTS):
        match.start()
        clock.advance(10)
        match.credit("takeoff")
        assert match.finish().number == n + 1
    assert match.attempts_left == 0
    with pytest.raises(MatchError, match="attempts"):
        match.start()
    assert len(match.attempts) == 3


def test_cannot_start_twice(match):
    match.start()
    with pytest.raises(MatchError, match="already"):
        match.start()


def test_abort_does_not_use_an_attempt(match, clock):
    match.start()
    match.credit("tunnel")
    clock.advance(20)
    match.abort()
    assert match.state == "idle" and match.attempts == [] and match.attempts_left == MAX_ATTEMPTS
    assert match.scorecard is None and match.resets == 0
    match.abort()  # harmless when idle
    match.start()  # fresh card, fresh clock
    assert match.elapsed == 0 and match.scorecard.total == 0


def test_credit_needs_a_running_match(match):
    res = match.credit("takeoff")
    assert not res.ok and "no match" in res.message


def test_best_score_and_skills_total(match, clock):
    for key in ("tunnel", "large_cube", "red_arch"):  # 15, 25, 5
        match.start()
        match.credit(key)
        match.finish()
    assert match.best_score == 25
    match.piloting_best = 60
    assert match.skills_total == 85
    assert match.last_result.score == 5


def test_persistence_round_trip(match, clock, tmp_path):
    match.start()
    clock.advance(12)
    match.credit("takeoff")
    match.reset_drone()
    match.finish(landing="landing_pad")
    match.piloting_best = 40
    assert match.save()

    fresh = MatchController(clock=clock, store_path=tmp_path / "attempts.json")
    assert fresh.load()
    assert fresh.attempts == match.attempts
    assert fresh.piloting_best == 40 and fresh.attempts_left == 2 and fresh.best_score == 10


def test_autosave_on_finish(clock, tmp_path):
    path = tmp_path / "nested" / "a.json"
    match = MatchController(clock=clock, store_path=path, autosave=True)
    match.start()
    match.finish()
    assert json.loads(path.read_text())["attempts"][0]["number"] == 1


def test_load_missing_file_is_a_quiet_no_op(match):
    assert match.load() is False and match.load_error is None and match.attempts == []


@pytest.mark.parametrize("content", ["{not json", "[]", '{"attempts": 5}', '{"attempts": [{"number": "x"}]}', ""])
def test_load_corrupt_file_keeps_state(clock, tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    match = MatchController(clock=clock, store_path=path)
    match.start()
    match.finish()
    assert match.load() is False
    assert match.load_error and "unreadable" in match.load_error
    assert len(match.attempts) == 1  # untouched


def test_new_event_forgets_attempts(match):
    match.start()
    match.finish()
    match.piloting_best = 10
    match.new_event()
    assert match.attempts == [] and match.piloting_best is None and match.state == "idle"


def test_default_store_path_is_logs_adc_attempts(clock):
    assert MatchController(clock=clock).store_path.as_posix() == "logs/adc_attempts.json"
