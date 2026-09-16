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

Run individually when only part needs redoing — `fetch` hits the network, so
never re-run it for a text change.

| stage | does | output |
|---|---|---|
| `fetch` | download public AMD sources | `sources/` + `manifest.json` |
| `xml` | machine-readable ISA → SQLite | `build/isa.db` |
| `gfx` | LLVM `AMDGPUUsage.rst` → gfx target map | `build/isa.db` (`gfx_target`) |
| `manual` | ISA PDFs → markdown pages + TSV indexes | `build/manual/` **(local only)** |
| `render` | templates + db → the skill | `dist/amd-gpu-isa` |
| `install` | dist → agent skill directories | `~/.claude/skills`, … |
| `export` | shareable tarball, PDF-derived content excluded | `dist/*.tar.gz` |
| `verify` | counts, licence boundary, selftest | — |

Useful flags: `--force` (re-download), `--link` (symlink instead of copy, for
iterating), `--agents claude,codex,pi`, `--manual` (also link the local-only
manual pages into the installed skill).

The `manual` stage needs pymupdf, the only non-stdlib dependency:

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf
python3 $B/build.py manual                       # ~55s, 6,058 pages
python3 $B/build.py install --link --manual
```

`render` recreates `dist/` from scratch, so re-run `install --manual` after it.

## The licence boundary — do not blur it

Two regimes, and they must not mix:

- **Machine-readable ISA XML** — MIT, marked `AMD Public Use`. Everything derived
  from it is redistributable, provided `NOTICE.md` travels with it.
- **ISA reference PDFs** — public to read, **not redistributable**. Each carries a
  Specification Agreement: *"You may not (i) duplicate any part of the
  Specification … or (iii) give any part of the Specification … to anyone else."*

So the builder uses **only** the XML. The PDFs are fetched for human reading and
are never parsed into the corpus — which is why the skill has no pseudocode.

The `manual` stage converts the PDFs for local reading — which is exactly what
the licence permits — into `build/manual/`. That output is gitignored, linked
rather than copied into the installed skill, and excluded by `build.py export`.

**Never publish `build/manual/`, and never copy `dist/` by hand to share it.**
Use `build.py export`, which writes a tarball and then inspects it to prove no
PDF-derived file got in. If you are asked to add manual content to `isa.db`,
don't: keeping the two apart by file is what makes the shipping decision a
one-line check instead of a column-by-column audit.

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
anyway — the corpus comes from the XML, so a missing PDF does not block it.
Report the failure to the user; a new document id
may need looking up in `https://docs.amd.com/api/khub/documents`.

## Checking the result

```bash
python3 $B/dist/amd-gpu-isa/scripts/isa.py selftest      # must be 10/10
python3 $B/build.py verify
```

`verify` re-derives instruction counts from the raw XML through a different code
path than the builder uses, so a parser bug cannot hide by agreeing with itself.
Expect 11,953 instructions / 40,059 encodings / 137,824 operands across 10
architectures, and 28 gfx targets mapped.

## The gfx map needs care

`gfx_to_db.py` parses LLVM's processor table. LLVM groups processors by *encoding*
generation, which is not the product architecture: `gfx1250` sits under "GCN GFX12
(RDNA 4)" but is CDNA 5, and "GCN GFX9 (Vega)" covers Vega plus CDNA 1-4. The
RDNA headings are trusted; the GFX9 block and `gfx125x` are overridden in
`OVERRIDES`, each row carrying its evidence into the `basis` column.

When AMD ships a new architecture, add its gfx targets there rather than trusting
the heading — and run `build.py verify`, which fails if any arch has no gfx target
or if a known trap regresses.
