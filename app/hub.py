"""Central hub: shared state, handle correlation, and browser broadcast.

The hub is the single meeting point between:
  * the stack-facing control server (one connection at a time),
  * the stack-facing telemetry server (receive-only),
  * the browser dashboard clients.
"""
from __future__ import annotations

import asyncio
import json
import time
import tomllib
from collections import deque
from typing import Any

from . import protocol


class PendingCommand:
    __slots__ = ("handle", "command", "sent_at", "silent", "future")

    def __init__(self, handle: int, command: dict, silent: bool):
        self.handle = handle
        self.command = command
        self.sent_at = time.time()
        self.silent = silent
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        # Silent (fire-and-forget) commands have no awaiter; consume any
        # exception so asyncio doesn't log "Future exception was never retrieved".
        self.future.add_done_callback(self._consume_exception)

    @staticmethod
    def _consume_exception(fut: asyncio.Future) -> None:
        if not fut.cancelled():
            fut.exception()


class Hub:
    RESPONSE_TIMEOUT = 5.0
    EVENT_LOG_MAX = 500

    def __init__(self) -> None:
        self.control_ws: Any | None = None
        self.control_connected = False
        self.telemetry_connected = False

        # Optional raw-frame logger (set by the CLI when --log-ws is given).
        self.wire_log: Any | None = None

        self.state: dict[str, Any] | None = None
        self.interface_version: str | None = None
        self.last_config_toml: str | None = None

        # Operator preference for how to register (NOT identity). The radio's
        # identity is owned by the MS and read live from MsRuntimeState.
        self.registration_type: str = "RegistrationToIndicatedCell"

        self.events: deque[dict] = deque(maxlen=self.EVENT_LOG_MAX)
        self.pending: dict[int, PendingCommand] = {}
        self._handle = 0

        # Live call-control state, keyed on the CMCE call_identifier. Built from
        # TNCC telemetry; released calls are dropped. Survives browser reconnects.
        self.calls: dict[int, dict[str, Any]] = {}

        # Downlink voice (U-plane): one ACELP decoder per active call, plus a
        # flag recording whether the native vocoder is usable at all. Lazily set
        # up on the first MsSpeechFrame so a missing/unbuildable codec never
        # blocks the rest of the app.
        self.decoders: dict[int, Any] = {}
        self._acelp_ok: bool | None = None

        # Uplink voice (U-plane, phase V2): one ACELP encoder per call we
        # transmit on, plus a lazily-probed encoder-availability flag. Mirror of
        # the decoder above; torn down on release with the matching decoder.
        self.encoders: dict[int, Any] = {}
        self._acelp_enc_ok: bool | None = None

        self.browser_clients: set[Any] = set()

    # -- handle allocation ----------------------------------------------------

    def next_handle(self) -> int:
        self._handle += 1
        return self._handle

    # -- control connection lifecycle ----------------------------------------

    def set_control(self, ws: Any | None) -> None:
        self.control_ws = ws
        self.control_connected = ws is not None
        if ws is None:
            # Fail any outstanding commands.
            for pc in list(self.pending.values()):
                if not pc.future.done():
                    pc.future.set_exception(ConnectionError("control disconnected"))
            self.pending.clear()

    def set_telemetry(self, connected: bool) -> None:
        self.telemetry_connected = connected

    # -- sending commands to the stack ---------------------------------------

    async def send_command(self, builder, *args, silent: bool = False, **kwargs) -> PendingCommand:
        """Build a command with a fresh handle, send it, and track it.

        `builder` is a function from protocol.py taking `handle` as first arg.
        Returns the PendingCommand whose `.future` resolves with the response.
        """
        handle = self.next_handle()
        command = builder(handle, *args, **kwargs)
        pc = PendingCommand(handle, command, silent)
        self.pending[handle] = pc

        if not self.control_connected or self.control_ws is None:
            self.pending.pop(handle, None)
            raise ConnectionError("control channel not connected")

        await self.control_ws.send(protocol.encode(command))
        if self.wire_log is not None:
            self.wire_log.record("out", "control", command)
        if not silent:
            variant, _ = protocol.variant_of(command)
            await self.broadcast({
                "type": "command",
                "handle": handle,
                "variant": variant,
                "command": command,
                "ts": time.time(),
            })
        return pc

    async def send_uncounted(self, message: dict) -> bool:
        """Send a fire-and-forget control command (no handle, no ack, no pending).

        Used for the high-rate uplink-speech stream (``MsUplinkSpeech``), which
        the interface does not acknowledge. Returns True if it was written to the
        wire, False if the control channel is down. Deliberately does NOT
        broadcast to browsers or expect a response — at ~16 frames/s that would
        flood the command feed and never resolve.
        """
        if not self.control_connected or self.control_ws is None:
            return False
        await self.control_ws.send(protocol.encode(message))
        if self.wire_log is not None:
            self.wire_log.record("out", "control", message)
        return True

    def handle_control_response(self, message: dict) -> dict:
        """Interpret a ControlResponse, update state, resolve pending.

        Returns a browser-facing envelope describing the response.
        """
        variant, payload = protocol.variant_of(message)
        handle = self._extract_handle(variant, payload)

        # Update cached state from management responses.
        if variant == "Management" and isinstance(payload, dict):
            inner_variant, inner = protocol.variant_of(payload)
            if inner_variant == "State" and isinstance(inner, dict):
                self.state = inner.get("state")
            elif inner_variant == "InterfaceVersion" and isinstance(inner, dict):
                self.interface_version = inner.get("version")
            elif inner_variant == "Config" and isinstance(inner, dict):
                self.last_config_toml = inner.get("toml")

        pc = self.pending.pop(handle, None) if handle is not None else None
        if pc is not None and not pc.future.done():
            pc.future.set_result(message)

        return {
            "type": "response",
            "handle": handle,
            "variant": variant,
            "response": message,
            "silent": bool(pc.silent) if pc else False,
            "ts": time.time(),
        }

    @staticmethod
    def _extract_handle(variant: str, payload: Any) -> int | None:
        if isinstance(payload, dict):
            if "handle" in payload:
                return payload["handle"]
            # Management wraps another externally-tagged variant.
            _, inner = protocol.variant_of(payload)
            if isinstance(inner, dict) and "handle" in inner:
                return inner["handle"]
        return None

    # -- inbound telemetry events --------------------------------------------

    def add_event(self, message: dict) -> dict:
        variant, payload = protocol.variant_of(message)
        envelope = {
            "type": "telemetry",
            "variant": variant,
            "event": message,
            "ts": time.time(),
        }
        self.events.append(envelope)
        if variant.startswith("Tncc"):
            self._apply_call_event(variant, payload)
        return envelope

    # -- downlink voice (U-plane) --------------------------------------------

    def _acelp_available(self) -> bool:
        if self._acelp_ok is None:
            try:
                from . import acelp

                self._acelp_ok = acelp.available()
            except Exception:  # pragma: no cover - defensive
                self._acelp_ok = False
        return self._acelp_ok

    def _decoder_for(self, cid: int):
        dec = self.decoders.get(cid)
        if dec is None:
            from . import acelp

            dec = acelp.AcelpDecoder()
            self.decoders[cid] = dec
        return dec

    def _close_decoder(self, cid: int) -> None:
        dec = self.decoders.pop(cid, None)
        if dec is not None:
            try:
                dec.close()
            except Exception:  # pragma: no cover - defensive
                pass

    # -- uplink voice (U-plane, phase V2) ------------------------------------

    def _acelp_encoder_available(self) -> bool:
        if self._acelp_enc_ok is None:
            try:
                from . import acelp

                self._acelp_enc_ok = acelp.encoder_available()
            except Exception:  # pragma: no cover - defensive
                self._acelp_enc_ok = False
        return self._acelp_enc_ok

    def _encoder_for(self, cid: int):
        enc = self.encoders.get(cid)
        if enc is None:
            from . import acelp

            enc = acelp.AcelpEncoder()
            self.encoders[cid] = enc
        return enc

    def _close_encoder(self, cid: int) -> None:
        enc = self.encoders.pop(cid, None)
        if enc is not None:
            try:
                enc.close()
            except Exception:  # pragma: no cover - defensive
                pass

    async def handle_uplink_audio(self, cid: int, pcm_b64: str | None) -> bool:
        """Encode one 60 ms mic frame and stream it to the MS — floor-gated.

        ``pcm_b64`` is base64 int16-LE mono @ 8 kHz, expected to be exactly 480
        samples (two 30 ms ACELP sub-frames). Sends ``MsUplinkSpeech`` only while
        this MS actually holds the floor (``holds_floor``); otherwise the frame
        is silently dropped so a late/racing browser frame can never leak onto
        the air. Fire-and-forget; returns True if transmitted.
        """
        if cid is None or not pcm_b64:
            return False
        call = self.calls.get(cid)
        if not self._uplink_allowed(call):
            return False
        if not self._acelp_encoder_available():
            return False
        try:
            import base64

            pcm = base64.b64decode(pcm_b64)
            enc = self._encoder_for(cid)
            bits = enc.encode_speech_frame(pcm)
        except Exception:  # pragma: no cover - encode is best effort
            return False
        message = protocol.ms_uplink_speech(cid, bits, frame_bits=274)
        return await self.send_uncounted(message)

    def handle_speech_frame(self, payload: Any) -> dict | None:
        """Turn one ``MsSpeechFrame`` into a compact audio envelope for the UI.

        Decodes the ACELP bits to PCM when the native vocoder is available and
        the frame belongs to a call we consider active. Always returns a light
        envelope carrying the talker + a "receiving" pulse (even without PCM) so
        the UI can show who is speaking; ``pcm`` is base64 int16 LE or ``None``.
        Returns ``None`` only for frames we deliberately drop (unknown call or a
        non-speech ``frame_bits``).
        """
        if not isinstance(payload, dict):
            return None
        cid = payload.get("call_identifier")
        if cid is None:
            return None

        talker = payload.get("transmitting_party_ssi")
        seq = payload.get("sequence")
        bad = bool(payload.get("bad_frame"))
        frame_bits = payload.get("frame_bits")
        data = payload.get("data") or []

        # Attribute the talker onto the live call so the floor/other-talker UI
        # lights up even independently of the audio path.
        call = self.calls.get(cid)
        if call is not None and talker is not None:
            call["talker_ssi"] = talker
            call["can_request_tx"] = False
            call["updated_at"] = time.time()

        # Format guard (§2/§7): only 274-bit TCH/S speech is decodable here.
        if frame_bits not in (None, 274) or (data and len(data) != 274):
            envelope = {
                "type": "audio", "call_identifier": cid, "sequence": seq,
                "talker_ssi": talker, "bad_frame": bad, "sample_rate": 8000,
                "pcm": None, "dropped": "unsupported_frame_bits",
            }
            return envelope

        pcm_b64 = None
        if self._acelp_available() and data:
            try:
                import base64

                dec = self._decoder_for(cid)
                pcm = dec.decode_speech_frame(data, bad_frame=bad)
                pcm_b64 = base64.b64encode(pcm).decode("ascii")
            except Exception:  # pragma: no cover - decode is best effort
                pcm_b64 = None

        return {
            "type": "audio", "call_identifier": cid, "sequence": seq,
            "talker_ssi": talker, "bad_frame": bad, "sample_rate": 8000,
            "pcm": pcm_b64,
        }

    # -- call-control state (TNCC / CMCE), keyed on call_identifier -----------

    def _apply_call_event(self, variant: str, payload: Any) -> None:
        """Fold a TNCC telemetry event into the per-call state map."""
        if not isinstance(payload, dict):
            return
        cid = payload.get("call_identifier")
        if cid is None:
            return
        # The event body sits under a variant-specific key ("indication",
        # "confirm", ...) or inline; grab whichever dict is present.
        body = {}
        for k in ("indication", "confirm", "request", "response"):
            if isinstance(payload.get(k), dict):
                body = payload[k]
                break

        if variant in ("TnccReleaseIndication", "TnccReleaseConfirm"):
            call = self.calls.get(cid, {"call_identifier": cid})
            call["state"] = "released"
            call["disconnect_cause"] = body.get("disconnect_cause")
            call["updated_at"] = time.time()
            # Drop from the live map so future snapshots omit the finished call;
            # the release event itself is still broadcast to tear down live UIs.
            self.calls.pop(cid, None)
            self._close_decoder(cid)
            self._close_encoder(cid)
            return

        call = self.calls.setdefault(cid, {
            "call_identifier": cid, "direction": None, "state": "setup",
            "peer_ssi": None, "group": False, "simplex": True,
            "talker_ssi": None, "tx_status": None, "can_request_tx": True,
            "holds_floor": False,
        })

        if variant == "TnccSetupIndication":
            bsi = body.get("basic_service_information") or {}
            is_group = bsi.get("communication_type") not in (None, "PointToPoint")
            call["group"] = call.get("group") or is_group
            # The real radio re-sends TnccSetupIndication during a group call to
            # report the current floor/talker: for a group the identity is the
            # CALLED party (GSSI) and the CALLING party is the current talker.
            if call["group"]:
                if body.get("called_party_ssi") is not None:
                    call["peer_ssi"] = body.get("called_party_ssi")
            else:
                call["direction"] = "mt"
                if body.get("calling_party_ssi") is not None:
                    call["peer_ssi"] = body.get("calling_party_ssi")
            call["simplex"] = body.get("simplex_duplex_selection") != "DuplexOperation"
            # The radio RE-SENDS this indication throughout a live group call as
            # floor housekeeping: calling_party_ssi is echoed as our OWN ISSI and
            # transmission_grant just alternates. Only treat it as a talker/floor
            # signal when the calling party is a REAL other member; otherwise leave
            # the floor to the authoritative TnccTxIndication.
            caller = body.get("calling_party_ssi")
            own = self._own_issi()
            if caller is not None and not (own is not None and caller == own):
                self._apply_floor(call, body.get("transmission_grant"), caller)
            # A group call goes straight to active; an individual call rings first.
            call["state"] = "active" if call["group"] else "incoming"
        elif variant == "TnccProceedIndication":
            if call["direction"] is None:
                call["direction"] = "mo"
            call["state"] = "proceeding"
        elif variant == "TnccAlertIndication":
            if call["direction"] is None:
                call["direction"] = "mo"
            call["state"] = "alerting"
            call["queued"] = body.get("call_queued") == "CallIsQueued"
        elif variant in ("TnccSetupConfirm", "TnccCompleteConfirm"):
            if call["direction"] is None:
                call["direction"] = "mo"
            call["state"] = "active"
            bsi = body.get("basic_service_information") or {}
            if bsi.get("communication_type") not in (None, "PointToPoint"):
                call["group"] = True
            self._apply_floor(
                call, body.get("transmission_grant") or body.get("transmission_status"), None)
        elif variant in ("TnccTxIndication", "TnccTxConfirm"):
            # Authoritative floor signal (unlike the re-sent SetupIndication echo).
            status = body.get("transmission_status") or body.get("transmission_grant")
            who = body.get("transmitting_party_ssi") if "transmitting_party_ssi" in body else None
            self._apply_floor(call, status, who)
            if call.get("state") in (None, "setup"):
                call["state"] = "active"
        elif variant == "TnccNotifyIndication":
            call["notify"] = body.get("call_status")
        call["updated_at"] = time.time()

    def _uplink_allowed(self, call: dict | None) -> bool:
        """Whether we may stream ``MsUplinkSpeech`` for this call right now (§4).

        Simplex/half-duplex and group calls are PTT floor-controlled: we transmit
        only while we hold the floor (``holds_floor``). A **duplex** individual
        call is NOT floor-controlled (§4.2): once it is through-connected the
        uplink grant is ours for the whole call with no PTT, so we allow streaming
        while it is ``active`` and stop only if the network explicitly revokes the
        grant (interrupt / not-granted / a REAL other user taking it).
        """
        if not call or call.get("state") == "released":
            return False
        simplex = call.get("simplex", True)
        group = call.get("group", False)
        if not simplex and not group:
            if call.get("state") != "active":
                return False
            tx = call.get("tx_status")
            if tx in ("TransmissionInterrupt", "TransmissionNotGranted"):
                return False
            if tx == "TransmissionGrantedToAnotherUser" and not call.get("holds_floor"):
                return False
            return True
        return bool(call.get("holds_floor"))

    def _apply_floor(self, call: dict, status: Any, talker_ssi: Any) -> None:
        """Fold a floor/transmission status (+ optional talker) into a call.

        Stores the RAW status. Who is talking and whether we may key are derived
        from the floor: a REAL other member holding it (``TransmissionGrantedToAnotherUser``
        with a non-own talker) sets ``talker_ssi`` and blocks our PTT; anything
        else — floor free/ours/denied/ceased, or the stack's echo of our OWN
        transmission (talker == our ISSI) — clears the talker and allows PTT.

        ``can_request_tx`` is derived here rather than from the stack's
        ``transmit_request_permission`` flag, which real captures show stuck at
        ``NotAllowed`` on every frame even while the MS transmits.
        """
        if not status:
            return
        call["tx_status"] = status
        own = self._own_issi()
        own_echo = talker_ssi is not None and own is not None and talker_ssi == own
        if status == "TransmissionGrantedToAnotherUser" and not own_echo:
            call["talker_ssi"] = talker_ssi if talker_ssi is not None else None
            call["can_request_tx"] = False
            call["holds_floor"] = False
        else:
            call["talker_ssi"] = None
            call["can_request_tx"] = True
            # We hold the floor when the stack grants it to us, OR when it echoes
            # our own transmission back as "...ToAnotherUser" with our own SSI.
            # A merely-free floor (NotGranted/Ceased) is NOT holding it.
            call["holds_floor"] = status == "TransmissionGranted" or own_echo

    def _own_issi(self) -> Any:
        """Our own ISSI from live MS state, used to spot the stack's echo of our
        own transmission. None until the MS has reported its state."""
        s = self.state
        return s.get("own_issi") if s else None

    def clear_call(self, cid: int) -> None:
        self.calls.pop(int(cid), None)

    # -- radio identity (owned by the MS, read from live state) --------------

    def identity(self) -> dict[str, Any] | None:
        """The radio's own identity, sourced from MsRuntimeState (never config).

        Returns None until the MS has reported its state via GetState.
        """
        s = self.state
        if not s:
            return None
        issi = s.get("own_issi")
        mcc = s.get("home_mcc")
        mnc = s.get("home_mnc")
        if issi is None or mcc is None or mnc is None:
            return None
        return {"issi": issi, "mcc": mcc, "mnc": mnc}

    # -- codeplug (talkgroup tree, parsed from the MS config TOML) ------------

    def codeplug(self) -> dict[str, Any] | None:
        """Parse the last-seen config TOML into a UI-friendly codeplug.

        The MS returns its full config (config_version "0.7") from GetConfig; the
        `[[folder]]` / `[[talkgroup]]` / `[[network]]` tables are the "radio
        programming" that drives the talkgroup tree. Returns None until a config
        has been read (or if it cannot be parsed).
        """
        toml = self.last_config_toml
        if not toml:
            return None
        try:
            doc = tomllib.loads(toml)
        except Exception:
            return None

        net = doc.get("net_info", {}) if isinstance(doc.get("net_info"), dict) else {}
        ms = doc.get("ms", {}) if isinstance(doc.get("ms"), dict) else {}

        def _rows(key: str) -> list[dict]:
            v = doc.get(key)
            return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []

        folders = [
            {"id": str(f.get("id", "")), "name": str(f.get("name", f.get("id", ""))),
             "order": int(f.get("order", 0))}
            for f in _rows("folder")
        ]
        talkgroups = [
            {"gssi": int(t["gssi"]), "name": str(t.get("name", t.get("gssi"))),
             "folder": (str(t["folder"]) if t.get("folder") is not None else None),
             "class_of_usage": (int(t["class_of_usage"]) if t.get("class_of_usage") is not None else None),
             "order": int(t.get("order", 0))}
            for t in _rows("talkgroup") if t.get("gssi") is not None
        ]
        networks = [
            {"mcc": int(n["mcc"]), "mnc": int(n["mnc"]),
             "name": (str(n["name"]) if n.get("name") is not None else None),
             "priority": int(n.get("priority", 0))}
            for n in _rows("network") if n.get("mcc") is not None and n.get("mnc") is not None
        ]
        scanlists = [
            {"name": str(sl.get("name", "")),
             "talkgroups": [int(g) for g in sl.get("talkgroups", []) if g is not None],
             "active": bool(sl.get("active", False)),
             "order": int(sl.get("order", 0))}
            for sl in _rows("scanlist") if sl.get("name")
        ]
        return {
            "folders": folders,
            "talkgroups": talkgroups,
            "networks": networks,
            "scanlists": scanlists,
            "mcc": net.get("mcc"),
            "mnc": net.get("mnc"),
            "attach_groups": [int(g) for g in ms.get("attach_groups", []) if g is not None],
            "config_version": doc.get("config_version"),
        }

    # -- browser broadcast ----------------------------------------------------

    def register_browser(self, ws: Any) -> None:
        self.browser_clients.add(ws)

    def unregister_browser(self, ws: Any) -> None:
        self.browser_clients.discard(ws)

    async def broadcast(self, envelope: dict) -> None:
        if not self.browser_clients:
            return
        data = json.dumps(envelope)
        dead = []
        for ws in list(self.browser_clients):
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.browser_clients.discard(ws)

    async def broadcast_status(self) -> None:
        await self.broadcast({
            "type": "status",
            "control": self.control_connected,
            "telemetry": self.telemetry_connected,
            "interface_version": self.interface_version,
            "ts": time.time(),
        })

    def snapshot(self) -> dict:
        """Full current state for a freshly connected browser."""
        return {
            "type": "snapshot",
            "control": self.control_connected,
            "telemetry": self.telemetry_connected,
            "interface_version": self.interface_version,
            "state": self.state,
            "identity": self.identity(),
            "registration_type": self.registration_type,
            "last_config_toml": self.last_config_toml,
            "codeplug": self.codeplug(),
            "calls": list(self.calls.values()),
            "events": list(self.events),
            "ts": time.time(),
        }

    # -- background maintenance ----------------------------------------------

    async def expire_pending(self) -> None:
        """Time out commands that never received a response."""
        now = time.time()
        for handle, pc in list(self.pending.items()):
            if now - pc.sent_at > self.RESPONSE_TIMEOUT:
                self.pending.pop(handle, None)
                if not pc.future.done():
                    pc.future.set_exception(asyncio.TimeoutError())
                if not pc.silent:
                    await self.broadcast({
                        "type": "timeout",
                        "handle": handle,
                        "variant": protocol.variant_of(pc.command)[0],
                        "ts": now,
                    })
