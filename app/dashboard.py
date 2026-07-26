"""Browser-facing dashboard server.

Runs on its own `websockets` server. Plain HTTP GET returns the single-page
dashboard; `/ws` upgrades to a browser WebSocket used to stream live updates
and receive operator commands. This is a local UI transport and is unrelated to
the stack's binary protocol.
"""
from __future__ import annotations

import http
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol, serve

from . import protocol
from . import codeplug as codeplug_mod
from .config import DashboardConfig
from .hub import Hub

log = logging.getLogger("tnmm.dashboard")

_STATIC = Path(__file__).parent / "static"


def _load_index() -> bytes:
    return (_STATIC / "index.html").read_bytes()


async def run_dashboard(hub: Hub, cfg: DashboardConfig, ssl_context=None) -> None:
    index_html = _load_index()
    # A short content hash of the served UI. It's baked into the page (replacing
    # the __UI_BUILD__ placeholder) and reported in every snapshot, so an already
    # open tab can detect that the server is now serving a different build (e.g.
    # after a restart with edited HTML/JS) and reload itself instead of silently
    # running stale JS over a reconnected WebSocket.
    ui_build = hashlib.sha1(index_html).hexdigest()[:12]
    index_html = index_html.replace(b"__UI_BUILD__", ui_build.encode("ascii"))

    async def process_request(path: str, request_headers):
        route = path.split("?", 1)[0]
        if route == "/ws":
            return None  # allow the WebSocket upgrade
        if route in ("/", "/index.html"):
            headers = [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(index_html))),
                ("Cache-Control", "no-store"),
            ]
            return (http.HTTPStatus.OK, headers, index_html)
        body = b"Not Found"
        return (http.HTTPStatus.NOT_FOUND,
                [("Content-Length", str(len(body)))], body)

    async def handler(ws: WebSocketServerProtocol) -> None:
        hub.register_browser(ws)
        try:
            snap = hub.snapshot()
            snap["ui_build"] = ui_build
            await ws.send(json.dumps(snap))
            async for raw in ws:
                await _on_browser_message(hub, ws, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            hub.unregister_browser(ws)

    async with serve(handler, cfg.host, cfg.port, process_request=process_request,
                     ssl=ssl_context):
        scheme = "https" if ssl_context is not None else "http"
        log.info("dashboard on %s://%s:%s", scheme, cfg.host, cfg.port)
        import asyncio
        await asyncio.Future()


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj)


async def _reply_error(ws: Any, action: str, message: str) -> None:
    await ws.send(_json({"type": "error", "action": action, "message": message}))


async def _on_browser_message(hub: Hub, ws: Any, raw) -> None:
    import json
    try:
        msg = json.loads(raw)
    except Exception:
        await _reply_error(ws, "?", "invalid JSON from browser")
        return

    action = msg.get("action")
    form = msg.get("form") or {}

    try:
        if action == "register":
            ident = hub.identity()
            if ident is None:
                await _reply_error(ws, action, "radio identity unknown — waiting for MS state")
                return
            await hub.send_command(
                protocol.tnmm_registration,
                registration_type=hub.registration_type,
                issi=int(ident["issi"]),
                mcc_of_issi=int(ident["mcc"]),
                mnc_of_issi=int(ident["mnc"]),
            )
        elif action == "deregister":
            ident = hub.identity()
            if ident is None:
                await _reply_error(ws, action, "radio identity unknown — waiting for MS state")
                return
            await hub.send_command(
                protocol.tnmm_deregistration,
                issi=int(ident["issi"]),
                mcc=int(ident["mcc"]),
                mnc=int(ident["mnc"]),
            )
        elif action == "get_state":
            await hub.send_command(protocol.get_state)
        elif action == "get_config":
            await hub.send_command(protocol.get_config)
        elif action == "get_interface_version":
            await hub.send_command(protocol.get_interface_version)
        elif action == "set_config":
            toml = msg.get("toml", "")
            await hub.send_command(protocol.set_config, toml)
        elif action == "set_codeplug":
            base = hub.last_config_toml
            if not base:
                await _reply_error(ws, action, "no config loaded yet — read config first")
                return
            payload = msg.get("codeplug")
            if not isinstance(payload, dict):
                await _reply_error(ws, action, "codeplug payload required")
                return
            try:
                new_toml = codeplug_mod.merge_codeplug(base, payload)
            except Exception as exc:  # malformed payload / serialize error
                await _reply_error(ws, action, f"codeplug merge failed: {exc}")
                return
            await hub.send_command(protocol.set_config, new_toml)
        elif action == "apply_config":
            await hub.send_command(protocol.apply_config)
        elif action == "activate_scanlist":
            name = msg.get("name") or form.get("name")
            if not name:
                await _reply_error(ws, action, "scan list name required")
                return
            active = bool(msg.get("active"))
            await hub.send_command(protocol.activate_scanlist, str(name), active)
        elif action == "set_cell_selection_mode":
            await hub.send_command(protocol.set_cell_selection_mode, bool(msg.get("manual")))
        elif action == "start_cell_scan":
            await hub.send_command(protocol.start_cell_scan)
        elif action == "stop_cell_scan":
            await hub.send_command(protocol.stop_cell_scan)
        elif action == "camp_on_cell":
            carrier = msg.get("carrier_hz")
            if carrier is None:
                await _reply_error(ws, action, "carrier_hz required")
                return
            try:
                carrier = int(carrier)
            except (TypeError, ValueError):
                await _reply_error(ws, action, "carrier_hz must be an integer")
                return
            await hub.send_command(protocol.camp_on_cell, carrier, bool(msg.get("register")))
        elif action == "attach_detach_group":
            op = msg.get("op") or form.get("op")
            report = msg.get("report") or "ReportNotRequested"
            cou = msg.get("class_of_usage") or "ClassOfUsage1"
            if op in ("attach", "select"):
                gssis = _as_int_list(msg.get("gssis") or form.get("gssis"))
                if not gssis:
                    await _reply_error(ws, action, "no gssis provided")
                    return
                items = [protocol.group_identity_request_item(g, "Attachment", class_of_usage=cou)
                         for g in gssis]
                # "select" switches talkgroup: detach the active set, then attach.
                mode = "Amendment" if op == "attach" else "DetachTheCurrentlyActiveGroupIdentities"
            elif op == "detach":
                gssis = _as_int_list(msg.get("gssis") or form.get("gssis"))
                if not gssis:
                    await _reply_error(ws, action, "no gssis provided")
                    return
                items = [protocol.group_identity_request_item(
                            g, "Detachment", detachment_request="UserInitiatedDetachment")
                         for g in gssis]
                mode = "Amendment"
            elif op == "detach_all":
                # List must be non-empty even though this mode ignores its contents.
                items = [protocol.group_identity_request_item(
                            0, "Detachment", detachment_request="UserInitiatedDetachment")]
                mode = "DetachTheCurrentlyActiveGroupIdentities"
            else:
                await _reply_error(ws, action, "op must be one of attach|select|detach|detach_all")
                return
            await hub.send_command(protocol.tnmm_attach_detach_group,
                                   mode=mode, group_identity_request=items, report=report)
        elif action == "status":
            await hub.send_command(protocol.tnmm_status)
        elif action == "energy_saving":
            await hub.send_command(protocol.tnmm_energy_saving)
        elif action == "call_setup":
            ssi = _opt_int(msg.get("ssi") or form.get("ssi"))
            if not ssi:
                await _reply_error(ws, action, "called party SSI required")
                return
            group = bool(msg.get("group"))
            duplex = bool(msg.get("duplex"))
            await hub.send_command(
                protocol.tncc_setup,
                called_party_ssi=ssi,
                communication_type=("PointToMultipoint" if group else "PointToPoint"),
                simplex_duplex=("DuplexOperation" if duplex else "SimplexOperation"),
                request_tx=not duplex,
            )
        elif action == "call_ring":
            # On/off-hook incoming call: send TnccSetupResponse as the *ringing*
            # signal (U-ALERT) the moment the notification is shown.
            cid = _opt_int(msg.get("call_identifier"))
            if cid is None:
                await _reply_error(ws, action, "call_identifier required")
                return
            duplex = bool(msg.get("duplex"))
            await hub.send_command(
                protocol.tncc_setup_response, cid,
                simplex_duplex=("DuplexOperation" if duplex else "SimplexOperation"),
                hook="HookOnHookOffSignallingOrCallAcceptanceSignalling")
        elif action == "call_complete":
            # On/off-hook Accept: TnccComplete emits U-CONNECT and connects the call.
            cid = _opt_int(msg.get("call_identifier"))
            if cid is None:
                await _reply_error(ws, action, "call_identifier required")
                return
            duplex = bool(msg.get("duplex"))
            await hub.send_command(
                protocol.tncc_complete, cid,
                simplex_duplex=("DuplexOperation" if duplex else "SimplexOperation"))
        elif action == "call_answer":
            # Direct (no-hook) Accept: a single TnccSetupResponse connects the call.
            cid = _opt_int(msg.get("call_identifier"))
            if cid is None:
                await _reply_error(ws, action, "call_identifier required")
                return
            duplex = bool(msg.get("duplex"))
            await hub.send_command(
                protocol.tncc_setup_response, cid,
                simplex_duplex=("DuplexOperation" if duplex else "SimplexOperation"))
        elif action == "call_reject":
            cid = _opt_int(msg.get("call_identifier"))
            if cid is None:
                await _reply_error(ws, action, "call_identifier required")
                return
            await hub.send_command(protocol.tncc_release, cid,
                                   disconnect_cause="CallRejectedByTheCalledParty")
        elif action == "call_release":
            cid = _opt_int(msg.get("call_identifier"))
            if cid is None:
                await _reply_error(ws, action, "call_identifier required")
                return
            await hub.send_command(protocol.tncc_release, cid)
            hub.clear_call(cid)
        elif action == "ptt":
            cid = _opt_int(msg.get("call_identifier"))
            if cid is None:
                await _reply_error(ws, action, "call_identifier required")
                return
            pressed = bool(msg.get("pressed"))
            await hub.send_command(
                protocol.tncc_tx, cid,
                transmission_condition=("RequestToTransmit" if pressed else "TransmissionCeased"))
        elif action == "uplink_audio":
            # High-rate mic stream (~16 frames/s). Floor-gated + fire-and-forget
            # inside the hub; no reply so we never flood the browser channel.
            cid = _opt_int(msg.get("call_identifier"))
            if cid is not None:
                await hub.handle_uplink_audio(cid, msg.get("pcm"))
        else:
            await _reply_error(ws, str(action), "unknown action")
    except KeyError as exc:
        await _reply_error(ws, str(action), f"missing field: {exc}")
    except (ValueError, TypeError) as exc:
        await _reply_error(ws, str(action), f"invalid input: {exc}")
    except ConnectionError as exc:
        await _reply_error(ws, str(action), str(exc))


def _opt_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def _as_int_list(v: Any) -> list[int]:
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    out = []
    for item in v:
        if item is None or item == "":
            continue
        out.append(int(item))
    return out
