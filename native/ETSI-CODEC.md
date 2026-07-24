# Adding the TETRA ACELP codec (ETSI EN 300 395-2)

Two-way voice (downlink decode + uplink encode) is driven by the **ETSI
EN 300 395-2** reference C speech codec. That source is **copyrighted by ETSI**
and is **not redistributed in this repository**, so you must add it yourself
before the native library can be built.

> ⚠️ **Read the "Re-entrancy fork" section below.** This project does *not* use
> the pristine ETSI files unchanged — five of them are patched to be re-entrant.
> Dropping in unmodified ETSI sources will compile but is **not** safe for
> per-call / concurrent decoding and encoding.

---

## 1. What this repo already provides

These files are ours (Apache-2.0) and are committed — do **not** replace them:

| File | Role |
|------|------|
| `native/acelp_decode.c` | Thin, BFI-aware decoder wrapper (stable ABI called from `app/acelp.py`). |
| `native/acelp_encode.c` | Encoder wrapper for the uplink/TX path. |
| `app/acelp.py`          | ctypes binding + on-demand `clang` build glue. |

The re-entrancy shim header `native/etsi/acelp_state_bridge.h` is also ours, but
it lives under the git-ignored `native/etsi/` tree (see §5). Keep a copy — it is
required to build.

## 2. Obtain the ETSI reference source

1. Go to the ETSI standards site: <https://www.etsi.org/standards> (or the ETSI
   deliverables portal) and locate **EN 300 395-2** — *"Terrestrial Trunked
   Radio (TETRA); Speech codec for full-rate traffic channel; Part 2: TETRA
   codec"*.
2. Download the deliverable. The reference **C source code** ships as the
   electronic attachment / annex archive that accompanies the PDF (historically
   the "diskette" files).
3. Extract it somewhere outside this repo.

> Confirm your own redistribution rights before committing any of these files to
> a public fork. For a private mirror you may keep them, but the public
> `tetra-tn-web-ui` repo intentionally excludes them.

## 3. Copy the files into `native/etsi/`

Create the folder if needed and copy the codec sources in **flat** (no
sub-folders):

```
native/etsi/
```

The build only compiles the **speech coder/decoder** subset, but the headers and
coefficient tables reference each other, so copy the whole speech-path set:

**C sources (required to compile):**

| Set     | Files |
|---------|-------|
| Decoder | `sdec_tet.c`, `sub_sc_d.c`, `sub_dsp.c`, `fbas_tet.c`, `fexp_tet.c`, `fmat_tet.c`, `tetra_op.c` |
| Encoder | `scod_tet.c` (plus the shared units above) |

**Header (required):** `source.h`

**Coefficient tables (`*.tab`) referenced by the above:** `const.tab`,
`grid.tab`, `ener_qua.tab`, `lag_wind.tab`, `window.tab`, `inv_sqrt.tab`,
`log2.tab`, `pow2.tab`, `clsp_334.tab`

If in doubt, copy **every** `*.c`, `*.h` and `*.tab` from the ETSI speech codec —
extra files are harmless; the compiler only pulls in what it needs.

Also make sure our shim `acelp_state_bridge.h` is present in `native/etsi/`.

After this step the folder should look roughly like:

```
native/etsi/
  acelp_state_bridge.h   <- OURS (keep)
  source.h               <- ETSI
  scod_tet.c sdec_tet.c sub_sc_d.c sub_dsp.c
  fbas_tet.c fexp_tet.c fmat_tet.c tetra_op.c
  const.tab grid.tab ener_qua.tab lag_wind.tab window.tab
  inv_sqrt.tab log2.tab pow2.tab clsp_334.tab
```

## 4. Re-entrancy fork (important)

The stock ETSI reference code keeps codec state in **file-scope globals**, so a
single process can only run one coder and one decoder at a time and it is not
thread-safe. This project serves many simultaneous calls, so five ETSI sources
are patched to hold their state in per-instance structs defined in
`acelp_state_bridge.h`:

- `scod_tet.c`  (encoder analysis)
- `sdec_tet.c`  (decoder synthesis)
- `sub_dsp.c`
- `sub_sc_d.c`
- `tetra_op.c`

Each `#include "acelp_state_bridge.h"` and threads a state pointer through the
functions instead of touching globals.

**Consequence:** freshly downloaded ETSI files compile against our wrappers but
keep their state in globals. A single simplex point-to-point call may work, but
**full-duplex calls can misbehave** (decode and encode run at the same time and
share global state), and the globals also break the in-process simulator/loopback
and tests and can leak stale state across group-call talker changes. Use the
re-entrant fork to be safe — keep your modified `native/etsi/` in a private
archive outside this repo and restore it on a fresh clone.

## 5. Why it is git-ignored

`.gitignore` excludes `native/etsi/` and the built libraries
(`native/*.dll` / `*.so` / `*.dylib` / `*.lib`). This keeps ETSI-copyrighted
material and arch-specific build artifacts out of the public repo. The trade-off
is that a fresh clone has no codec until you redo §2–§3.

## 6. Build

With the sources in place and **`clang` on `PATH`**, nothing else is needed —
the shared libraries are compiled **on demand** the first time the app needs
audio:

- `app.acelp.build_library()`         → `native/tetra_acelp.dll` (decoder)
- `app.acelp.build_encoder_library()` → `native/tetra_acelp_enc.dll` (encoder)

(`.so` on Linux, `.dylib` on macOS.)

To build manually / verify the toolchain, the effective command is:

```bash
# decoder
clang -shared -O2 -Inative/etsi -Inative \
  native/etsi/sdec_tet.c native/etsi/sub_sc_d.c native/etsi/sub_dsp.c \
  native/etsi/fbas_tet.c native/etsi/fexp_tet.c native/etsi/fmat_tet.c \
  native/etsi/tetra_op.c native/acelp_decode.c \
  -o native/tetra_acelp.dll

# encoder (swap sdec_tet.c -> scod_tet.c and the wrapper)
clang -shared -O2 -Inative/etsi -Inative \
  native/etsi/scod_tet.c native/etsi/sub_sc_d.c native/etsi/sub_dsp.c \
  native/etsi/fbas_tet.c native/etsi/fexp_tet.c native/etsi/fmat_tet.c \
  native/etsi/tetra_op.c native/acelp_encode.c \
  -o native/tetra_acelp_enc.dll
```

### Architecture note (Windows)

`app/acelp.py` passes `--target` to clang based on the **running interpreter's**
architecture (`sysconfig.get_platform()`), not the host CPU. This matters when
an x64 Python runs emulated on an ARM64 host: a mismatch produces
`OSError: [WinError 193] %1 is not a valid Win32 application` at load time. If
you build by hand, match the target to your Python (e.g.
`--target=x86_64-pc-windows-msvc` for 64-bit x64 Python).

## 7. Verify

```bash
python -m pytest tests/test_uplink.py -q     # encoder round-trip + floor gating
python -m app --simulate                     # then place a call and talk (localhost = no TLS)
```

If the decoder/encoder can't be built or loaded the app logs an
`AcelpUnavailable` reason (missing `clang`, missing sources, or an arch
mismatch) and simply runs without audio — the rest of the UI still works.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `clang not found on PATH` | Install LLVM/clang and ensure it's on `PATH`. |
| `failed to build TETRA ACELP decoder/encoder` | A source or `.tab` is missing from `native/etsi/`; re-check §3. Read the captured clang stderr in the exception. |
| `WinError 193 ... not a valid Win32 application` | DLL/Python architecture mismatch; see §6 architecture note. |
| Audio works for one call, garbles across group-call talker changes or in the in-process simulator | You used pristine (globals) ETSI sources — restore your private re-entrant fork (§4). |
| No audio, UI otherwise fine | Expected when the codec is absent; check the startup log for `AcelpUnavailable`. |
