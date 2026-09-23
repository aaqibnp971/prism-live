"""Batched measured PPI through the existing model/session, not a fictional HRM packet."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bridge.baseline import BaselineCapture
from bridge.beat_scheduler import Interval, PacketResult, Tuning
from bridge.contract import validate
from bridge.hrv import HrvInterval
from bridge.logging import SessionLog
from bridge.packets import PpiPacket, PpiSample
from bridge.ppi_scheduler import PpiBeatScheduler
from bridge.psv import BaselinePhase, HeartRateWindow, PsvModel
from bridge.session import Session, Timings, _Regulate
from tools.synthetic_rr import Fault, Profile, generate_ppi


def run_ppi(tmp_path, bpm=68, *, start=20_000, until=100_000, faults=()):
    scheduler = PpiBeatScheduler()
    model = PsvModel(scheduler.t)
    log = SessionLog(tmp_path)
    messages = []
    session = Session(
        model,
        log,
        messages.append,
        now_ms=0,
        timings=Timings(hold_ms=11_000, hold_to_end=True),
    )
    notes = list(generate_ppi(Profile.from_spec(f"{bpm}:{until / 1000 + 1}"), faults))
    moments = set(range(0, int(until) + 1, 50)) | {start}
    moments.update(n.t_s * 1000 for n in notes if n.t_s * 1000 <= until)
    index = 0
    margins = []
    for now in sorted(moments):
        while index < len(notes) and notes[index].t_s * 1000 <= now:
            note = notes[index]
            arrival = note.t_s * 1000
            model.on_packet(arrival, scheduler.on_packet(arrival, note.payload))
            index += 1
        if now == start:
            assert session.start(now) is None
        for event in scheduler.tick(now):
            assert event.t_play - now >= 300
            messages.append(session.beat_message(event))
        session.tick(now)
        if model.last_trusted_beat_ms is not None:
            margins.append(model.signal_lost_ms - (now - model.last_trusted_beat_ms))
    log.close()
    records = [
        json.loads(line)
        for path in sorted(tmp_path.glob("S-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for message in messages:
        validate(message, "out")
    return model, session, records, margins


def test_ppi_timing_policy_is_source_specific_not_a_legacy_retune():
    legacy, ppi = PsvModel(), PsvModel(Tuning.ppi())
    assert (legacy.signal_lost_ms, legacy.measurement_settle_ms) == (6200, 2000)
    assert (ppi.signal_lost_ms, ppi.measurement_settle_ms) == (10_200, 10_200)
    assert not legacy.measured_ppi and ppi.measured_ppi


def test_reported_baseline_hr_and_gate_do_not_wait_for_or_duplicate_hrv():
    capture = BaselineCapture(0, measured_ppi=True)
    raw = [Interval(t, 1000, True, 45_100) for t in range(1000, 46_001, 1000)]
    capture.add_reported(raw, 45_100)
    assert capture.reported_past_end_ms == 45_100
    assert not capture.ready(45_100)  # waits for useful HRV until session's hold cap
    first = capture.result()
    assert first.passed and first.hr_base_bpm == 60
    assert first.accepted_intervals == 45 and first.accepted_ms == 45_000
    assert not first.hrv_complete and first.rmssd_base_ms is None
    capture.add_reported(raw, 45_200)  # repeat cannot double the gate
    classified = [HrvInterval(i.t_beat, i.rr_ms, True, False, True, 1) for i in raw]
    capture.add(classified, 50_000)
    capture.add(classified, 50_001)  # repeated classification cannot double HRV/gate
    final = capture.result()
    assert final.accepted_intervals == 45 and final.accepted_ms == 45_000
    assert final.hrv_complete and final.rmssd_base_ms == 1
    # A late classification cannot rewrite evidence as it stood at an earlier cap.
    historical = capture.result(45_200)
    assert historical == first


def test_ppi_hold_cap_takes_hr_baseline_without_pretending_hrv_complete():
    model = PsvModel(Tuning.ppi())
    assert model.start_baseline(10_000)
    raw = tuple(Interval(t, 1000, True, 55_100) for t in range(11_000, 55_001, 1000))
    model.on_packet(55_100, PacketResult(raw, ()))
    assert model.baseline is None
    assert model.close_baseline(66_000) is BaselinePhase.READY
    baseline = model.baseline
    assert baseline.passed and baseline.hr_base_bpm == 60
    assert not baseline.hrv_complete and baseline.rmssd_base_ms is None
    assert baseline.ln_rmssd_base is None and baseline.hr_sd_bpm is None
    assert baseline.rmssd_base_differences > 0  # classified evidence is counted, not invented
    # Later complete HRV must not silently enrich the frozen per-session baseline.
    later = tuple(Interval(t, 1000, True, 70_100) for t in range(56_000, 70_001, 1000))
    model.on_packet(70_100, PacketResult(later, ()))
    assert model.baseline == baseline


def test_ppi_cap_never_uses_a_late_report_and_failed_gate_still_fails():
    model = PsvModel(Tuning.ppi())
    model.start_baseline(10_000)
    raw = tuple(Interval(t, 1000, True, 70_000) for t in range(11_000, 56_001, 1000))
    model.on_packet(70_000, PacketResult(raw, ()))
    assert model.close_baseline(66_000) is BaselinePhase.FAILED
    assert model.baseline.accepted_intervals == 0


@pytest.mark.parametrize("bpm", [45, 48, 60, 95, 180])
@pytest.mark.parametrize("start", [20_000, 24_800])
def test_five_second_bursts_meet_baseline_cap_and_do_not_claim_signal_loss(tmp_path, bpm, start):
    model, session, records, margins = run_ppi(tmp_path, bpm, start=start)
    end = next(r for r in records if r.get("event") == "baseline_end")
    assert end["outcome"] == "ready"
    assert end["held_ms"] == 11_000
    assert model.baseline.hr_base_bpm == pytest.approx(bpm, abs=2)
    assert model.baseline_decided_ms <= start + 56_000
    assert model.baseline_reported_ms <= model.baseline_decided_ms
    assert session.segment == "load"
    assert min(margins) > 0
    assert not any(
        r.get("event") == "signal" and r.get("lost") and r["t_engine"] >= start for r in records
    )


def test_ppi_quality_not_contact_or_hr_controls_model_acceptance():
    model, scheduler = PsvModel(Tuning.ppi()), PpiBeatScheduler()
    samples = tuple(PpiSample(800, 1, False, False, 0) for _ in range(7))
    result = scheduler.on_packet(10_000, PpiPacket(samples, "test", 10_000))
    # Even an accidental legacy contact field cannot make PPI a wear-state detector.
    model.on_packet(10_000, replace(result, contact=False))
    assert model.last_trusted_beat_ms is not None
    estimate = model.estimate(10_000)
    assert estimate.signal.contact is None and estimate.signal.contact_factor == 1
    assert estimate.hr_bpm == 75


class PpiWindows(PsvModel):
    def __init__(self, meets_at=180_000):
        super().__init__(Tuning.ppi())
        self.asked = []
        self.meets_at = meets_at

    def heart_rate(self, start, end):
        self.asked.append((start, end))
        bpm = 90 if end < self.meets_at else 75
        return HeartRateWindow(bpm, end - start, 30)


def test_ppi_windows_wait_for_packet_settlement_and_keep_the_original_threshold():
    model = PpiWindows()
    regulate = _Regulate(100_000, 70, Timings())
    assert not regulate.take_hr_load(model, 110_199)
    assert regulate.take_hr_load(model, 110_200)
    assert regulate.threshold == 80
    assert regulate.judge_windows(model, 130_199) is None
    assert model.asked == [(70_000, 100_000)]
    regulate.judge_windows(model, 130_200)
    assert model.asked[-1] == (100_000, 120_000)
    regulate.judge_windows(model, 190_199)
    assert regulate.first_met is None
    assert regulate.judge_windows(model, 190_200) == 80_000
    assert regulate.first_met_decided_ms == 190_200
    assert regulate.cap() == 190_200  # never retroactively start resolve at 180 s


def test_ppi_late_window_decision_does_not_extend_the_hard_cap():
    model = PpiWindows(meets_at=200_000)
    regulate = _Regulate(100_000, 70, Timings())
    regulate.take_hr_load(model, 110_200)
    regulate.judge_windows(model, 205_000)
    assert regulate.first_met is None
    assert regulate.cap() == 205_000
    regulate.judge_windows(model, 210_200)
    assert regulate.first_met is None  # evidence available after timeout cannot change it
    assert regulate.cap() == 205_000


def test_ppi_session_resolve_boundary_uses_decision_time_not_window_end(tmp_path):
    model = PpiWindows()
    log = SessionLog(tmp_path)
    session = Session(model, log, lambda _: None, now_ms=100_000)
    session._segment, session._start = "regulate", 100_000
    session._regulate = _Regulate(100_000, 70, Timings())
    session._regulate.take_hr_load(model, 110_200)
    assert session._regulate_due(190_200) == ("resolve", 190_200, "regulated")
    assert session._regulate.first_met == 80_000  # record retains the true window endpoint
    log.close()


def test_ppi_hr_window_does_not_fade_during_a_normal_five_second_burst_gap():
    model = PsvModel(Tuning.ppi())
    raw = tuple(Interval(t, 800, True, 30_000) for t in range(8000, 28_801, 800))
    model.on_packet(30_000, PacketResult(raw, ()))
    before = dict(model.estimate(30_000).components)["hr_fill"]
    during_gap = dict(model.estimate(35_244).components)["hr_fill"]
    assert before == during_gap == 1


def test_ppi_signal_loss_uses_reconstructed_age_and_no_contact_fallback(tmp_path):
    model = PsvModel(Tuning.ppi())
    model.on_packet(10_000, PacketResult((Interval(9500, 800, True, 10_000),), ()))
    log = SessionLog(tmp_path)
    session = Session(model, log, lambda _: None, now_ms=10_000)
    session.tick(19_699)
    assert not session.signal_lost
    session.tick(19_700)
    assert session.signal_lost
    log.close()


def test_ppi_silence_reaches_quality_gate_cap_and_resets(tmp_path):
    _, session, records, _ = run_ppi(tmp_path, faults=[Fault.from_spec("disconnect@30:90")])
    end = next(r for r in records if r.get("event") == "baseline_end")
    assert end["outcome"] == "failed" and end["held_ms"] == 11_000
    assert session.segment == "idle"


def test_a_complete_ppi_session_keeps_the_105_second_regulate_cap(tmp_path):
    _, session, records, margins = run_ppi(tmp_path, until=330_000)
    segments = {r["segment"]: r["at_ms"] for r in records if r.get("event") == "segment"}
    assert segments["load"] == 76_000
    assert segments["regulate"] == segments["load"] + 75_000
    assert segments["resolve"] == segments["regulate"] + 105_000
    assert segments["reset"] == segments["resolve"] + 45_000
    assert session.segment == "idle" and min(margins) > 0
    threshold = next(r for r in records if r.get("event") == "regulate_threshold")
    assert threshold["t_engine"] == segments["regulate"] + 10_200
    assert threshold["hr_load_bpm"] is not None
