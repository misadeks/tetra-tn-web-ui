"""Tests for the downlink-voice (MsSpeechFrame) decode path.

Validates the native ACELP vocoder end to end: a synthetic tone is encoded to
real ACELP bits (via a test-only encoder DLL), packed into an MsSpeechFrame,
and pushed through the hub's speech-frame handler exactly as the telemetry
server does. If the native codec can't be built on this platform the codec
tests skip rather than fail.
"""
import base64
import ctypes
import math
import struct

import pytest

from app import acelp
from app.hub import Hub

pytestmark = pytest.mark.skipif(
    not acelp.available(), reason="native ACELP decoder unavailable (needs clang)"
)


def _load_encoder():
    """Load the test-only encoder DLL, building it if necessary."""
    import shutil
    import subprocess
    import sys

    native = acelp._NATIVE
    name = "tetra_enc_test.dll" if sys.platform.startswith("win") else "libtetra_enc_test.so"
    lib_path = native / name
    if not lib_path.exists():
        clang = shutil.which("clang")
        if not clang:
            pytest.skip("clang not available to build encoder")
        etsi = native / "etsi"
        srcs = ["scod_tet.c", "sub_sc_d.c", "sub_dsp.c", "fbas_tet.c",
                "fexp_tet.c", "fmat_tet.c", "tetra_op.c"]
        cmd = [clang]
        tgt = acelp._clang_target()
        if tgt:
            cmd.append(f"--target={tgt}")
        cmd += ["-shared", "-O2", f"-I{etsi}", f"-I{native}",
                *[str(etsi / s) for s in srcs], str(native / "enc_test.c"),
                "-o", str(lib_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not lib_path.exists():
            pytest.skip("could not build test encoder: " + (proc.stderr or proc.stdout))
    enc = ctypes.CDLL(str(lib_path))
    enc.tetra_enc_init.restype = None
    enc.tetra_enc_encode.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.POINTER(ctypes.c_uint8)]
    enc.tetra_enc_init()
    return enc


def _encode_tone_frame(enc, base, freq=300, amp=8000):
    """Encode two 240-sample sub-frames of a tone -> 274 one-bit values."""
    bits = []
    for sub in range(2):
        pcm = [int(amp * math.sin(2 * math.pi * freq * (base + sub * 240 + i) / 8000))
               for i in range(240)]
        cin = (ctypes.c_int16 * 240)(*pcm)
        cbits = (ctypes.c_uint8 * 137)()
        enc.tetra_enc_encode(cin, cbits)
        bits.extend(int(b) for b in cbits)
    return bits


def test_roundtrip_tone_energy():
    """A 300 Hz tone survives encode -> decode with strong, ~300 Hz output."""
    enc = _load_encoder()
    dec = acelp.AcelpDecoder()
    samples = []
    for fr in range(30):
        bits = _encode_tone_frame(enc, fr * 480)
        pcm = dec.decode_speech_frame(bits)
        assert len(pcm) == 960  # 480 int16 samples
        samples.extend(struct.unpack("<480h", pcm))
    dec.close()

    steady = samples[480 * 8:]  # skip codec warm-up
    rms = (sum(x * x for x in steady) / len(steady)) ** 0.5
    assert rms > 500, f"decoded tone too quiet (rms={rms})"
    zc = sum(1 for i in range(1, len(steady)) if steady[i - 1] < 0 <= steady[i])
    est = zc * 8000 / len(steady)
    assert 250 < est < 350, f"decoded tone freq off ({est} Hz)"


def test_bad_frame_conceals_without_crash():
    dec = acelp.AcelpDecoder()
    out = dec.decode_subframe([0] * 137, bad_frame=True)
    assert len(out) == 480  # 240 int16 samples
    dec.close()


def test_hub_speech_frame_produces_audio_envelope():
    enc = _load_encoder()
    hub = Hub()
    hub.state = {"own_issi": 1234567}
    cid = 68
    hub.calls[cid] = {"call_identifier": cid, "group": True, "state": "active",
                      "talker_ssi": None, "can_request_tx": True}
    bits = _encode_tone_frame(enc, 0)
    env = hub.handle_speech_frame({
        "call_identifier": cid, "timeslot": 2, "sequence": 1,
        "transmitting_party_ssi": 2200699, "frame_bits": 274,
        "bad_frame": False, "data": bits,
    })
    assert env["type"] == "audio"
    assert env["call_identifier"] == cid
    assert env["talker_ssi"] == 2200699
    assert env["sample_rate"] == 8000
    # Real PCM present and correctly sized (480 int16 = 960 bytes).
    pcm = base64.b64decode(env["pcm"])
    assert len(pcm) == 960
    # Talker attributed onto the call, PTT blocked while another member talks.
    assert hub.calls[cid]["talker_ssi"] == 2200699
    assert hub.calls[cid]["can_request_tx"] is False
    # A decoder is now cached for the call; release tears it down.
    assert cid in hub.decoders
    hub._apply_call_event("TnccReleaseIndication",
                          {"call_identifier": cid, "indication": {}})
    assert cid not in hub.decoders


def test_hub_drops_non_speech_frame_bits():
    hub = Hub()
    env = hub.handle_speech_frame({
        "call_identifier": 5, "frame_bits": 137, "bad_frame": False,
        "data": [0] * 137, "sequence": 1, "transmitting_party_ssi": None,
    })
    assert env is not None
    assert env["pcm"] is None
    assert env.get("dropped") == "unsupported_frame_bits"
