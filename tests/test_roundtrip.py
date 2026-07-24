"""End-to-end round trip over the real binary/JSON WebSocket wire path.

Spins the control + telemetry servers and the bundled fake stack in-process,
then drives a full register -> confirm -> deregister cycle through the hub
(exactly the path the browser dashboard uses).
"""
import asyncio

import pytest

import fake_stack
from app import protocol
from app.config import ChannelConfig
from app.hub import Hub
from app.stack_servers import run_control_server, run_telemetry_server

CONTROL_PORT = 19102
TELEMETRY_PORT = 19101


async def _wait_for(predicate, timeout=5.0, interval=0.05):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _has_event(hub, variant, match=None):
    for env in hub.events:
        name, payload = protocol.variant_of(env["event"])
        if name == variant and (match is None or match(payload)):
            return True
    return False


@pytest.fixture
async def running_system():
    hub = Hub()
    control_cfg = ChannelConfig(host="127.0.0.1", port=CONTROL_PORT)
    telemetry_cfg = ChannelConfig(host="127.0.0.1", port=TELEMETRY_PORT)

    tasks = [
        asyncio.create_task(run_control_server(hub, control_cfg)),
        asyncio.create_task(run_telemetry_server(hub, telemetry_cfg)),
    ]
    await asyncio.sleep(0.3)  # let servers bind
    tasks.append(asyncio.create_task(fake_stack.run(
        control_url=f"ws://127.0.0.1:{CONTROL_PORT}",
        telemetry_url=f"ws://127.0.0.1:{TELEMETRY_PORT}",
        credentials=None, chaos=False,
    )))

    connected = await _wait_for(lambda: hub.control_connected and hub.telemetry_connected)
    assert connected, "fake stack did not connect both channels"

    try:
        yield hub
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_bootstrap_learns_version_and_state(running_system):
    hub = running_system
    ok = await _wait_for(lambda: hub.interface_version is not None and hub.state is not None)
    assert ok
    assert hub.interface_version == "bluestation-ms-interface-2"
    assert hub.state["registration_state"] == "Idle"


async def test_register_confirm_deregister_roundtrip(running_system):
    hub = running_system

    # --- Register: acceptance ack on the control socket ---
    pc = await hub.send_command(
        protocol.tnmm_registration,
        registration_type="RegistrationToIndicatedCell",
        issi=1000001, mcc_of_issi=901, mnc_of_issi=9999,
    )
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    variant, payload = protocol.variant_of(ack)
    assert variant == "TnmmAck"
    assert payload["accepted"] is True

    # --- The real outcome arrives asynchronously on telemetry ---
    got_confirm = await _wait_for(lambda: _has_event(
        hub, "TnmmRegistrationConfirm", lambda p: p.get("registration_status") == "Success"))
    assert got_confirm, "no successful TnmmRegistrationConfirm on telemetry"

    assert _has_event(hub, "MsRegistration", lambda p: p.get("issi") == 1000001)
    assert _has_event(hub, "TnmmServiceIndication", lambda p: p.get("service_status") == "InService")

    # Poll state: fake stack should now report Registered.
    pc2 = await hub.send_command(protocol.get_state)
    await asyncio.wait_for(pc2.future, timeout=5.0)
    assert await _wait_for(lambda: hub.state and hub.state["registration_state"] == "Registered")

    # --- Deregister ---
    pc3 = await hub.send_command(protocol.tnmm_deregistration, issi=1000001, mcc=901, mnc=9999)
    ack3 = await asyncio.wait_for(pc3.future, timeout=5.0)
    assert protocol.variant_of(ack3)[0] == "TnmmAck"

    got_dereg = await _wait_for(lambda: _has_event(
        hub, "MsDeregistration", lambda p: p.get("issi") == 1000001))
    assert got_dereg, "no MsDeregistration on telemetry"

    pc4 = await hub.send_command(protocol.get_state)
    await asyncio.wait_for(pc4.future, timeout=5.0)
    assert await _wait_for(lambda: hub.state and hub.state["registration_state"] == "Idle")


async def test_dormant_primitive_is_rejected(running_system):
    hub = running_system
    pc = await hub.send_command(protocol.tnmm_status)
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    _, payload = protocol.variant_of(ack)
    assert payload["accepted"] is False
    assert payload["detail"] == "dormant"


async def test_group_attach_detach_roundtrip(running_system):
    hub = running_system

    # Attach before registration is rejected with the real detail string.
    pc = await hub.send_command(
        protocol.tnmm_attach_detach_group, mode="Amendment",
        group_identity_request=[protocol.group_identity_request_item(
            4242, "Attachment", class_of_usage="ClassOfUsage1")])
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    _, payload = protocol.variant_of(ack)
    assert payload["accepted"] is False
    assert payload["detail"] == "not registered; cannot attach/detach groups"

    # Register first.
    pc = await hub.send_command(
        protocol.tnmm_registration,
        registration_type="RegistrationToIndicatedCell",
        issi=1000001, mcc_of_issi=901, mnc_of_issi=9999,
    )
    await asyncio.wait_for(pc.future, timeout=5.0)
    await _wait_for(lambda: _has_event(
        hub, "TnmmRegistrationConfirm", lambda p: p.get("registration_status") == "Success"))

    async def _poll_state():
        pc2 = await hub.send_command(protocol.get_state)
        await asyncio.wait_for(pc2.future, timeout=5.0)

    await _poll_state()
    assert await _wait_for(lambda: hub.state and hub.state.get("registration_state") == "Registered")

    # Attach a new group -> accepted, Confirm on telemetry, state updated.
    pc = await hub.send_command(
        protocol.tnmm_attach_detach_group, mode="Amendment",
        group_identity_request=[protocol.group_identity_request_item(
            4242, "Attachment", class_of_usage="ClassOfUsage1")])
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    assert protocol.variant_of(ack)[1]["accepted"] is True

    def _confirm_has(gtsi, type_id):
        def match(p):
            for g in (p.get("group_identities") or []):
                if g.get("gtsi") == gtsi and \
                   g.get("group_identity_attach_detach_type_identifier") == type_id:
                    return True
            return False
        return match

    assert await _wait_for(lambda: _has_event(
        hub, "TnmmAttachDetachGroupIdentityConfirm", _confirm_has(4242, "Attachment")))

    await _poll_state()
    assert await _wait_for(lambda: hub.state and 4242 in hub.state.get("attached_groups", []))

    # Detach it again -> Confirm on telemetry, state updated.
    pc = await hub.send_command(
        protocol.tnmm_attach_detach_group, mode="Amendment",
        group_identity_request=[protocol.group_identity_request_item(
            4242, "Detachment", detachment_request="UserInitiatedDetachment")])
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    assert protocol.variant_of(ack)[1]["accepted"] is True

    assert await _wait_for(lambda: _has_event(
        hub, "TnmmAttachDetachGroupIdentityConfirm", _confirm_has(4242, "Detachment")))

    await _poll_state()
    assert await _wait_for(lambda: hub.state and 4242 not in hub.state.get("attached_groups", []))


async def test_management_config_roundtrip(running_system):
    hub = running_system
    pc = await hub.send_command(protocol.get_config)
    resp = await asyncio.wait_for(pc.future, timeout=5.0)
    _, mgmt = protocol.variant_of(resp)
    inner_variant, inner = protocol.variant_of(mgmt)
    assert inner_variant == "Config"
    assert "config_version" in inner["toml"]
    assert "********" in inner["toml"]  # secret redacted

    pc2 = await hub.send_command(protocol.set_config, inner["toml"])
    ack = await asyncio.wait_for(pc2.future, timeout=5.0)
    _, mgmt2 = protocol.variant_of(ack)
    assert protocol.variant_of(mgmt2)[0] == "Ack"
    assert protocol.variant_of(mgmt2)[1]["restart_required"] is True


async def test_codeplug_parsed_from_config(running_system):
    hub = running_system
    pc = await hub.send_command(protocol.get_config)
    await asyncio.wait_for(pc.future, timeout=5.0)

    cp = hub.codeplug()
    assert cp is not None
    assert cp["config_version"] == "0.7"
    names = {t["gssi"]: t["name"] for t in cp["talkgroups"]}
    assert names.get(101) == "Dispatch"
    assert names.get(300) == "Emergency"
    # class_of_usage is the on-air value; the UI maps it to TNMM ClassOfUsage(N+1).
    cou = {t["gssi"]: t["class_of_usage"] for t in cp["talkgroups"]}
    assert cou[300] == 3
    assert protocol.class_of_usage_for(cou[300]) == "ClassOfUsage4"
    assert cp["attach_groups"] == [101]
    assert {f["id"] for f in cp["folders"]} == {"work", "ops"}
    # Scan lists (interface-2): named GSSI sets with a programmed-default active flag.
    sls = {s["name"]: s for s in cp["scanlists"]}
    assert sls["Alpha"]["talkgroups"] == [101, 102]
    assert sls["Alpha"]["active"] is True
    assert sls["Ops"]["active"] is False


async def test_activate_scanlist_roundtrip(running_system):
    """ActivateScanlist toggles a scan list and drives on-air affiliation."""
    hub = running_system

    # Register: base [101] plus default-active "Alpha" [101,102] -> [101,102].
    pc = await hub.send_command(
        protocol.tnmm_registration,
        registration_type="RegistrationToIndicatedCell",
        issi=1000001, mcc_of_issi=901, mnc_of_issi=9999,
    )
    await asyncio.wait_for(pc.future, timeout=5.0)
    await _wait_for(lambda: _has_event(
        hub, "TnmmRegistrationConfirm", lambda p: p.get("registration_status") == "Success"))

    async def _poll_state():
        pc2 = await hub.send_command(protocol.get_state)
        await asyncio.wait_for(pc2.future, timeout=5.0)

    await _poll_state()
    assert await _wait_for(lambda: hub.state and hub.state.get("active_scanlists") == ["Alpha"])
    assert await _wait_for(lambda: hub.state and hub.state.get("attached_groups") == [101, 102])

    # Activate "Ops" [220, 300] -> affiliation becomes [101,102,220,300].
    pc = await hub.send_command(protocol.activate_scanlist, "Ops", True)
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    _, mgmt = protocol.variant_of(ack)
    assert protocol.variant_of(mgmt)[0] == "Ack"
    assert protocol.variant_of(mgmt)[1]["accepted"] is True

    await _poll_state()
    assert await _wait_for(
        lambda: hub.state and hub.state.get("attached_groups") == [101, 102, 220, 300])
    assert await _wait_for(
        lambda: hub.state and sorted(hub.state.get("active_scanlists", [])) == ["Alpha", "Ops"])

    # Deactivate "Alpha" -> its GSSIs drop unless still covered; 101 stays (base
    # attach group), 102 leaves; 220/300 remain (from "Ops").
    pc = await hub.send_command(protocol.activate_scanlist, "Alpha", False)
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    assert protocol.variant_of(protocol.variant_of(ack)[1])[1]["accepted"] is True

    await _poll_state()
    assert await _wait_for(
        lambda: hub.state and hub.state.get("attached_groups") == [101, 220, 300])
    assert await _wait_for(
        lambda: hub.state and hub.state.get("active_scanlists") == ["Ops"])


async def test_group_switch_roundtrip(running_system):
    """DetachTheCurrentlyActiveGroupIdentities + Attachment switches talkgroup."""
    hub = running_system

    # Register (seeds attached_groups from [ms] attach_groups = [101]).
    pc = await hub.send_command(
        protocol.tnmm_registration,
        registration_type="RegistrationToIndicatedCell",
        issi=1000001, mcc_of_issi=901, mnc_of_issi=9999,
    )
    await asyncio.wait_for(pc.future, timeout=5.0)
    await _wait_for(lambda: _has_event(
        hub, "TnmmRegistrationConfirm", lambda p: p.get("registration_status") == "Success"))

    async def _poll_state():
        pc2 = await hub.send_command(protocol.get_state)
        await asyncio.wait_for(pc2.future, timeout=5.0)

    await _poll_state()
    # Registration seeds base attach_groups (101) plus the default-active scan
    # list "Alpha" ([101, 102]).
    assert await _wait_for(lambda: hub.state and hub.state.get("attached_groups") == [101, 102])

    # Switch to GSSI 300 -> the active set is detached and 300 attached.
    pc = await hub.send_command(
        protocol.tnmm_attach_detach_group,
        mode="DetachTheCurrentlyActiveGroupIdentities",
        group_identity_request=[protocol.group_identity_request_item(
            300, "Attachment", class_of_usage=protocol.class_of_usage_for(3))])
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    assert protocol.variant_of(ack)[1]["accepted"] is True

    await _wait_for(lambda: _has_event(hub, "TnmmAttachDetachGroupIdentityConfirm"))
    await _poll_state()
    assert await _wait_for(lambda: hub.state and hub.state.get("attached_groups") == [300])


async def test_call_control_roundtrip(running_system):
    """MO individual call: setup -> active -> PTT floor grant -> release."""
    hub = running_system

    # Must be registered before placing a call.
    pc = await hub.send_command(
        protocol.tnmm_registration,
        registration_type="RegistrationToIndicatedCell",
        issi=1000001, mcc_of_issi=901, mnc_of_issi=9999,
    )
    await asyncio.wait_for(pc.future, timeout=5.0)
    await _wait_for(lambda: _has_event(
        hub, "TnmmRegistrationConfirm", lambda p: p.get("registration_status") == "Success"))

    # Originate an individual (PointToPoint) simplex call, requesting the floor.
    pc = await hub.send_command(
        protocol.tncc_setup, called_party_ssi=4001,
        communication_type="PointToPoint", simplex_duplex="SimplexOperation",
        request_tx=True)
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    variant, payload = protocol.variant_of(ack)
    assert variant == "TnccAck"
    assert payload["accepted"] is True

    # Call progresses to active via telemetry (Proceed -> Alert -> SetupConfirm).
    assert await _wait_for(lambda: _has_event(hub, "TnccSetupConfirm")), \
        "no TnccSetupConfirm on telemetry"
    assert await _wait_for(lambda: len(hub.calls) == 1), "hub did not track the call"
    cid = next(iter(hub.calls))
    assert hub.calls[cid]["state"] == "active"

    # PTT: request the floor -> granted.
    pc = await hub.send_command(
        protocol.tncc_tx, cid, transmission_condition="RequestToTransmit")
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    assert protocol.variant_of(ack)[1]["accepted"] is True
    assert await _wait_for(lambda: _has_event(
        hub, "TnccTxIndication",
        lambda p: (p.get("indication") or {}).get("transmission_status") == "TransmissionGranted"))

    # Release the call -> ReleaseConfirm and the call is dropped from hub state.
    pc = await hub.send_command(protocol.tncc_release, cid)
    ack = await asyncio.wait_for(pc.future, timeout=5.0)
    assert protocol.variant_of(ack)[1]["accepted"] is True
    assert await _wait_for(lambda: _has_event(hub, "TnccReleaseConfirm"))
    assert await _wait_for(lambda: cid not in hub.calls), "released call still tracked"


class _CaptureClient:
    """Fake browser WS that records every envelope the hub broadcasts."""

    def __init__(self):
        self.msgs = []

    async def send(self, data):
        import json as _json
        self.msgs.append(_json.loads(data))


@pytest.mark.skipif(
    not __import__("app.acelp", fromlist=["encoder_available"]).encoder_available(),
    reason="native ACELP encoder unavailable (needs clang)",
)
async def test_uplink_speech_loopback(running_system):
    """Uplink voice E2E: browser mic frame -> hub encode -> control -> fake-stack
    loopback -> telemetry -> hub decode -> audio broadcast back to the browser.

    Proves the whole U-plane TX chain and the floor gate over the real wire."""
    import base64
    import math
    import struct

    hub = running_system

    # Register + originate a simplex individual call, requesting the floor.
    pc = await hub.send_command(
        protocol.tnmm_registration, registration_type="RegistrationToIndicatedCell",
        issi=1000001, mcc_of_issi=901, mnc_of_issi=9999)
    await asyncio.wait_for(pc.future, timeout=5.0)
    await _wait_for(lambda: _has_event(
        hub, "TnmmRegistrationConfirm", lambda p: p.get("registration_status") == "Success"))

    pc = await hub.send_command(
        protocol.tncc_setup, called_party_ssi=4001,
        communication_type="PointToPoint", simplex_duplex="SimplexOperation",
        request_tx=True)
    await asyncio.wait_for(pc.future, timeout=5.0)
    assert await _wait_for(lambda: len(hub.calls) == 1)
    cid = next(iter(hub.calls))

    # Take the floor -> the fake stack grants it and the hub records holds_floor.
    pc = await hub.send_command(protocol.tncc_tx, cid, transmission_condition="RequestToTransmit")
    await asyncio.wait_for(pc.future, timeout=5.0)
    assert await _wait_for(lambda: hub.calls.get(cid, {}).get("holds_floor") is True), \
        "hub never recorded the floor grant"

    # Capture broadcasts, then stream a few 60 ms mic frames of a 300 Hz tone.
    cap = _CaptureClient()
    hub.browser_clients.add(cap)

    def _tone_b64(phase):
        pcm = [int(8000 * math.sin(2 * math.pi * 300 * (phase + i) / 8000)) for i in range(480)]
        return base64.b64encode(struct.pack("<480h", *pcm)).decode("ascii")

    sent = 0
    for fr in range(6):
        if await hub.handle_uplink_audio(cid, _tone_b64(fr * 480)):
            sent += 1
        await asyncio.sleep(0.02)
    assert sent == 6, f"floor-held frames should all transmit, got {sent}"

    # The loopback should come back as decoded audio envelopes for this call.
    def _got_audio():
        return any(m.get("type") == "audio" and m.get("call_identifier") == cid and m.get("pcm")
                   for m in cap.msgs)
    assert await _wait_for(_got_audio, timeout=5.0), "no decoded uplink loopback audio"

    # Drop the floor -> further mic frames are gated off (nothing transmitted).
    hub.calls[cid]["holds_floor"] = False
    assert await hub.handle_uplink_audio(cid, _tone_b64(9999)) is False
