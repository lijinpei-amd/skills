# Structured output directory: what `capture_att_trace.rs` does

## Concepts

- **Storage base** (`${ATT_STORAGE_BASE}`): the directory holding the captures of every kernel. See [Storage base](#storage-base).
- **Kernel id** (`${KERNEL_ID}`): names one kernel across all its versions, unique within a storage base. `^[a-z][a-z0-9_]{0,39}$`. It has no `-`, because `-` separates the fields of a directory name.
- **Version** (`${VERSION}`): `baseline`, then `v1`, `v2`, … (`^(baseline|v[1-9][0-9]{0,3})$`). Every optimization and every test-command change bumps it.
- **Desc** (`${DESC}`): 1 to 5 words joined by `-` that say what the version changed (`^[a-z0-9]+(-[a-z0-9]+){0,4}$`, at most 40 characters, no word like `prof<N>` or `rep<N>`). Required for every version except `baseline`, and forbidden for `baseline`.
- **Capture profile** (`prof<P>`): one exact sequence of rocprofv3 option tokens (same tokens, same order). Numbered per kernel id from 0, so `prof1` means the same rocprofv3 options in every version of the kernel.
- **Repeat** (`rep<R>`): another capture of the same version with the same capture profile. Numbered from 1.
- **Kernel index** (`${KERNEL_ID}/index.toml`): every capture of the kernel, with its version, profile, repeat and verdict. It wins over the captures' own `att_capture.toml` files.
- **Structured output directory**: one capture's directory, named and recorded by this spec.

## Layout

```
${ATT_STORAGE_BASE}/
  ${KERNEL_ID}/
    .lock            -- lock file, see "Index"
    index.toml       -- the kernel index
    <YYYY-MM-DD-HH-MM-SS>-${KERNEL_ID}-${VERSION}[-${DESC}][-prof<P>][-rep<R>]/
      att_capture.toml
      rocprofv3.log, rocprofv3's output
```

## Command line

`$S` = the skill directory, the one containing `SKILL.md`.

```
rust-script $S/scripts/capture_att_trace.rs --capture-att-trace [OPTIONS] -- [ROCPROFV3 OPTIONS...] -- <COMMAND> [ARGS...]
rust-script $S/scripts/capture_att_trace.rs --list-kernels -- [ROCPROFV3 OPTIONS...] -- <COMMAND> [ARGS...]
rust-script $S/scripts/capture_att_trace.rs --list-filtered-kernels -- [ROCPROFV3 OPTIONS...] -- <COMMAND> [ARGS...]
rust-script $S/scripts/capture_att_trace.rs --rebuild-index --kernel-id ID [--storage-base DIR]
```

- Give exactly one mode: `--capture-att-trace`, `--list-kernels`, `--list-filtered-kernels` or `--rebuild-index`. None, or more than one, is an error (exit 2). Only `--capture-att-trace` captures a trace.
- Everything between the two `--` is passed to `rocprofv3` unchanged. These are the *capture options*; they define the capture profile.
- Everything after the second `--` is the *test command*.
- The script adds `--att` (unless it is already given) and `-d <output dir>`. Passing `-d` or `--output-directory` yourself is an error.
- A given `--kernel-iteration-range` is checked before rocprofv3 runs: one that does not parse, has no value, includes dispatch 0, or has a reversed range such as `5-3` is an error, reported as `rocprofv3-options` (exit 2). rocprofv3 counts dispatches from 1, so 0 would select nothing, and it silently selects nothing from a reversed range. Leaving the range out is allowed: ATT then traces each matching kernel's 1st dispatch.
- A glog fatal line (`F<mmdd> …`) from the command or any process it started fails the run; this applies to captures and to every listing run. rocprofv3 can hang in its signal handler after a failed profiling setup, and the hung process can be a worker of a launcher such as `bash run.sh` or `torchrun`, even one that has already exited: the script is the subreaper of the command's processes, so it keeps watching them until all are gone.
  - Processes still running 30 s after the line are killed: `FAILED: rocprofv3 killed after a fatal error`, exit code 137.
  - All of them exited by themselves but the command's exit code is 0: `FAILED: rocprofv3 fatal error`, exit code 134, the code of the abort a fatal error causes.
- Without a fatal line, if a background process the command started keeps its stdout or stderr open after the command exits, the script stops waiting for it 10 s later and logs `output may be incomplete: …`, which makes the verdict at best `OK WITH WARNINGS`.

| Option | Allowed values | Notes |
| --- | --- | --- |
| `--capture-att-trace` | | Capture the trace. `--version`, `--desc`, `--output-dir`, `--note` and `--dry-run` are used only with it |
| `--kernel-id ID` | see [Concepts](#concepts) | Required in structured mode |
| `--version VER` | see [Concepts](#concepts) | Required in structured mode |
| `--desc WORDS` | see [Concepts](#concepts) | Required except for `baseline`; forbidden for `baseline` |
| `--storage-base DIR` | an existing directory, or a path that can be created | Overrides the configured storage base |
| `--output-dir DIR` | a path that does not exist, or an empty directory | [Explicit mode](#explicit-mode) |
| `--note TEXT` | any text | Recorded in `att_capture.toml` and the index |
| `--dry-run` | | Validate, pick the directory, print the JSON, and stop. Creates nothing |
| `--rebuild-index` | | Rebuild the kernel index; see [Recovery](#recovery) |
| `--list-kernels` | | Capture nothing; list the kernels the command dispatches, ignoring the kernel filters. See [Listing kernels](#listing-kernels) |
| `--list-filtered-kernels` | | Like `--list-kernels`, plus the dispatches the kernel filters select. See [Listing kernels](#listing-kernels) |

`--kernel` and `--root` were renamed to `--kernel-id` and `--storage-base`; the old names are errors.

## Storage base

The first of:

1. `--storage-base DIR`;
2. `storage-base` in `${XDG_CONFIG_HOME:-~/.config}/capture_att_trace/config.toml` (an absolute path, or one starting with `~/`);
3. `${XDG_DATA_HOME:-~/.local/share}/capture_att_trace`.

The resolved path is `storage-base` in the output. None of these depends on the current directory, so captures you compare always share one base.

## Directory name

```
${ATT_STORAGE_BASE}/${KERNEL_ID}/<YYYY-MM-DD-HH-MM-SS>-${KERNEL_ID}-${VERSION}[-${DESC}][-prof<P>][-rep<R>]
```

- The timestamp is UTC, taken when the script runs. A dry run therefore shows the timestamp of the dry run; the profile and repeat it shows are the ones a capture would get.
- **`P`:** the id of the index's capture profile with exactly these capture options. Without one, a new profile gets one more than the largest id, starting at 0. `-prof0` is left out.
- **`R`:** one more than the largest `rep` among the index's captures of this version with profile `P`, starting at 1. `-rep1` is left out.
- If the name already exists, the script waits one second and takes a new timestamp.

Examples, with the timestamp left out:

| Name | Meaning |
| --- | --- |
| `gemm_bf16-baseline` | First capture of the baseline |
| `gemm_bf16-baseline-rep2` | Same code, command and capture options as above, captured again |
| `gemm_bf16-baseline-prof1` | Same code and command, the kernel's second capture profile |
| `gemm_bf16-v2-lds-swizzle-prof1-rep2` | Second capture of `v2` with the kernel's second capture profile |

## Index

`${KERNEL_ID}/index.toml`:

```toml
schema = 1
kernel-id = "gemm_bf16"

[[profile]]
id = 1
rocprofv3-options = ["--kernel-include-regex", "gemm_bf16_tn", "--kernel-iteration-range", "3"]

[[version]]
name = "v2"
desc = "lds-swizzle"
command = ["./bench_gemm", "--m", "8192"]

[version.isa-kernels]   # merged from v2's captures, by demangled kernel name
"void gemm_bf16_tn_kernel<256, 128>(…)" = ["sha256:…"]

[[capture]]
dir = "2026-11-08-14-03-22-gemm_bf16-v2-lds-swizzle-prof1-rep2"
version = "v2"
profile = 1
rep = 2
started = 2026-11-08T14:03:22Z
verdict = "OK"
isa-hash = "sha256:…"
note = "XOR swizzle on the LDS B tile"
```

- **The index wins.** `att_capture.toml` repeats the same fields for its capture, but the script reads only the index, except in `--rebuild-index`.
- **Locking.** Every read-modify-write of the index holds an exclusive `flock` on `${KERNEL_ID}/.lock`. The index is written to a temp file and renamed into place.
- **Write order.** Under the lock, the script adds the capture to the index with `verdict = "PENDING"`, which reserves its profile and repeat numbers, then creates the directory and writes `att_capture.toml`. It releases the lock while rocprofv3 runs, then takes it again to record the verdict and `isa-hash` in both files: the index first, then `att_capture.toml`. Each file is written to a temp file and renamed into place, so an interrupted write leaves its previous version intact.
- **Numbers are never reused.** Failed captures, and `PENDING` captures left by a crash, keep their entries and their numbers, also through `--rebuild-index`.

### Recovery

If `index.toml` cannot be parsed, or is missing while capture directories exist, the script exits 1 and names `--rebuild-index`. `--rebuild-index` rebuilds the index from the captures' `att_capture.toml` files, under the lock, and copies an existing index to a new `index.toml.bak-<timestamp>` first (`-2`, `-3`, … when that name exists). A capture that a readable old index records but no `att_capture.toml` rebuilds (interrupted before that file or its directory was written, or with either lost since) keeps its entry, with its profile and version declarations, so its numbers stay reserved. The old index merges hashes per version, not per capture, so the kept entry's version also keeps all its recorded `isa-kernels`. The same holds for a capture whose `att_capture.toml` has no `result` (its run stopped between the final index write and the final `att_capture.toml` write) while the old index records it as finished: the old entry, with its verdict and its version's hashes, wins over the initial metadata. With no readable old index, such a capture is rebuilt as `PENDING`, without hashes. It refuses, with exit code 1 and every problem listed in `error.messages["--rebuild-index"]`, when:

- two captures claim the same version, profile and repeat;
- two captures, or a capture and a kept entry, disagree on a profile's options or on a version's desc or test command;
- a kept entry's version records hashes for a kernel that neither contain nor are contained in the union of those the captures' `att_capture.toml` files recorded. Recorded captures are not checked against each other: each passed the consistency check against the hashes accepted when it finished, and captures can finish in a different order than their directory names sort;
- a kept entry's profile or version is not declared in the old index;
- an `att_capture.toml` cannot be parsed or lacks a field;
- a capture directory has no `att_capture.toml` and no readable old index records it, so its identity cannot be recovered automatically. The message says what the directory holds, which may be a trace: inspect it, and move it out of the kernel directory to rebuild without it.

## Consistency checks

Before the capture, the new capture must agree with the index's entry for its version. On a mismatch the script exits 2 and creates nothing:

| Check | Error on |
| --- | --- |
| Same `desc` | `--desc`: reuse the recorded desc, or bump `--version` |
| Same test command (same tokens) | `--version`: a test-command change bumps the version |

After the capture, the script compares the kernel code:

- **`isa-hash`:** with `--att`, rocprofv3 writes every loaded code object as `<gpu>_code_object_id_<N>.out`. The script finds the kernels that `--kernel-include-regex` (minus `--kernel-exclude-regex`) selects and hashes their machine-code bytes; a kernel is a function symbol with a `<name>.kd` descriptor beside it. The regexes are matched against the name rocprofv3's filters see under the capture's `-M`/`-T` options: the demangled name by default, the `<symbol>.kd` with `-M` alone, the truncated name with `-T`. The hash input pairs each kernel's demangled name with its hash, whatever `-M`/`-T` say. Debug info does not change these bytes unless it changes codegen.
- **Same version, different machine code:** for each kernel this capture hashed that the version's `isa-kernels` already records, one set of hashes must contain the other. A JIT kernel can load a different number of variants per run (Triton autotune with cached results loads only the winner), so a subset is the same code, while a hash on each side that the other lacks is a code change. Kernels only one side hashed are not compared, so captures whose filters select different kernels, such as different capture profiles, do not conflict. On a mismatch: verdict `INCONSISTENT: version reused for different code`, exit code 5, and a message in `result.consistency-errors` naming the kernels. The capture stays in the index; capture again with a new `--version`. Otherwise the capture's hashes are merged into `isa-kernels`, unless rocprofv3 failed or was killed: such a run may have loaded only some variants, so it is compared but not recorded.
- **Another version with the same test command and, for every kernel this capture hashed, nested hash sets as above:** a warning in `result.consistency-warnings`.
- **An index from before per-kernel hashes** has a combined `isa-hash` per version instead of `isa-kernels`. A capture whose combined hash differs gets a warning, not an error, since a different kernel selection also changes it. `--rebuild-index` rebuilds `isa-kernels` as the union of the `result.isa-kernels` of the captures whose `result.isa-recorded` is true. For a capture written before that field, it takes those with rocprofv3 exit code 0, no `result.consistency-errors`, and a verdict other than `INCONSISTENT` or `FAILED: rocprofv3 fatal error`.
- **No hash** (no `--kernel-include-regex`, a regex Rust cannot compile, or no matching kernel): a warning in `result.consistency-warnings`, and the check is skipped.

## Explicit mode

`--output-dir DIR` opts out of everything structured: no storage base, no index, no numbering, no consistency checks, no `isa-hash`. Combining it with `--kernel-id`, `--version`, `--desc`, `--storage-base` or `--rebuild-index` is an error. The directory still gets `att_capture.toml` (with `structured = false`), and stdout still carries the JSON.

## Listing kernels

`--list-kernels` and `--list-filtered-kernels` run the test command under `rocprofv3 --kernel-trace --output-format csv` in a new directory under the system temp directory, and print the kernels it dispatched, named as `--kernel-include-regex` sees them. They write no `att_capture.toml` and touch no index. Combining either with another mode, `--kernel-id`, `--version`, `--desc`, `--storage-base`, `--output-dir`, `--note` or `--dry-run` is an error.

Of the rocprofv3 options, `--list-kernels` passes on only `-M`/`--mangled-kernels` and `-T`/`--truncate-kernels` (each with its optional BOOL). `--list-filtered-kernels` also passes on the kernel filters `--kernel-include-regex`, `--kernel-exclude-regex` and `--kernel-iteration-range`, and checks `--kernel-iteration-range` as a capture does. Either way the capture options can be passed unchanged; the rest go to `ignored-rocprofv3-options`.

**Selection.** `--list-filtered-kernels` also collects `--pmc CU_NUM`, a constant from the agent properties that needs no hardware counter packets and that rocprofiler-sdk defines for gfx9 and gfx1250 (`MAX_WAVE_SIZE` is not defined for gfx1250). You do not pass `--pmc` or `--kernel-trace`; the script adds both. rocprofv3 applies the same kernel filters to counter collection as to ATT, so the dispatches in its `counter_collection.csv` are exactly those an ATT capture with these options traces. Without `--kernel-iteration-range`, the script adds `--kernel-iteration-range 1`: ATT without a range traces each kernel symbol's 1st dispatch, while counter collection would count every dispatch.

With no kernel filter, the selection is what an unfiltered ATT capture traces: each kernel symbol's 1st dispatch.

**Fallback.** If that run fails, a plain `--kernel-trace` run follows and the selection is approximated: the Rust `regex` crate stands in for rocprofv3's regex engine, and the dispatch-id order within each kernel symbol for its iteration count.

The temp directory is removed on `OK` and kept, as `output-dir`, otherwise.

| Field | Content |
| --- | --- |
| `rocprofv3-command`, `ignored-rocprofv3-options`, `rocprofv3-exit-code` | The run the listing comes from, the options left out of it, and its exit code |
| `kernels` | One entry per name, by `total-ns` descending: `name`, `dispatches`, `total-ns`, `percent` (of all kernels' `total-ns`), `dispatches-per-symbol` (one count per loaded kernel symbol, the unit `--kernel-iteration-range` counts in) and, with `--list-filtered-kernels`, `selected` and `selected-dispatch-ids` |
| `selection-source` | `--list-filtered-kernels` only: `rocprofv3`, or `approximation` for the fallback |
| `kernel-iteration-range`, `selected-kernels`, `selected-dispatches` | `--list-filtered-kernels` only: the range applied (`1 (the ATT default)` when none was given), and how many names and dispatches it selected |
| `filter-errors` | `approximation` only: a regex the `regex` crate cannot compile, or an invalid range |
| `warnings` | Why the selection was approximated, with the failed run's log |
| `output-dir` | The kept temp directory, when the verdict is not `OK` |
| `verdict`, `exit-code` | `OK`, `OK WITH WARNINGS` (0), `FAILED: no kernel_trace.csv` (3), `FAILED: rocprofv3 killed after a fatal error` (137), `FAILED: rocprofv3 fatal error` (134), or `FAILED: rocprofv3 failed` (rocprofv3's exit code) |

## Files in the output directory

| File | Content |
| --- | --- |
| `att_capture.toml` | Metadata, written before the capture and again with `result` after it |
| `rocprofv3.log` | rocprofv3's stdout and stderr (also streamed to the script's stderr) |
| everything else | rocprofv3's output: `stats_*.csv`, `ui_output_agent_*_dispatch_*/`, `<host>/…*.att`, `*_code_object_id_*.out` |

## `att_capture.toml` and stdout

stdout carries exactly one JSON object. On success, a dry run, or a failed capture, it holds the same fields as `att_capture.toml`. Absent fields are left out in both (TOML has no null).

| Field | Content |
| --- | --- |
| `schema`, `output-dir`, `structured` | `schema` is 1 |
| `storage-base`, `kernel-id`, `version`, `desc`, `profile`, `rep` | Structured mode only |
| `note`, `rocprofv3-options`, `command`, `rocprofv3-command` | The note, the capture options, the test command, and the full command run |
| `dry-run` | `true` only for `--dry-run`. A dry run has no environment fields and no `result` |
| `started`, `host`, `cwd`, `env`, `gpu-archs`, `rocm-version`, `rocprofv3-version`, `decoder` | The environment. `started` is UTC. `env` holds `HIP_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` and `ROCM_PATH` |
| `setup-warnings` | For example, the decoder library was not found |
| `isa-hash` | Structured mode, when computed |
| `result.verdict` | `OK`, `OK WITH WARNINGS`, `INCONSISTENT: version reused for different code`, `UNUSABLE: empty stats`, `FAILED: no ui_output directory`, `FAILED: rocprofv3 killed after a fatal error`, `FAILED: rocprofv3 fatal error`, or `FAILED: rocprofv3 failed` |
| `result.exit-code`, `result.rocprofv3-exit-code` | The script's exit code and rocprofv3's |
| `result.att-files`, `result.ui-output-dirs`, `result.stats` | What was produced. Each `stats` entry is `{file, instruction-rows}` |
| `result.log-warnings` | Up to 15 `{count, line}` entries: log lines matching `buffer full`, `data lost`, `incomplete`, `invalid_shader_data`, `could not fetch`, `not loaded`, `decoder`, `error` or `counter not found` (case-insensitive), and glog fatal lines (`F<mmdd> …`) |
| `result.isa-kernels`, `result.consistency-errors`, `result.consistency-warnings` | Structured mode. `isa-kernels` lists `{name, hash}` for each hashed kernel |
| `result.isa-recorded` | Structured mode: whether this capture's hashes were merged into its version's `isa-kernels`. `--rebuild-index` replays it |

When several verdicts apply, the first in this order wins: `FAILED`, `UNUSABLE`, `INCONSISTENT`, `OK WITH WARNINGS`, `OK`. `OK WITH WARNINGS` means `setup-warnings`, `result.log-warnings` or `result.consistency-warnings` is not empty.

`--rebuild-index` prints `{index, backup, profiles, versions, captures}` instead, and `--list-kernels` and `--list-filtered-kernels` the fields in [Listing kernels](#listing-kernels).

### On errors

```json
{"error": {"options": ["--desc", "--version"], "messages": {"--desc": ["..."], "--version": ["..."]}}}
```

`error.options` lists the problematic options. The capture options are reported as `rocprofv3-options` and the test command as `command`. Internal errors (exit 1) have an empty `options` and the message in `messages.internal`. No directory is created on invalid options.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | `OK`, `OK WITH WARNINGS`, a dry run, a rebuilt index, or listed kernels |
| 1 | Internal error, including an unreadable or missing index (`error.messages.internal`) and a refused `--rebuild-index` |
| 2 | Invalid options, including a desc or test command that disagrees with the index |
| 3 | `FAILED: no ui_output directory`; when listing kernels, `FAILED: no kernel_trace.csv` |
| 4 | `UNUSABLE: empty stats` (some `stats_*.csv` has no instruction rows) |
| 5 | `INCONSISTENT: version reused for different code` |
| 134 | `FAILED: rocprofv3 fatal error`: a fatal line, but exit code 0 |
| 137 | `FAILED: rocprofv3 killed after a fatal error` |
| other | `FAILED: rocprofv3 failed`, with rocprofv3's exit code (128 + signal if it was killed). `result.verdict` is authoritative when the code is ambiguous |
