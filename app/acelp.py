"""ctypes binding to the vendored ETSI TETRA ACELP speech decoder.

The heavy vocoder is the EN 300 395-2 reference C code (in ``native/etsi``),
compiled to a small shared library exposing a decoder-only ABI
(``native/acelp_decode.c``). We drive it from Python so the app can turn the
raw ACELP bits carried by ``MsSpeechFrame`` telemetry into PCM the browser can
play (voice-RX / downlink-audio brief, phase V1 = listen only).

One :class:`AcelpDecoder` instance == one call's decode history. Feed it the
137-bit sub-frames in order; it returns 240 int16 samples (30 ms @ 8 kHz) each.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
from pathlib import Path

# ---- paths -----------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_NATIVE = _ROOT / "native"
_ETSI = _NATIVE / "etsi"

if sys.platform.startswith("win"):
    _LIB_NAME = "tetra_acelp.dll"
    _ENC_LIB_NAME = "tetra_acelp_enc.dll"
elif sys.platform == "darwin":
    _LIB_NAME = "libtetra_acelp.dylib"
    _ENC_LIB_NAME = "libtetra_acelp_enc.dylib"
else:
    _LIB_NAME = "libtetra_acelp.so"
    _ENC_LIB_NAME = "libtetra_acelp_enc.so"

_LIB_PATH = _NATIVE / _LIB_NAME
_ENC_LIB_PATH = _NATIVE / _ENC_LIB_NAME

# Decoder source set (no encoder, no standalone main()): the ETSI source
# decoder plus our thin BFI-aware wrapper.
_DECODER_SOURCES = [
    "sdec_tet.c",
    "sub_sc_d.c",
    "sub_dsp.c",
    "fbas_tet.c",
    "fexp_tet.c",
    "fmat_tet.c",
    "tetra_op.c",
]

# Encoder source set: the ETSI source *coder* plus our re-entrant wrapper. Same
# shared DSP/maths units as the decoder, with scod_tet.c (analysis) in place of
# sdec_tet.c (synthesis).
_ENCODER_SOURCES = [
    "scod_tet.c",
    "sub_sc_d.c",
    "sub_dsp.c",
    "fbas_tet.c",
    "fexp_tet.c",
    "fmat_tet.c",
    "tetra_op.c",
]

BITS_PER_SUBFRAME = 137
SAMPLES_PER_SUBFRAME = 240  # 30 ms @ 8 kHz
SAMPLE_RATE = 8000

_build_lock = threading.Lock()
_lib = None
_lib_error: str | None = None
_enc_lib = None
_enc_lib_error: str | None = None


class AcelpUnavailable(RuntimeError):
    """Raised when the native decoder can't be loaded or built."""


def _find_clang() -> str | None:
    import shutil

    return shutil.which("clang") or shutil.which("clang.exe")


def _clang_target() -> str | None:
    """clang --target matching the *running* Python's architecture.

    Needed because clang defaults to the host arch, but the interpreter that
    will ``ctypes.CDLL`` the result may differ (e.g. x64 Python emulated on an
    ARM64 host). A mismatch yields WinError 193 at load time.

    We key off the interpreter's own build (``sysconfig.get_platform()``) rather
    than ``platform.machine()``: the latter reports the *host* CPU (ARM64) even
    when Python itself is the emulated x64 build, which would pick the wrong
    target for the DLL we are about to load.
    """
    import sysconfig

    if not sys.platform.startswith("win"):
        return None  # native default matches the host Python on Linux/macOS
    plat = (sysconfig.get_platform() or "").lower()
    if plat == "win-amd64":
        return "x86_64-pc-windows-msvc"
    if plat == "win-arm64":
        return "aarch64-pc-windows-msvc"
    if plat in ("win32", "win-x86"):
        return "i686-pc-windows-msvc"
    # Fall back to the host machine if the platform tag is unfamiliar.
    import platform

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x86_64-pc-windows-msvc"
    if machine in ("arm64", "aarch64"):
        return "aarch64-pc-windows-msvc"
    if machine in ("x86", "i386", "i686"):
        return "i686-pc-windows-msvc"
    return None


def build_library(force: bool = False) -> Path:
    """Compile the decoder shared library with clang if it is missing.

    Returns the path to the built library. Raises :class:`AcelpUnavailable`
    if clang is not available or the compile fails.
    """
    with _build_lock:
        if _LIB_PATH.exists() and not force:
            return _LIB_PATH
        clang = _find_clang()
        if not clang:
            raise AcelpUnavailable(
                "clang not found on PATH; cannot build the TETRA ACELP decoder"
            )
        wrapper = _NATIVE / "acelp_decode.c"
        sources = [str(_ETSI / name) for name in _DECODER_SOURCES] + [str(wrapper)]
        cmd = [clang]
        target = _clang_target()
        if target:
            cmd.append(f"--target={target}")
        cmd += [
            "-shared",
            "-O2",
            f"-I{_ETSI}",
            f"-I{_NATIVE}",
            *sources,
            "-o",
            str(_LIB_PATH),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not _LIB_PATH.exists():
            raise AcelpUnavailable(
                "failed to build TETRA ACELP decoder:\n" + (proc.stderr or proc.stdout)
            )
        return _LIB_PATH


def build_encoder_library(force: bool = False) -> Path:
    """Compile the encoder shared library with clang if it is missing.

    The mirror of :func:`build_library` for the uplink/TX path. Returns the path
    to the built library; raises :class:`AcelpUnavailable` on missing clang or a
    failed compile.
    """
    with _build_lock:
        if _ENC_LIB_PATH.exists() and not force:
            return _ENC_LIB_PATH
        clang = _find_clang()
        if not clang:
            raise AcelpUnavailable(
                "clang not found on PATH; cannot build the TETRA ACELP encoder"
            )
        wrapper = _NATIVE / "acelp_encode.c"
        sources = [str(_ETSI / name) for name in _ENCODER_SOURCES] + [str(wrapper)]
        cmd = [clang]
        target = _clang_target()
        if target:
            cmd.append(f"--target={target}")
        cmd += [
            "-shared",
            "-O2",
            f"-I{_ETSI}",
            f"-I{_NATIVE}",
            *sources,
            "-o",
            str(_ENC_LIB_PATH),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not _ENC_LIB_PATH.exists():
            raise AcelpUnavailable(
                "failed to build TETRA ACELP encoder:\n" + (proc.stderr or proc.stdout)
            )
        return _ENC_LIB_PATH


def _load_library():
    global _lib, _lib_error
    if _lib is not None:
        return _lib
    try:
        if not _LIB_PATH.exists():
            build_library()
        lib = ctypes.CDLL(str(_LIB_PATH))
        lib.tetra_dec_create.restype = ctypes.c_void_p
        lib.tetra_dec_create.argtypes = []
        lib.tetra_dec_destroy.restype = None
        lib.tetra_dec_destroy.argtypes = [ctypes.c_void_p]
        lib.tetra_dec_decode.restype = ctypes.c_int
        lib.tetra_dec_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
        ]
        _lib = lib
        return _lib
    except AcelpUnavailable:
        raise
    except OSError as exc:  # pragma: no cover - platform dependent
        _lib_error = str(exc)
        raise AcelpUnavailable(f"cannot load {_LIB_PATH}: {exc}") from exc


def available() -> bool:
    """True if the decoder can be loaded (built on demand if needed)."""
    try:
        _load_library()
        return True
    except AcelpUnavailable:
        return False


def _load_encoder_library():
    global _enc_lib, _enc_lib_error
    if _enc_lib is not None:
        return _enc_lib
    try:
        if not _ENC_LIB_PATH.exists():
            build_encoder_library()
        lib = ctypes.CDLL(str(_ENC_LIB_PATH))
        lib.tetra_enc_create.restype = ctypes.c_void_p
        lib.tetra_enc_create.argtypes = []
        lib.tetra_enc_destroy.restype = None
        lib.tetra_enc_destroy.argtypes = [ctypes.c_void_p]
        lib.tetra_enc_encode.restype = ctypes.c_int
        lib.tetra_enc_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.POINTER(ctypes.c_uint8),
        ]
        _enc_lib = lib
        return _enc_lib
    except AcelpUnavailable:
        raise
    except OSError as exc:  # pragma: no cover - platform dependent
        _enc_lib_error = str(exc)
        raise AcelpUnavailable(f"cannot load {_ENC_LIB_PATH}: {exc}") from exc


def encoder_available() -> bool:
    """True if the encoder can be loaded (built on demand if needed)."""
    try:
        _load_encoder_library()
        return True
    except AcelpUnavailable:
        return False


class AcelpDecoder:
    """Stateful per-call TETRA ACELP speech decoder."""

    def __init__(self) -> None:
        lib = _load_library()
        self._lib = lib
        self._ctx = lib.tetra_dec_create()
        if not self._ctx:
            raise AcelpUnavailable("tetra_dec_create returned NULL")
        self._buf = (ctypes.c_int16 * SAMPLES_PER_SUBFRAME)()

    def decode_subframe(self, bits, bad_frame: bool = False) -> bytes:
        """Decode 137 codec-order bits -> 240 int16 PCM samples (little-endian bytes).

        ``bits`` is a sequence of 137 values, each 0 or 1. ``bad_frame`` sets the
        BFI so the codec conceals instead of decoding garbage.
        """
        if self._ctx is None:
            raise AcelpUnavailable("decoder already closed")
        if len(bits) != BITS_PER_SUBFRAME:
            raise ValueError(
                f"expected {BITS_PER_SUBFRAME} bits, got {len(bits)}"
            )
        cbits = (ctypes.c_uint8 * BITS_PER_SUBFRAME)()
        for i, b in enumerate(bits):
            cbits[i] = 1 if b else 0
        rc = self._lib.tetra_dec_decode(
            self._ctx, cbits, 1 if bad_frame else 0, self._buf
        )
        if rc != 0:
            raise AcelpUnavailable(f"tetra_dec_decode failed (rc={rc})")
        return bytes(self._buf)

    def decode_speech_frame(self, data, bad_frame: bool = False) -> bytes:
        """Decode a full MsSpeechFrame payload (274 bits = 2 sub-frames).

        Returns 480 int16 samples (60 ms) as little-endian bytes.
        """
        n = len(data)
        if n != 2 * BITS_PER_SUBFRAME:
            raise ValueError(
                f"expected {2 * BITS_PER_SUBFRAME} bits, got {n}"
            )
        first = self.decode_subframe(data[:BITS_PER_SUBFRAME], bad_frame)
        second = self.decode_subframe(data[BITS_PER_SUBFRAME:], bad_frame)
        return first + second

    def close(self) -> None:
        if self._ctx is not None:
            self._lib.tetra_dec_destroy(self._ctx)
            self._ctx = None

    def __del__(self):  # pragma: no cover - GC timing
        try:
            self.close()
        except Exception:
            pass


class AcelpEncoder:
    """Stateful per-call TETRA ACELP speech encoder (uplink/TX, phase V2).

    The exact mirror of :class:`AcelpDecoder`. Feed it 240-sample (30 ms) PCM
    sub-frames in order; it returns the 137 codec-order bits for each, ready to
    drop into ``MsUplinkSpeech.data`` (one bit per byte).
    """

    def __init__(self) -> None:
        lib = _load_encoder_library()
        self._lib = lib
        self._ctx = lib.tetra_enc_create()
        if not self._ctx:
            raise AcelpUnavailable("tetra_enc_create returned NULL")
        self._pcm = (ctypes.c_int16 * SAMPLES_PER_SUBFRAME)()
        self._bits = (ctypes.c_uint8 * BITS_PER_SUBFRAME)()

    def encode_subframe(self, pcm) -> list[int]:
        """Encode 240 int16 PCM samples -> a list of 137 codec-order bits (0/1).

        ``pcm`` may be a sequence of 240 ints, or 480 little-endian bytes.
        """
        if self._ctx is None:
            raise AcelpUnavailable("encoder already closed")
        if isinstance(pcm, (bytes, bytearray)):
            if len(pcm) != 2 * SAMPLES_PER_SUBFRAME:
                raise ValueError(
                    f"expected {2 * SAMPLES_PER_SUBFRAME} PCM bytes, got {len(pcm)}"
                )
            import struct

            samples = struct.unpack(f"<{SAMPLES_PER_SUBFRAME}h", bytes(pcm))
        else:
            samples = pcm
            if len(samples) != SAMPLES_PER_SUBFRAME:
                raise ValueError(
                    f"expected {SAMPLES_PER_SUBFRAME} PCM samples, got {len(samples)}"
                )
        for i, s in enumerate(samples):
            self._pcm[i] = int(s)
        rc = self._lib.tetra_enc_encode(self._ctx, self._pcm, self._bits)
        if rc != 0:
            raise AcelpUnavailable(f"tetra_enc_encode failed (rc={rc})")
        return [int(b) for b in self._bits]

    def encode_speech_frame(self, pcm) -> list[int]:
        """Encode a full 60 ms uplink frame (2 sub-frames) -> 274 bits.

        ``pcm`` is 480 int16 samples (a sequence of 480 ints, or 960 LE bytes),
        oldest 30 ms first. Returns 274 one-bit-per-byte values for
        ``MsUplinkSpeech.data``.
        """
        if isinstance(pcm, (bytes, bytearray)):
            if len(pcm) != 4 * SAMPLES_PER_SUBFRAME:
                raise ValueError(
                    f"expected {4 * SAMPLES_PER_SUBFRAME} PCM bytes, got {len(pcm)}"
                )
            half = 2 * SAMPLES_PER_SUBFRAME
            first = self.encode_subframe(bytes(pcm[:half]))
            second = self.encode_subframe(bytes(pcm[half:]))
        else:
            if len(pcm) != 2 * SAMPLES_PER_SUBFRAME:
                raise ValueError(
                    f"expected {2 * SAMPLES_PER_SUBFRAME} PCM samples, got {len(pcm)}"
                )
            first = self.encode_subframe(pcm[:SAMPLES_PER_SUBFRAME])
            second = self.encode_subframe(pcm[SAMPLES_PER_SUBFRAME:])
        return first + second

    def close(self) -> None:
        if self._ctx is not None:
            self._lib.tetra_enc_destroy(self._ctx)
            self._ctx = None

    def __del__(self):  # pragma: no cover - GC timing
        try:
            self.close()
        except Exception:
            pass
