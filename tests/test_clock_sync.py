"""The client clock exchange: the contract's arithmetic, and the JS port giving the same answers."""

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from bridge.clock_sync import ClockSync

JS = Path(__file__).parents[1] / "web" / "shared" / "clock_sync.js"
OFFSET = 123_456.0  # T_engine is this far ahead of the client's clock


def exchange(sync, t_client, up_ms, down_ms, server_ms=0.0):
    """One ping and its pong, with the given delay each way."""
    ping = sync.ping(t_client)
    t_engine = round(t_client + up_ms + OFFSET + server_ms)
    pong = {
        "type": "clock",
        "v": 1,
        "role": "pong",
        "t_client_sent": ping["t_client_sent"],
        "t_engine": t_engine,
    }
    return sync.on_pong(pong, t_client + up_ms + server_ms + down_ms)


def test_symmetric_delay_gives_the_exact_offset():
    sync = ClockSync()
    assert not sync.ready and sync.offset is None
    assert exchange(sync, 1000.0, 10, 10)
    assert sync.offset == OFFSET
    assert sync.to_local(OFFSET + 5000) == 5000
    assert sync.to_engine(5000) == OFFSET + 5000


def test_a_slow_one_way_sample_is_discarded():
    sync = ClockSync()
    for i in range(5):
        exchange(sync, 1000.0 + 2000 * i, 5, 5)
    assert not exchange(sync, 20_000.0, 5, 80)  # rtt 85 against a median of 10
    assert sync.offset == OFFSET


def test_the_offset_is_the_median_of_the_last_nine():
    sync = ClockSync()
    for i in range(9):
        exchange(sync, 1000.0 + 2000 * i, 5, 5)
    exchange(sync, 20_000.0, 5, 13)  # used, but pulls the offset by 4 ms
    assert sync.offset == OFFSET
    assert sync.samples == 9


def test_localhost_rtts_near_zero_are_not_all_discarded():
    sync = ClockSync()
    exchange(sync, 1000.0, 0.05, 0.05)
    assert exchange(sync, 3000.0, 0.8, 0.8)  # 16 x the median, but under the 4 ms floor


def test_a_lasting_rise_in_network_delay_is_accepted_after_a_few_samples():
    sync = ClockSync()
    t = 1000.0
    for _ in range(9):
        exchange(sync, t, 3, 3)
        t += 2000
    used = []
    for _ in range(9):
        used.append(exchange(sync, t, 30, 30))
        t += 2000
    assert not used[0]
    assert all(used[5:])


def test_nonsense_rtts_are_ignored():
    sync = ClockSync()
    assert not sync.on_pong({"t_client_sent": 5000.0, "t_engine": 1}, 4000.0)
    assert not sync.on_pong({"t_client_sent": 0.0, "t_engine": 1}, 60_000.0)
    with pytest.raises(RuntimeError):
        sync.to_local(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_js_port_gives_identical_offsets():
    rng = random.Random(7)
    samples, t = [], 1000.0
    for i in range(60):
        base = 0.3 if i < 20 else 6.0 if i < 40 else 25.0
        up, down = base * rng.uniform(0.5, 1.5), base * rng.uniform(0.5, 1.5)
        if rng.random() < 0.15:
            down += rng.uniform(20, 200)
        sent = t + rng.uniform(0, 3) / 7
        pong = {"t_client_sent": sent, "t_engine": round(sent + up + OFFSET)}
        samples.append([pong, sent + up + down])
        t += 2000

    sync = ClockSync()
    python = [[sync.on_pong(pong, now), sync.offset] for pong, now in samples]

    script = f"""
import {{ ClockSync }} from {json.dumps(JS.as_uri())};
let text = "";
for await (const chunk of process.stdin) text += chunk;
const sync = new ClockSync();
const out = JSON.parse(text).map(([pong, now]) => [sync.onPong(pong, now), sync.offset]);
console.log(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(samples),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == python
    assert any(not used for used, _ in python)  # the vector exercises discards
