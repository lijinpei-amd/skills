# amdgpu-isa-builder follow-ups

Open findings from the code reviews of the plugin-generation change (`dc23c35`).
None affect the normal build or query path. They need an interrupted build,
an installed (non-checkout) builder, Windows, `-O`, or a future code change.
Paths are relative to this directory.

## Manual storage
- [ ] `builder/pdf_to_manual.py:259`: `recover_manual_backups` crashes on
  every build if the manual root was deleted while a backup remains. Create
  the root first, or discard the backup.
- [ ] `builder/pdf_to_manual.py:260`: backups whose originals still exist
  (interrupted after a successful swap, or index copies) are never removed,
  so each interrupted run can leave about 100 MB of hidden pages.
- [ ] `builder/pdf_to_manual.py:290`: the first publish renames the stage into
  the root, together with `.amdgpu-isa-stage-owner`, which then stays forever.
- [ ] `builder/pdf_to_manual.py:298`: arch dirs and indexes are published one
  rename at a time, and the backup/rollback/recovery code exists to cover the
  gaps. Simpler: build the complete new root (hardlink unchanged archs), then
  swap it in once.
- [ ] `builder/pdf_to_manual.py:389`: an arch whose PDF has no outline gets pages
  and instr-index rows but no `toc.tsv` rows, so `manual --grep` rejects it
  while `show` still links its pages.
- [ ] `builder/pdf_to_manual.py:453`: the "rows in rebuilt archs" count
  includes retained archs.
- [ ] `builder/pdf_to_manual.py:525`: `csv.Error` from `write_index` escapes as
  a traceback instead of "manual rebuild failed".

## Plugin generation
- [ ] `builder/plugin.py:261`: outputs that are no longer generated (e.g. a
  removed `references/*.md`) are never pruned, and `validate.py` can't see
  them.
- [ ] `builder/plugin.py:173`: a killed `plugin` run leaves `.generated-*` temp
  files in the tracked plugin, and verify then fails until they are deleted
  by hand.
- [ ] `builder/plugin.py:200`: `generate()` reads `build/isa.db` without the DB
  lock, so a concurrent xml/gfx publish can mismatch `db_sha256`, counts and
  DB.

## Verification
- [ ] `builder/release_regressions.py:211`, `:302`: full `verify` copies the
  sibling plugin and loads `scripts/validate.py`, so it always fails in an
  installed builder.
- [ ] `builder/release_regressions.py:71`: 72 bare `assert`s pass silently under
  `PYTHONOPTIMIZE`.
- [ ] `builder/verify.py:636`: "manual: reject unknown architecture" passes only
  when manual pages are built. Its hint also suggests `--arch typo`.
- [ ] `builder/verify.py:184`: purity guesses the plugin layout from the parent
  dir name. Use `../../.claude-plugin/plugin.json`.
- [ ] `builder/verify.py:276`: the whole `isa.py selftest` gets the 45 s
  default timeout, while each example may take 30 s.
- [ ] `skill-template/scripts/isa.py:693`: `selftest` substitutes names that
  exist for missing pinned ones, so it can't detect lost data. At least report
  the substitutions.

## Portability
- [ ] `builder/database.py:4` (and `pdf_to_manual.py`): the module-level `fcntl`
  import breaks the xml, gfx and verify stages on Windows.
- [ ] `builder/database.py:135`: `PRAGMA schema_version=1` fails under SQLite
  defensive mode.

## Cleanup
- [ ] `skill-template/scripts/isa.py:600`: `gfx --arch` is interpolated into the
  SQL string. Use a bound parameter.
- [ ] `skill-template/scripts/isa.py:458`: `manual_rows` resolves every row's
  path before filtering (~32k per `show`).
- [ ] Duplicated helpers:
  - The stage-ownership check exists in `pdf_to_manual.py:226` and
    `database.owned_stage`.
  - `plugin.sha256` duplicates `fetch.sha256`.
  - The AST literal extraction is copied 4×.
  - The `isa.py` CLI call is spelled out across `verify.py`; move
    `regressions.cli` into `check_helpers`.
- [ ] `builder/plugin.py:53`: the output inventory is defined twice
  (`_CORE_OUTPUTS` plus `STATIC_OUTPUTS`).
