"""Structured codeplug editing.

The MS exchanges its whole configuration as a single TOML document
(``config_version "0.7"``) via GetConfig / SetConfig. The browser codeplug
editor only touches the "radio programming" sections — folders, talkgroups,
scan lists, frequency lists, networks and the network/cell identity — but the
document also carries operator-only sections (``phy_io``, ``telemetry`` /
``command`` auth with ``********`` secret sentinels, ...).

Python's stdlib can *read* TOML (``tomllib``) but cannot write it, so this
module ships a small serializer that is good enough for this document shape:
scalars, inline scalar arrays, nested tables and arrays-of-tables. We parse the
current document, overwrite only the editor-managed keys from a JSON payload and
re-serialize the *whole* document — every untouched section (including secret
sentinels) is preserved verbatim as data.
"""

from __future__ import annotations

import json
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


# ---------------------------------------------------------------------------
# serializer
# ---------------------------------------------------------------------------

def _fmt_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        # TOML basic strings share JSON's escaping rules for the cases we emit.
        return json.dumps(v)
    raise TypeError(f"unsupported scalar type: {type(v)!r}")


def _fmt_array(items: list) -> str:
    return "[" + ", ".join(_fmt_scalar(x) for x in items) + "]"


def _is_table_array(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(x, dict) for x in v)


def _emit(table: dict, path: str, lines: list[str]) -> None:
    scalars: list[tuple[str, Any]] = []
    subtables: list[tuple[str, dict]] = []
    arrays: list[tuple[str, list]] = []
    for k, v in table.items():
        if isinstance(v, dict):
            subtables.append((k, v))
        elif _is_table_array(v):
            arrays.append((k, v))
        else:
            scalars.append((k, v))

    # Scalars and inline arrays must precede any [table]/[[array]] header.
    for k, v in scalars:
        if isinstance(v, list):
            lines.append(f"{k} = {_fmt_array(v)}")
        else:
            lines.append(f"{k} = {_fmt_scalar(v)}")

    for k, v in subtables:
        header = k if not path else f"{path}.{k}"
        lines.append("")
        lines.append(f"[{header}]")
        _emit(v, header, lines)

    for k, rows in arrays:
        header = k if not path else f"{path}.{k}"
        for row in rows:
            lines.append("")
            lines.append(f"[[{header}]]")
            _emit(row, header, lines)


def dumps(doc: dict) -> str:
    """Serialize a (JSON-like) dict into a TOML document string."""
    lines: list[str] = []
    _emit(doc, "", lines)
    return "\n".join(lines).lstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def _as_int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def merge_codeplug(base_toml: str, payload: dict) -> str:
    """Overlay editor sections from ``payload`` onto ``base_toml`` → new TOML.

    ``payload`` mirrors the shape produced by :meth:`hub.Hub.codeplug`. Only the
    keys present in the payload are replaced; everything else in the base
    document (operator sections, secrets) is preserved.
    """
    doc: dict = tomllib.loads(base_toml) if base_toml else {}

    if "folders" in payload:
        doc["folder"] = [
            {"id": str(f.get("id", "")),
             "name": str(f.get("name", f.get("id", ""))),
             "order": _as_int(f.get("order"), 0)}
            for f in payload.get("folders") or []
        ]

    if "talkgroups" in payload:
        rows = []
        for t in payload.get("talkgroups") or []:
            gssi = _as_int(t.get("gssi"))
            if gssi is None:
                continue
            row: dict[str, Any] = {"gssi": gssi, "name": str(t.get("name", gssi))}
            folder = t.get("folder")
            if folder not in (None, ""):
                row["folder"] = str(folder)
            cou = _as_int(t.get("class_of_usage"))
            if cou is not None:
                row["class_of_usage"] = cou
            row["order"] = _as_int(t.get("order"), 0)
            rows.append(row)
        doc["talkgroup"] = rows

    if "scanlists" in payload:
        doc["scanlist"] = [
            {"name": str(sl.get("name", "")),
             "talkgroups": [g for g in (_as_int(x) for x in sl.get("talkgroups", [])) if g is not None],
             "active": bool(sl.get("active", False)),
             "order": _as_int(sl.get("order"), 0)}
            for sl in payload.get("scanlists") or [] if sl.get("name")
        ]

    if "frequency_lists" in payload:
        rows = []
        for fl in payload.get("frequency_lists") or []:
            if not fl.get("name"):
                continue
            row = {"name": str(fl.get("name")),
                   "mode": str(fl.get("mode", "List")),
                   "frequencies": [g for g in (_as_int(x) for x in fl.get("frequencies", [])) if g is not None]}
            dwell = _as_int(fl.get("dwell_ms"))
            if dwell is not None:
                row["dwell_ms"] = dwell
            rows.append(row)
        doc["frequency_list"] = rows

    if "networks" in payload:
        rows = []
        for n in payload.get("networks") or []:
            mcc, mnc = _as_int(n.get("mcc")), _as_int(n.get("mnc"))
            if mcc is None or mnc is None:
                continue
            row = {"mcc": mcc, "mnc": mnc}
            if n.get("name") not in (None, ""):
                row["name"] = str(n.get("name"))
            row["priority"] = _as_int(n.get("priority"), 0)
            rows.append(row)
        doc["network"] = rows

    if payload.get("mcc") is not None or payload.get("mnc") is not None:
        net = doc.setdefault("net_info", {})
        if payload.get("mcc") is not None:
            net["mcc"] = _as_int(payload.get("mcc"))
        if payload.get("mnc") is not None:
            net["mnc"] = _as_int(payload.get("mnc"))

    if "cell_info" in payload and isinstance(payload["cell_info"], dict):
        cell = doc.setdefault("cell_info", {})
        for k in ("location_area", "colour_code"):
            if payload["cell_info"].get(k) is not None:
                cell[k] = _as_int(payload["cell_info"][k])

    if "attach_groups" in payload:
        ms = doc.setdefault("ms", {})
        ms["attach_groups"] = [g for g in (_as_int(x) for x in payload.get("attach_groups", [])) if g is not None]

    return dumps(doc)
