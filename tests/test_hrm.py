"""The Heart Rate Measurement packet format, both directions."""

import pytest

from bridge.hrm import (
    FLAG_RR_PRESENT,
    HrmPacket,
    encode_hrm,
    parse_hrm,
    rr_ms_to_raw,
    rr_raw_to_ms,
)


def test_rr_units_are_1024ths_of_a_second():
    assert rr_raw_to_ms(1024) == 1000.0
    assert rr_ms_to_raw(1000.0) == 1024
    assert rr_ms_to_raw(rr_raw_to_ms(903)) == 903


def test_round_trip_keeps_native_units():
    payload = encode_hrm(68, [903, 917])
    assert payload[0] & FLAG_RR_PRESENT
    packet = parse_hrm(payload)
    assert packet.rr_raw == (903, 917)
    assert packet.rr_ms == pytest.approx((881.8, 895.5), abs=0.05)


def test_no_intervals_means_flag_clear_and_empty_tuple():
    payload = encode_hrm(68)
    assert not payload[0] & FLAG_RR_PRESENT
    assert parse_hrm(payload) == HrmPacket(68, True, True, None, False, ())


def test_fields_are_indexed_from_the_flags():
    # 16-bit heart rate, no contact support, energy expended present: every offset moves.
    packet = parse_hrm(encode_hrm(300, [512], contact_supported=False, energy_expended_kj=42))
    assert (packet.hr_bpm, packet.contact_supported, packet.contact_detected) == (300, False, None)
    assert packet.energy_expended_kj == 42
    assert packet.rr_ms == (500.0,)


def test_contact_lost_is_visible():
    assert parse_hrm(encode_hrm(68, contact_detected=False)).contact_detected is False


def test_rr_flag_set_with_no_intervals_is_reported_not_rejected():
    packet = parse_hrm(bytes([FLAG_RR_PRESENT, 68]))
    assert packet.rr_flag and packet.rr_raw == ()


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x10", b"\x10\x44\x87", b"\x00\x44\x87\x03", b"\x01\x44"],
    ids=["empty", "flags only", "odd RR bytes", "trailing bytes no flag", "16-bit hr cut short"],
)
def test_malformed_payloads_are_rejected(payload):
    with pytest.raises(ValueError):
        parse_hrm(payload)
