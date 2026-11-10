# Troubleshooting

| Symptom | Fix |
| --- | --- |
| Decoder not found: the script's `setup-warnings` says `no librocprof-trace-decoder.so`, or rocprofv3 reports the decoder library is missing | Check `ls ${ROCM_PATH:-/opt/rocm}/lib/librocprof-trace-decoder.so*`. ROCm 7.13 and later bundle the decoder. Otherwise build `projects/rocprof-trace-decoder` from ROCm/rocm-systems and add `--att-library-path <dir>` to the capture command |
| No `ui_output_agent_*` directory, exit code 0 | The regex matched nothing (it is searched in the name `--kernel-trace` shows with the same `-M`/`-T`; escape parentheses), or the app launched the kernel fewer than N times |
| `stats_*.csv` empty, or RCV shows only Occupancy | No wave ran on the traced CU (on CDNA5, the traced SIMD). Launch more workgroups, try another `--att-target-cu`, or widen `--att-shader-engine-mask` (for example `0x11111111`) |
| `SQTT data buffer full`, `GPU buffer full!` or `Wave incomplete` | Raise `--att-buffer-size` (one buffer is shared by all traced SEs, up to 2 GB), or trace fewer SEs |
| `Data Lost` | Too much bandwidth: trace fewer SEs, or use a larger perfcounter period |
| `FAILED: rocprofv3 killed after a fatal error` or `FAILED: rocprofv3 fatal error` | rocprofv3 printed a fatal line (`F<mmdd> …`). With `killed`, some process kept running, so the script killed the command's processes 30 s later; otherwise they exited by themselves, but the command still exited 0, for example a launcher that ignores its worker's failure. Read that line in `rocprofv3.log`. `Could not set agent to be profiled` has failed on one GPU and then worked on others of the same machine; run again, on another GPU if you can |
| `INVALID_SHADER_DATA` | aqlprofile or the decoder does not support this GPU. Update ROCm (7.0 or later) and the decoder (0.2.0 or later; on gfx1250, the newest) |
| `Stitch Incomplete` or unresolved instructions | The code objects do not match the binary that ran; on gfx1250, also check the decoder version |
