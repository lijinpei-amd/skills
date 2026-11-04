---
name: locking-gpus
description: Use when about to run anything that may execute on a GPU on a shared multi-GPU machine — benchmarks, before/after or A/B perf comparisons, HIP/ROCm binaries, PyTorch/Triton scripts, GPU test suites, rocprof profiling — or when GPU timings look noisy, inflated, or inconsistent between runs.
---

# Locking GPUs

Two processes on one GPU corrupt each other's timings: four copies of a kernel
sharing an MI308X each ran 3.7x slower (68.5 ms vs 18.7 ms). So every GPU job
runs under an exclusive `flock` on `~/.gpu_locks/<N>` with
`HIP_VISIBLE_DEVICES=<N>`, and `scripts/gpu-lock.sh` does all of it.

`$S` = the directory containing this SKILL.md.

## Run

```bash
bash "$S/scripts/gpu-lock.sh" ./bench 200
FOO=1 bash "$S/scripts/gpu-lock.sh" python run.py                  # env assignments go first
bash "$S/scripts/gpu-lock.sh" bash -c './base 200 && ./patched 200'  # A/B on one GPU
bash "$S/scripts/gpu-lock.sh" ./bench 400 >run400.log 2>&1 &          # background: keep stderr
```

The script takes one free GPU (idle ones first), sets `HIP_VISIBLE_DEVICES`, and
execs the command; the kernel drops the lock when the command exits, even on
`kill -9`. The command's exit status passes through, and nested calls reuse the
outer GPU. `GPU_LOCK_GPUS="0 1"` limits the candidates.

## Rules

- **Lock only the GPU part.** Build first, unlocked; a compile under the lock
  blocks everyone else.
- **One lock per comparison.** Run both sides of a before/after or A/B inside a
  single `gpu-lock.sh bash -c '...'` so they share a GPU; separate calls can
  land on different GPUs and add GPU-to-GPU variance.
- **One lock per parallel job.** Wrap each concurrent job separately; each gets
  its own GPU, so their results also carry GPU-to-GPU variance.
- **Read the script's stderr** and report it with the numbers:
  - `gpu-lock: GPU 6` — the GPU used.
  - `gpu-lock: GPU 6, WARNING: ... by processes outside gpu-lock` — the only
    free GPU carries load from something that ignores the locks (checked once,
    at pick time). Timings from it are skewed: rerun later, or report them as
    flagged, never as clean.
  - `gpu-lock: all GPUs (...) locked, waiting...` — other jobs hold every GPU
    and the script blocks until one frees. To bound only the wait, set
    `GPU_LOCK_TIMEOUT=<sec>`: it exits 75 with `no GPU free after <sec>s`
    without running the command (`timeout` would also kill a slow job). Then
    retry later or tell the user the GPUs are busy. Never run unlocked, kill a
    holder, or delete lock files.
- **Leave `HIP_VISIBLE_DEVICES` / `ROCR_VISIBLE_DEVICES` unset** before calling;
  lock indices assume the default device order.
- **Multi-GPU jobs** (torchrun, multi-device tests) need more than the one GPU
  this protocol locks; ask the user.

## Status

```bash
for f in ~/.gpu_locks/*; do flock -n "$f" true && echo "${f##*/} free" || echo "${f##*/} held: $(cat "$f")"; done
```

A held lock file shows the holder's PID, start time and command. A held lock
always has a live holder: the kernel drops it when the last process holding it
exits, so there are no stale locks to clean up.
