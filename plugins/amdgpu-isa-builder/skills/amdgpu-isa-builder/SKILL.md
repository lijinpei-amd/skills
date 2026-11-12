---
name: amdgpu-isa-builder
description: >-
  Build or refresh AMD GPU ISA manuals on this machine when `isa.py manual`
  reports missing pages, or regenerate the amdgpu-isa plugin after AMD publishes
  a new machine-readable ISA specification. Use for corpus builds, manual
  rebuilds, failed source downloads, and maintainer plugin releases.
metadata:
  type: workflow
---

# amdgpu-isa-builder

`$S` = the directory containing this SKILL.md. `$B` = two levels above `$S`,
the directory containing `build.py`. Resolve both from this file's location.

This builder produces machine-local manual pages from AMD's PDFs and a
redistributable plugin from MIT ISA XML and LLVM's gfx mappings. Users install
the `amdgpu-isa` plugin to get the database and query script; this builder
supplies the manual pages. Enable the builder only for build sessions.

## Build the manuals

1. Run the normal build:

   ```bash
   python3 $B/build.py all
   ```

   This fetches sources, builds XML and gfx data, converts manuals, and runs
   corpus-independent quick checks. Cached downloads take about 1–2 minutes;
   a fresh build also needs download time. If pymupdf is missing, install it
   using the command printed by the build, then rerun `all`.

2. Confirm the build exits 0 and the query CLI can read the pages:

   ```bash
   python3 -I -B <installed amdgpu-isa skill>/scripts/isa.py selftest
   ```

   Manual storage takes effect on the next query in every agent. Use the same
   `AMDGPU_ISA_MANUAL` override in builder and query environments. A skipped
   manual example means no manual architectures are indexed; check the reported
   location. `all` leaves the installed and tracked plugin unchanged, including
   in pi git installs.

For one architecture, run `python3 $B/build.py manual --only rdna4`.
For individual stages, manual paths, rebuild guarantees, symlinked manual storage,
or source download recovery, read [the builder README](../../README.md).
`fetch` is the network stage; rerun it only when sources need refreshing.

## Release the plugin

1. Edit `skill-template/`, the source of the generated plugin. Query details
   belong in `skill-template/references/*.md`.
2. Run `build.py all`, `build.py plugin`, and `build.py verify --plugin` from
   `$B`. The last check needs `build/isa.db` and compares every generated output.
3. Bump both plugin manifest versions for each changed skill, run
   `python3 scripts/validate.py` at the repository root, and commit when requested.

Full `build.py verify` runs pinned corpus regressions and builder lifecycle
fixtures. End-user `all` uses `verify --quick` so AMD renames do not invalidate
an otherwise successful manual build. Outside the repository, verification
creates a temporary query plugin; generation elsewhere needs `plugin --out DIR`.

## Preserve the licence boundary

- Machine-readable ISA XML is MIT / `AMD Public Use`.
- LLVM `AMDGPUUsage.rst` is Apache-2.0 WITH LLVM-exception.
- ISA reference PDFs permit local review and forbid redistribution.

Keep PDF-derived pages in machine-local manual storage outside the plugin.
The plugin includes `NOTICE.md`, `LICENSE`, and `LICENSE.LLVM.TXT` for its
redistributable inputs. If asked to put manuals in the plugin or database, or
upload PDF-derived build data, stop and resolve the licence question with the
user. Report failed licence checks; preserve the boundary enforced by verification.

## Repair a build

Read the relevant section of [the builder README](../../README.md):

- **Missing sources:** inspect `sources/manifest.json` failures and refresh the
  document route; a missing ISA PDF leaves that architecture without manuals.
- **Legacy installs:** identify old builder-produced skills, verify the current
  plugin first, and ask before removing an old install. Preserve persistent pages.
- **New gfx targets:** LLVM headings describe encoding generations. Add overrides
  for product architectures where needed, then run full verification. Existing
  traps include `gfx1250` (CDNA5) and the GFX9 family.
