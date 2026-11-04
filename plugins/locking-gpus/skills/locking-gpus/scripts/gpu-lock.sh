#!/usr/bin/env bash
# gpu-lock: run a command on one exclusively locked GPU.
#
#   gpu-lock.sh <cmd> [args...]
#
# Takes flock -n on ~/.gpu_locks/<N> for a free GPU (random start, idle GPUs
# first), sets HIP_VISIBLE_DEVICES=<N>, and execs <cmd>. The lock fd is
# inherited by <cmd>, so the kernel releases the lock when <cmd> exits, even on
# kill -9. If every GPU is locked, retries every second. Only one lock is ever
# held, so there is no deadlock. A nested call reuses the outer GPU. The lock
# file records the last holder's PID, start time and command.
#
# Locks are advisory: a GPU with load or resident VRAM but no lock is in use by
# something outside this protocol. Such a GPU is taken only when no idle one is
# free, with a warning on stderr.
#
# Env:
#   GPU_LOCK_GPUS   candidate GPU indices, e.g. "0 1" (default: all GPUs)
#   GPU_LOCK_DIR    lock directory (default: ~/.gpu_locks)
#   GPU_LOCK_TIMEOUT  give up waiting for a GPU after this many seconds and
#                     exit 75 without running <cmd> (default: wait forever)
set -u

if [[ $# -eq 0 ]]; then
  echo "usage: gpu-lock.sh <cmd> [args...]" >&2
  exit 2
fi

if [[ -n "${GPU_LOCK_HELD:-}" ]]; then
  exec "$@"
fi

lock_dir=${GPU_LOCK_DIR:-$HOME/.gpu_locks}
mkdir -p "$lock_dir"

# HIP device i is the i-th KFD topology node with a nonzero gpu_id, the same
# enumeration ROCr uses. Keep each one's sysfs device dir for the load check.
sysfs=()
for node in $(ls -v /sys/class/kfd/kfd/topology/nodes 2>/dev/null); do
  p=/sys/class/kfd/kfd/topology/nodes/$node
  [[ "$(<"$p/gpu_id")" == 0 ]] && continue
  minor=$(awk '$1 == "drm_render_minor" {print $2}' "$p/properties")
  sysfs+=("/sys/class/drm/renderD$minor/device")
done

if [[ -n "${GPU_LOCK_GPUS:-}" ]]; then
  read -ra gpus <<<"$GPU_LOCK_GPUS"
else
  gpus=("${!sysfs[@]}")
fi
ngpus=${#gpus[@]}
if ((ngpus == 0)); then
  echo "gpu-lock: no GPUs found" >&2
  exit 1
fi

# Prints why GPU $1 looks in use, or nothing if it looks idle.
busy_reason() {
  local d=${sysfs[$1]:-}
  [[ -r "$d/gpu_busy_percent" && -r "$d/mem_info_vram_used" ]] || return
  local busy=$(<"$d/gpu_busy_percent")
  local vram_mib=$(($(<"$d/mem_info_vram_used") >> 20))
  if ((busy > 5 || vram_mib > 1024)); then
    echo "${busy}% busy, ${vram_mib} MiB VRAM used"
  fi
}

announced=0
while :; do
  start=$((RANDOM % ngpus))
  for accept_busy in 0 1; do
    for ((i = 0; i < ngpus; i++)); do
      g=${gpus[$(((start + i) % ngpus))]}
      exec {fd}>>"$lock_dir/$g"
      if flock -n "$fd"; then
        reason=$(busy_reason "$g")
        if [[ -z "$reason" ]]; then
          echo "gpu-lock: GPU $g" >&2
        elif ((accept_busy)); then
          echo "gpu-lock: GPU $g, WARNING: $reason by processes outside gpu-lock; timings may be skewed" >&2
        else
          exec {fd}>&-
          continue
        fi
        # Record the holder for status checks; exec keeps the PID.
        echo "$$ $(date +%FT%T) $*" >"$lock_dir/$g"
        export HIP_VISIBLE_DEVICES=$g GPU_LOCK_HELD=$g
        exec "$@"
      fi
      exec {fd}>&-
    done
  done
  if [[ -n "${GPU_LOCK_TIMEOUT:-}" ]] && ((SECONDS >= GPU_LOCK_TIMEOUT)); then
    echo "gpu-lock: no GPU free after ${GPU_LOCK_TIMEOUT}s, giving up" >&2
    exit 75
  fi
  if ((!announced)); then
    echo "gpu-lock: all GPUs (${gpus[*]}) locked, waiting..." >&2
    announced=1
  fi
  sleep 1
done
