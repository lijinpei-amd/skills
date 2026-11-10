# Trace a region the app marks

`--selected-regions` traces the code between `roctxProfilerResume(0)` and `roctxProfilerPause(0)`. Every dispatch inside the region is traced; `--kernel-include-regex`, `--kernel-exclude-regex` and `--kernel-iteration-range` do not apply. It cannot be combined with `--att-consecutive-kernels`.

Each region is one trace, numbered from 1: `ui_output_agent_*_dispatch_1`, `ui_output_agent_*_dispatch_2`, … For this benchmark loop, `--selected-regions` traces only the 15th iteration. Kernel launches are asynchronous and `roctxProfilerResume(0)` does not wait for them, so synchronize on both sides of the region: before `Resume`, so earlier iterations finish before the trace starts, and before `Pause`, so the traced kernel finishes before it stops:

```cpp
if(iter == 14) { hipDeviceSynchronize(); roctxProfilerResume(0); }
gemm_bf16_tn_kernel<256, 128><<<grid, block>>>(a, b, c, k);
if(iter == 14) { hipDeviceSynchronize(); roctxProfilerPause(0); }
```
