# Trace dispatches in a time window

`--collection-period` (`-P`) turns ATT on only during time windows. Use it when the kernel you want runs at a known time, such as after a long warm-up or model load, and counting its dispatches from the start is impractical.

```bash
rust-script $S/scripts/capture_att_trace.rs --capture-att-trace --kernel-id gemm_bf16 --version v2 --desc lds-swizzle -- \
  --collection-period 30:5:1 --kernel-include-regex gemm_bf16_tn --kernel-iteration-range 1 \
  -- ./bench_gemm --iters 100000
```

This traces the 1st `gemm_bf16_tn` dispatch after second 30.

## Format

`-P START_DELAY:DURATION:REPEAT [START_DELAY:DURATION:REPEAT …]`, in seconds. `--collection-period-unit {hour,min,sec,msec,usec,nsec}` changes the unit; decimals work (`0.5`).

| Option | Windows (seconds) |
| --- | --- |
| `-P 30:5:1` | 30–35 |
| `-P 10:5:3` | 10–15, 25–30, 40–45: each repeat waits START_DELAY again |
| `-P 10:5:1 20:5:1` | 10–15, 35–40: each triplet's delay starts when the previous window closes |
| `-P 500:20:1 --collection-period-unit msec` | 0.5–0.52 |

- START_DELAY counts from when the app initializes the ROCm runtime, not from process start.
- The rocprofv3 docs say REPEAT `0` repeats forever. The rocprofiler-sdk source runs it once, like `1`.

## With the kernel filters

- The filters apply inside the windows only. `--kernel-iteration-range` counts only the dispatches made inside a window, continuing across windows: with `-P 30:5:1 --kernel-iteration-range 1`, run 1 is the 1st matching dispatch after second 30, not the app's 1st.
- Without a range, ATT traces the 1st matching dispatch inside the windows.
- `--att-consecutive-kernels N`: the first traced dispatch must fall inside a window; keep the window open until the N dispatches have run.
- `--selected-regions` cannot be combined with `--collection-period`: rocprofv3 exits with an error.

## Check the selection

`--list-kernels` and `--list-filtered-kernels` ignore `-P` (it shows up in `ignored-rocprofv3-options`), so they cannot preview which dispatch a window selects. After the capture, the `ui_output_agent_*_dispatch_<id>` directory names the dispatch traced.
