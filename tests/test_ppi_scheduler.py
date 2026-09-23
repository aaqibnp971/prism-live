"""The phone path plays measured variability, never a predicted one-second lattice."""

from dataclasses import replace

import pytest

from bridge.beat_scheduler import OK
from bridge.packets import PpiPacket, PpiSample
from bridge.ppi_scheduler import PpiBeatScheduler
from tools.synthetic_rr import Fault, Profile, generate_ppi


def packet(arrival, rrs, **changes):
    return PpiPacket(tuple(PpiSample(rr, 1, False, **changes) for rr in rrs), "phone", arrival)


def replay(notes):
    scheduler = PpiBeatScheduler()
    events, intervals, leads = [], [], []
    now = 0
    for note in notes:
        at = note.t_s * 1000
        while now < at:
            batch = scheduler.tick(now)
            events.extend(batch)
            leads.extend(round(e.t_play) - now for e in batch)
            now += 20
        result = scheduler.on_packet(at, note.payload)
        intervals.extend(result.intervals)
    end = now + 12_000
    while now <= end:
        batch = scheduler.tick(now)
        events.extend(batch)
        leads.extend(round(e.t_play) - now for e in batch)
        now += 20
    return scheduler, intervals, events, leads


def test_one_event_for_each_measured_accepted_interval_and_original_irregularity():
    scheduler = PpiBeatScheduler()
    rrs = [800, 850, 760, 820, 775, 830]
    result = scheduler.on_packet(5250, packet(5000, rrs))
    events = [e for now in range(5250, 18_000, 20) for e in scheduler.tick(now)]
    assert len(events) == len(rrs)
    assert [e.rr_ms for e in events] == rrs
    assert all(e.quality == OK and e.t_play - e.t_emitted >= 300 for e in events)
    assert [round(b.t_play - a.t_play)
            for a, b in zip(events, events[1:], strict=False)] == rrs[1:]
    assert result.contact is None
    assert scheduler.tick(100_000) == []  # no extrapolated beats once measured ones end


def test_quality_precedes_median_and_does_not_use_contact_or_reported_hr():
    scheduler = PpiBeatScheduler()
    samples = (
        PpiSample(800, 1, False, False, 0),
        PpiSample(800, 81, False, True, 75),
        PpiSample(800, 1, True, True, 75),
        PpiSample(800, 80, False, None, None),
    )
    result = scheduler.on_packet(5250, PpiPacket(samples, "phone", 5000))
    assert [i.accepted for i in result.intervals] == [True, False, False, True]
    assert scheduler.quality_rejected == 2
    assert len(result.events) == 2
    assert len(scheduler._window) == 2


@pytest.mark.parametrize("rate", [45, 60, 90, 120, 180])
@pytest.mark.parametrize("seed", [1, 3, 9])
def test_captured_cadence_has_no_late_loss_or_predicted_beats(rate, seed):
    scheduler, intervals, events, leads = replay(generate_ppi(
        Profile.from_spec(f"{rate}:360"), seed=seed,
    ))
    assert events
    assert len(events) == sum(i.accepted for i in intervals)
    assert scheduler.stats.skipped == 0
    assert min(leads) >= 300
    assert all(b.t_play - a.t_play >= 250 for a, b in zip(events, events[1:], strict=False))
    assert all(abs(e.phase_step_ms) <= .04 * 2000 for e in events)
    assert [e.rr_ms for e in events] == [i.rr_ms for i in intervals if i.accepted]


def test_capture_maximum_7858_ms_lookback_still_has_lead():
    scheduler = PpiBeatScheduler()
    samples = [500] + [982.25] * 8
    result = scheduler.on_packet(10_250, packet(10_000, samples))
    assert result.intervals[0].t_beat == pytest.approx(142)
    assert scheduler.stats.skipped == 0
    assert min(target for target, _, _ in scheduler._pending) - 10_250 > 300


@pytest.mark.parametrize("seconds", [2, 5, 10])
def test_stall_drops_expired_slots_without_stretching_rr(seconds):
    scheduler = PpiBeatScheduler()
    scheduler.on_packet(5250, packet(5000, [800] * 6))
    scheduler.tick(10_000)
    events = scheduler.tick(10_000 + seconds * 1000)
    events += [e for now in range(10_000 + seconds * 1000, 30_000, 20)
               for e in scheduler.tick(now)]
    assert all(e.rr_ms == 800 and round(e.t_play) - e.t_emitted >= 300 for e in events)
    assert scheduler.stats.skipped > 0


def test_stale_and_duplicate_packets_are_not_made_fresh():
    scheduler = PpiBeatScheduler()
    p = packet(5000, [800] * 6)
    assert scheduler.on_packet(5250, p).intervals
    assert scheduler.on_packet(5300, p).intervals == ()
    assert scheduler.on_packet(8000, replace(p, arrived_ms=6000)).intervals == ()


def test_missing_burst_marks_discontinuity_and_does_not_fill_the_gap():
    notes = list(generate_ppi(Profile.from_spec("75:80"),
                              faults=[Fault("disconnect", 20, 10)]))
    scheduler, intervals, events, leads = replay(notes)
    assert any(not i.contiguous and i.t_beat > 20_000 for i in intervals)
    assert any(b.t_play - a.t_play > 5000 for a, b in zip(events, events[1:], strict=False))
    assert all(e.quality == OK for e in events)
    assert min(leads) >= 300
    assert scheduler.stats.link_gaps > 0
