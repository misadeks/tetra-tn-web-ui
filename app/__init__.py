"""TNMM Demo UI — a Python server for the BlueStation MS external interface."""

__version__ = "1.0.0"

# Subprotocols the stack requests per channel (we must accept/echo these).
CONTROL_SUBPROTOCOL = "bluestation-control-v1"
TELEMETRY_SUBPROTOCOL = "bluestation-telemetry-v1"

# Interface schema this app implements.
INTERFACE_VERSION = "bluestation-ms-interface-2"
