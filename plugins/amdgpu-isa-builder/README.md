# amdgpu-isa-builder

Builds the `amd-gpu-isa` agent skill — an AMD GPU ISA reference covering CDNA 1-5
and RDNA 1-4 — from AMD's public sources.

**This repo ships no AMD content.** It ships the code and the skill text; the
corpus is generated on your machine. That is a licence requirement, not a
preference: the machine-readable ISA XML is MIT, but AMD's ISA reference PDFs
grant review rights only and explicitly forbid passing any part to anyone else.
Generating locally keeps the two regimes from mixing.

## Usage

This plugin is meant to sit **disabled**. It is needed only when rebuilding the
corpus, and an enabled skill spends context in every session whether it is used
or not — so the cycle is activate, build, deactivate.

### 1. Activate

The three agents differ here, because only Claude Code has a disabled state.

**Claude Code** — install once, then enable per rebuild:

```bash
claude plugin marketplace add lijinpei-amd/skills   # once
#   then, in a session:  /plugin install amdgpu-isa-builder@jinpei-skills
claude plugin enable amdgpu-isa-builder
```

**Codex** — no enable/disable, so installing *is* activating:

```bash
codex plugin marketplace add lijinpei-amd/skills    # once
codex plugin add amdgpu-isa-builder@jinpei-skills
```

**pi** — loads everything under `~/.agents/skills/` unconditionally, so do not
install the builder there; point at it for one session:

```bash
pi --skill <this-dir>/skills/amdgpu-isa-builder
```

### 2. Build

Identical on all three: ask the agent to rebuild the AMD GPU ISA skill, and it
runs the same script. `build.py` needs no agent at all, so this also works on its
own:

```bash
python3 build.py all          # fetch, xml, gfx, manual, render, verify  (~2 min)
```

There are no partial builds: `all` includes the ISA manuals and `install` links
them in, because a skill missing pseudocode and prose would answer confidently
from structure alone. The cost of that choice is that what you build is
local-only — see [Licence](#licence).

Installing is where they diverge again, though one command covers all three:

**Claude Code**

```bash
python3 build.py install --agents claude      # -> ~/.claude/skills/amd-gpu-isa
```

**Codex**

```bash
python3 build.py install --agents codex       # -> ~/.codex/skills/amd-gpu-isa
```

**pi**

```bash
python3 build.py install --agents pi          # -> ~/.agents/skills/amd-gpu-isa
```

Omit `--agents` to do all three at once, which is the usual case. Start a new
agent session afterwards to pick up the rebuilt skill, then check it answers:

```bash
python3 ~/.claude/skills/amd-gpu-isa/scripts/isa.py which V_DOT2_F32_BF16   # Claude Code
python3 ~/.codex/skills/amd-gpu-isa/scripts/isa.py  which V_DOT2_F32_BF16   # Codex
python3 ~/.agents/skills/amd-gpu-isa/scripts/isa.py which V_DOT2_F32_BF16   # pi
```

Flags: `--force` re-downloads, `--link` symlinks instead of copying (for
iterating), `--agents claude,codex,pi` selects install targets.

`render` rebuilds `dist/` from scratch, so re-run `install` after it.

Everything except `render` and `install` is independently re-runnable; `fetch` is
the only stage that touches the network, so never re-run it for a text change.

### 3. Deactivate

**Claude Code** — stays installed, costs nothing until re-enabled:

```bash
claude plugin disable amdgpu-isa-builder
```

**Codex** — uninstall, since there is no disabled state:

```bash
codex plugin remove amdgpu-isa-builder
```

**pi** — nothing to undo; `--skill` applied to that session only:

```bash
# (no command)
```

Deactivating the builder does not touch the skill it produced: `amd-gpu-isa` is
installed into each agent's own skills directory and stays enabled. Only the
build machinery goes quiet.

### Dependencies

**To build the skill:** Python 3 (developed on 3.12), **pymupdf**, and `git` —
`verify` shells out to it to assert the licence boundary. `fetch` needs network
access; the other stages do not.

pymupdf is the only third-party package and it is required, since the `manual`
stage is part of every build. Set it up once, before the first build:

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf
```

`build.py` looks for that venv and uses it for the `manual` stage alone, so the
rest of the build stays stdlib-only. Without it, `build.py all` stops and tells
you this rather than producing a skill with no pseudocode in it.

**To use the built skill:** Python 3, nothing else. `isa.py` imports only
`argparse`, `json`, `os`, `re`, `sqlite3` and `sys`, so the corpus is queryable
on any machine with a Python interpreter — no pymupdf, and no `sqlite3` binary,
which many machines lack.

## Structure of generated skill

`render` writes `dist/amd-gpu-isa/`:

```
amd-gpu-isa/
├── SKILL.md              frontmatter + a decision table: question -> isa.py command
├── scripts/
│   └── isa.py            the whole query interface, stdlib only (28K)
├── data/
│   └── isa.db            19 MB SQLite corpus, XML-derived
├── NOTICE.md             AMD copyright + MIT text, as MIT requires
├── build-info.json       timestamp, per-arch counts, source URLs + sha256, and
│                         the two flags that gate sharing:
│                         includes_pdf_derived / redistributable
└── manual -> build/manual    the ISA manuals, linked at install (LOCAL ONLY)
```

The database holds **11,953 instructions, 40,059 encodings, 137,824 operands**
across 10 architectures, plus 52 gfx targets mapped to those architectures.
Normalised tables underneath, but five flattened views on top — `v_inst`,
`v_presence`, `v_name` (resolves aliases), `v_operand`, `v_gfx` — so most
queries need no join, and an FTS5 index over descriptions.

`isa.py` exposes canned subcommands for the common questions — `show`, `which`,
`search`, `list`, `encodings`, `diff`, `gfx`, `archs`, `groups`, `manual` — and
`sql` for everything else, against a read-only connection. `schema` prints the
DDL so an agent can write its own query; `selftest` checks the corpus actually
answers correctly.

### The manual pages

Built and linked in every time, because they carry what the XML does not:
pseudocode, prose, and the encoding tables as AMD wrote them. The skill is meant
to be authoritative, so this is not a flag:

```
build/manual/                    6,058 pages + 418 figures from 13 manuals
├── toc.tsv                      3,116 rows   arch level title page_physical path
├── instr-index.tsv             32,741 rows   arch instruction page_physical
│                                             page_printed kind path
├── format-index.tsv             5,040 rows   arch encoding page_physical
│                                             page_printed path
├── cdna5/                         832 pages
│   ├── p0001.md ... p0832.md    one per PDF page, front matter: pdf, arch,
│   │                            page_physical, page_printed, chapter
│   └── img/                        37 figures, content-addressed
├── rdna4/  707 · rdna3_5/ 653 · rdna3/ 609 · cdna4/ 608 · cdna3/ 561
├── gcn3/   348 · cdna1/  283 · cdna2/  275 · rdna1/ 297 · rdna2/  291
└── vega/   296 · vega7nm/ 298
```

The three TSVs are the point: they make 6,058 pages addressable without reading
them, so `isa.py manual` resolves a question to a page or two. `instr-index.tsv`
marks each row `definition` or `table`, which is what separates an instruction's
real description from a bare opcode listing.

Note the 13 manuals against the database's 10 architectures — the manual corpus
also covers GCN3, Vega and Vega 7nm, which have no machine-readable XML.

## How it generates the amd-gpu-isa skill

Seven stages, each writing into the next; `all` runs every one but `install`:

| stage | does | output |
|---|---|---|
| `fetch` | download 21 public AMD sources | `sources/` + `manifest.json` |
| `xml` | machine-readable ISA → SQLite | `build/isa.db` |
| `gfx` | LLVM `AMDGPUUsage.rst` → gfx target map | `build/isa.db` (`gfx_target`) |
| `manual` | ISA PDFs → one markdown page each + 3 TSV indexes | `build/manual/` **(local only)** |
| `render` | templates + db → the skill | `dist/amd-gpu-isa` |
| `install` | dist → agent skill dirs, manual linked in | `~/.claude/skills`, … |
| `verify` | counts, licence boundary, selftest | — |

**fetch** pulls 14 ISA reference PDFs, 4 architecture white papers, the
machine-readable ISA zip and LLVM's `AMDGPUUsage` (rst + html, at a pinned
commit). Canonical `amd.com` URLs now 301 to a JavaScript viewer rather than the
file, so each source falls back to the docs.amd.com portal's file endpoint, with
document ids resolved from the portal's own index — 8 of the 21 currently arrive
that way. The manifest records url, actual route, content type, size and sha256
per file. A source that stops resolving is recorded as FAILED rather than
silently saved as an HTML error page; the build continues, since the database
comes from the XML — but a missing ISA PDF leaves that architecture without
manual pages, so the result is incomplete rather than merely smaller.

That portal migration happened between one build and the next, which is the
second argument for generating rather than shipping: a bundled corpus would have
gone quietly stale, whereas a fetch layer can be pointed at the new route.

**xml** iterparses the per-architecture XML into normalised tables
(`instruction`, `inst_encoding`, `operand`, `encoding_field`, …) and then defines
the views and FTS index over them.

**gfx** parses LLVM's processor table. LLVM groups processors by *encoding*
generation, which is not the product architecture, so the RDNA headings are
trusted while the GFX9 block and `gfx125x` are overridden — each row carries its
evidence in a `basis` column.

**manual** renders each PDF page to markdown with pymupdf, strips running
headers and footers (by page band plus normalised frequency, so a heading that
happens to sit high on the page survives), extracts and content-addresses images
so a logo repeated 2,235 times is stored once, and classifies each page. The
classifier distinguishes an instruction's *definition* page from a bare opcode
table by a structural rule rather than by prose: an opcode table contains only
mnemonics and bare integers, so any other line means definition. That holds
across manual generations where a prose test did not.

**render** substitutes the live counts and architecture list into
`skill-template/SKILL.md.tmpl`, copies `isa.py` and the database, and emits
`NOTICE.md` and `build-info.json`.

**verify** runs eleven checks, the important ones being adversarial: instruction
counts are re-derived straight from the raw XML by regex — a different code path
from the builder — so a parser bug cannot pass by agreeing with itself; git is
asserted to be tracking nothing AMD-derived; and `dist/` is walked to confirm
PDF-derived pages appear there only as a symlink into `build/`, never as a copy.

## Licence

`isa.py`, the build scripts and the skill text are MIT (see `LICENSE`).

Generated instruction data derives from AMD's machine-readable ISA specification,
which each file declares `Copyright (c) 2026 Advanced Micro Devices, Inc.`,
`AMD Public Use`, `License: MIT`. `render.py` emits `NOTICE.md` alongside the
corpus to carry that attribution, as MIT requires.

AMD's ISA reference PDFs grant review rights only and forbid passing any part to
anyone else. The `manual` stage converts them for **local** use — reading them is
what the licence permits — and that output never leaves the machine: it lives in
`build/manual/` (gitignored), and the installed skill reaches it by symlink, so
there is exactly one copy.

The boundary is structural rather than remembered. Three gitignored directories
hold every AMD-derived byte:

```
sources/   build/   dist/
```

The builder has no command that publishes, uploads or packages anything — the
only way out is a deliberate manual copy. `verify` asserts git tracks none of
those directories and that `dist/` holds no PDF-derived file of its own, and
`build-info.json` records what was built:

```json
"db_redistributable": true,        // isa.db alone: XML-derived, MIT
"includes_pdf_derived": true,      // the skill reaches the ISA manuals
"redistributable": false           // so the skill as a whole may not be shared
```

**Because the manuals are always built, every install is local-only.** That is
the deliberate trade: an authoritative skill in exchange for one that cannot be
handed to anyone. If you ever do hand-copy `dist/`, note that it contains a live
symlink into `build/manual/` — copy it with a tool that follows symlinks and the
ISA manuals come along.

Only `isa.db` is redistributable on its own, as `db_redistributable` records,
and only with `NOTICE.md` beside it.

## Status

All stages work end to end, and a default build is complete: database, gfx map
and all 6,058 manual pages.

The two halves answer different questions. `isa.db` is structural — names,
encodings, opcodes, operands, one-line descriptions — and is what makes
cross-architecture questions cheap to ask. The manual pages carry the semantics:
pseudocode, prose, and AMD's own tables. Where they disagree, the manual is the
authority; the database is a re-derivation of the XML, which is itself generated.

Known sharp edge, worth knowing before trusting the gfx map: LLVM files
`gfx1250` under "GCN GFX12 (RDNA 4)" though the hardware is CDNA 5, and puts Vega
and CDNA 1-4 together under "GCN GFX9". Those cases are overridden with recorded
evidence and `verify` fails if either regresses, but a new architecture will
arrive mis-filed the same way and needs adding to `OVERRIDES` by hand rather than
trusting the heading.

One unfixed hazard: on a failed `--force` re-download, `fetch.py` removes the
destination file, so refreshing a document AMD has since moved deletes the good
local copy. Download to a temp file and move on success.
