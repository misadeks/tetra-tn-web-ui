"""Optional raw WebSocket frame logger.

When enabled (``--log-ws``), every stack-facing WebSocket frame — control and
telemetry, both directions — is appended to a JSONL file, one JSON object per
line, so the traffic can be replayed/analysed offline. Each record carries a
timestamp, direction, channel, the decoded message (and its variant when we can
work it out), plus the raw payload for frames that failed to decode.

This is deliberately dependency-free and cheap: a single append-mode file handle
guarded by a lock (frames arrive from the asyncio loop, but we keep it
thread-safe so it is usable from anywhere).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from . import protocol

log = logging.getLogger("tnmm.wire")


def default_path() -> str:
    """An auto-generated, timestamped log file name in the CWD."""
    return datetime.now().strftime("ws-log-%Y%m%d-%H%M%S.jsonl")


class WireLog:
    """Append-only JSONL sink for raw WebSocket frames.

    ``path`` writes frames to a JSONL file (``None`` to skip the file).
    ``console`` also prints each frame's JSON line to stdout.
    """

    def __init__(self, path: str | None = None, console: bool = False) -> None:
        self.path = path
        self.console = console
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8") if path else None
        self._count = 0

    def record(self, direction: str, channel: str, message: Any = None,
               *, raw: Any = None) -> None:
        """Log one frame.

        ``direction`` is ``"in"`` (stack -> app) or ``"out"`` (app -> stack).
        ``channel`` is ``"control"`` or ``"telemetry"``. Pass ``message`` for a
        decoded JSON dict, or ``raw`` for an undecodable frame.
        """
        now = time.time()
        rec: dict[str, Any] = {
            "ts": round(now, 6),
            "iso": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
            "dir": direction,
            "channel": channel,
        }
        if message is not None:
            rec["variant"] = _variant_of(message)
            rec["message"] = message
        if raw is not None:
            rec["raw"] = _raw_repr(raw)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()
            if self.console:
                print(line, flush=True)
            self._count += 1
        # Also surface to the console at DEBUG (visible with -v) for live tailing.
        log.debug("%s %-9s %s", "->" if direction == "out" else "<-", channel,
                  rec.get("variant") or rec.get("raw"))

    def close(self) -> None:
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _variant_of(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    try:
        variant, _ = protocol.variant_of(message)
        return variant
    except Exception:
        # Fall back to the outermost key for non-standard envelopes.
        return next(iter(message), None)


def _raw_repr(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        try:
            return bytes(raw).decode("utf-8")
        except Exception:
            return "base64:" + __import__("base64").b64encode(bytes(raw)).decode("ascii")
    return str(raw)
