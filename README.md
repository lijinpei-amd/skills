# amdgpu-isa-builder

Builds the `amd-gpu-isa` agent skill — an AMD GPU ISA reference covering CDNA 1-5
and RDNA 1-4 — from AMD's public sources.

**This repo ships no AMD content.** It ships the code and the skill text; the
corpus is generated on your machine. That is a licence requirement, not a
preference: the machine-readable ISA XML is MIT, but AMD's ISA reference PDFs
grant review rights only and explicitly forbid passing any part to anyone else.
Generating locally keeps the two regimes from mixing.

It also keeps working. AMD moved most of this documentation behind a JavaScript
portal between one build and the next; a shipped corpus would have silently
rotted, while `fetch.py` adapts.

## Use

```bash
python3 build.py all          # fetch, build, render, verify   (~1 min)
python3 build.py install      # into Claude Code, Codex and pi
```

Start a new agent session afterwards. Then:

```bash
python3 ~/.claude/skills/amd-gpu-isa/scripts/isa.py which V_DOT2_F32_BF16
```

### Stages

| stage | does | output |
|---|---|---|
| `fetch` | download public AMD sources | `sources/` + `manifest.json` |
| `xml` | machine-readable ISA → SQLite | `build/isa.db` |
| `gfx` | LLVM `AMDGPUUsage.rst` → gfx target map | `build/isa.db` (`gfx_target`) |
| `manual` | ISA PDFs → one markdown page each + 3 TSV indexes | `build/manual/` **(local only)** |
| `render` | templates + db → the skill | `dist/amd-gpu-isa` |
| `install` | dist → agent skill directories | `~/.claude/skills`, … |
| `export` | shareable tarball, PDF-derived content excluded | `dist/*.tar.gz` |
| `verify` | counts, licence boundary, selftest | — |

`--force` re-downloads, `--link` symlinks instead of copying (for iterating),
`--agents claude,codex,pi` selects install targets, and `--manual` additionally
links the local-only manual pages into the installed skill.

`manual` is the one stage that is not stdlib-only — it needs pymupdf:

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf
python3 build.py manual            # ~55s for 6,058 pages
python3 build.py install --link --manual
```

`render` rebuilds `dist/` from scratch, so re-run `install --manual` after it.

## What gets built

19 MB SQLite database: **11,953 instructions, 40,059 encodings, 137,824 operands**
across 10 architectures, queried through `isa.py` — canned subcommands for the
common questions, raw `SELECT` for everything else, flattened views so most
queries need no join. Stdlib only; no `sqlite3` binary required, since many
machines lack one.

## Layout

```
build.py              entry point
builder/              fetch, xml_to_db, render, install, verify
skill-template/       SKILL.md.tmpl + scripts/isa.py -- the shipped skill payload
skills/               the amdgpu-isa-builder skill itself (publishable)
sources/  build/  dist/     gitignored; every AMD-derived byte lives here
```

The three gitignored directories are the licence boundary, and `build.py verify`
asserts git is tracking none of them.

## Licence

`isa.py`, the build scripts and the skill text are MIT (see `LICENSE`).

Generated instruction data derives from AMD's machine-readable ISA specification,
which each file declares `Copyright (c) 2026 Advanced Micro Devices, Inc.`,
`AMD Public Use`, `License: MIT`. `render.py` emits `NOTICE.md` alongside the
corpus to carry that attribution, as MIT requires.

AMD's ISA reference PDFs grant review rights only and forbid passing any part to
anyone else. The `manual` stage converts them for **local** use — reading them is
what the licence permits — and that output never leaves the machine: it lives in
`build/manual/` (gitignored), is linked rather than copied into the installed
skill, and `build.py export` is the single command that produces a shareable
artifact, excluding it. `verify` runs an export and inspects the tarball rather
than trusting the exclusion.

## Status

All stages work end to end. The corpus is XML-derived only, which makes it
structural rather than semantic: it has instruction names, encodings, opcodes,
operands and one-line descriptions, but no pseudocode or prose — that lives in
the PDFs, which cannot be redistributed.

The gfx-target→arch map is derived from LLVM's `AMDGPUUsage.rst`, not
hand-written. Note that LLVM's headings are *encoding* generations rather than
product architectures — it files `gfx1250` under "GCN GFX12 (RDNA 4)" though the
hardware is CDNA 5, and puts Vega and CDNA 1-4 together under "GCN GFX9". The
RDNA headings are taken as-is; the GFX9 block and `gfx125x` are overridden, each
with its evidence recorded in the `basis` column. `verify` asserts those cases.
