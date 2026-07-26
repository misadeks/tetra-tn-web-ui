"""Stack-facing WebSocket servers (control + telemetry).

Remember the counter-intuitive topology: **the stack is the WS client and this
app is the WS server.** The stack dials out to us on two ports. We send binary
UTF-8 JSON frames (the proven path) and accept **either** binary or text frames on
receive — the interface spec describes text frames while some builds use binary.
Keepalive is WS ping/pong (handled by the `websockets` library).
"""
from __future__ import annotations

import asyncio
import base64
import http
import logging
import ssl
from typing import Awaitable, Callable

import websockets
from websockets.server import WebSocketServerProtocol, serve

from . import CONTROL_SUBPROTOCOL, TELEMETRY_SUBPROTOCOL, protocol
from .config import ChannelConfig
from .hub import Hub

log = logging.getLogger("tnmm.stack")


def _basic_auth_check(credentials: tuple[str, str] | None):
    """Return a websockets `process_request` enforcing HTTP Basic auth.

    When no credentials are configured we accept every connection (demo mode).
    """
    if credentials is None:
        return None

    expected = "Basic " + base64.b64encode(
        f"{credentials[0]}:{credentials[1]}".encode("utf-8")
    ).decode("ascii")

    async def process_request(path, request_headers):
        provided = request_headers.get("Authorization")
        if provided != expected:
            body = b"Unauthorized"
            headers = [
                ("WWW-Authenticate", 'Basic realm="bluestation"'),
                ("Content-Length", str(len(body))),
                ("Content-Type", "text/plain"),
            ]
            return (http.HTTPStatus.UNAUTHORIZED, headers, body)
        return None

    return process_request


def _server_ssl(cfg: ChannelConfig) -> ssl.SSLContext | None:
    if not cfg.use_tls:
        return None
    if not cfg.ca_cert:
        raise ValueError("use_tls=true requires ca_cert (PEM with cert+key)")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cfg.ca_cert)
    return ctx


async def run_control_server(hub: Hub, cfg: ChannelConfig) -> None:
    async def handler(ws: WebSocketServerProtocol) -> None:
        if hub.control_ws is not None:
            log.warning("control: second connection from %s, replacing", ws.remote_address)
        log.info("control: stack connected (%s), subprotocol=%s",
                 ws.remote_address, ws.subprotocol)
        hub.set_control(ws)
        await hub.broadcast_status()
        try:
            # Learn schema version + initial state as soon as the stack connects.
            await _bootstrap(hub)
            async for message in ws:
                await _on_control_message(hub, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            if hub.control_ws is ws:
                hub.set_control(None)
                log.info("control: stack disconnected")
                await hub.broadcast_status()

    async with serve(
        handler, cfg.host, cfg.port,
        subprotocols=[CONTROL_SUBPROTOCOL],
        process_request=_basic_auth_check(cfg.credentials),
        ssl=_server_ssl(cfg),
        max_size=4 * 1024 * 1024,
    ):
        log.info("control server listening on %s:%s (%s)",
                 cfg.host, cfg.port, CONTROL_SUBPROTOCOL)
        await asyncio.Future()  # run forever


async def run_telemetry_server(hub: Hub, cfg: ChannelConfig) -> None:
    async def handler(ws: WebSocketServerProtocol) -> None:
        log.info("telemetry: stack connected (%s), subprotocol=%s",
                 ws.remote_address, ws.subprotocol)
        hub.set_telemetry(True)
        await hub.broadcast_status()
        try:
            async for message in ws:
                await _on_telemetry_message(hub, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            hub.set_telemetry(False)
            log.info("telemetry: stack disconnected")
            await hub.broadcast_status()

    async with serve(
        handler, cfg.host, cfg.port,
        subprotocols=[TELEMETRY_SUBPROTOCOL],
        process_request=_basic_auth_check(cfg.credentials),
        ssl=_server_ssl(cfg),
        max_size=4 * 1024 * 1024,
    ):
        log.info("telemetry server listening on %s:%s (%s)",
                 cfg.host, cfg.port, TELEMETRY_SUBPROTOCOL)
        await asyncio.Future()


async def _bootstrap(hub: Hub) -> None:
    try:
        await hub.send_command(protocol.get_interface_version, silent=True)
        await hub.send_command(protocol.get_state, silent=True)
        # Fetch the config too so the codeplug (talkgroup tree) is ready app-wide.
        await hub.send_command(protocol.get_config, silent=True)
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("bootstrap failed: %s", exc)


async def _on_control_message(hub: Hub, raw) -> None:
    try:
        message = protocol.decode(raw)
    except Exception:
        log.warning("control: undecodable frame, ignoring")
        if hub.wire_log is not None:
            hub.wire_log.record("in", "control", raw=raw)
        return
    if hub.wire_log is not None:
        hub.wire_log.record("in", "control", message)
    envelope = hub.handle_control_response(message)
    await hub.broadcast(envelope)
    # When we learn a fresh config, push the parsed codeplug (talkgroup tree).
    if envelope.get("variant") == "Management":
        _, payload = protocol.variant_of(message)
        inner_variant, _ = protocol.variant_of(payload) if isinstance(payload, dict) else ("", None)
        if inner_variant == "Config":
            await hub.broadcast({
                "type": "config",
                "toml": hub.last_config_toml,
                "codeplug": hub.codeplug(),
            })
    # Any state change should refresh the dashboard status line too.
    await hub.broadcast_status()


async def _on_telemetry_message(hub: Hub, raw) -> None:
    try:
        message = protocol.decode(raw)
    except Exception:
        log.warning("telemetry: undecodable frame, ignoring")
        if hub.wire_log is not None:
            hub.wire_log.record("in", "telemetry", raw=raw)
        return
    if hub.wire_log is not None:
        hub.wire_log.record("in", "telemetry", message)
    variant, payload = protocol.variant_of(message)
    # Downlink voice: decode ACELP -> PCM and ship a compact audio envelope
    # instead of broadcasting the raw 274-sample bit array (and don't pile these
    # high-rate frames into the event log).
    if variant == "MsSpeechFrame":
        audio = hub.handle_speech_frame(payload)
        if audio is not None:
            await hub.broadcast(audio)
        return
    envelope = hub.add_event(message)
    await hub.broadcast(envelope)
    # Some telemetry means the MS runtime state just changed (talkgroup set,
    # registration). Pull a fresh GetState right away so the dashboard reflects
    # it immediately instead of waiting up to `poll_interval` for the next tick.
    if envelope.get("variant") in _STATE_CHANGING_EVENTS:
        try:
            await hub.send_command(protocol.get_state, silent=True)
            # If we don't have the codeplug yet, grab it now that the MS state
            # changed (covers a connect-time fetch that raced the link coming up).
            if _needs_config(hub):
                await hub.send_command(protocol.get_config, silent=True)
        except Exception:  # pragma: no cover - best effort
            pass


# Telemetry variants after which the MsRuntimeState (attached groups,
# registration, service) is expected to have changed.
_STATE_CHANGING_EVENTS = frozenset({
    "TnmmAttachDetachGroupIdentityConfirm",
    "MsGroupAttach",
    "MsGroupDetach",
    "TnmmRegistrationConfirm",
    "TnmmRegistrationIndication",
    "TnmmDeregistrationConfirm",
    "MsRegistrationChange",
})


async def poll_state_loop(hub: Hub, interval: float) -> None:
    """Poll GetState every `interval` seconds while control is connected,
    and expire timed-out commands."""
    while True:
        await asyncio.sleep(interval)
        await hub.expire_pending()
        if hub.control_connected:
            try:
                await hub.send_command(protocol.get_state, silent=True)
                # If we still have no codeplug, keep asking. The MS serves its
                # config as soon as the control link is up (no registration
                # needed); this just covers a lost/raced connect-time fetch.
                if _needs_config(hub):
                    await hub.send_command(protocol.get_config, silent=True)
            except Exception:
                pass


def _needs_config(hub: Hub) -> bool:
    toml = hub.last_config_toml
    return not (toml and toml.strip())
