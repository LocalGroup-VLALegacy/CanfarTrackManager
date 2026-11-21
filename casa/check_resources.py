# Runs inside CASA (casa -c check_resources.py <label> <outdir>): record the CPU and memory
# limits CASA believes it has, alongside what the container actually got.
import json
import os
import sys
import datetime as dt

label, outdir = sys.argv[-2], sys.argv[-1]


def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None


def call(name, *args):
    fn = getattr(casalog, name, None)
    if fn is None:
        return "not available"
    try:
        return fn(*args)
    except Exception as e:  # report, don't fail the check
        return "error: %r" % e


rep = {
    "label": label,
    "ts": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    "session": os.environ.get("skaha_sessionid", "local"),
    "casa_version": casalog.version() if hasattr(casalog, "version") else None,
    "env": {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")},
    "container": {"cpu.max": read("/sys/fs/cgroup/cpu.max"), "memory.max": read("/sys/fs/cgroup/memory.max"),
                  "affinity_cpus": len(os.sched_getaffinity(0)), "os.cpu_count": os.cpu_count()},
    "casalog_methods": sorted(m for m in dir(casalog)
                              if any(k in m.lower() for k in ("memory", "cpu", "omp", "thread"))),
    "casa": {
        "getMemoryTotal(False)_KiB": call("getMemoryTotal", False),
        "getMemoryTotal(True)_KiB": call("getMemoryTotal", True),
        "getMemoryFraction": call("getMemoryFraction"),
        "getNumCPUs(False)": call("getNumCPUs", False),
        "getNumCPUs(True)": call("getNumCPUs", True),
        "ompGetNumThreads": call("ompGetNumThreads"),
    },
}
os.makedirs(outdir, exist_ok=True)
path = os.path.join(outdir, "casa_resources_%s_%s.json" % (label, rep["session"]))
with open(path, "w") as f:
    json.dump(rep, f, indent=1, default=str)
casalog.post("check_resources wrote %s" % path)
print(json.dumps(rep, indent=1, default=str))
