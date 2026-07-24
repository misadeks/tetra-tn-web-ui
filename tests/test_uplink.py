"""Tests for the uplink-voice (MsUplinkSpeech) encode + transmit path (phase V2).

Validates the native ACELP *encoder* end to end and the hub's floor gating:
a synthetic tone is ACELP-encoded by the production ``AcelpEncoder``, decoded
back, and checked for energy/frequency; then the hub's ``handle_uplink_audio``
is exercised to confirm it only emits ``MsUplinkSpeech`` while this MS holds the
floor. If the native codec can't be built on this platform the tests skip.
"""
import base64
import json
import math
import struct

import pytest

from app import acelp, protocol
from app.hub import Hub

pytestmark = pytest.mark.skipif(
    not acelp.encoder_available(),
    reason="native ACELP encoder unavailable (needs clang)",
)


def _tone_pcm(freq: int, n: int, amp: int = 8000, phase: int = 0) -> list[int]:
    return [int(amp * math.sin(2 * math.pi * freq * (phase + i) / 8000)) for i in range(n)]


def test_encoder_roundtrip_tone():
    """A 300 Hz tone survives encode -> decode with the right pitch and energy."""
    enc = acelp.AcelpEncoder()
    dec = acelp.AcelpDecoder()
    try:
        pcm_out = []
        for fr in range(30):
            bits = enc.encode_speech_frame(_tone_pcm(300, 480, phase=fr * 480))
            assert len(bits) == 274
            assert all(b in (0, 1) for b in bits)
            pcm = dec.decode_speech_frame(bits)
            pcm_out.extend(struct.unpack("<480h", pcm))
    finally:
        enc.close()
        dec.close()
    steady = pcm_out[480 * 8:]  # skip codec warm-up
    rms = (sum(x * x for x in steady) / len(steady)) ** 0.5
    zc = sum(1 for i in range(1, len(steady)) if steady[i - 1] < 0 <= steady[i])
    freq = zc * 8000 / len(steady)
    assert rms > 500, f"encoded tone too quiet: rms={rms:.0f}"
    assert 250 < freq < 350, f"pitch drifted: {freq:.0f} Hz"


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _pcm_b64(freq: int) -> str:
    frame = struct.pack("<480h", *_tone_pcm(freq, 480))
    return base64.b64encode(frame).decode("ascii")


@pytest.mark.asyncio
async def test_uplink_gated_on_floor():
    hub = Hub()
    ws = _FakeWs()
    hub.control_ws = ws
    hub.control_connected = True
    cid = 4242
    hub.calls[cid] = {"call_identifier": cid, "holds_floor": False}
    b64 = _pcm_b64(440)

    # No floor -> dropped, nothing on the wire.
    assert await hub.handle_uplink_audio(cid, b64) is False
    assert ws.sent == []

    # Floor granted -> exactly one MsUplinkSpeech with 274 one-bit-per-byte data.
    hub.calls[cid]["holds_floor"] = True
    assert await hub.handle_uplink_audio(cid, b64) is True
    assert len(ws.sent) == 1
    msg = json.loads(bytes(ws.sent[0]).decode("utf-8"))
    assert "MsUplinkSpeech" in msg
    body = msg["MsUplinkSpeech"]
    assert body["call_identifier"] == cid
    assert body["frame_bits"] == 274
    assert len(body["data"]) == 274
    assert all(b in (0, 1) for b in body["data"])

    # Floor lost again -> dropped.
    hub.calls[cid]["holds_floor"] = False
    assert await hub.handle_uplink_audio(cid, b64) is False
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_uplink_duplex_streams_without_floor():
    """A duplex individual call (§4.2) transmits for its whole active life with no
    PTT floor, and stops on release or an explicit grant revoke."""
    hub = Hub()
    ws = _FakeWs()
    hub.control_ws = ws
    hub.control_connected = True
    cid = 77
    # Duplex, active, no floor held -> still allowed to stream.
    hub.calls[cid] = {"call_identifier": cid, "group": False, "simplex": False,
                      "state": "active", "holds_floor": False, "tx_status": None}
    b64 = _pcm_b64(440)
    assert await hub.handle_uplink_audio(cid, b64) is True
    assert len(ws.sent) == 1

    # Not yet through-connected -> no uplink.
    hub.calls[cid]["state"] = "proceeding"
    assert await hub.handle_uplink_audio(cid, b64) is False
    assert len(ws.sent) == 1

    # Active again, but the network revoked the grant -> stop.
    hub.calls[cid]["state"] = "active"
    hub.calls[cid]["tx_status"] = "TransmissionInterrupt"
    assert await hub.handle_uplink_audio(cid, b64) is False
    assert len(ws.sent) == 1

    # Released -> stop.
    hub.calls[cid]["tx_status"] = None
    hub.calls[cid]["state"] = "released"
    assert await hub.handle_uplink_audio(cid, b64) is False
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_uplink_unknown_call_dropped():
    hub = Hub()
    ws = _FakeWs()
    hub.control_ws = ws
    hub.control_connected = True
    assert await hub.handle_uplink_audio(9999, _pcm_b64(440)) is False
    assert ws.sent == []
