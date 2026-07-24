# PROTOCOL.md — BlueStation MS external interface (`bluestation-ms-interface-1`)

Reference for the wire protocol this app implements. The app is the **server**;
the stack is the **client** that dials out to it.

## 1. Connection model

| Channel   | App listens on            | Subprotocol the stack requests | Traffic                                    |
|-----------|---------------------------|--------------------------------|--------------------------------------------|
| Control   | `command.host:command.port` (e.g. `0.0.0.0:9102`) | `bluestation-control-v1`   | UI→stack commands; stack→UI responses (same socket) |
| Telemetry | `telemetry.host:telemetry.port` (e.g. `0.0.0.0:9101`) | `bluestation-telemetry-v1` | stack→UI events only (receive-only)        |

### Wire invariants (do not deviate)

1. **Binary frames.** Messages are JSON encoded as UTF-8 inside **binary**
   WebSocket frames — never text frames. Send `json.dumps(obj).encode("utf-8")`
   as binary; decode incoming binary frames as UTF-8 JSON. (A text frame is
   logged as "unexpected" and ignored.)
2. **Externally-tagged enums.** `{"VariantName": { ...fields... }}`. Enum
   *values* serialize as their exact Rust identifier strings (e.g.
   `"Registered"`, `"InService"`, `"CaCell"`).
3. **Optional fields may be omitted** (default absent/null). Send only what you
   need.
4. **`handle` (u32)** is a caller-chosen correlation id echoed back in the
   matching response. Allocate an incrementing handle per command.
5. **Keepalive is WS Ping/Pong.** Use a library that auto-responds to pings
   (Python `websockets` does). If no traffic flows within the heartbeat timeout
   the stack disconnects and reconnects — servers must tolerate reconnects.
6. **HTTP Basic auth.** If credentials are configured, the stack adds
   `Authorization: Basic base64(user:pass)` on the upgrade. Validate it (or
   accept all in demo mode).
7. The handshake carries `User-Agent: BlueStation/<version>` and
   `Sec-WebSocket-Protocol: <subprotocol>`. The server must **accept/echo** the
   subprotocol in the handshake response.

## 2. Control channel — UI → stack (`ControlCommand`)

Plane A (TNMM, top-level variants):

```json
{"TnmmRegistration":{"handle":6,"request":{
  "registration_type":"RegistrationToIndicatedCell",
  "issi":1000001,"mcc_of_issi":901,"mnc_of_issi":9999
}}}
{"TnmmDeregistration":{"handle":7,"request":{"issi":1000001,"mcc":901,"mnc":9999}}}
{"TnmmAttachDetachGroupIdentity":{"handle":8,"request":{
  "group_identity_attach_detach_mode":"Amendment",
  "group_identity_request":[
    {"gtsi":91,"group_identity_attach_detach_type_identifier":"Attachment",
     "class_of_usage":"ClassOfUsage1","group_identity_detachment_request":null}],
  "group_identity_report":"ReportNotRequested"}}}
{"TnmmStatus":{"handle":9,"request":{ ... }}}                       // dormant
{"TnmmEnergySaving":{"handle":10,"request":{ ... }}}               // dormant
```

Plane B (management, wrapped in `Management`):

```json
{"Management":{"GetState":{"handle":1}}}
{"Management":{"GetInterfaceVersion":{"handle":2}}}
{"Management":{"GetConfig":{"handle":3}}}
{"Management":{"SetConfig":{"handle":4,"toml":"<full TOML document as a string>"}}}
{"Management":{"ApplyConfig":{"handle":5}}}
```

### `TnmmRegistrationRequest`

Mandatory: `registration_type` (`"PeriodicRegistration"` |
`"RegistrationToIndicatedCell"`), `issi` (u32), `mcc_of_issi` (u16),
`mnc_of_issi` (u16).

Optional (omit for a basic demo): `required_cell_type_list` /
`preferred_cell_type_list` (`["CaCell"|"DaCell", …]`), `preferred_la_list` /
`preferred_mcc_list` / `preferred_mnc_list` (`[u16]`), `energy_economy_mode`,
`group_identity_request`, `group_identity_attach_detach_mode`.

### `TnmmDeregistrationRequest`

All optional: `issi` (u32), `mcc` (u16), `mnc` (u16).

### `TnmmAttachDetachGroupIdentityRequest` (cl. 15.3.3.1)

- `group_identity_attach_detach_mode` (**M**): `"Amendment"` (add/remove the
  listed groups) | `"DetachTheCurrentlyActiveGroupIdentities"` (detach all; the
  list is ignored but must still be non-empty).
- `group_identity_request` (**M**, non-empty array). Each entry:
  - `gtsi` (u64): `GTSI = MNI<<24 | GSSI`; the stack uses the low 24 bits as the
    GSSI (address type 0), so a plain GSSI (e.g. `91`) works.
  - `group_identity_attach_detach_type_identifier` (**M**): `"Attachment"` |
    `"Detachment"`.
  - `class_of_usage` (`"ClassOfUsage1"`…`"ClassOfUsage8"` | null): only for
    `Attachment`; null → stack default.
  - `group_identity_detachment_request` (`"UserInitiatedDetachment"` | null):
    only for `Detachment`; null → user-initiated.
- `group_identity_report` (**O**): `"ReportRequested"` | `"ReportNotRequested"` |
  null. The MS forces "not report" on air regardless.

`TnmmAck` is only the **acceptance** ack (rejects if not registered, if a group
op is already in flight, or if the list is empty). The real outcome arrives on
telemetry as `TnmmAttachDetachGroupIdentityConfirm`; `attached_groups` in
`MsRuntimeState` (polled via `GetState`) reflects the result (sorted). To
**switch** talkgroup, send mode `"DetachTheCurrentlyActiveGroupIdentities"` with a
single `Attachment` entry — the active set is detached and the new group attached
in one PDU. To **add a scanning group** use `"Amendment"` + `Attachment`; to
**drop** one use `"Amendment"` + `Detachment`. The UI tracks the selected (TX)
talkgroup client-side; the remaining attached entries are scanned.

## 3. Control channel — stack → UI (`ControlResponse`)

```json
{"TnmmAck":{"handle":6,"accepted":true,"detail":null}}
{"Management":{"State":{"handle":1,"state":{ ...MsRuntimeState... }}}}
{"Management":{"InterfaceVersion":{"handle":2,"version":"bluestation-ms-interface-1"}}}
{"Management":{"Config":{"handle":3,"toml":"config_version = \"0.7\"\n..."}}}
{"Management":{"Ack":{"handle":4,"accepted":true,"restart_required":true,"message":"staged"}}}
{"Management":{"Error":{"handle":3,"message":"..."}}}
```

`TnmmAck` is only the **acceptance** ack; the real TNMM outcome arrives
asynchronously on the **telemetry** channel.

### `MsRuntimeState`

```
registration_state : "Idle" | "Registering" | "Registered" | "Detaching"
service_status     : "InService" | "InGracefulServiceDegradationMode" |
                     "InServiceWaitingForRegistration" | "OutOfService" | "MmBusy" | "MmIdle"
own_issi           : u32
home_mcc           : u16
home_mnc           : u16
serving_la         : u16
rssi_dbfs          : f32 | null   // serving-cell DL level (uncalibrated dBFS); null before first measurement / OOS
colour_code        : u8
attached_groups    : [u32]        // LIVE attached GSSIs, sorted
restart_required   : bool
```

### Codeplug (talkgroup tree, config `0.7`)

`GetConfig` returns the full MS config TOML (`config_version = "0.7"`). Its
`[[folder]]` / `[[talkgroup]]` / `[[network]]` tables are the "radio programming"
that drives the talkgroup tree. The app parses this into a codeplug and pushes it
to the browser (`{"type":"config","toml":...,"codeplug":{folders,talkgroups,...}}`).
Each talkgroup's `class_of_usage` is the **on-air** value (0..7); the UI maps it to
TNMM `ClassOfUsage(N+1)` when attaching (`ClassOfUsage1` == on-air 0).

## 4. Telemetry channel — stack → UI (`TelemetryEvent`, receive-only)

```json
{"MsRegistration":{"issi":1000001}}
{"MsDeregistration":{"issi":1000001}}
{"MsGroupAttach":{"issi":1000001,"gssis":[100,200]}}
{"MsGroupDetach":{"issi":1000001,"gssis":[100]}}
{"TnmmAttachDetachGroupIdentityConfirm":{
  "group_identity_attach_detach_mode":"Amendment","group_identity_report":"ReportNotRequested",
  "group_identities":[{"gtsi":91,"group_identity_attach_detach_type_identifier":"Attachment",
    "group_identity_lifetime":"PermanentAttachmentNotNeeded","class_of_usage":"ClassOfUsage1",
    "group_identity_detachment_reason":null}]}}
{"TnmmRegistrationIndication": { ... }}
{"TnmmRegistrationConfirm":    { ... }}
{"TnmmServiceIndication":{"service_status":"InService","disable_status":"Enabled"}}
```

Payload style differs by variant: `MsRegistration` etc. are **struct variants**
(`{"issi":…}`); the TNMM ones are **newtype variants** wrapping one object.

### `TnmmRegistrationIndication` / `TnmmRegistrationConfirm`

```
registration_status         : "Success" | "Failure" | "LaRegistrationExpired" |
                              "NoPreferredCellFound" | "NoPermittedCellTypes"
registration_reject_cause   : null | one of {ItsiUnknown, IllegalMs, LaNotAllowed, LaUnknown,
                              NetworkFailure, Congestion, ForwardRegistrationFailure,
                              ServiceNotSubscribed, MandatoryElementError, MessageConsistencyError,
                              RoamingNotSupported, MigrationNotSupported, NoCipherKsg,
                              IdentifiedCipherKsgNotSupported, RequestedCipherKeyTypeNotAvailable,
                              IdentifiedCipherKeyNotAvailable, CipheringRequired, AuthenticationFailure}
cell_type_where_registered  : "CaCell" | "DaCell"
la_where_registered         : u16
mcc_where_registered        : u16
mnc_where_registered        : u16
(optional: swmis_required_cell_types, energy_economy_mode, energy_economy_mode_status,
 group_identities, group_identity_attach_detach_mode)
```

`TnmmServiceIndication`: `service_status` (see enum above), `disable_status`
(`"Enabled" | "TemporaryDisabled" | "PermanentlyDisabled"`).

`TnmmAttachDetachGroupIdentityConfirm`: echoes
`group_identity_attach_detach_mode` / `group_identity_report` and carries
`group_identities` — what the SwMI actually (de)activated. On attach each entry
has `group_identity_lifetime` + `class_of_usage`; on detach it has
`group_identity_detachment_reason` (`"PermanentlyDetached"` |
`"Temporary1Detached"` | `"Temporary2Detached"` | `"UnknownGroupIdentity"`). An
empty array means the operation failed or timed out (T353).

Treat any other `TelemetryEvent` variant gracefully (log + display raw JSON) —
the stack may emit more later.

## 5. Secrets

`GetConfig` returns TOML with secrets already redacted to `"********"`. When
sending `SetConfig`, leave any `"********"` untouched (the stack preserves the
on-disk secret); only changed non-sentinel values are written.

## 6. Guardrails

- Do **not** invent message variants, fields, or enum values beyond the above.
  Unknown inbound variants: log + show raw, never crash.
- Keep Plane A (TNMM) and Plane B (Management) visually and structurally
  separate; Plane B is non-standard / implementation-defined.
- The three most common mistakes: forgetting **binary frames**, forgetting
  **externally-tagged JSON**, and getting the **stack-is-the-client** role
  backwards. Verify those first.
