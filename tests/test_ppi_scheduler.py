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

def _settled():
    scheduler = PpiBeatScheduler()
    scheduler.on_packet(10_000, packet(10_000, [800] * 6))
    assert scheduler.last_rejections == [] and scheduler.last_packet_skip is None
    return scheduler


def test_every_refused_interval_carries_its_reason_for_the_session_log():
    scheduler = _settled()
    burst = PpiPacket(
        (
            PpiSample(800, 1, True),  # blocker
            PpiSample(800, 81, False),  # error estimate over 10 %
            PpiSample(2500, 1, False),  # outside the plausible range
            PpiSample(1100, 1, False),  # more than 20 % from the median
            PpiSample(800, 1, False),  # fine
        ),
        "phone",
        15_000,
    )
    result = scheduler.on_packet(15_000, burst)
    reasons = [item["reason"] for item in scheduler.last_rejections]
    assert len(reasons) == 4 == sum(not interval.accepted for interval in result.intervals)
    assert reasons[0] == "blockerBit set"
    assert reasons[1].startswith("errorEstimate 81 ms over 10%")
    assert reasons[2].startswith("outside ") and "2000" in reasons[2]
    assert "from the recent median 800 ms" in reasons[3]
    refused = [i for i in result.intervals if not i.accepted]
    assert [item["rr_ms"] for item in scheduler.last_rejections] == [i.rr_ms for i in refused]
    assert [item["t_beat"] for item in scheduler.last_rejections] == [i.t_beat for i in refused]

    # The next clean burst starts a fresh list: reasons never accumulate across bursts.
    scheduler.on_packet(20_000, packet(20_000, [800] * 6))
    assert scheduler.last_rejections == []


def test_recording_reasons_does_not_change_what_the_scheduler_decides():
    def run(read_diagnostics):
        scheduler, out = PpiBeatScheduler(), []
        for k in range(12):
            arrival = 10_000 + 5_000 * k
            samples = tuple(
                PpiSample(780 + 7 * ((k + j) % 5), 90 if (k + j) % 4 == 0 else 1, (k + j) % 7 == 0)
                for j in range(6)
            )
            result = scheduler.on_packet(arrival, PpiPacket(samples, "phone", arrival))
            if read_diagnostics:
                list(scheduler.last_rejections)
            out.append((result, tuple(scheduler.tick(arrival + 100))))
        return out, scheduler.stats

    assert run(True) == run(False)


def test_a_whole_burst_that_is_ignored_says_why():
    scheduler = _settled()
    scheduler.on_packet(17_000, packet(15_000, [800] * 6))
    assert scheduler.last_packet_skip == "handled more than 1500 ms after it arrived"
    scheduler.on_packet(20_000, packet(20_000, [800] * 6))
    assert scheduler.last_packet_skip is None
    scheduler.on_packet(20_100, packet(20_000, [800] * 6))
    assert scheduler.last_packet_skip == "arrival time not after the previous burst"
    assert scheduler.last_rejections == []
