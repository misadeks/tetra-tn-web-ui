"""Manual cell survey + register-to-cell tests (bluestation-ms-interface-3).

Covers the four Management command builders and the fake-stack simulation of the
survey (MsScanResult / MsScanComplete telemetry) and camp/register behaviour.
"""
import asyncio

import pytest

import fake_stack
from app import protocol


def test_survey_command_builders():
    assert protocol.set_cell_selection_mode(10, True) == {
        "Management": {"SetCellSelectionMode": {"handle": 10, "manual": True}}}
    assert protocol.set_cell_selection_mode(10, False) == {
        "Management": {"SetCellSelectionMode": {"handle": 10, "manual": False}}}
    assert protocol.start_cell_scan(11) == {
        "Management": {"StartCellScan": {"handle": 11}}}
    assert protocol.stop_cell_scan(12) == {
        "Management": {"StopCellScan": {"handle": 12}}}
    assert protocol.camp_on_cell(13, 430425000, True) == {
        "Management": {"CampOnCell": {
            "handle": 13, "carrier_hz": 430425000, "register": True}}}


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


async def _send(state, ws, inner):
    await fake_stack._handle_management(state, ws, inner)


def _drain(state):
    events = []
    while not state.telemetry_q.empty():
        events.append(state.telemetry_q.get_nowait())
    return events


@pytest.mark.asyncio
async def test_set_cell_selection_mode_toggles_state():
    state = fake_stack.FakeState()
    ws = _FakeWs()
    await _send(state, ws, {"SetCellSelectionMode": {"handle": 1, "manual": True}})
    assert state.selection_mode_manual is True
    assert ws.sent[-1]["Management"]["Ack"]["accepted"] is True
    assert state.ms_runtime_state()["selection_mode_manual"] is True

    await _send(state, ws, {"SetCellSelectionMode": {"handle": 2, "manual": False}})
    assert state.selection_mode_manual is False


@pytest.mark.asyncio
async def test_scan_emits_result_per_cell_then_complete():
    state = fake_stack.FakeState()
    ws = _FakeWs()
    await _send(state, ws, {"StartCellScan": {"handle": 3}})
    ack = ws.sent[-1]["Management"]["Ack"]
    assert ack["accepted"] is True

    # Let the survey run to completion (2 programmed carriers, ~0.4s dwell each).
    await asyncio.sleep(1.5)
    events = _drain(state)
    results = [protocol.variant_of(e) for e in events]
    names = [n for n, _ in results]
    assert names.count("MsScanResult") == 2
    assert names[-1] == "MsScanComplete"

    complete = [p for n, p in results if n == "MsScanComplete"][0]
    assert complete == {"found": 2, "scanned": 2}
    assert state.scan_in_progress is False

    # Every result carries the frozen schema fields; colour_code is always null.
    for n, p in results:
        if n == "MsScanResult":
            assert p["colour_code"] is None
            assert set(p) >= {"carrier_hz", "mcc", "mnc", "location_area",
                              "rssi_dbfs", "registration_required",
                              "late_entry_supported"}


@pytest.mark.asyncio
async def test_camp_unknown_carrier_rejected():
    state = fake_stack.FakeState()
    ws = _FakeWs()
    await _send(state, ws, {"CampOnCell": {
        "handle": 4, "carrier_hz": 123000000, "register": True}})
    ack = ws.sent[-1]["Management"]["Ack"]
    assert ack["accepted"] is False
    assert "123000000" in ack["message"]


@pytest.mark.asyncio
async def test_camp_and_register_registers_on_cell():
    state = fake_stack.FakeState()
    ws = _FakeWs()
    carrier = fake_stack._programmed_carriers(state)[0]
    await _send(state, ws, {"CampOnCell": {
        "handle": 5, "carrier_hz": carrier, "register": True}})
    assert ws.sent[-1]["Management"]["Ack"]["accepted"] is True

    await asyncio.sleep(0.9)
    assert state.registration_state == "Registered"
    assert state.service_status == "InService"
    events = _drain(state)
    names = [protocol.variant_of(e)[0] for e in events]
    assert "TnmmRegistrationConfirm" in names


@pytest.mark.asyncio
async def test_camp_monitor_only_does_not_register():
    state = fake_stack.FakeState()
    ws = _FakeWs()
    carrier = fake_stack._programmed_carriers(state)[0]
    await _send(state, ws, {"CampOnCell": {
        "handle": 6, "carrier_hz": carrier, "register": False}})
    assert ws.sent[-1]["Management"]["Ack"]["accepted"] is True

    await asyncio.sleep(0.9)
    assert state.registration_state != "Registered"
    assert state.service_status == "InServiceWaitingForRegistration"
