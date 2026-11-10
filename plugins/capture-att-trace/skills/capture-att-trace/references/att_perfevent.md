# Collecting ATT PerfEvent

On gfx9 GPUs (CDNA3 gfx942, CDNA4 gfx950), an ATT capture can also sample up to 8 SQ hardware counters into the trace buffer. These samples are PerfEvent records, timestamped on the same shader clock as the traced instructions. gfx1250 and RDNA GPUs have no PerfEvent; use SPM there (see [per_architecture_options](per_architecture_options.md)).

This file covers collection only. Add the options below between the two `--` of the capture command, next to the kernel filters. Any change to them gives the capture a new capture profile (`-prof<P>`).

## Options

| Option | Value | Notes |
| --- | --- | --- |
| `--att-activity N` | period, 1 to 16; docs recommend 8 | Preset: the period plus 8 activity counters (`SQ_BUSY_CU_CYCLES SQ_VALU_MFMA_BUSY_CYCLES SQ_ACTIVE_INST_VALU SQ_ACTIVE_INST_LDS SQ_ACTIVE_INST_VMEM SQ_ACTIVE_INST_FLAT SQ_ACTIVE_INST_SCA SQ_ACTIVE_INST_MISC`). It cannot be combined with `--att-perfcounters` or `--att-perfcounter-ctrl` |
| `--att-perfcounters "SQ_A SQ_B:MASK …"` | up to 8 space-separated `SQ_` counters | Basic SQ counters of the GPU, listed by `rocprofv3 -L`. `:MASK` is a hex SIMD mask, default `0xF` (all 4 SIMDs); `:2` counts SIMD 1 only. The same name with the same mask twice is collected once |
| `--att-perfcounter-ctrl N` | period, 1 to 32; docs recommend 2 to 8 | Lower samples more often and uses more trace bandwidth |
| `--att-perfcounter-target-only` | optional BOOL | Counts only on the target CU (`--att-target-cu`) instead of every CU of each traced SE. Cuts bandwidth, so a low period becomes affordable |

`--att-perfcounters` and `--att-perfcounter-ctrl` go together: give both, or use `--att-activity` instead.

## Gotchas

- **One of the pair alone:** rocprofv3 prints the fatal line `ATT Perf requires setting both perfcounter_ctrl and perfcounter list!` and then hangs. The capture script kills it 30 s later: `FAILED: rocprofv3 killed after a fatal error`.
- **With `--pmc`:** rocprofv3 refuses to start (`ATT perfcounters cannot be enabled with PMC`, or `ATT activity …`), exit 1. PerfEvent and PMC are separate captures.
- **A counter name the GPU lacks:** only a warning, `counter not found: NAME`, and the capture ends `OK WITH WARNINGS` with that line in `result.log-warnings`. The name stays in `counter_names`, but the counters after it move up one slot: from that name on, each column carries the data of the counter listed after it, and the last column stays 0. Recapture without that name.
- **`Data Lost` or buffer-full warnings:** samples share the ATT buffer with the instruction trace. Raise the period, add `--att-perfcounter-target-only`, or collect fewer counters.

## Sample spacing

Spacing grows with the period and with the number of counters, rounded up to 4. Measured on gfx950, in trace clock ticks:

| Options | Spacing |
| --- | --- |
| `--att-activity 8` (8 counters, every CU of SE 0) | 288 |
| `--att-perfcounter-ctrl 2`, 3 counters, `--att-perfcounter-target-only` | 72 |

The thread-trace blog gives period 1 as about 40 cycles for up to 4 counters and about 80 for more.

## Examples

- Activity overview of the kernel:
  `--att-activity 8`
- Chosen counters on the target CU:
  `--att-perfcounter-ctrl 2 --att-perfcounters "SQ_INSTS_VALU SQ_LDS_BANK_CONFLICT SQ_BUSY_CYCLES" --att-perfcounter-target-only`
- LDS bank conflicts per SIMD, one counter per SIMD mask:
  `--att-perfcounter-ctrl 1 --att-perfcounters "SQ_LDS_BANK_CONFLICT:1 SQ_LDS_BANK_CONFLICT:2 SQ_LDS_BANK_CONFLICT:4 SQ_LDS_BANK_CONFLICT:8" --att-perfcounter-target-only`

## Check the capture

The PerfEvent capture is done when every `ui_output_agent_*_dispatch_*` directory passes all three checks:

1. `filenames.json` has `counter_names`: the requested counters in order, with any `:MASK` other than `0xF`.
2. Each traced SE has a `se<N>_perfcounter.json` whose `data` is not empty. Each row is `[time, c0, c1, c2, c3, CU, bank]`. Bank 0 holds counters 1 to 4 of `counter_names` and bank 1 holds counters 5 to 8. With `--att-perfcounter-target-only`, the `CU` column holds only the target CU.
3. `rocprofv3.log` has no `counter not found` and no `Data Lost` line.
