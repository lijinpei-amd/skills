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

`$B` = the directory two levels above this SKILL.md — the one containing
`build.py`. Resolve it from this file's own location; under a plugin install the
absolute path differs per agent.

This plugin is meant to sit **disabled** most of the time, so it costs nothing in
sessions that are not rebuilding. Enable it, rebuild, disable it again:
`claude plugin enable amdgpu-isa-builder` / `... disable`.

## Normal rebuild

```bash
python3 $B/build.py all          # fetch, xml, gfx, manual, render, verify  (~2 min)
python3 $B/build.py install      # into Claude Code, Codex and pi
```

Then tell the user to start a new agent session to pick it up.

`all` includes the ISA manuals, and `install` links them in. That is not
optional: without them the skill has no pseudocode and no prose, only structure.
If pymupdf is missing, `all` stops and tells you how to install it — do that
rather than routing around it with individual stages, which would produce a
quietly incomplete skill.

## Stages

Run individually when only part needs redoing — `fetch` hits the network, so
never re-run it for a text change.

| stage | does | output |
|---|---|---|
| `fetch` | download public AMD sources | `sources/` + `manifest.json` |
| `xml` | machine-readable ISA → SQLite | `build/isa.db` |
| `gfx` | LLVM `AMDGPUUsage.rst` → gfx target map | `build/isa.db` (`gfx_target`) |
| `manual` | ISA PDFs → markdown pages + TSV indexes | `build/manual/` **(local only)** |
| `render` | templates + db → the skill | `dist/amd-gpu-isa` |
| `install` | dist → agent skill dirs, manual linked in | `~/.claude/skills`, … |
| `verify` | counts, licence boundary, selftest | — |

Useful flags: `--force` (re-download), `--link` (symlink instead of copy, for
iterating), `--agents claude,codex,pi`.

The `manual` stage needs pymupdf, the only non-stdlib dependency:

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf
python3 $B/build.py manual                       # ~55s, 6,058 pages
```

`render` recreates `dist/` from scratch, so re-run `install` after it.

## The licence boundary — do not blur it

Two regimes, and they must not mix:

- **Machine-readable ISA XML** — MIT, marked `AMD Public Use`. Everything derived
  from it is redistributable, provided `NOTICE.md` travels with it.
- **ISA reference PDFs** — public to read, **not redistributable**. Each carries a
  Specification Agreement: *"You may not (i) duplicate any part of the
  Specification … or (iii) give any part of the Specification … to anyone else."*

`isa.db` is built from the XML alone, so the database itself stays MIT. The
`manual` stage converts the PDFs for local reading — exactly what the licence
permits — into `build/manual/`, which is gitignored and linked rather than
copied, so those pages exist in one place on disk.

**Every install is therefore local-only, by design.** The skill includes the
manuals because an ISA reference without pseudocode is not authoritative, and
the price of that choice is that the *installed skill* may not be shared:
`build-info.json` records `"redistributable": false` and
`"includes_pdf_derived": true` to say so. Read those flags before copying an
installed skill anywhere.

**This builder has no command that shares anything.** Everything it produces is
local. If you are asked to publish, upload or send the corpus, treat that as a
licence question and stop — and note that `dist/` contains a live symlink to the
manual pages, so copying it with a symlink-following tool takes them along. If
you are asked to fold manual content into `isa.db`, don't: keeping the two apart
by file is what keeps the boundary a one-line check rather than a
column-by-column audit.

**Never** commit anything from `sources/`, `build/` or `dist/`. `build.py verify`
enforces that, along with the consistency of the two flags above; if it fails on
a licence check, stop and tell the user rather than working around it.

## When sources are missing

AMD moved most GPU documentation behind a JavaScript portal at docs.amd.com; the
canonical `amd.com` URLs 301 to a viewer page instead of the file. `fetch.py`
falls back to the portal's file endpoint using document ids resolved from its
own index, and records which route each file came from.

If a document stops resolving, the fetch is recorded as FAILED in
`sources/manifest.json` rather than silently saving an HTML error page. The
build still completes — the database comes from the XML — but a missing ISA PDF
means that architecture has no manual pages, so the skill is incomplete for it
rather than merely smaller. Report the failure to the user; a new document id
may need looking up in `https://docs.amd.com/api/khub/documents`.

## Checking the result

```bash
python3 $B/build.py verify
python3 ~/.claude/skills/amd-gpu-isa/scripts/isa.py selftest   # after install
```

Run `selftest` from an **installed** skill, not from `dist/`: the manual pages
are linked in at install time, so against `dist/` the two manual cases report as
skipped (`12/12 passed, 2 skipped`) and the manuals go untested. From an
installed copy all 14 must pass.

`verify` re-derives instruction counts from the raw XML through a different code
path than the builder uses, so a parser bug cannot hide by agreeing with itself.
Expect 11,953 instructions / 40,059 encodings / 137,824 operands across 10
architectures, and 52 gfx targets mapped.

## The gfx map needs care

`gfx_to_db.py` parses LLVM's processor table. LLVM groups processors by *encoding*
generation, which is not the product architecture: `gfx1250` sits under "GCN GFX12
(RDNA 4)" but is CDNA 5, and "GCN GFX9 (Vega)" covers Vega plus CDNA 1-4. The
RDNA headings are trusted; the GFX9 block and `gfx125x` are overridden in
`OVERRIDES`, each row carrying its evidence into the `basis` column.

When AMD ships a new architecture, add its gfx targets there rather than trusting
the heading — and run `build.py verify`, which fails if any arch has no gfx target
or if a known trap regresses.
