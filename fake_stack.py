"""Bundled fake BlueStation stack for hardware-free demos.

Mirrors the real topology exactly: it connects **as a WebSocket client** to the
demo app's two servers (control + telemetry), speaks binary UTF-8 JSON frames,
negotiates the correct subprotocol per channel, and optionally sends HTTP Basic
auth. It answers management/TNMM requests and drives a realistic telemetry
scenario. `--chaos` randomly drops channels and injects registration failures.

Run standalone (app must already be listening):
    python fake_stack.py [--control ws://127.0.0.1:9102] [--telemetry ws://127.0.0.1:9101] [--chaos]
Or via the app:
    python -m app --simulate [--chaos]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import random

import websockets
from websockets.client import connect

from app import (CONTROL_SUBPROTOCOL, INTERFACE_VERSION, TELEMETRY_SUBPROTOCOL,
                 protocol)

log = logging.getLogger("fake-stack")

SAMPLE_TOML = """config_version = "0.7"
stack_mode     = "Ms"

[phy_io]
backend = "SoapySdr"

[phy_io.soapysdr]
ppm_err     = 0
device      = "driver=sx"
sample_rate = 600000
rx_antenna  = "RX"
tx_antenna  = "TX"
rx_gain_lna = 48.0
rx_gain_pga = 8.0

[net_info]
mcc = 901
mnc = 9999

[cell_info]
location_area = 1
colour_code   = 1

[ms]
issi             = 1000001
subscriber_class = 1
attach_groups    = [101]

[[folder]]
id    = "work"
name  = "Work"
order = 1

[[folder]]
id    = "ops"
name  = "Operations"
order = 2

[[talkgroup]]
gssi           = 101
name           = "Dispatch"
folder         = "work"
class_of_usage = 0
order          = 1

[[talkgroup]]
gssi           = 102
name           = "Field Ops"
folder         = "work"
class_of_usage = 0
order          = 2

[[talkgroup]]
gssi           = 220
name           = "Command"
folder         = "ops"
class_of_usage = 1
order          = 1

[[talkgroup]]
gssi           = 300
name           = "Emergency"
folder         = "ops"
class_of_usage = 3
order          = 2

[[network]]
mcc      = 901
mnc      = 9999
name     = "Home"
priority = 0

[[scanlist]]
name       = "Alpha"
talkgroups = [101, 102]
active     = true
order      = 1

[[scanlist]]
name       = "Ops"
talkgroups = [220, 300]
active     = false
order      = 2

[[frequency_list]]
name        = "primary"
mode        = "List"
frequencies = [439825000, 439850000]
dwell_ms    = 800

[telemetry]
host    = "127.0.0.1"
port    = 9101
username = "ui-operator"
password = "********"

[command]
host    = "127.0.0.1"
port    = 9102
username = "ui-operator"
password = "********"
"""

# GSSIs the MS attaches automatically at registration (mirrors [ms] attach_groups
# in SAMPLE_TOML above).
INITIAL_ATTACH_GROUPS = [101]

# Programmed scan lists (mirror the [[scanlist]] blocks in SAMPLE_TOML). Each is a
# named GSSI set with a programmed-default `active` flag. The MS's desired
# affiliation is INITIAL_ATTACH_GROUPS ∪ (GSSIs of every active scan list).
SCANLISTS = [
    {"name": "Alpha", "gssis": [101, 102], "active": True, "order": 1},
    {"name": "Ops", "gssis": [220, 300], "active": False, "order": 2},
]


class FakeState:
    def __init__(self) -> None:
        self.registration_state = "Idle"
        self.service_status = "OutOfService"
        self.own_issi = 1000001
        self.home_mcc = 901
        self.home_mnc = 9999
        self.serving_la = 0
        self.colour_code = 0
        self.rssi_dbfs: float | None = None
        self.attached_groups: list[int] = []
        self.group_op_in_progress = False
        self.restart_required = False
        # Names of the scan lists currently active (seeded from programmed default).
        self.active_scanlists: list[str] = [sl["name"] for sl in SCANLISTS if sl["active"]]
        # Live CMCE calls keyed on call_identifier. Each: {direction, group,
        # simplex, peer_ssi, floor (None|"own"|<ssi>), state}.
        self.calls: dict[int, dict] = {}
        self._next_cid = 50
        # Telemetry events are produced here and drained by the telemetry client.
        self.telemetry_q: asyncio.Queue = asyncio.Queue()

    def new_cid(self) -> int:
        cid = self._next_cid
        self._next_cid += 1
        return cid

    def desired_groups(self) -> list[int]:
        """Base attach groups ∪ the GSSIs of every active scan list."""
        groups = set(INITIAL_ATTACH_GROUPS)
        for sl in SCANLISTS:
            if sl["name"] in self.active_scanlists:
                groups.update(sl["gssis"])
        return sorted(groups)

    def ms_runtime_state(self) -> dict:
        return {
            "registration_state": self.registration_state,
            "service_status": self.service_status,
            "own_issi": self.own_issi,
            "home_mcc": self.home_mcc,
            "home_mnc": self.home_mnc,
            "serving_la": self.serving_la,
            "rssi_dbfs": self.rssi_dbfs,
            "colour_code": self.colour_code,
            "attached_groups": sorted(self.attached_groups),
            "active_scanlists": list(self.active_scanlists),
            "restart_required": self.restart_required,
        }

    def emit(self, event: dict) -> None:
        self.telemetry_q.put_nowait(event)


def _auth_headers(credentials):
    if not credentials:
        return None
    token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- control channel: respond to the app's commands --------------------------

async def _handle_control_command(state: FakeState, ws, message: dict, chaos: bool) -> None:
    variant, payload = protocol.variant_of(message)

    if variant == "Management":
        await _handle_management(state, ws, payload)
        return

    handle = payload.get("handle") if isinstance(payload, dict) else None

    if variant == "TnmmRegistration":
        await ws.send(protocol.encode({"TnmmAck": {"handle": handle, "accepted": True, "detail": None}}))
        asyncio.create_task(_scenario_register(state, payload.get("request", {}), chaos))
    elif variant == "TnmmDeregistration":
        await ws.send(protocol.encode({"TnmmAck": {"handle": handle, "accepted": True, "detail": None}}))
        asyncio.create_task(_scenario_deregister(state))
    elif variant == "TnmmAttachDetachGroupIdentity":
        await _handle_group_attach_detach(state, ws, handle, payload.get("request", {}))
    elif variant == "TnccSetup":
        await _handle_tncc_setup(state, ws, handle, payload.get("request", {}))
    elif variant in ("TnccSetupResponse", "TnccComplete"):
        await _handle_tncc_answer(state, ws, handle, payload.get("call_identifier"), variant)
    elif variant == "TnccTx":
        await _handle_tncc_tx(state, ws, handle, payload.get("call_identifier"),
                              payload.get("request", {}))
    elif variant == "TnccRelease":
        await _handle_tncc_release(state, ws, handle, payload.get("call_identifier"),
                                   payload.get("request", {}))
    elif variant == "MsUplinkSpeech":
        _handle_ms_uplink_speech(state, payload)
    elif variant in ("TnmmStatus", "TnmmEnergySaving"):
        await ws.send(protocol.encode({"TnmmAck": {"handle": handle, "accepted": False, "detail": "dormant"}}))
    else:
        log.warning("control: unknown command %s", variant)


async def _handle_group_attach_detach(state: FakeState, ws, handle, request: dict) -> None:
    mode = request.get("group_identity_attach_detach_mode")
    items = request.get("group_identity_request") or []
    report = request.get("group_identity_report")

    if state.registration_state != "Registered":
        await ws.send(protocol.encode({"TnmmAck": {
            "handle": handle, "accepted": False,
            "detail": "not registered; cannot attach/detach groups"}}))
        return
    if not items:
        await ws.send(protocol.encode({"TnmmAck": {
            "handle": handle, "accepted": False,
            "detail": "no group identities in the request"}}))
        return
    if state.group_op_in_progress:
        await ws.send(protocol.encode({"TnmmAck": {
            "handle": handle, "accepted": False,
            "detail": "a group attach/detach is already in progress"}}))
        return

    state.group_op_in_progress = True
    await ws.send(protocol.encode({"TnmmAck": {"handle": handle, "accepted": True, "detail": None}}))
    asyncio.create_task(_scenario_group(state, mode, items, report))


async def _scenario_group(state: FakeState, mode, items, report) -> None:
    # The real result follows the D-ATTACH/DETACH ACK; simulate SwMI latency.
    await asyncio.sleep(0.5)
    confirmed = []
    attached_now: list[int] = []
    detached_now: list[int] = []

    if mode == "DetachTheCurrentlyActiveGroupIdentities":
        # Detach everything currently attached, then attach the listed groups
        # (this is the "switch talkgroup" primitive).
        for gssi in sorted(state.attached_groups):
            confirmed.append({
                "gtsi": gssi,
                "group_identity_attach_detach_type_identifier": "Detachment",
                "group_identity_lifetime": None, "class_of_usage": None,
                "group_identity_detachment_reason": "PermanentlyDetached"})
            detached_now.append(gssi)
        state.attached_groups = []
        for it in items:
            if it.get("group_identity_attach_detach_type_identifier") != "Attachment":
                continue
            gssi = int(it.get("gtsi", 0)) & 0xFFFFFF
            if gssi and gssi not in state.attached_groups:
                state.attached_groups.append(gssi)
                attached_now.append(gssi)
                confirmed.append({
                    "gtsi": gssi,
                    "group_identity_attach_detach_type_identifier": "Attachment",
                    "group_identity_lifetime": "PermanentAttachmentNotNeeded",
                    "class_of_usage": it.get("class_of_usage") or "ClassOfUsage1",
                    "group_identity_detachment_reason": None})
    else:  # Amendment
        for it in items:
            gssi = int(it.get("gtsi", 0)) & 0xFFFFFF  # low 24 bits = GSSI (address type 0)
            typ = it.get("group_identity_attach_detach_type_identifier")
            if typ == "Attachment":
                if gssi and gssi not in state.attached_groups:
                    state.attached_groups.append(gssi)
                    attached_now.append(gssi)
                confirmed.append({
                    "gtsi": gssi,
                    "group_identity_attach_detach_type_identifier": "Attachment",
                    "group_identity_lifetime": "PermanentAttachmentNotNeeded",
                    "class_of_usage": it.get("class_of_usage") or "ClassOfUsage1",
                    "group_identity_detachment_reason": None})
            elif typ == "Detachment":
                known = gssi in state.attached_groups
                state.attached_groups = [x for x in state.attached_groups if x != gssi]
                if known:
                    detached_now.append(gssi)
                confirmed.append({
                    "gtsi": gssi,
                    "group_identity_attach_detach_type_identifier": "Detachment",
                    "group_identity_lifetime": None, "class_of_usage": None,
                    "group_identity_detachment_reason":
                        "PermanentlyDetached" if known else "UnknownGroupIdentity"})

    state.attached_groups = sorted(state.attached_groups)
    state.emit({"TnmmAttachDetachGroupIdentityConfirm": {
        "group_identity_attach_detach_mode": mode,
        "group_identity_report": report,
        "group_identities": confirmed}})
    # Legacy convenience events mirror the confirmed deltas.
    if attached_now:
        state.emit({"MsGroupAttach": {"issi": state.own_issi, "gssis": sorted(attached_now)}})
    if detached_now:
        state.emit({"MsGroupDetach": {"issi": state.own_issi, "gssis": sorted(detached_now)}})
    state.group_op_in_progress = False


async def _handle_management(state: FakeState, ws, payload: dict) -> None:
    inner_variant, inner = protocol.variant_of(payload)
    handle = inner.get("handle") if isinstance(inner, dict) else None

    if inner_variant == "GetInterfaceVersion":
        resp = {"Management": {"InterfaceVersion": {"handle": handle, "version": INTERFACE_VERSION}}}
    elif inner_variant == "GetState":
        resp = {"Management": {"State": {"handle": handle, "state": state.ms_runtime_state()}}}
    elif inner_variant == "GetConfig":
        resp = {"Management": {"Config": {"handle": handle, "toml": SAMPLE_TOML}}}
    elif inner_variant == "SetConfig":
        state.restart_required = True
        resp = {"Management": {"Ack": {"handle": handle, "accepted": True,
                                       "restart_required": True, "message": "staged"}}}
    elif inner_variant == "ApplyConfig":
        resp = {"Management": {"Ack": {"handle": handle, "accepted": True,
                                       "restart_required": True, "message": "applied; restart to take effect"}}}
    elif inner_variant == "ActivateScanlist":
        await _handle_activate_scanlist(state, ws, handle, inner)
        return
    else:
        resp = {"Management": {"Error": {"handle": handle, "message": f"unsupported: {inner_variant}"}}}

    await ws.send(protocol.encode(resp))


async def _handle_activate_scanlist(state: FakeState, ws, handle, inner: dict) -> None:
    """Toggle a programmed scan list, then reconcile the on-air affiliation.

    Desired affiliation = base attach groups ∪ active scan lists' GSSIs. When
    registered, the delta against `attached_groups` is applied and mirrored with
    legacy MsGroupAttach/MsGroupDetach convenience events.
    """
    name = inner.get("name")
    active = bool(inner.get("active"))
    known = next((sl for sl in SCANLISTS if sl["name"] == name), None)
    if known is None:
        await ws.send(protocol.encode({"Management": {"Ack": {
            "handle": handle, "accepted": False, "restart_required": False,
            "message": f"unknown scan list: {name}"}}}))
        return

    if active and name not in state.active_scanlists:
        state.active_scanlists.append(name)
    elif not active and name in state.active_scanlists:
        state.active_scanlists.remove(name)

    await ws.send(protocol.encode({"Management": {"Ack": {
        "handle": handle, "accepted": True, "restart_required": False,
        "message": f"scan list {name} {'activated' if active else 'deactivated'}"}}}))

    if state.registration_state != "Registered":
        return

    desired = state.desired_groups()
    current = set(state.attached_groups)
    added = [g for g in desired if g not in current]
    removed = [g for g in state.attached_groups if g not in desired]
    state.attached_groups = desired
    if added:
        state.emit({"MsGroupAttach": {"issi": state.own_issi, "gssis": sorted(added)}})
    if removed:
        state.emit({"MsGroupDetach": {"issi": state.own_issi, "gssis": sorted(removed)}})


# --- telemetry scenarios -----------------------------------------------------

async def _scenario_register(state: FakeState, request: dict, chaos: bool) -> None:
    state.registration_state = "Registering"
    state.service_status = "InServiceWaitingForRegistration"
    await asyncio.sleep(1.0)

    issi = request.get("issi", state.own_issi)
    mcc = request.get("mcc_of_issi", state.home_mcc)
    mnc = request.get("mnc_of_issi", state.home_mnc)

    if chaos and random.random() < 0.25:
        state.registration_state = "Idle"
        state.service_status = "OutOfService"
        state.rssi_dbfs = None
        state.emit({"TnmmRegistrationConfirm": {
            "registration_status": "Failure",
            "registration_reject_cause": random.choice(
                ["NetworkFailure", "Congestion", "AuthenticationFailure", "LaNotAllowed"]),
            "cell_type_where_registered": "CaCell",
            "la_where_registered": 0, "mcc_where_registered": mcc, "mnc_where_registered": mnc,
        }})
        state.emit({"TnmmServiceIndication": {"service_status": "OutOfService", "disable_status": "Enabled"}})
        return

    state.registration_state = "Registered"
    state.service_status = "InService"
    state.serving_la = 1
    state.colour_code = 1
    state.rssi_dbfs = -57.5
    state.attached_groups = state.desired_groups()
    state.emit({"TnmmRegistrationConfirm": {
        "registration_status": "Success",
        "registration_reject_cause": None,
        "cell_type_where_registered": "CaCell",
        "la_where_registered": 1, "mcc_where_registered": mcc, "mnc_where_registered": mnc,
    }})
    state.emit({"MsRegistration": {"issi": issi}})
    if state.attached_groups:
        state.emit({"MsGroupAttach": {"issi": issi, "gssis": list(state.attached_groups)}})
    state.emit({"TnmmServiceIndication": {"service_status": "InService", "disable_status": "Enabled"}})


async def _scenario_deregister(state: FakeState) -> None:
    state.registration_state = "Detaching"
    await asyncio.sleep(0.5)
    issi = state.own_issi
    state.registration_state = "Idle"
    state.service_status = "OutOfService"
    state.serving_la = 0
    state.rssi_dbfs = None
    state.attached_groups = []
    state.emit({"MsDeregistration": {"issi": issi}})
    state.emit({"TnmmServiceIndication": {"service_status": "OutOfService", "disable_status": "Enabled"}})


# --- call control (TNCC / CMCE) scenarios ------------------------------------

async def _tncc_ack(ws, handle, accepted: bool = True, detail=None) -> None:
    await ws.send(protocol.encode({"TnccAck": {
        "handle": handle, "accepted": accepted, "detail": detail}}))


def _tx_indication(cid: int, *, talker, status: str) -> dict:
    return {"TnccTxIndication": {"call_identifier": cid, "indication": {
        "encryption_flag": "ClearEndToEndTransmission", "notification_indicator": None,
        "transmitting_party_ssi": talker, "transmitting_party_extension": None,
        "external_subscriber_number": None,
        "transmit_request_permission": "AllowedToRequestForTransmission",
        "transmission_status": status}}}


def _speech_frame(cid: int, *, talker, sequence: int, data: list) -> dict:
    return {"MsSpeechFrame": {
        "call_identifier": cid, "timeslot": 2, "sequence": sequence,
        "transmitting_party_ssi": talker, "frame_bits": 274,
        "bad_frame": False, "data": data}}


def _handle_ms_uplink_speech(state: "FakeState", payload: dict) -> None:
    """Loop uplink speech back as a downlink frame (demo/loopback only).

    The real network would route these bits to the far end; here we echo them
    straight back over telemetry as an ``MsSpeechFrame`` from ourselves, but ONLY
    while we actually hold the floor. That lets ``--simulate`` prove the whole
    mic -> ACELP encode -> transport -> decode -> speaker path without hardware.
    """
    if not isinstance(payload, dict):
        return
    cid = payload.get("call_identifier")
    data = payload.get("data") or []
    call = state.calls.get(cid) if cid is not None else None
    if call is None:
        return
    # Only echo while we may transmit. Simplex/group are floor-gated (the fake
    # stack marks our own floor with the sentinel "own"); a DUPLEX individual call
    # (§4.2) is granted for its whole through-connected life with no floor/PTT, so
    # echo it whenever it is active.
    duplex_active = (not call.get("simplex", True)) and not call.get("group") \
        and call.get("state") == "active"
    if not duplex_active and call.get("floor") not in ("own", state.own_issi):
        return
    if payload.get("frame_bits") not in (None, 274) or len(data) != 274:
        return
    seq = call.get("_ul_seq", 0)
    call["_ul_seq"] = seq + 1
    state.emit(_speech_frame(cid, talker=state.own_issi, sequence=seq, data=list(data)))


async def _emit_speech_over(state: "FakeState", cid: int, talker: int,
                            duration: float, seq_start: int) -> int:
    """Stream synthetic MsSpeechFrames (~60 ms cadence) for a remote talker's
    over, so the UI actually hears audio in the demo. Returns the next sequence.
    Stops early if the call ends or the floor changes hands."""
    from app.demo_speech import TONE_FRAMES

    seq = seq_start
    n = max(1, int(duration / 0.06))
    for i in range(n):
        call = state.calls.get(cid)
        if call is None or call.get("floor") != talker:
            break
        state.emit(_speech_frame(cid, talker=talker, sequence=seq,
                                 data=TONE_FRAMES[i % len(TONE_FRAMES)]))
        seq += 1
        await asyncio.sleep(0.06)
    return seq


async def _handle_tncc_setup(state: FakeState, ws, handle, request: dict) -> None:
    if state.registration_state != "Registered":
        await _tncc_ack(ws, handle, False, "not registered; cannot originate a call")
        return
    if request.get("called_party_type_identifier") != "Ssi":
        await _tncc_ack(ws, handle, False, "only SSI addressing is implemented")
        return
    ssi = request.get("called_party_ssi")
    if not ssi:
        await _tncc_ack(ws, handle, False, "called_party_ssi required")
        return
    await _tncc_ack(ws, handle, True)
    bsi = request.get("basic_service_information") or {}
    group = bsi.get("communication_type") not in (None, "PointToPoint")
    simplex = request.get("simplex_duplex_selection") != "DuplexOperation"
    want_floor = request.get("request_to_transmit_send_data") == "RequestToTransmitSendData"
    asyncio.create_task(_scenario_outgoing_call(
        state, int(ssi), group, simplex, want_floor, request.get("basic_service_information")))


async def _scenario_outgoing_call(state: FakeState, ssi: int, group: bool,
                                  simplex: bool, want_floor: bool, bsi) -> None:
    cid = state.new_cid()
    sdx = "SimplexOperation" if simplex else "DuplexOperation"
    state.calls[cid] = {"direction": "mo", "group": group, "simplex": simplex,
                        "peer_ssi": ssi, "floor": None, "state": "proceeding"}
    await asyncio.sleep(0.4)
    if cid not in state.calls:
        return
    state.emit({"TnccProceedIndication": {"call_identifier": cid, "indication": {
        "basic_service_information_offered": bsi, "call_status": "CallIsProgressing",
        "hook_method": "NoHookSignallingDirectThroughConnect",
        "notification_indicator": None, "simplex_duplex": sdx}}})
    if not group:
        await asyncio.sleep(0.7)
        if cid not in state.calls:
            return
        state.emit({"TnccAlertIndication": {"call_identifier": cid, "indication": {
            "basic_service_information_offered": bsi, "call_queued": "CallIsNotQueued",
            "call_time_out_set_up_phase": "PreDefined",
            "notification_indicator": None, "simplex_duplex": sdx}}})
    await asyncio.sleep(0.8)
    if cid not in state.calls:
        return
    grant = "TransmissionGranted" if want_floor else "TransmissionNotGranted"
    state.calls[cid]["state"] = "active"
    state.calls[cid]["floor"] = "own" if want_floor else None
    state.emit({"TnccSetupConfirm": {"call_identifier": cid, "confirm": {
        "basic_service_information": bsi, "call_priority": "PriorityNotDefined",
        "call_ownership": "ACallOwner", "call_amalgamation": "CallNotAmalgamated",
        "call_time_out": "Infinite",
        "hook_method_selection": "NoHookSignallingDirectThroughConnect",
        "notification_indicator": None, "simplex_duplex_selection": sdx,
        "transmission_grant": grant,
        "transmission_request_permission": "AllowedToRequestForTransmission"}}})
    if want_floor:
        state.emit(_tx_indication(cid, talker=state.own_issi, status="TransmissionGranted"))
    # An individual call is ended by the far party after a while (D-RELEASE);
    # group calls persist until the local user leaves.
    if not group:
        asyncio.create_task(_scenario_peer_hangup(state, cid, random.uniform(10, 18)))
    else:
        # Other talkgroup members chime in over time so the UI shows the talker.
        asyncio.create_task(_scenario_group_chatter(state, cid))


async def _scenario_peer_hangup(state: FakeState, cid: int, delay: float,
                                cause: str = "UserRequestedDisconnection") -> None:
    """Simulate the far MS ending an established individual call after a while."""
    await asyncio.sleep(delay)
    if cid not in state.calls:
        return
    state.calls.pop(cid, None)
    state.emit({"TnccReleaseIndication": {"call_identifier": cid, "indication": {
        "disconnect_cause": cause, "notification_indicator": None}}})


# BS "call hang" timeout for group calls: the call context persists after the
# talker releases the floor, then the infrastructure tears it down if nobody
# re-keys within the hang time (ETSI TS 100 392-2 cl. 14.5.2).
GROUP_HANG_SECONDS = 6.0


def _cancel_group_hang(call: dict) -> None:
    task = call.get("hang_task")
    if task is not None and not task.done():
        task.cancel()
    call["hang_task"] = None


async def _group_hang_timeout(state: FakeState, cid: int) -> None:
    try:
        await asyncio.sleep(GROUP_HANG_SECONDS)
    except asyncio.CancelledError:
        return
    call = state.calls.get(cid)
    if call is None or call.get("floor") is not None:
        return  # re-keyed in time -> keep the call up
    state.calls.pop(cid, None)
    state.emit({"TnccReleaseIndication": {"call_identifier": cid, "indication": {
        "disconnect_cause": "ExpiryOfTimer", "notification_indicator": None}}})


def _arm_group_hang(state: FakeState, cid: int) -> None:
    call = state.calls.get(cid)
    if call is None:
        return
    _cancel_group_hang(call)
    call["hang_task"] = asyncio.create_task(_group_hang_timeout(state, cid))


# Simulated group activity: other members of the talkgroup key up now and then so
# the UI can show "who is talking". Runs for the life of a group call; pauses
# while the floor is taken (locally or by another remote), and rearms the BS hang
# timer whenever a remote talker finishes so the call still clears if it goes idle.
_GROUP_MEMBERS = [4001, 4002, 5551234, 1000042, 2000055]


async def _scenario_group_chatter(state: FakeState, cid: int) -> None:
    seq = 1
    while True:
        # Key up sooner than the BS group-hang timeout so a remote talker
        # reliably grabs the floor before the call is torn down (keeps the
        # demo lively and audible).
        await asyncio.sleep(random.uniform(2.0, 4.5))
        call = state.calls.get(cid)
        if call is None:
            return
        if call.get("floor") is not None:
            continue  # someone already holds the floor (local or remote)
        talker = random.choice(_GROUP_MEMBERS)
        call["floor"] = talker
        _cancel_group_hang(call)
        state.emit(_tx_indication(cid, talker=talker,
                                  status="TransmissionGrantedToAnotherUser"))
        # Stream actual voice for this over so the UI plays audio + shows talker.
        over = random.uniform(2.5, 5.0)
        seq = await _emit_speech_over(state, cid, talker, over, seq)
        call = state.calls.get(cid)
        if call is None:
            return
        if call.get("floor") == talker:   # still this remote talker -> release
            call["floor"] = None
            state.emit(_tx_indication(cid, talker=None, status="TransmissionCeased"))
            _arm_group_hang(state, cid)


async def _handle_tncc_answer(state: FakeState, ws, handle, cid, variant) -> None:
    call = state.calls.get(cid) if cid is not None else None
    if call is None:
        await _tncc_ack(ws, handle, False, "unknown call identifier")
        return
    await _tncc_ack(ws, handle, True)
    on_hook = call.get("hook") == "HookOnHookOffSignallingOrCallAcceptanceSignalling"
    sdx = "SimplexOperation" if call.get("simplex", True) else "DuplexOperation"
    # On/off-hook: the FIRST TnccSetupResponse is only the ringing signal (U-ALERT);
    # the call is NOT connected until the peer taps Accept -> TnccComplete (U-CONNECT).
    if variant == "TnccSetupResponse" and on_hook:
        call["state"] = "ringing"
        # No indication is looped back for the ring: the U-ALERT goes on-air to the
        # caller (ringback). We simply wait for the peer's TnccComplete (Accept).
        return
    # Direct call (TnccSetupResponse) or on/off-hook completion (TnccComplete):
    # the call is now fully connected. Emit the matching confirm.
    call["state"] = "active"
    call["floor"] = None
    confirm_name = "TnccCompleteConfirm" if variant == "TnccComplete" else "TnccSetupConfirm"
    state.emit({confirm_name: {"call_identifier": cid, "confirm": {
        "basic_service_information": None, "call_priority": "PriorityNotDefined",
        "call_ownership": "NotACallOwner", "call_amalgamation": "CallNotAmalgamated",
        "call_time_out": "Infinite",
        "hook_method_selection": call.get("hook") or "NoHookSignallingDirectThroughConnect",
        "notification_indicator": None, "simplex_duplex_selection": sdx,
        "transmission_grant": "TransmissionNotGranted",
        "transmission_request_permission": "AllowedToRequestForTransmission"}}})
    state.emit(_tx_indication(cid, talker=None, status="TransmissionCeased"))
    # The caller eventually hangs up from their end (D-RELEASE).
    asyncio.create_task(_scenario_peer_hangup(state, cid, random.uniform(9, 16)))


async def _handle_tncc_tx(state: FakeState, ws, handle, cid, request: dict) -> None:
    call = state.calls.get(cid) if cid is not None else None
    if call is None:
        await _tncc_ack(ws, handle, False, "unknown call identifier")
        return
    await _tncc_ack(ws, handle, True)
    cond = request.get("transmission_condition")
    if cond == "RequestToTransmit":
        # Re-keying cancels the group-call hang timeout; the context stays up.
        if call.get("group"):
            _cancel_group_hang(call)
        if call["floor"] in (None, "own"):
            call["floor"] = "own"
            status, talker = "TransmissionGranted", state.own_issi
        else:
            status, talker = "TransmissionNotGranted", call["floor"]
        state.emit({"TnccTxConfirm": {"call_identifier": cid, "confirm": {
            "transmission_grant": status, "transmission_status": status,
            "transmission_request_permission": "AllowedToRequestForTransmission",
            "notification_indicator": None}}})
        state.emit(_tx_indication(cid, talker=talker, status=status))
    else:  # TransmissionCeased
        if call["floor"] == "own":
            call["floor"] = None
        state.emit(_tx_indication(cid, talker=None, status="TransmissionCeased"))
        # Floor released: the call context lingers, then the BS hangs it up
        # unless someone re-keys within the hang time. Individual calls end via
        # the peer-hangup scenario instead.
        if call.get("group"):
            _arm_group_hang(state, cid)


async def _handle_tncc_release(state: FakeState, ws, handle, cid, request: dict) -> None:
    call = state.calls.get(cid) if cid is not None else None
    if call is None:
        await _tncc_ack(ws, handle, False, "unknown call identifier")
        return
    await _tncc_ack(ws, handle, True)
    _cancel_group_hang(call)
    state.calls.pop(cid, None)
    state.emit({"TnccReleaseConfirm": {"call_identifier": cid, "confirm": {
        "disconnect_cause": request.get("disconnect_cause", "UserRequestedDisconnection"),
        "disconnect_status": "DisconnectionSuccessful", "notification_indicator": None}}})


async def _scenario_incoming_call(state: FakeState) -> None:
    """Simulate an inbound individual call: ring, then time out if unanswered."""
    if state.registration_state != "Registered" or state.calls:
        return
    cid = state.new_cid()
    caller = random.choice([4001, 4002, 5551234, 1000042])
    bsi = {"circuit_mode_service": "SpeechService", "communication_type": "PointToPoint",
           "data_service": None, "data_call_capacity": None,
           "encryption_flag": "ClearEndToEndTransmission",
           "speech_service": "TetraEncodedOneTimeslotSpeech"}
    state.calls[cid] = {"direction": "mt", "group": False, "simplex": True,
                        "peer_ssi": caller, "floor": None, "state": "incoming",
                        "hook": "HookOnHookOffSignallingOrCallAcceptanceSignalling"}
    state.emit({"TnccSetupIndication": {"call_identifier": cid, "indication": {
        "basic_service_information": bsi, "call_priority": "PriorityNotDefined",
        "call_time_out": "Infinite", "called_party_ssi": state.own_issi,
        "called_party_extension": None, "calling_party_ssi": caller,
        "calling_party_extension": None, "external_subscriber_number_calling": None,
        "clir_control": "PresentationNotRestricted",
        "hook_method_selection": "HookOnHookOffSignallingOrCallAcceptanceSignalling",
        "notification_indicator": None, "simplex_duplex_selection": "SimplexOperation",
        "transmission_grant": "TransmissionNotGranted",
        "transmission_request_permission": "AllowedToRequestForTransmission"}}})
    await asyncio.sleep(18)
    call = state.calls.get(cid)
    if call and call.get("state") in ("incoming", "ringing"):
        state.calls.pop(cid, None)
        state.emit({"TnccReleaseIndication": {"call_identifier": cid, "indication": {
            "disconnect_cause": "ExpiryOfTimer", "notification_indicator": None}}})


# --- client connection loops (with reconnect) --------------------------------

async def _control_client(state: FakeState, url: str, credentials, chaos: bool) -> None:
    headers = _auth_headers(credentials)
    while True:
        try:
            async with connect(url, subprotocols=[CONTROL_SUBPROTOCOL],
                               extra_headers=headers, max_size=4 * 1024 * 1024) as ws:
                log.info("control: connected to %s", url)
                async for raw in ws:
                    if isinstance(raw, str):
                        log.warning("control: unexpected text frame")
                        continue
                    try:
                        message = protocol.decode(raw)
                    except Exception:
                        continue
                    await _handle_control_command(state, ws, message, chaos)
        except (OSError, websockets.WebSocketException) as exc:
            log.info("control: disconnected (%s), retrying…", exc.__class__.__name__)
        await asyncio.sleep(1.0)


async def _telemetry_client(state: FakeState, url: str, credentials) -> None:
    headers = _auth_headers(credentials)
    while True:
        try:
            async with connect(url, subprotocols=[TELEMETRY_SUBPROTOCOL],
                               extra_headers=headers, max_size=4 * 1024 * 1024) as ws:
                log.info("telemetry: connected to %s", url)
                while True:
                    event = await state.telemetry_q.get()
                    await ws.send(protocol.encode(event))
        except (OSError, websockets.WebSocketException) as exc:
            log.info("telemetry: disconnected (%s), retrying…", exc.__class__.__name__)
        await asyncio.sleep(1.0)


async def _chaos_loop(state: FakeState) -> None:
    """Periodically emit spontaneous telemetry to keep the demo lively."""
    while True:
        await asyncio.sleep(random.uniform(8, 15))
        if state.registration_state != "Registered":
            continue
        # Occasionally ring the radio with an inbound individual call.
        if not state.calls and random.random() < 0.35:
            asyncio.create_task(_scenario_incoming_call(state))
            continue
        # Occasionally simulate a downlink break: service drops to OutOfService
        # while registration_state stays Registered (coverage vs. registration split).
        if random.random() < 0.4:
            state.service_status = "OutOfService"
            state.rssi_dbfs = None
            state.emit({"TnmmServiceIndication": {"service_status": "OutOfService", "disable_status": "Enabled"}})
            await asyncio.sleep(random.uniform(4, 8))
            if state.registration_state != "Registered":
                continue
            state.service_status = "InService"
            state.rssi_dbfs = round(random.uniform(-70, -52), 1)
            state.emit({"TnmmServiceIndication": {"service_status": "InService", "disable_status": "Enabled"}})
        else:
            state.emit({"TnmmServiceIndication": {
                "service_status": random.choice(["InService", "InGracefulServiceDegradationMode"]),
                "disable_status": "Enabled"}})


async def run(control_url: str, telemetry_url: str, credentials=None, chaos: bool = False) -> None:
    state = FakeState()
    tasks = [
        asyncio.create_task(_control_client(state, control_url, credentials, chaos)),
        asyncio.create_task(_telemetry_client(state, telemetry_url, credentials)),
    ]
    if chaos:
        tasks.append(asyncio.create_task(_chaos_loop(state)))
    await asyncio.gather(*tasks)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fake BlueStation stack (WS client simulator)")
    p.add_argument("--control", default="ws://127.0.0.1:9102")
    p.add_argument("--telemetry", default="ws://127.0.0.1:9101")
    p.add_argument("--username", default="")
    p.add_argument("--password", default="")
    p.add_argument("--chaos", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
    creds = (args.username, args.password) if args.username else None
    try:
        asyncio.run(run(args.control, args.telemetry, creds, args.chaos))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
