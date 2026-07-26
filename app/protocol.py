"""Message catalog helpers for the BlueStation MS interface.

All builders return plain dicts using serde's *externally-tagged* enum shape:
    {"VariantName": { ...fields... }}
The transport layer JSON-encodes these as UTF-8 and sends them in *binary*
WebSocket frames. Nothing here does any I/O.

Do NOT invent variants/fields/enum values beyond what the interface defines.
"""
from __future__ import annotations

import json
from typing import Any

# --- Enum value vocabularies (exact Rust identifier strings) -----------------

REGISTRATION_TYPES = ["PeriodicRegistration", "RegistrationToIndicatedCell"]

CELL_TYPES = ["CaCell", "DaCell"]

# TNMM-ATTACH DETACH GROUP IDENTITY (cl. 15.3.3.1) vocabularies.
GROUP_ATTACH_DETACH_MODES = ["Amendment", "DetachTheCurrentlyActiveGroupIdentities"]
GROUP_ATTACH_DETACH_TYPES = ["Attachment", "Detachment"]
CLASS_OF_USAGE = [f"ClassOfUsage{i}" for i in range(1, 9)]
GROUP_IDENTITY_DETACHMENT_REQUESTS = ["UserInitiatedDetachment"]
GROUP_IDENTITY_REPORTS = ["ReportRequested", "ReportNotRequested"]
GROUP_IDENTITY_DETACHMENT_REASONS = [
    "PermanentlyDetached", "Temporary1Detached", "Temporary2Detached", "UnknownGroupIdentity",
]
GROUP_IDENTITY_LIFETIMES = [
    "PermanentAttachmentNotNeeded", "AttachmentNeededForNextItsiAttach",
    "AttachmentNotAllowedAfterNextItsiAttach", "AttachmentNeededForNextLocationUpdate",
]


def class_of_usage_for(onair: int) -> str:
    """Map a codeplug on-air class-of-usage (0..7) to the TNMM enum.

    Per cl. 16.10.6 the codeplug stores the on-air 3-bit value (0..7) while TNMM
    numbers classes 1..8, so ``ClassOfUsage(N) == onair N-1``. Example: codeplug
    ``class_of_usage = 0`` → ``"ClassOfUsage1"``.
    """
    n = max(0, min(7, int(onair)))
    return f"ClassOfUsage{n + 1}"


REGISTRATION_STATES = ["Idle", "Registering", "Registered", "Detaching"]

SERVICE_STATUSES = [
    "InService",
    "InGracefulServiceDegradationMode",
    "InServiceWaitingForRegistration",
    "OutOfService",
    "MmBusy",
    "MmIdle",
]

DISABLE_STATUSES = ["Enabled", "TemporaryDisabled", "PermanentlyDisabled"]

REGISTRATION_STATUSES = [
    "Success",
    "Failure",
    "LaRegistrationExpired",
    "NoPreferredCellFound",
    "NoPermittedCellTypes",
]

REGISTRATION_REJECT_CAUSES = [
    "ItsiUnknown", "IllegalMs", "LaNotAllowed", "LaUnknown", "NetworkFailure",
    "Congestion", "ForwardRegistrationFailure", "ServiceNotSubscribed",
    "MandatoryElementError", "MessageConsistencyError", "RoamingNotSupported",
    "MigrationNotSupported", "NoCipherKsg", "IdentifiedCipherKsgNotSupported",
    "RequestedCipherKeyTypeNotAvailable", "IdentifiedCipherKeyNotAvailable",
    "CipheringRequired", "AuthenticationFailure",
]

# Secret sentinel returned by GetConfig; must be preserved on SetConfig.
SECRET_SENTINEL = "********"


# --- Wire (de)serialization --------------------------------------------------

def encode(message: dict[str, Any]) -> bytes:
    """JSON-encode a message as UTF-8 bytes for a binary WebSocket frame."""
    return json.dumps(message).encode("utf-8")


def decode(data: bytes | str) -> dict[str, Any]:
    """Decode a binary (or text) WebSocket frame into a dict."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8")
    return json.loads(data)


def variant_of(message: dict[str, Any]) -> tuple[str, Any]:
    """Return (variant_name, payload) for an externally-tagged message."""
    if not isinstance(message, dict) or len(message) != 1:
        return ("Unknown", message)
    (name, payload), = message.items()
    return (name, payload)


def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
    """Optional fields may be omitted; drop None values before sending."""
    return {k: v for k, v in d.items() if v is not None}


# --- Control channel: UI -> stack (ControlCommand) ---------------------------

def tnmm_registration(handle: int, *, registration_type: str, issi: int,
                      mcc_of_issi: int, mnc_of_issi: int,
                      **optional: Any) -> dict[str, Any]:
    request = _strip_none({
        "registration_type": registration_type,
        "issi": issi,
        "mcc_of_issi": mcc_of_issi,
        "mnc_of_issi": mnc_of_issi,
        **optional,
    })
    return {"TnmmRegistration": {"handle": handle, "request": request}}


def tnmm_deregistration(handle: int, *, issi: int | None = None,
                        mcc: int | None = None, mnc: int | None = None) -> dict[str, Any]:
    request = _strip_none({"issi": issi, "mcc": mcc, "mnc": mnc})
    return {"TnmmDeregistration": {"handle": handle, "request": request}}


def group_identity_request_item(gtsi: int, type_identifier: str, *,
                                class_of_usage: str | None = None,
                                detachment_request: str | None = None) -> dict[str, Any]:
    """One entry of a TnmmAttachDetachGroupIdentity `group_identity_request` list.

    `gtsi` is GTSI = (MNI<<24 | GSSI); the stack uses the low 24 bits as the GSSI,
    so a plain GSSI works. `class_of_usage` is only meaningful for `"Attachment"`;
    `detachment_request` only for `"Detachment"`.
    """
    return {
        "gtsi": int(gtsi),
        "group_identity_attach_detach_type_identifier": type_identifier,
        "class_of_usage": class_of_usage,
        "group_identity_detachment_request": detachment_request,
    }


def tnmm_attach_detach_group(handle: int, *, mode: str,
                             group_identity_request: list[dict],
                             report: str | None = "ReportNotRequested") -> dict[str, Any]:
    """Attach or detach group identities (TNMM-ATTACH DETACH GROUP IDENTITY).

    `mode` is one of GROUP_ATTACH_DETACH_MODES. `group_identity_request` is a
    non-empty list built with `group_identity_request_item`. The stack rejects an
    empty list even for the "detach all" mode, so pass at least one placeholder.
    """
    request = {
        "group_identity_attach_detach_mode": mode,
        "group_identity_request": list(group_identity_request),
        "group_identity_report": report,
    }
    return {"TnmmAttachDetachGroupIdentity": {"handle": handle, "request": request}}


def tnmm_status(handle: int, request: dict | None = None) -> dict[str, Any]:
    return {"TnmmStatus": {"handle": handle, "request": request or {}}}


def tnmm_energy_saving(handle: int, request: dict | None = None) -> dict[str, Any]:
    return {"TnmmEnergySaving": {"handle": handle, "request": request or {}}}


def management(inner: dict[str, Any]) -> dict[str, Any]:
    return {"Management": inner}


def get_state(handle: int) -> dict[str, Any]:
    return management({"GetState": {"handle": handle}})


def get_interface_version(handle: int) -> dict[str, Any]:
    return management({"GetInterfaceVersion": {"handle": handle}})


def get_config(handle: int) -> dict[str, Any]:
    return management({"GetConfig": {"handle": handle}})


def set_config(handle: int, toml: str) -> dict[str, Any]:
    return management({"SetConfig": {"handle": handle, "toml": toml}})


def apply_config(handle: int) -> dict[str, Any]:
    return management({"ApplyConfig": {"handle": handle}})


def activate_scanlist(handle: int, name: str, active: bool) -> dict[str, Any]:
    """Toggle a programmed scan list on/off (interface-2, Plane B).

    The MS's desired affiliation becomes the union of ``[ms].attach_groups`` and
    the GSSIs of every currently-active scan list, so activating/deactivating a
    scan list drives attach/detach on the air. Answered with a management ``Ack``.
    """
    return management({"ActivateScanlist": {"handle": handle, "name": str(name),
                                            "active": bool(active)}})


# --- Manual cell selection / survey (interface-3, Plane B) -------------------
# Receive-only carrier survey and manual camp/register. The MS RF stack owns
# scanning, cell-identity parsing, camp (cl. 18.3.4.6) and registration (cl. 16.4);
# these commands just drive it. Each is answered with a management ``Ack``.

def set_cell_selection_mode(handle: int, manual: bool) -> dict[str, Any]:
    """Switch cell selection between Auto (stack camps itself) and Manual."""
    return management({"SetCellSelectionMode": {"handle": handle, "manual": bool(manual)}})


def start_cell_scan(handle: int) -> dict[str, Any]:
    """Survey the combined ``[[frequency_list]]`` candidate set once (no wrap).

    Emits one ``MsScanResult`` telemetry event per found cell, then a single
    ``MsScanComplete``. Receive-only — transmits nothing on air.
    """
    return management({"StartCellScan": {"handle": handle}})


def stop_cell_scan(handle: int) -> dict[str, Any]:
    """Abort an in-progress survey (a completion still arrives for cells seen)."""
    return management({"StopCellScan": {"handle": handle}})


def camp_on_cell(handle: int, carrier_hz: int, register: bool) -> dict[str, Any]:
    """Camp on ``carrier_hz`` (a programmed downlink carrier) and optionally register.

    ``register=True`` forces an ITSI attach even if the cell advertises
    registration-not-required. An unknown carrier is rejected (``accepted:false``).
    """
    return management({"CampOnCell": {"handle": handle,
                                      "carrier_hz": int(carrier_hz),
                                      "register": bool(register)}})


# --- Call control (TNCC / CMCE) vocabularies --------------------------------
# Top-level ControlCommand variants (NOT wrapped in Management), answered with a
# transport-only TnccAck; real call progress arrives later on telemetry.

CIRCUIT_MODE_SERVICES = ["DataService", "SpeechService"]
COMMUNICATION_TYPES = ["PointToPoint", "PointToMultipoint",
                       "PointToMultipointAcknowledged", "Broadcast"]
SPEECH_SERVICES = ["TetraEncodedOneTimeslotSpeech", "ProprietaryEncodedOneTimeslotSpeech"]
ENCRYPTION_FLAGS = ["ClearEndToEndTransmission", "EncryptedEndToEndTransmission"]
SIMPLEX_DUPLEX = ["SimplexOperation", "DuplexOperation"]
HOOK_METHODS = ["NoHookSignallingDirectThroughConnect",
                "HookOnHookOffSignallingOrCallAcceptanceSignalling"]
REQUEST_TO_TRANSMIT = ["RequestToTransmitSendData",
                       "RequestThatOtherMsLsMayTransmitSendData"]
CALLED_PARTY_TYPES = ["Sna", "Ssi", "Tsi"]  # only Ssi is implemented by the MS
TRANSMISSION_CONDITIONS = ["RequestToTransmit", "TransmissionCeased"]
TX_DEMAND_PRIORITIES = ["LowPriority", "HighPriority", "PreEmptivePriority",
                        "EmergencyPreEmptivePriority"]
DISCONNECT_TYPES = ["DisconnectCall", "LeaveCallWithoutDisconnection",
                    "LeaveCallTemporarily"]
TRANSMISSION_STATUSES = [
    "TransmissionCeased", "TransmissionGranted", "TransmissionNotGranted",
    "TransmissionRequestQueued", "TransmissionGrantedToAnotherUser",
    "TransmissionInterrupt", "TransmissionWait", "TransmissionRequestFailed",
]


def _basic_service(*, speech: bool = True,
                   communication_type: str = "PointToPoint",
                   encryption: str = "ClearEndToEndTransmission") -> dict[str, Any]:
    return {
        "circuit_mode_service": "SpeechService" if speech else "DataService",
        "communication_type": communication_type,
        "data_service": None,
        "data_call_capacity": None,
        "encryption_flag": encryption,
        "speech_service": "TetraEncodedOneTimeslotSpeech" if speech else None,
    }


def tncc_setup(handle: int, *, called_party_ssi: int,
               communication_type: str = "PointToPoint",
               simplex_duplex: str = "SimplexOperation",
               request_tx: bool = True,
               call_priority: str = "PriorityNotDefined",
               encryption: str = "ClearEndToEndTransmission",
               hook: str = "NoHookSignallingDirectThroughConnect") -> dict[str, Any]:
    """Originate a call (TNCC-SETUP). PointToPoint+Ssi = individual; other
    communication types = group call to that GSSI. Only Ssi addressing works."""
    request = {
        "access_priority": None,
        "area_selection": None,
        "basic_service_information": _basic_service(
            communication_type=communication_type, encryption=encryption),
        "call_priority": call_priority,
        "called_party_type_identifier": "Ssi",
        "called_party_sna": None,
        "called_party_ssi": int(called_party_ssi),
        "called_party_extension": None,
        "external_subscriber_number_called": None,
        "clir_control": None,
        "hook_method_selection": hook,
        "request_to_transmit_send_data": (
            "RequestToTransmitSendData" if request_tx
            else "RequestThatOtherMsLsMayTransmitSendData"),
        "simplex_duplex_selection": simplex_duplex,
        "traffic_stealing": None,
    }
    return {"TnccSetup": {"handle": handle, "request": request}}


def tncc_setup_response(handle: int, call_identifier: int, *,
                        simplex_duplex: str = "SimplexOperation",
                        hook: str = "NoHookSignallingDirectThroughConnect") -> dict[str, Any]:
    """Answer an incoming call (U-CONNECT) named by ``call_identifier``."""
    response = {
        "access_priority": None,
        "basic_service_information": None,
        "clir_control": None,
        "hook_method_selection": hook,
        "simplex_duplex_selection": simplex_duplex,
        "traffic_stealing": None,
    }
    return {"TnccSetupResponse": {"handle": handle, "call_identifier": int(call_identifier),
                                  "response": response}}


def tncc_complete(handle: int, call_identifier: int, *,
                  simplex_duplex: str = "SimplexOperation",
                  hook: str = "NoHookSignallingDirectThroughConnect") -> dict[str, Any]:
    """Completion step for hook-signalling calls; maps to the same 'answer'."""
    request = {
        "access_priority": None,
        "basic_service_information_offered": None,
        "hook_method": hook,
        "simplex_duplex": simplex_duplex,
        "traffic_stealing": None,
    }
    return {"TnccComplete": {"handle": handle, "call_identifier": int(call_identifier),
                             "request": request}}


def tncc_tx(handle: int, call_identifier: int, *, transmission_condition: str,
            tx_demand_priority: str = "LowPriority",
            encryption: str = "ClearEndToEndTransmission") -> dict[str, Any]:
    """PTT: request the floor (``RequestToTransmit``) or release it
    (``TransmissionCeased``)."""
    request = {
        "access_priority": None,
        "encryption_flag": encryption,
        "traffic_stealing": None,
        "transmission_condition": transmission_condition,
        "tx_demand_priority": tx_demand_priority,
    }
    return {"TnccTx": {"handle": handle, "call_identifier": int(call_identifier),
                       "request": request}}


def tncc_release(handle: int, call_identifier: int, *,
                 disconnect_type: str = "DisconnectCall",
                 disconnect_cause: str = "UserRequestedDisconnection") -> dict[str, Any]:
    """Hang up (``DisconnectCall``) or leave the call locally."""
    request = {
        "access_priority": None,
        "disconnect_cause": disconnect_cause,
        "disconnect_type": disconnect_type,
        "traffic_stealing": None,
    }
    return {"TnccRelease": {"handle": handle, "call_identifier": int(call_identifier),
                            "request": request}}


# --- Uplink voice (U-plane, phase V2) ---------------------------------------
# Top-level ControlCommand variant carrying raw ACELP speech bits UI -> MS. It is
# FIRE-AND-FORGET: no handle, no ControlResponse/ack. Send one every 60 ms while
# this MS holds the floor (see the voice-TX brief §2/§4). ``data`` is the codec
# bits ONE BIT PER BYTE (each 0 or 1), length == ``frame_bits`` (274 for TCH/S).

def ms_uplink_speech(call_identifier: int, data: list[int],
                     frame_bits: int = 274) -> dict[str, Any]:
    """Build an ``MsUplinkSpeech`` command (no handle; not answered)."""
    return {"MsUplinkSpeech": {
        "call_identifier": int(call_identifier),
        "frame_bits": int(frame_bits),
        "data": [1 if b else 0 for b in data],
    }}
