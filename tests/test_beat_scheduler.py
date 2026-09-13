"""The beat scheduler, driven offline by the synthetic armband."""

import statistics
from dataclasses import dataclass

import pytest

from bridge.beat_scheduler import BeatEvent, BeatScheduler, Interval, Tuning
from bridge.hrm import encode_hrm, parse_hrm
from tools.synthetic_rr import DEFAULT_PROFILE, Fault, Profile, generate

LATENCY_MS = 40  # the link
TICK_MS = 20  # the host loop


@dataclass
class Run:
    beats: list[BeatEvent]  # ok and interpolated, in emission order
    rejected: list[BeatEvent]
    intervals: list[Interval]
    sched: BeatScheduler


def replay(*fault_specs, seed=1, profile=DEFAULT_PROFILE, tuning=None) -> Run:
    """Feed every packet at its arrival time, ticking every TICK_MS in between."""
    sched = BeatScheduler(tuning)
    run = Run([], [], [], sched)
    now = 0.0
    for note in generate(profile, [Fault.from_spec(s) for s in fault_specs], seed):
        arrival = note.t_s * 1000 + LATENCY_MS
        while now + TICK_MS <= arrival:
            now += TICK_MS
            run.beats += sched.tick(now)
        result = sched.on_packet(arrival, note.payload)
        run.rejected += result.events
        run.intervals += result.intervals
    for _ in range(100):
        now += TICK_MS
        run.beats += sched.tick(now)
    return run


def between(events, t0_s, t1_s):
    return [e for e in events if t0_s * 1000 <= e.t_play <= t1_s * 1000]


def spacings(events):
    return [b.t_play - a.t_play for a, b in zip(events, events[1:], strict=False)]


def bpm(events):
    return statistics.mean(60_000 / s for s in spacings(events))


# --- the clean run ---


def test_played_rhythm_follows_the_heart_rate():
    run = replay()
    assert bpm(between(run.beats, 5, 45)) == pytest.approx(68, abs=2)
    assert bpm(between(run.beats, 115, 125)) == pytest.approx(104, abs=3)
    true_beats = sum(len(parse_hrm(n.payload).rr_raw) for n in generate())
    assert len(run.beats) == pytest.approx(true_beats, abs=5)
    assert all(e.quality == "ok" for e in run.beats)


def test_every_beat_is_scheduled_at_least_300_ms_ahead():
    for run in (replay(), replay("disconnect@100:8"), replay("artefact_burst@60")):
        assert all(e.t_play - e.t_emitted >= 300 for e in run.beats)


def test_no_two_beats_overlap():
    for run in (replay(), replay("doubled_beat@60"), replay("artefact_burst@60")):
        assert min(spacings(run.beats)) >= 250
        assert all(
            s >= 0.95 * e.interval_ms for s, e in zip(spacings(run.beats), run.beats, strict=False)
        )


def test_phase_correction_never_steps_more_than_5_percent():
    run = replay()
    for a, b in zip(run.beats, run.beats[1:], strict=False):
        assert abs(a.phase_step_ms) <= 0.05 * a.interval_ms
        assert b.t_play - a.t_play == pytest.approx(a.interval_ms + a.phase_step_ms, abs=1e-6)
    for run in (replay("dropped_packet@30"), replay("disconnect@100:8"), replay("missed_beat@60")):
        assert all(abs(e.phase_step_ms) <= 0.05 * e.interval_ms for e in run.beats)


def test_nothing_plays_before_the_first_accepted_beat():
    sched = BeatScheduler()
    assert sched.tick(1000) == []
    assert not sched.running


def test_message_matches_the_contract():
    run = replay()
    event = run.beats[10]
    assert event.message("S-20260915-0042") == {
        "type": "beat",
        "v": 1,
        "session": "S-20260915-0042",
        "seq": event.seq,
        "t_play": round(event.t_play),
        "rr_ms": round(event.rr_ms, 1),
        "hr_bpm": round(event.hr_bpm, 1),
        "quality": "ok",
    }
    assert isinstance(event.message("s")["t_play"], int)
    seqs = sorted(e.seq for e in run.beats + run.rejected)
    assert seqs == list(range(1, len(seqs) + 1))


# --- every fault mode from 1.1 ---


def test_dropped_packet_neither_stutters_nor_gaps():
    clean, run = replay(), replay("dropped_packet@30")
    around = between(run.beats, 29, 36)
    interval = statistics.mean(spacings(between(clean.beats, 29, 36)))
    assert all(0.8 * interval <= s <= 1.25 * interval for s in spacings(around))
    assert len(around) == pytest.approx(len(between(clean.beats, 29, 36)), abs=1)
    assert run.sched.stats.link_gaps == 1


def test_disconnect_interpolates_then_stops_then_resumes():
    run = replay("disconnect@100:8")
    interpolated = [e for e in run.beats if e.quality == "interpolated"]
    assert interpolated
    assert all(101_500 <= e.t_emitted <= 105_500 for e in interpolated)
    assert not between(run.beats, 105.5, 108.0)  # silence, not an invented heartbeat
    resumed = between(run.beats, 109, 120)
    assert resumed and all(e.quality == "ok" for e in resumed)
    assert (run.sched.stats.stops, run.sched.stats.starts) == (1, 2)


def test_doubled_beat_is_rejected_and_not_played():
    clean, run = replay(), replay("doubled_beat@60")
    assert sorted(e.rr_ms for e in run.rejected) == pytest.approx([209.0, 611.3], abs=0.5)
    assert all(e.quality == "rejected" for e in run.rejected)
    assert len(between(run.beats, 58, 65)) == pytest.approx(
        len(between(clean.beats, 58, 65)), abs=1
    )


def test_missed_beat_is_rejected_and_filled_in():
    clean, run = replay(), replay("missed_beat@60")
    (rejected,) = run.rejected
    interval = statistics.mean(spacings(between(clean.beats, 55, 60)))
    assert 1.6 * interval < rejected.rr_ms < 2.4 * interval
    assert max(spacings(between(run.beats, 58, 65))) < 1.25 * interval
    assert len(between(run.beats, 58, 65)) == pytest.approx(
        len(between(clean.beats, 58, 65)), abs=1
    )


def test_artefact_burst_is_rejected_and_the_rhythm_holds():
    clean, run = replay(), replay("artefact_burst@60")
    assert sum(1 for e in run.rejected if 60_000 <= e.t_emitted <= 68_000) >= 4
    interval = statistics.mean(spacings(between(clean.beats, 55, 59)))
    assert all(0.8 * interval <= s <= 1.2 * interval for s in spacings(between(run.beats, 59, 70)))
    assert len(between(run.beats, 59, 70)) == pytest.approx(
        len(between(clean.beats, 59, 70)), abs=2
    )
    assert between(run.beats, 200, 240)


def test_a_step_change_in_rate_recovers_after_the_window_resets():
    run = replay(profile=Profile.from_spec("60:20,90:20"))
    assert run.sched.stats.window_resets >= 1
    assert bpm(between(run.beats, 32, 40)) == pytest.approx(90, abs=3)


def test_accepted_intervals_are_available_for_hrv():
    run = replay("missed_beat@60")
    accepted = [i for i in run.intervals if i.accepted]
    assert len(accepted) == run.sched.stats.accepted
    assert len(run.intervals) - len(accepted) == run.sched.stats.rejected == 1
    assert all(b.t_beat > a.t_beat for a, b in zip(run.intervals, run.intervals[1:], strict=False))


def test_tuning_is_a_knob():
    run = replay(tuning=Tuning(buffer_ms=1500.0))
    assert all(e.t_play - e.t_emitted >= 300 for e in run.beats)
    assert bpm(between(run.beats, 5, 45)) == pytest.approx(68, abs=2)


# --- tags for HRV (docs/known-limits.md) ---


def test_intervals_are_tagged_where_a_packet_was_lost_and_while_bootstrapping():
    run = replay("disconnect@15:4", profile=Profile.from_spec("68:30"))
    breaks = [k for k, i in enumerate(run.intervals) if not i.contiguous]
    assert breaks[0] == 0 and len(breaks) == 2
    assert run.intervals[breaks[1]].arrival - run.intervals[breaks[1] - 1].arrival > 4_000
    accepted = [i for i in run.intervals if i.accepted]
    assert [i.bootstrap for i in accepted[:4]] == [True, True, True, False]
    assert not any(i.bootstrap for i in accepted[3:])


def test_a_window_reset_bootstraps_again():
    run = replay(profile=Profile.from_spec("60:20,90:20"))
    assert run.sched.stats.window_resets >= 1
    late = [k for k, i in enumerate(run.intervals) if i.bootstrap and k > 2]
    assert len(late) == 3 * run.sched.stats.window_resets


def test_a_link_gap_ended_by_an_empty_packet_still_reanchors():
    """Below 60 bpm a packet can carry no interval. The gap must not be forgotten."""
    sched = BeatScheduler()
    for n in range(1, 6):
        sched.on_packet(n * 1000.0, encode_hrm(50, [1229]))
    sched.on_packet(9000.0, encode_hrm(50))  # the first packet after a 4 s gap is empty
    (after,) = sched.on_packet(10_000.0, encode_hrm(50, [1229])).intervals
    assert not after.contiguous
    assert 10_000 - 1200 <= after.t_beat <= 10_000 - 20


def test_a_zero_interval_is_rejected_without_crashing():
    sched = BeatScheduler()
    for n in range(1, 6):
        sched.on_packet(n * 1000.0, encode_hrm(70, [880]))
    result = sched.on_packet(6000.0, encode_hrm(70, [895, 0, 462]))
    assert [i.accepted for i in result.intervals] == [True, False, False]
    assert all(e.rr_ms > 0 for e in result.events)
    replay("artefact_burst@40:5", seed=7, profile=Profile.from_spec("100:90"))  # made a raw 0


@pytest.mark.parametrize(
    ("fault", "seed"),
    [("disconnect@30:4", 15), ("artefact_burst@30:5", 2)],
    ids=["disconnect", "burst"],
)
def test_a_restart_never_plays_on_top_of_a_beat_already_sent(fault, seed):
    run = replay(fault, seed=seed)
    for a, b in zip(run.beats, run.beats[1:], strict=False):
        assert b.t_play - a.t_play >= 0.95 * a.interval_ms, (a, b)


def test_a_packet_result_carries_the_contact_bit_as_sent():
    scheduler = BeatScheduler()
    assert scheduler.on_packet(1000, encode_hrm(70, [700])).contact is True
    assert scheduler.on_packet(2000, encode_hrm(70, [700], contact_detected=False)).contact is False
    assert scheduler.on_packet(3000, encode_hrm(70, [700], contact_supported=False)).contact is None
