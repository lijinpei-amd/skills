# amdgpu-isa-builder

Builds the AMD GPU ISA reference — CDNA 1-5 and RDNA 1-4 — from AMD's public
sources, as two outputs under two licences:

- **The `amdgpu-isa` plugin** (`../amdgpu-isa`): the SQLite corpus, `isa.py`
  and the skill text. Derived from the MIT machine-readable ISA XML and LLVM's
  `AMDGPUUsage.rst`, so it is redistributable and committed to this repo. Users
  install that plugin; only its maintainer regenerates it, with `build.py plugin`.
- **The ISA manual pages**: AMD's reference PDFs converted to markdown. Those
  PDFs grant review rights only and forbid passing any part to anyone else, so
  the pages are built per machine into persistent storage at
  `~/.local/share/amdgpu-isa/manual`, where the plugin's `isa.py` finds them.

**This builder ships no AMD content.** It ships the code and the templates.

## Usage

This plugin is meant to sit **disabled**. It is needed only when building the
manual pages or regenerating the plugin, and an enabled skill spends context in
every session whether it is used or not — so the cycle is activate, build,
deactivate.

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

Identical on all three: ask the agent to build the AMD ISA manual pages, and it
runs the same script. `build.py` needs no agent at all, so this also works on its
own:

```bash
python3 build.py all          # fetch, xml, gfx, manual, verify --quick
```

With cached downloads, expect roughly 1–2 minutes; a fresh build also needs
time to download the sources.

The `manual` stage publishes the pages into
`~/.local/share/amdgpu-isa/manual` (absolute `$XDG_DATA_HOME` is honoured; relative values are ignored; set
`AMDGPU_ISA_MANUAL` to use another path, in the builder's environment and the
agent's). Building at this path requires a writable parent on the same
filesystem as the manual root. Queries during a rebuild may see a partially
published manual. Override paths expand `~` and become absolute, including
relative paths and trailing slashes. A symlinked manual root uses its existing
directory target without replacing the link. Dangling links and links to files
are reported without recreating or changing their targets. The default storage
sits outside every builder and skill directory, so builder updates and removal
preserve it. Check it from any agent:

```bash
python3 -I -B <amdgpu-isa plugin>/skills/amdgpu-isa/scripts/isa.py selftest   # 14/14, none skipped
```

`all` builds machine-local data and runs `verify --quick` against the installed
plugin without regenerating it, including inside a pi git install or full-repo
clone. Explicit `build.py verify` runs the full regression suite. Plugin
generation requires an explicit `build.py plugin`; it runs only in a checkout
with a sibling plugin, or with an explicit output directory.

Flags: `--force` re-downloads. Every stage is independently re-runnable; `fetch`
is the only one that touches the network, so never re-run it for a text change.

`build.py manual --only rdna4` rebuilds that architecture's pages and replaces
only its rows in the three indexes. Installed rows for other architectures are
preserved, including when their PDFs are missing from the source directory.
If no matching PDFs are available, the stage exits 1 without creating a manual
directory or changing an existing installation.
The manual stage requires a usable `isa.db` before changing pages, because its
instruction and format indexes depend on that database. Rebuilt architectures
replace their page and image trees, removing leftovers from shorter PDFs.
Pages and indexes are staged before replacing architecture directories and
index files one by one. Index replacements are atomic, with copies retained
for rollback; architecture replacement can temporarily leave a gap. Conversion
and index-generation failures leave the
previous installation intact. Publication failures trigger rollback.
On a first build, the manual root appears only after successful conversion and
index generation. `toc.tsv` marks a published manual installation; the query
CLI treats an empty directory or partial pages without that file as unavailable.
Only the architectures in its `arch` column are installed manual architectures.
Stages and recovery backups sit beside the resolved manual root, including when
it is reached through a symlink. Builds share a lock in that parent directory;
the next build restores missing entries from matching recovery backups before
removing owned abandoned stages. Backups with remaining originals are preserved. New
roots, pages, figures and indexes honor the process umask; rebuilds preserve
existing root and index permissions.

### Migrating old skill installs

Older builder versions installed real skill directories by default and
symlinks with `--link`, under `~/.claude/skills/`, `~/.codex/skills/` and
`~/.agents/skills/`. The name was `amd-gpu-isa` before commit `5fb6c93`, then
`amdgpu-isa`. Those copies are stale, and their names collide
with the generated plugin; pi can drop the plugin as a duplicate.

Install the `amdgpu-isa` plugin for each client and check its CLI before
removing an old install. Identify it by its `SKILL.md` and `build-info.json`
content (legacy builds include PDF-derived content and declare
`redistributable: false`), or a `manual` link into the old builder's
`build/manual`. A real directory can be a legacy install too. Inspect the
contents and ask the user before removing any verified old install. Preserve
unrelated skills, the persistent manual directory and its contents. The builder
no longer installs skills.

### Maintainer releases

End users run only `build.py all`. To release a changed plugin:

1. Run `python3 build.py all`, `python3 build.py plugin`, and
   `python3 build.py verify --plugin`.
2. Bump `version` in both `amdgpu-isa` `plugin.json` files if this release
   changes their content.
3. Run `python3 ../../scripts/validate.py` and `git diff --check`.
4. Review and commit the intended plugin and builder changes.

`--plugin` requires `build/isa.db` and fails explicitly if it is missing.

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

Deactivating the builder touches neither output: the `amdgpu-isa` plugin is
installed on its own, and the persistent manual directory stays where it is. Only the build
machinery goes quiet.

### Dependencies

**To build:** Python 3 (developed on 3.12), **pymupdf**, and `git` — `verify`
shells out to it to assert the licence boundary. `fetch` needs network access;
the other stages do not.

pymupdf is the only third-party package, needed by the `manual` stage alone. Set
it up once, before the first build:

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf
```

`build.py` looks for that venv and uses it for the `manual` stage alone, so the
rest of the build stays stdlib-only. Without it, `build.py all` stops and tells
you this rather than leaving the manual pages unbuilt.

**To use the plugin:** Python 3, nothing else. `isa.py` imports only stdlib
modules (`argparse`, `json`, `os`, `re`, `sqlite3`, `sys`), so the corpus is
queryable on any machine with a Python interpreter — no pymupdf, and no `sqlite3`
binary, which many machines lack.

## Structure of the generated plugin

`plugin` writes `../amdgpu-isa/skills/amdgpu-isa/`, and nothing else may live
there. `verify` checks the whole plugin against the generated outputs and its
two manifests; it ignores real `__pycache__` directories:

```
amdgpu-isa/
├── SKILL.md              frontmatter + a decision table: question -> isa.py command
├── references/
│   └── query-semantics.md JSON fields, instruction identities and SQL semantics
├── scripts/
│   └── isa.py            the whole query interface, stdlib only
├── data/
│   └── isa.db            19 MB SQLite corpus, XML- and LLVM-derived
├── NOTICE.md             AMD's MIT notice + LLVM provenance (pinned commit)
├── LICENSE               MIT, for isa.py and the skill text
├── LICENSE.LLVM.TXT      Apache-2.0 WITH LLVM-exception, for the gfx map
└── build-info.json       source release date, per-arch counts, source URLs + sha256,
                          db_sha256, and redistributable=true /
                          includes_pdf_derived=false
```

The output is a pure function of `build/isa.db`, `skill-template/`, `LICENSE`
and `sources/` — no timestamps of its own, no absolute paths. Maintainer
`verify --plugin` compares the text outputs exactly and databases by their
schema and ordered rows, including `sqlite_stat1` planner statistics for ordinary
tables. FTS shadow tables and their statistics are excluded because they describe
SQLite storage rather than corpus content. The shipped `db_sha256` always matches the shipped file's
actual bytes.
The `built` field is the latest architecture release date in the source XML,
not the rebuild time. Both database stages run `ANALYZE` and compact the
database. SQLite before 3.27 uses a copy followed by `VACUUM`; newer versions
use `VACUUM INTO`. XML and gfx work in sibling database stages under a parent
build lock; failures preserve the published database. The next stage removes
interrupted XML, gfx and compaction files with matching ownership records;
unowned files and symlinks are preserved. Repeating `gfx` with the same SQLite version preserves bytes.
For logically identical rebuilds with another SQLite version, `plugin` keeps
the existing database bytes, checksum and file modification times. LLVM
provenance comes from `sources/manifest.json`; no separate pin file is needed.

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
`which` and `show` follow aliases across architectures, including sibling
spellings. Their `also_present_as` JSON contains one entry per additional
architecture; `names` lists its canonical alternatives and `name` is the first.

### The manual pages

Built per machine, never shipped, because they carry what the XML does not:
pseudocode, prose, and the encoding tables as AMD wrote them:

```
~/.local/share/amdgpu-isa/manual/ 6,058 pages + 418 figures from 13 manuals
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

## How it builds

`all` runs the five local-build stages. Plugin generation is explicit:

| stage | does | output |
|---|---|---|
| `fetch` | download 21 public AMD sources | `sources/` + `manifest.json` |
| `xml` | machine-readable ISA → SQLite | `build/isa.db` |
| `gfx` | LLVM `AMDGPUUsage.rst` → gfx target map | `build/isa.db` (`gfx_target`) |
| `manual` | ISA PDFs → one markdown page each + 3 TSV indexes | persistent manual directory **(local only)** |
| `plugin` | explicit maintainer generation: templates + db → redistributable plugin | `../amdgpu-isa/skills/amdgpu-isa` |
| `verify` | counts, licence boundary, manual pages, answers; `--plugin` also checks generated outputs | — |

`all` continues to manual conversion if `gfx` fails. Its quick verification
warns and skips the gfx checks; a standalone builder also defers query-plugin
generation until the mapping is fixed. Explicit `build.py gfx`, full verification
and plugin releases stay strict. Plugin generation requires a populated gfx map
and its query view.
`all` finishes with `verify --quick`; run `build.py verify` explicitly for the
full suite, or `build.py verify --plugin` to include generator comparisons.

**fetch** pulls 14 ISA reference PDFs, 4 architecture white papers, the
machine-readable ISA zip and LLVM's `AMDGPUUsage` (rst + html, at a pinned
commit). Canonical `amd.com` URLs now 301 to a JavaScript viewer rather than the
file, so each source falls back to the docs.amd.com portal's file endpoint, with
document ids resolved from the portal's own index — 7 of the 21 currently arrive
that way — and then to GPUOpen's byte-identical copies of the RDNA 2-4 guides.
RDNA 3.5 has left the portal's public index and now arrives only from GPUOpen.
The manifest records url, actual route, content type, size and sha256 per file,
and the XML zip is unpacked into `sources/xml/extracted/` on every run. A source
that stops resolving is recorded as FAILED rather than silently saved as an HTML error page; the build continues, since the database
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

**plugin** substitutes the live counts and architecture list into
`skill-template/SKILL.md.tmpl`, fills the pinned LLVM commit into
`NOTICE.md.tmpl`, copies `isa.py`, `references/*.md`, the database and both licences, and writes
`build-info.json`.

**verify** is adversarial where it matters: instruction counts are re-derived
straight from the raw XML by regex — a different code path from the builder — so
a parser bug cannot pass by agreeing with itself; git is asserted to be tracking
nothing AMD-derived in this builder; the plugin must hold nothing but generated
files and declare itself redistributable; and the query CLI's selftest
and regression cases run against the plugin. `scripts/validate.py` at the repo
root also compares the plugin's `isa.py` and licence files with their sources,
since that check needs no build inputs.
The maintainer `--plugin` flag additionally regenerates into a scratch directory
and compares generated text and logical database content. Default verification
accepts an installed plugin that differs from newer machine-local build inputs.
Outside a skills-repo checkout, `verify` generates a temporary plugin for the
query checks, including manual-only architectures. Child errors,
timeouts and malformed query output become failed checks with a final summary.
`--quick` runs corpus-independent selftest, structural, licence and manual
checks, so upstream instruction renames do not fail an end-user build. Pinned
query expectations and builder lifecycle fixtures run only in full `verify`;
`--plugin` retains pinned query checks even with `--quick`, which still skips
builder lifecycle fixtures.

## Licence

`isa.py`, the build scripts and the skill text are MIT (see `LICENSE`).

The plugin's database has two sources. Instruction data derives from AMD's
machine-readable ISA specification, which each file declares `Copyright (c) 2026
Advanced Micro Devices, Inc.`, `AMD Public Use`, `License: MIT`. The gfx target
map derives from LLVM's `AMDGPUUsage.rst`, Apache-2.0 WITH LLVM-exception. The
plugin therefore carries `NOTICE.md` and `LICENSE.LLVM.TXT`, and declares
`MIT AND (Apache-2.0 WITH LLVM-exception)`.

AMD's ISA reference PDFs grant review rights only and forbid passing any part to
anyone else. The `manual` stage converts them for **local** use — reading them is
what the licence permits — and that output never leaves the machine: it lives in
the persistent manual directory outside the builder and plugin installations.

The boundary is structural rather than remembered. Downloaded inputs and build
artefacts stay in two gitignored directories:

```
sources/   build/
```

PDF-derived pages stay in the machine-local manual directory and never enter
the generated plugin.

`verify` asserts git tracks neither, and that the plugin — the one output that
is shared — holds no PDF-derived file or link. If you are asked to fold manual
content into `isa.db` or the plugin, don't: keeping the two apart by file is what
keeps the boundary a one-line check rather than a column-by-column audit.

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
