# Split LLVM Debug Logs

`$S` = the directory containing this skill's `SKILL.md`; resolve it from the
loaded skill, not a fixed agent install path. Both helpers use only the Python
standard library (Python 3.10+). Run these examples from the LLVM checkout and
substitute its discovered build directory for `build`.

## Scheduler regions or other regex-delimited logs

Capture scheduler diagnostics, then split at pre-RA region banners and the
post-RA scheduler marker:

```bash
log_dir=$(mktemp -d "${TMPDIR:-/tmp}/llvm-logs.XXXXXX")
build/bin/llc -mtriple=amdgcn-amd-amdhsa -mcpu=gfx950 \
  -debug-only=machine-scheduler input.ll -o /dev/null 2> "$log_dir/schedule.log"
python3 "$S/scripts/sched_log_split.py" "$log_dir/schedule.log" \
  '^\*+ MI Scheduling \*+$|^Before post-MI-sched:$' -o "$log_dir/regions"
```

The regex is required and is searched on each line; anchor it when matching a
whole banner. Each matching line starts the next chunk and stays in that chunk.
Any preamble becomes the first chunk. `schedule.log` produces `schedule_1.log`,
`schedule_2.log`, etc. The post-RA marker separates output that lacks an
`MI Scheduling` banner. The helper also works with other boundary regexes.

`-o` / `--output-dir` defaults to the current directory. Existing files matching
the input's numbered chunk names cause an error, including stale higher-numbered
chunks. Choose a fresh output directory for each run. Empty input writes no chunks;
non-empty input without a match produces one chunk.

## Per-pass LLVM IR, MachineIR, or MLIR snapshots

Capture full pass dumps, then split them into ordered snapshots:

```bash
log_dir=$(mktemp -d "${TMPDIR:-/tmp}/llvm-ir-logs.XXXXXX")
build/bin/opt -passes=instcombine -print-after-all -disable-output input.ll \
  2> "$log_dir/ir.log"
python3 "$S/scripts/split_ir_dump.py" "$log_dir/ir.log" -o "$log_dir/passes"
```

For backend snapshots, capture `llc -print-after=machine-scheduler` or
`-print-after-all`; for MLIR, use `mlir-opt --mlir-print-ir-after-all` with the
desired passes. Use full dumps such as `-print-after-all` when extracting snapshots;
`-print-changed=diff` produces diffs rather than full snapshots.

- LLVM new/legacy pass-manager headers produce `.ll`, MachineIR headers produce
  `.mir`, and MLIR headers produce `.mlir`.
- Files are named `0000_<pass>.ll`, etc. `index.txt` records their order and headers.
  Text before the first recognized header is ignored.
- `Before` dumps start with `0000_initial.<ext>`. Later snapshots are named after
  the previous pass, and `index.txt` records its `After` header; the `Before`
  header is omitted from snapshot contents. The final pass's result is absent
  unless the log also contains an after dump.
  The shift follows log order across functions or operations, so synthesized
  pass/unit labels can be misleading at those boundaries. Prefer after dumps
  when pass attribution matters.
- `-o` / `--output` is required and must name a new or empty directory. Existing
  contents and symlink output directories are refused. Input `-` reads stdin.

Keep the original log. Dumps can contain surrounding diagnostics or incomplete
module/MIR context, so inspect extracted files before replaying them with LLVM tools.
`.mir` snapshots are printer output, not MIR YAML; for a replayable `.mir`, rerun
`llc -stop-after=<pass> -o out.mir`.
