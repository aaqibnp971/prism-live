"""The synthetic armband: notification cadence, contents, variability and each fault."""

import json
import statistics

import pytest

from bridge.hrm import FLAG_RR_PRESENT, parse_hrm
from tools.synthetic_rr import DEFAULT_PROFILE, Fault, Profile, generate, main


def run(*fault_specs, seed=1, profile=DEFAULT_PROFILE):
    return list(generate(profile, [Fault.from_spec(s) for s in fault_specs], seed))


def rr_sequence(notes, t0=0.0, t1=float("inf")):
    """Every delivered RR interval in ms, in order, from notifications sent in [t0, t1]."""
    return [rr for n in notes if t0 <= n.t_s <= t1 for rr in parse_hrm(n.payload).rr_ms]


def rmssd(rr):
    return statistics.pstdev(b - a for a, b in zip(rr, rr[1:], strict=False))


# --- the heart rate curve ---


def test_default_profile_is_the_four_minute_arc():
    assert DEFAULT_PROFILE.duration_s == 240
    assert [DEFAULT_PROFILE.hr_at(t) for t in (0, 45, 120, 195, 240)] == [68, 68, 105, 75, 75]


def test_profile_spec_round_trips():
    assert Profile.from_spec("68:45,68-105:75,105-75:75,75:45") == DEFAULT_PROFILE


# --- notifications ---


def test_notifications_arrive_about_once_a_second():
    times = [n.t_s for n in run()]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= 0.92 - 1e-9 and max(gaps) <= 1.08 + 1e-9
    assert len(times) == pytest.approx(240, abs=3)


def test_packets_carry_zero_one_or_two_intervals():
    counts = [len(parse_hrm(n.payload).rr_raw) for n in run()]
    assert set(counts) <= {0, 1, 2}
    assert {1, 2} <= set(counts)
    slow = [len(parse_hrm(n.payload).rr_raw) for n in run(profile=Profile.from_spec("40:20"))]
    assert 0 in slow  # at 40 bpm some seconds close no beat at all


def test_rr_present_flag_tracks_the_payload():
    for note in run(profile=Profile.from_spec("40:20")):
        assert bool(note.payload[0] & FLAG_RR_PRESENT) == bool(parse_hrm(note.payload).rr_raw)


def test_intervals_are_native_units_that_add_up_to_elapsed_time():
    total_s = sum(rr_sequence(run())) / 1000
    assert 238 < total_s <= 240.01  # 2.4 % out, and this fails, if raw units were read as ms


def test_variability_shrinks_as_the_rate_rises():
    notes = run()
    assert rmssd(rr_sequence(notes, 0, 45)) > 1.5 * rmssd(rr_sequence(notes, 105, 135))


def test_same_seed_same_stream():
    assert [n.payload for n in run(seed=7)] == [n.payload for n in run(seed=7)]
    assert [n.payload for n in run(seed=7)] != [n.payload for n in run(seed=8)]


# --- faults ---


def test_dropped_packet_loses_that_second_and_nothing_else():
    base = run()
    lost = next(n for n in base if n.t_s >= 30)
    assert run("dropped_packet@30") == [n for n in base if n is not lost]


def test_disconnect_goes_silent_then_resumes():
    base = run()
    assert run("disconnect@100:8") == [n for n in base if not 100 <= n.t_s < 108]
    assert len(base) - len(run("disconnect@100:8")) >= 7


def test_doubled_beat_splits_one_interval_under_300_ms():
    base, faulty = rr_sequence(run()), rr_sequence(run("doubled_beat@60"))
    i = next(i for i, (a, b) in enumerate(zip(base, faulty, strict=False)) if a != b)
    assert faulty[i] < 300
    assert faulty[i] + faulty[i + 1] == pytest.approx(base[i], abs=1.0)  # time is conserved
    assert faulty[i + 2 :] == base[i + 1 :]


def test_missed_beat_merges_two_intervals():
    base, faulty = rr_sequence(run()), rr_sequence(run("missed_beat@60"))
    i = next(i for i, (a, b) in enumerate(zip(base, faulty, strict=False)) if a != b)
    assert faulty[i] == pytest.approx(base[i] + base[i + 1], abs=1.0)
    assert faulty[i + 1 :] == base[i + 2 :]


def test_artefact_burst_is_five_seconds_of_garbage_then_recovery():
    calm, wild = rr_sequence(run(), 60, 67), rr_sequence(run("artefact_burst@60"), 60, 67)
    assert max(calm) - min(calm) < 200
    assert max(wild) - min(wild) > 500
    after = rr_sequence(run("artefact_burst@60"), 75, 90)
    assert max(after) - min(after) < 200


def test_fault_spec_parsing():
    assert Fault.from_spec("disconnect@100:8") == Fault("disconnect", 100.0, 8.0)
    assert Fault.from_spec("artefact_burst@60") == Fault("artefact_burst", 60.0, 5.0)
    with pytest.raises(ValueError):
        Fault.from_spec("wobble@10")


# --- command line ---


def test_cli_prints_json_lines(capsys):
    assert main(["--seed", "3", "--profile", "68:30,68-90:30", "--fault", "disconnect@20:5"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert set(json.loads(lines[0])) == {"t_ms", "payload", "hr_bpm", "rr_ms"}
    assert len(lines) == pytest.approx(55, abs=3)
