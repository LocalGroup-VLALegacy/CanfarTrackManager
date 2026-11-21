#!/usr/bin/env bash
# Run CASA with the session's real core and memory allocation passed through.
#   casa_with_limits.sh <workdir> [casa args...]
# Same approach as the fir Slurm jobs (lglbs_hi_scripts make_casa_startup / run_casa),
# but the limits come from the container's cgroup instead of SLURM_* variables:
#   - OMP_NUM_THREADS = allocated cores (CPU affinity still shows every node core);
#   - a per-job startup.py sets casalog.setMemoryTotal() to CASA_MEM_PERCENT (default
#     90) percent of the cgroup memory limit, setMemoryFraction(99), and the core count;
#   - the user's ~/.casa/startup.py (analysisUtils) is still run afterwards.
# Env: CASA (binary), CASA_MEM_PERCENT, CASA_CORES / CASA_MEM_MB (overrides).
set -euo pipefail
WORK=$1; shift
CASA=${CASA:-/arc/home/ekoch/casa-6.6.6-18-pipeline-2025.1.0.37-py3.10.el8/bin/casa}
mkdir -p "$WORK/casa_cache"

cgroup_cores() {   # cgroup v2 cpu.max "quota period", v1 cfs files; empty if unlimited
    local q p
    if [[ -r /sys/fs/cgroup/cpu.max ]]; then read -r q p < /sys/fs/cgroup/cpu.max
    elif [[ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]]; then
        q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us); fi
    [[ -n ${q:-} && $q != max && $q != -1 ]] && echo $(( (q + p - 1) / p ))
}
cgroup_mem_mb() {
    local m
    if [[ -r /sys/fs/cgroup/memory.max ]]; then m=$(cat /sys/fs/cgroup/memory.max)
    elif [[ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]]; then m=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes); fi
    [[ -n ${m:-} && $m != max && $m -lt 9000000000000000000 ]] && echo $(( m / 1048576 ))
}

CORES=${CASA_CORES:-$(cgroup_cores || true)}; CORES=${CORES:-${OMP_NUM_THREADS:-1}}
LIMIT_MB=$(cgroup_mem_mb || true)
PCT=${CASA_MEM_PERCENT:-90}
MEM_MB=${CASA_MEM_MB:-$(( ${LIMIT_MB:-0} * PCT / 100 ))}
export OMP_NUM_THREADS=$CORES

STARTUP="$WORK/casa_startup.py"
cat > "$STARTUP" <<PY
# Written by casa_with_limits.sh for session ${skaha_sessionid:-local}: cgroup limit ${LIMIT_MB:-none} MB, ${CORES} cores.
import os
_mem_mb = ${MEM_MB}
if _mem_mb > 0:
    casalog.setMemoryTotal(_mem_mb)
    casalog.setMemoryFraction(99)
if hasattr(casalog, "setNumCPUs"):
    casalog.setNumCPUs(${CORES})
casalog.post("casa_with_limits: memory total %d MB (fraction 99), %d cores, OMP_NUM_THREADS=%s"
             % (_mem_mb, ${CORES}, os.environ.get("OMP_NUM_THREADS")))
_user_startup = os.path.expanduser("~/.casa/startup.py")
if os.path.exists(_user_startup):
    exec(open(_user_startup).read())
PY
echo "casa_with_limits: cores=$CORES cgroup_limit_mb=${LIMIT_MB:-none} casa_memory_mb=$MEM_MB startup=$STARTUP"
exec xvfb-run -a "$CASA" --nologger --nogui --log2term --nocrashreport \
    --startupfile "$STARTUP" --cachedir "$WORK/casa_cache" "$@"
