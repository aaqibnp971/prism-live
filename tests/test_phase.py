from types import SimpleNamespace

import pytest

from bridge.phase import (
    BASELINE_TO_LOAD_FRAMES,
    LOAD_FRAMES,
    PULSE_ALIGNMENT_PHASE,
    SAMPLE_RATE,
    STEM_FRAMES,
    PhaseTracker,
)


class Frames:
    def __init__(self, frame=0, block=512):
        self.value = frame
        self.config = SimpleNamespace(max_block_frames=block)

    def frames_rendered(self):
        return self.value


def test_every_stem_phase_and_strict_next_boundary_come_from_frames_rendered():
    source = Frames(STEM_FRAMES["bed"] + 123)
    phase = PhaseTracker.for_shim(source)
    assert phase.phase_frames("bed") == 123
    assert phase.phase_frames("pulse") == source.value % STEM_FRAMES["pulse"]
    assert phase.next_boundary("bed") == 2 * STEM_FRAMES["bed"]
    assert phase.next_boundary("pulse", STEM_FRAMES["pulse"]) == 2 * STEM_FRAMES["pulse"]


def test_gate_crossing_is_one_maximum_block_before_the_chosen_boundary():
    phase = PhaseTracker(Frames(), 512)
    boundary = 3 * STEM_FRAMES["air"]
    timing = phase.gate_timing("air", boundary)
    assert timing.boundary_frame == boundary
    assert timing.send_frame == boundary - 512
    with pytest.raises(ValueError, match="not a positive air boundary"):
        phase.gate_timing("air", boundary + 1)


def test_air_can_choose_the_last_boundary_before_a_pulse_boundary():
    phase = PhaseTracker(Frames(), 512)
    pulse_boundary = 7 * STEM_FRAMES["pulse"]
    air_boundary = phase.boundary_before("air", pulse_boundary)
    assert air_boundary < pulse_boundary
    assert air_boundary % STEM_FRAMES["air"] == 0
    assert pulse_boundary - air_boundary <= STEM_FRAMES["air"]


def test_nearest_boundary_is_within_half_a_loop_and_ties_go_earlier():
    phase = PhaseTracker(Frames(), 512)
    loop = STEM_FRAMES["air"]
    assert phase.nearest_boundary("air", loop + loop // 2) == loop
    assert abs(phase.nearest_boundary("air", loop + 123_456) - (loop + 123_456)) <= loop // 2


def test_start_alignment_waits_at_most_one_pulse_loop_outside_the_session():
    source = Frames()
    phase = PhaseTracker.for_shim(source)
    for pressed in (0, 1, PULSE_ALIGNMENT_PHASE, STEM_FRAMES["pulse"] - 1, 9_876_543):
        alignment = phase.start_alignment(pressed)
        assert 0 <= alignment.wait_frames < STEM_FRAMES["pulse"]
        assert alignment.fire_frame % STEM_FRAMES["pulse"] == PULSE_ALIGNMENT_PHASE
        assert (alignment.fire_frame + BASELINE_TO_LOAD_FRAMES) % STEM_FRAMES["pulse"] == 0
        assert alignment.wait_ms < 11_000
    assert phase.start_alignment(PULSE_ALIGNMENT_PHASE).wait_ms == 0
    assert phase.start_alignment(PULSE_ALIGNMENT_PHASE + 1).wait_ms == pytest.approx(
        11_000 - 1000 / SAMPLE_RATE
    )


def test_an_aligned_session_orders_air_close_before_the_first_regulate_pulse_boundary():
    phase = PhaseTracker(Frames(), 512)
    for cycle in range(26):  # every relative air phase (11 and 13 seconds are coprime)
        baseline = PULSE_ALIGNMENT_PHASE + cycle * STEM_FRAMES["pulse"]
        plan = phase.session_gate_plan(baseline)
        assert plan.load_frame == baseline + BASELINE_TO_LOAD_FRAMES
        assert plan.regulate_frame == plan.load_frame + LOAD_FRAMES
        assert plan.pulse_open.boundary_frame == plan.load_frame
        assert plan.regulate_frame < plan.pulse_close.boundary_frame
        assert plan.air_close.boundary_frame < plan.pulse_close.boundary_frame
        assert plan.air_close.boundary_frame + round(1.5 * SAMPLE_RATE) < (
            plan.pulse_close.boundary_frame + round(1.5 * SAMPLE_RATE)
        )
        assert plan.pulse_close.boundary_frame - plan.air_close.boundary_frame <= STEM_FRAMES["air"]


def test_a_misaligned_baseline_cannot_make_a_session_gate_plan():
    phase = PhaseTracker(Frames(), 512)
    with pytest.raises(ValueError, match="pulse phase 10 s"):
        phase.session_gate_plan(PULSE_ALIGNMENT_PHASE + 1)
