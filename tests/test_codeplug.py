"""Codeplug TOML serializer / merge round-trip tests."""
import tomllib

from app import codeplug as C
from fake_stack import SAMPLE_TOML


def _merge(payload):
    out = C.merge_codeplug(SAMPLE_TOML, payload)
    return out, tomllib.loads(out)


def test_merge_preserves_operator_sections_and_secrets():
    out, doc = _merge({"talkgroups": [
        {"gssi": 101, "name": "Dispatch", "folder": "work", "class_of_usage": 0, "order": 1},
    ]})
    # untouched operator sections survive
    assert doc["phy_io"]["backend"] == "SoapySdr"
    assert doc["phy_io"]["soapysdr"]["rx_gain_lna"] == 48.0
    # secret sentinels are preserved verbatim
    assert doc["command"]["password"] == "********"
    assert doc["telemetry"]["password"] == "********"


def test_add_talkgroup_and_frequency_list():
    payload = {
        "talkgroups": [
            {"gssi": 101, "name": "Dispatch", "folder": "work", "class_of_usage": 0, "order": 1},
            {"gssi": 9001, "name": "New TG", "folder": "ops", "class_of_usage": 2, "order": 2},
        ],
        "frequency_lists": [
            {"name": "primary", "mode": "List", "frequencies": [439825000], "dwell_ms": 800},
            {"name": "second", "mode": "Range", "frequencies": [400000000, 410000000]},
        ],
    }
    _, doc = _merge(payload)
    assert [t["gssi"] for t in doc["talkgroup"]] == [101, 9001]
    assert doc["talkgroup"][1]["name"] == "New TG"
    assert len(doc["frequency_list"]) == 2
    assert doc["frequency_list"][1]["mode"] == "Range"
    # dwell_ms omitted when not provided
    assert "dwell_ms" not in doc["frequency_list"][1]


def test_merge_untouched_sections_only_replaces_present_keys():
    # Payload with only folders must not wipe talkgroups already present.
    _, doc = _merge({"folders": [{"id": "z", "name": "Zone", "order": 1}]})
    assert doc["folder"] == [{"id": "z", "name": "Zone", "order": 1}]
    assert len(doc["talkgroup"]) == 4  # unchanged from sample


def test_net_and_cell_and_attach_groups():
    _, doc = _merge({
        "mcc": 262, "mnc": 2,
        "cell_info": {"location_area": 7, "colour_code": 3},
        "attach_groups": [101, 220],
    })
    assert doc["net_info"] == {"mcc": 262, "mnc": 2}
    assert doc["cell_info"]["location_area"] == 7
    assert doc["cell_info"]["colour_code"] == 3
    assert doc["ms"]["attach_groups"] == [101, 220]


def test_dumps_types_round_trip():
    src = {"s": "x", "i": 1, "f": 2.5, "b": True, "arr": [1, 2],
           "t": {"k": "v"}, "rows": [{"a": 1}, {"a": 2}]}
    doc = tomllib.loads(C.dumps(src))
    assert doc == src
