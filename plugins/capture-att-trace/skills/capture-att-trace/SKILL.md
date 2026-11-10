---
name: capture-att-trace
description: "Use when capturing an AMD GPU thread trace (ATT / SQTT, `rocprofv3 --att`) of a kernel on CDNA3 (MI300X, MI300A, MI325X, gfx942), CDNA4 (MI350X, MI355X, gfx950) or CDNA5 (MI450, gfx1250). Typical cases: tracing a kernel to find where its time goes (stalls, waits, idle, busy pipes, hot source lines) and so pick the next optimization direction; tracing a new optimization version to compare it with an earlier capture; producing a ui_output directory for ROCprof Compute Viewer."
---

# Capture an ATT trace

**In scope:** capturing an ATT trace of a kernel with `rocprofv3 --att`, including collecting hardware performance counters inside the trace as [ATT PerfEvent](https://github.com/ROCm/rocprofiler-sdk/blob/f4cce0d2d40068162166e42f526c644df6ecd7b6/source/include/rocprofiler-sdk/experimental/thread-trace/trace_decoder_types.h#L178) records, described in [att_perfevent](references/att_perfevent.md).

**Out of scope:** analyzing a captured trace; [PC (Program Counter) sampling](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/how-to/using-pc-sampling.html); and the other way AMD GPUs collect hardware performance counters, [PMC](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/how-to/using-rocprofv3.html#kernel-counter-collection) (`rocprofv3 --pmc`).

`$S` = the directory containing this SKILL.md.

## Steps

### 1. Decide the command to trace

- **The command line:** capture the same thing you test. When developing or optimizing a kernel, the test or benchmark command and the capture command must run the same kernel binary or assembly, with the same grid, block and inputs.
- **Debug info:** build the test and the capture with the same debug-info flags. `-gline-tables-only` (or `-g`) maps instructions to source lines, but it can, rarely, change the generated code or the ELF section layout, and so the performance. Measuring that impact is a separate task, outside this skill.

### 2. Filter the kernels to trace

A program usually dispatches several kernels, each identified by its name, and may dispatch each one many times, interleaved with the others.

Repeated dispatches of the same kernel can differ in performance even with the same grid, block and inputs: a typical benchmark runs warm-up iterations, then steady-state ones. Capture the run the user asks for; when they name none, capture a steady-state run.

Filter the target kernel by name and iteration range.

#### 2.1 Kernel Filter Options

- `-M [BOOL]` | `--mangled-kernels [BOOL]`: show and match the raw symbol, with its `.kd` suffix, instead of the demangled name. `-M` alone means true.
- `-T [BOOL]` | `--truncate-kernels [BOOL]`: show and match only the function name: the demangled name without return type, namespaces, template arguments and parameter list. It overrides `-M`.
- `--kernel-include-regex REGULAR_EXPRESSION`: trace only kernels whose name contains a match. Default `.*`, every kernel.
- `--kernel-exclude-regex REGULAR_EXPRESSION`: of the included kernels, drop those whose name contains a match. Default: none dropped.
- `--kernel-iteration-range KERNEL_ITERATION_RANGE [KERNEL_ITERATION_RANGE …]`: which dispatches of each selected kernel to trace, counted from 1. A range is `N` or `N-M`, both ends included. Several ranges add up: `3 10-12` and `"[3,10-12]"` both select dispatches 3, 10, 11 and 12.

The filters see the name that `--kernel-trace` prints with the same `-M`/`-T` flags, so use the same `-M`/`-T` in the name-discovery run and in the capture. For one kernel:

| Flags | Name shown and matched |
| --- | --- |
| none (default) | `void gemm_bf16_tn_kernel<256, 128>(float const*, float const*, float*, int)` |
| `-M` | `_Z19gemm_bf16_tn_kernelILi256ELi128EEvPKfS1_Pfi.kd` |
| `-T`, or `-M -T` | `gemm_bf16_tn_kernel` |

`--kernel-rename` never changes what the filters see. It only replaces kernel names in the output with ROCTx range names, so don't copy a name from a run that used it.

- **The name filter comes first.** The regexes match any substring of the name. For a full match, anchor the regex: `^regex$`. The match is against the name determined from `-M`/`-T` flags.
  - `--kernel-include-regex gemm_bf16_tn` matches `void gemm_bf16_tn_kernel<256, 128>(…)`. `--kernel-include-regex gemm` matches it too, and also every other kernel with `gemm` anywhere in its name, such as `splitk_gemm_reduce(…)`.
  - `( ) [ ] * + ? . | ^ $` are regex syntax. Escape them to match the full name: `'gemm_bf16_tn_kernel<256, 128>\(float const\*'`. An unescaped, unmatched `(` makes the regex invalid.
  - `--kernel-include-regex gemm_bf16 --kernel-exclude-regex _nn_`: traces `gemm_bf16_tn_kernel`, not `gemm_bf16_nn_kernel`.
- **The count is per matching kernel.** Only that kernel's dispatches are counted. Make the regex match only the kernel you want. Each iteration of `./bench_gemm --iters 20` dispatches tn (`gemm_bf16_tn_kernel`), then bias (`bias_add_kernel`):

  | Kernels dispatched | Options | Traces |
  | --- | --- | --- |
  | tn 1, bias 1, tn 2, bias 2, tn 3, bias 3, … | `--kernel-include-regex gemm_bf16_tn --kernel-iteration-range 3` | tn 3 |
  | tn 1, bias 1, tn 2, bias 2, tn 3, bias 3, … | `--kernel-include-regex _kernel --kernel-iteration-range 3` | tn 3, bias 3 |

- **The count is per loaded kernel symbol.** Each GPU that loads the code object, and each reload or JIT recompile of the kernel, starts its own count from 1:

  | Kernels dispatched | Options | Traces |
  | --- | --- | --- |
  | tn on GPU 0 and on GPU 1 | `--kernel-include-regex gemm_bf16_tn --kernel-iteration-range 3` | tn 3 on each GPU |
  | `matmul_kernel` JIT-compiled for 4 Triton autotune configs | `--kernel-include-regex matmul_kernel --kernel-iteration-range 3` | the 3rd dispatch of each config that ran at least 3 times, up to 4 |

- **Without a range,** ATT traces only run 1 of each matching kernel. A range including 0 would select nothing, so the script refuses it (exit 2).
  - `--kernel-include-regex gemm_bf16_tn`: traces tn 1, a warm-up run.

Pick the runs to trace. The examples use the same bench:

- **A steady-state run:** `--kernel-iteration-range N`. The command must launch the kernel at least N times.
  - `--kernel-include-regex gemm_bf16_tn --kernel-iteration-range 15`: traces tn 15. Needs `--iters 15` or more.
- **Several runs:** `--kernel-iteration-range N-M`. Each run is a separate trace.
  - `--kernel-include-regex gemm_bf16_tn --kernel-iteration-range 10-12`: traces tn 10, 11 and 12, one `ui_output_agent_*_dispatch_*` directory each.
- **The target run plus the dispatches after it, in one trace:** `--att-consecutive-kernels N`. The trace starts at the dispatch the filters select and covers N dispatches in total, the following ones whatever their kernel name. Another selected dispatch inside the trace restarts the count, so the trace can hold more than N: select a single run (`--kernel-iteration-range N`, not `N-M`) when the trace must hold exactly N dispatches.
  - `--kernel-include-regex gemm_bf16_tn --kernel-iteration-range 3 --att-consecutive-kernels 2`: one trace covering tn 3 and bias 3, in the `ui_output_agent_*_dispatch_*` directory of tn 3.

#### 2.2 Test the Kernel Filter Options

- If you don't know the name, list the kernels the command dispatches, most total time first, named as the filters see them. Pass the rocprofv3 options you will capture with, without `--att`, `--pmc` or `--kernel-trace`; the kernel filters among them are ignored:

  ```bash
  rust-script $S/scripts/capture_att_trace.rs --list-kernels -- -- ./bench_gemm --iters 20
  ```

- To see which dispatches the filters select, use `--list-filtered-kernels` with the same options:

  ```bash
  rust-script $S/scripts/capture_att_trace.rs --list-filtered-kernels -- \
    --kernel-include-regex 'gemm_bf16_tn_kernel' --kernel-iteration-range 15 \
    -- ./bench_gemm --iters 20
  ```

  It prints (trimmed):

  ```json
  {
    "kernels": [
      {"name": "gemm_bf16_tn_kernel", "dispatches": 20, "percent": 91.3, "dispatches-per-symbol": [20], "selected": true, "selected-dispatch-ids": [29]},
      {"name": "bias_add_kernel", "dispatches": 20, "percent": 8.7, "dispatches-per-symbol": [20], "selected": false, "selected-dispatch-ids": []}
    ],
    "selected-dispatches": 1,
    "verdict": "OK"
  }
  ```

  - Capture when the kernels with `"selected": true` are the traces you want. Otherwise change the filters and list again.
  - Verdict `OK WITH WARNINGS`: the selection is approximate; read `warnings`.

#### 2.3 Advanced Kernel Filter Options

You can also use [ROCTx](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/latest/how-to/using-rocprofiler-sdk-roctx.html) to annotate code with markers and ranges to be traced (`--selected-regions`), see [roctx_usage](references/rocprofv3_roctx.md).

You can also trace only the dispatches in a time window (`--collection-period`), see [collection_period](references/collection_period.md).


### 3. Decide Output Directory

The options of this step belong to the script, not to rocprofv3: they go before the first `--`.

If the user names an output directory, pass it as `--output-dir DIR`, which must not exist or must be an empty directory. This opts out of everything structured below. Otherwise, use the [structured output directory](#31-structured-output-directory).

#### 3.1 Structured Output Directory

The script files each capture under `<storage base>/<kernel-id>/` and records it in that kernel's `index.toml`, so accumulated captures stay identifiable. Make these decisions and pass them to `scripts/capture_att_trace.rs`:

- **`--kernel-id ID`:** one descriptive name for every version of one kernel. It identifies the kernel and need not equal `--kernel-include-regex`. Before inventing one, list the storage base (`storage-base` in the `--dry-run` output) and reuse this kernel's existing id. Allowed: `^[a-z][a-z0-9_]{0,39}$`.
- **`--version VER`:** `baseline`, then `v1`, `v2` and so on. Every optimization and every test-command change (cache flushing, warm-up or steady repeat count) bumps it. The versions so far are the `[[version]]` entries of `<storage base>/<kernel-id>/index.toml`. Allowed: `^(baseline|v[1-9][0-9]{0,3})$`.
- **`--desc WORDS`:** for every version except `baseline`, which takes none: a few words, just enough to recall what changed in this version. Allowed: `^[a-z0-9]+(-[a-z0-9]+){0,4}$`, at most 40 characters, no word like `prof<N>` or `rep<N>`.
- **`--storage-base DIR`:** only when the user names one. The default comes from `${XDG_CONFIG_HOME:-~/.config}/capture_att_trace/config.toml`, else `${XDG_DATA_HOME:-~/.local/share}/capture_att_trace`.

The script picks the rest of the name, `<YYYY-MM-DD-HH-MM-SS, UTC>-<kernel-id>-<version>[-<desc>][-prof<P>][-rep<R>]`: `-prof<P>` numbers each distinct sequence of rocprofv3 options per kernel id, and `-rep<R>` numbers repeats of the same version and profile; `-prof0` and `-rep1` are left out. It refuses a version that the index recorded with a different `--desc` or a different test command.

On invalid options the script exits 2 and creates no directory; `error.options` lists the problematic options and `error.messages` says why for each. Otherwise, `output-dir` in the output is the absolute path of the directory it picked. `--dry-run` prints the same without capturing. The timestamp is taken when the script runs, so a later real capture gets a later timestamp; the rest of the name is the same.

See [the spec](references/structured_output_directory_spec.md) for name examples, the index, locking, recovery and every output field.

### 4. Run `capture_att_trace.rs`

```bash
rust-script $S/scripts/capture_att_trace.rs --capture-att-trace \
  --note "XOR swizzle on the LDS B tile" \
  --kernel-id gemm_bf16 \
  --version v2 \
  --desc lds-swizzle \
  -- \
  --kernel-include-regex 'gemm_bf16_tn_kernel' --kernel-iteration-range 15 \
  -- \
  ./bench_gemm --m 8192 --n 8192 --k 8192 --iters 20
```

- **Before the first `--`:** the script options from step 3.
- **Between the two `--`:** the rocprofv3 options from step 2, plus any for your architecture from [per_architecture_options](references/per_architecture_options.md). Read it before passing any `--att-*` or `--spm*` option: CDNA3/CDNA4 and CDNA5 take different values. The script adds `--att` and `-d` itself; `--pmc` and `--kernel-trace` stay out.
- **After the second `--`:** the test command from step 1.
- **Other options:** `--help` lists all options.
- **First run:** needs `rust-script` (`cargo install rust-script`), Rust 1.89 or later, and access to crates.io. rust-script compiles the script, which takes about a minute. Later runs start immediately.
- **Output:** stdout is one JSON object, the same content as `att_capture.toml`. rocprofv3's own output goes to stderr. The verdict is in `result.verdict`:

  | Verdict | Exit code |
  | --- | --- |
  | `OK` | 0 |
  | `OK WITH WARNINGS` (`setup-warnings`, `result.log-warnings` or `result.consistency-warnings` is not empty) | 0 |
  | `INCONSISTENT: version reused for different code` | 5 |
  | `UNUSABLE: empty stats` | 4 |
  | `FAILED: no ui_output directory` | 3 |
  | `FAILED: rocprofv3 killed after a fatal error` | 137 |
  | `FAILED: rocprofv3 fatal error` (a fatal line, yet exit code 0) | 134 |
  | `FAILED: rocprofv3 failed` | rocprofv3's exit code |
  | invalid options: `error` instead of `output-dir`, and no directory is created | 2 |
  | internal error, such as an unreadable `index.toml` (`error.messages.internal`) | 1 |

  - `UNUSABLE` or `FAILED`: fix the cause with [troubleshooting](references/troubleshooting.md) and run again. The new run gets a new directory.
  - `INCONSISTENT`: a kernel's machine code differs from earlier captures of this version that hashed the same kernel (`result.consistency-errors` names it). Capture again with the next `--version` and a `--desc` for the change.
  - `OK WITH WARNINGS`: read the warnings. Fix and re-run for those listed in [troubleshooting](references/troubleshooting.md). Lines from the app's own output that match the filter can be ignored. A `consistency-warnings` entry saying another version has the same kernel code and test command means this version changed nothing; check that the build picked up the change.
  - An index error names `--rebuild-index`: run `rust-script $S/scripts/capture_att_trace.rs --rebuild-index --kernel-id ID`, then capture again.

### 5. Report status

Report:
1. the capture directory;
2. the verdict (`result.verdict`) and any warnings (`setup-warnings`, `result.log-warnings`, `result.consistency-warnings`);
3. the `ui_output_agent_*_dispatch_*` directory, which opens in ROCprof Compute Viewer with `rocprof-compute-viewer <ui dir>`;
4. for a retry, what you changed since the failed run.

## References

- [structured_output_directory_spec](references/structured_output_directory_spec.md): every option, output field and exit code of `capture_att_trace.rs`, plus the directory layout, index, recovery and consistency checks.
- [per_architecture_options](references/per_architecture_options.md): `--att-*` and SPM option values for CDNA3, CDNA4 and CDNA5.
- [troubleshooting](references/troubleshooting.md): symptoms and fixes for `FAILED`, `UNUSABLE` and warning verdicts.
- [att_perfevent](references/att_perfevent.md): sampling SQ counters inside the trace (ATT PerfEvent) on CDNA3 and CDNA4.
- [rocprofv3_roctx](references/rocprofv3_roctx.md): tracing a code region marked with ROCTx (`--selected-regions`).
- [collection_period](references/collection_period.md): tracing only the dispatches in a time window (`--collection-period`).
