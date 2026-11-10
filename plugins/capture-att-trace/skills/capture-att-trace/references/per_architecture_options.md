# Per-architecture options

Find the GPU with `rocminfo | grep -m1 -oE 'gfx[0-9a-f]+'` and use the matching column.

| | CDNA3 | CDNA4 | CDNA5 |
| --- | --- | --- | --- |
| gfx / GPUs | gfx942: MI300X, MI300A, MI325X | gfx950: MI350X, MI355X | gfx1250: MI450 series |
| Instruction-level detail from | the target CU, in each traced shader engine (SE) | the target CU, in each traced SE | one SIMD of the target CU, in each traced SE |
| `--att-target-cu` (default 1) | CU index | CU index | WGP index (1 WGP = 1 CU on gfx1250) |
| `--att-simd-select` | bitmask of the 4 SIMDs, default `0xF` | bitmask, default `0xF` | one SIMD **ID**, `0` to `3`. It is not a mask, so never pass `0xF` |
| Counters inside the trace | ATT PerfEvent: `--att-activity 8`, or chosen `SQ_` counters. Read [att_perfevent](att_perfevent.md) before adding these options | same as CDNA3 | **none**: the `--att-perf*` and `--att-activity` flags are gfx9-only. Use SPM instead, in the same capture command: `--spm-beta-enabled --spm SQ_CYCLES,<counters> -f json`. `SQ_CYCLES` aligns SPM with the trace. Pick counters marked `SPM: Supported` in `rocprofv3 -L`; `rocprofv3-avail list --spm-config` lists the agents that support SPM |
| Timing unit in the trace | 4-cycle steps | 4-cycle steps | 1 cycle |
| Wait instructions | `s_waitcnt` | `s_waitcnt` | `s_wait_*` |

- **CDNA3 and CDNA4:** these GPUs have many shader engines (`rocminfo | grep -m1 'Shader Engines'`). The default mask, `0x1`, traces only SE 0, so a small grid can leave the target CU empty.
- **CDNA5:** if stitching fails or the ISA is missing, the decoder predates gfx1250. Use the decoder from a ROCm release that supports gfx1250, or build it with `-DLLVM_DIR=/opt/rocm/llvm/lib/cmake/llvm`.
