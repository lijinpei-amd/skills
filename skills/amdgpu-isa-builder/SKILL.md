---
name: amdgpu-isa-builder
description: >-
  Build or refresh the local amd-gpu-isa skill from public AMD sources. Use when
  asked to build, rebuild, refresh or install the AMD GPU ISA corpus, or after
  AMD publishes a new ISA specification.
metadata:
  type: workflow
---

# amdgpu-isa-builder

Builds the `amd-gpu-isa` skill on this machine. The corpus is never distributed —
it is generated locally from AMD's published sources, for licence reasons set out
below.

`$B` = the builder checkout (ask the user if it is not obvious; typically
`~/development/amdgpu-isa-builder`).

## Normal rebuild

```bash
python3 $B/build.py all          # fetch, build, render, verify  (~1 min)
python3 $B/build.py install      # into Claude Code, Codex and pi
```

Then tell the user to start a new agent session to pick it up.

## Stages

Run individually when only part needs redoing — `fetch` hits the network and
`pdf` is slow, so never re-run them for a text change.

| stage | does | output |
|---|---|---|
| `fetch` | download public AMD sources | `sources/` + `manifest.json` |
| `xml` | machine-readable ISA → SQLite | `build/isa.db` |
| `pdf` | ISA manuals → pseudocode/notes | `build/enrich.db` (local only) |
| `render` | templates + db → the skill | `dist/amd-gpu-isa` |
| `install` | dist → agent skill directories | `~/.claude/skills`, … |
| `verify` | counts, licence boundary, selftest | — |

Useful flags: `--force` (re-download), `--link` (symlink instead of copy, for
iterating), `--agents claude,codex,pi`, `--with-enrich` (see below).

## The licence boundary — do not blur it

Two regimes, and they must not mix:

- **Machine-readable ISA XML** — MIT, marked `AMD Public Use`. Everything derived
  from it is redistributable, provided `NOTICE.md` travels with it.
- **ISA reference PDFs** — public to read, **not redistributable**. Each carries a
  Specification Agreement: *"You may not (i) duplicate any part of the
  Specification … or (iii) give any part of the Specification … to anyone else."*

So: `isa.db` is XML-derived and shareable. `enrich.db` is PDF-derived and must
stay on this machine. `render --with-enrich` produces a **local-only** build and
says so in `NOTICE.md` and `build-info.json`.

**Never** commit anything from `sources/`, `build/` or `dist/`, and never publish
a build whose `build-info.json` says `"redistributable": false`. `build.py verify`
enforces both; if it fails on the licence check, stop and tell the user rather
than working around it.

## When sources are missing

AMD moved most GPU documentation behind a JavaScript portal at docs.amd.com; the
canonical `amd.com` URLs 301 to a viewer page instead of the file. `fetch.py`
falls back to the portal's file endpoint using document ids resolved from its
own index, and records which route each file came from.

If a document stops resolving, the fetch is recorded as FAILED in
`sources/manifest.json` rather than silently saving an HTML error page. Build
anyway — the skill degrades to whatever sources are present and says which
architectures lack enrichment. Report the failure to the user; a new document id
may need looking up in `https://docs.amd.com/api/khub/documents`.

## Checking the result

```bash
python3 $B/dist/amd-gpu-isa/scripts/isa.py selftest      # must be 10/10
python3 $B/build.py verify
```

`verify` re-derives instruction counts from the raw XML through a different code
path than the builder uses, so a parser bug cannot hide by agreeing with itself.
Expect 11,953 instructions / 40,059 encodings / 137,824 operands across 10
architectures.
