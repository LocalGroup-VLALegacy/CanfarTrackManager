#!/usr/bin/env bash
# Calibrate worker: one VLA track, one review iteration, through
# the LGLBS pipeline and QAPlotter. Runs in images.canfar.net/casa-6/casa:6.6.1-17-pipeline
# (used for the OS and xvfb-run) with a CASA install chosen by CASA_HOME.
#
# Worker contract (plan §3.4), same as the placeholder worker.py:
#   - exit 0 at once if this iteration has a valid result.json; take worker.lock (session ID);
#   - write logs to /arc as it runs; write run.json on exit and result.json last;
#   - result.json only if the outputs verify (exit status alone is not trusted).
#
# Layout (plan §3.2):
#   tracks/<EB>/raw/<SDM>/                    staged once per track (from TAR), shared by iterations
#   tracks/<EB>/iter_<n>/inputs/              manual_flagging.txt, refant_ignore.txt (frozen by the controller)
#   tracks/<EB>/iter_<n>/logs/attempt_<k>/    stdout.log, run.json, peak_* samples
#   tracks/<EB>/iter_<n>/work/                CASA working directory (fresh per attempt; old one moved aside)
#   tracks/<EB>/iter_<n>/products/            renamed out of work/ after CASA; QAPlotter output added here
#   tracks/<EB>/iter_<n>/result.json
#
# Env from the controller: TM_ROOT, EB, ITERATION, ATTEMPT. From config (worker.env):
#   CASA_HOME   CASA install (bin/casa runs the pipeline, bin/python3 runs QAPlotter). Required.
#   CASA        optional override of the CASA binary (default $CASA_HOME/bin/casa)
#   PIPELINE    ReductionPipeline checkout          TAR       NRAO tar holding the SDM
#   CASA_WRAPPER  casa_with_limits.sh               SDM_MODE  link (default) | copy
#   QA_DEPS     QAPlotter install for CASA's python (qa/install_qadeps.sh); QA_SCRIPT <repo>/qa/run_qa.sh
#   VLA_PIPELINE_CONFIG_DIR  optional per-project pipeline cfgs (default: the repo's config_files/)
#   ANTCORR_DIR optional offline VLA_antcorr_tables (only a fallback: CANFAR sessions reach NRAO)
#   QA_FLAG_LINK optional link to the track's flag tab, shown on the QA page
set -uo pipefail

ITERATION=${ITERATION:-1}
TRACK="$TM_ROOT/tracks/$EB"
IDIR="$TRACK/iter_$ITERATION"
LOGDIR="$IDIR/logs/attempt_$ATTEMPT"
SID=${skaha_sessionid:-nosession}
mkdir -p "$LOGDIR" "$IDIR/inputs"
exec > >(tee -a "$LOGDIR/stdout.log") 2>&1
now() { date -u +%FT%TZ; }
say() { echo "$(now) [$SID] $*"; }
STARTED=$(now); T0=$(date +%s)
CASA=${CASA:-${CASA_HOME:+$CASA_HOME/bin/casa}}

say "start eb=$EB iteration=$ITERATION attempt=$ATTEMPT host=$(hostname) cpu.max=$(cat /sys/fs/cgroup/cpu.max 2>/dev/null) memory.max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null) OMP_NUM_THREADS=${OMP_NUM_THREADS:-unset}"
say "CASA_HOME=${CASA_HOME:-unset} CASA=$CASA PIPELINE=$PIPELINE VLA_PIPELINE_CONFIG_DIR=${VLA_PIPELINE_CONFIG_DIR:-<repo config_files>}"

if [[ -f $IDIR/result.json ]] && grep -q '"ok": true' "$IDIR/result.json"; then
    say "valid result.json already exists for iteration $ITERATION; nothing to do"; exit 0
fi
LOCK="$TRACK/worker.lock"
if ! (set -o noclobber; echo "$SID" > "$LOCK") 2>/dev/null; then
    if [[ $(cat "$LOCK") != "$SID" ]]; then say "worker.lock held by $(cat "$LOCK"); refusing to run"; exit 3; fi
fi

SAMPLER_PID=""; declare -A STEP_S=()
finish() {
    local code=$1
    [[ -n $SAMPLER_PID ]] && kill "$SAMPLER_PID" 2>/dev/null
    sample_once
    local cg_peak; cg_peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo null)
    local steps=""; for k in "${!STEP_S[@]}"; do steps+="\"$k\": ${STEP_S[$k]}, "; done
    printf '{\n "eb": "%s", "iteration": %s, "attempt": %s, "session": "%s", "started": "%s", "ended": "%s",\n "exit_code": %s, "wall_s": %s, "step_s": {%s},\n "peak_anon_bytes": %s, "peak_cgroup_bytes": %s, "peak_workdir_bytes": %s,\n "sdm_bytes": %s, "sdm_mode": "%s", "casa": "%s"\n}\n' \
        "$EB" "$ITERATION" "$ATTEMPT" "$SID" "$STARTED" "$(now)" "$code" "$(( $(date +%s) - T0 ))" "${steps%, }" \
        "$(cat "$LOGDIR/peak_anon_bytes" 2>/dev/null || echo null)" "$cg_peak" \
        "$(cat "$LOGDIR/peak_workdir_bytes" 2>/dev/null || echo 0)" "${SDM_BYTES:-null}" "${SDM_MODE:-link}" "$CASA" \
        > "$LOGDIR/partial-run.json"
    mv "$LOGDIR/partial-run.json" "$LOGDIR/run.json"
    [[ $(cat "$LOCK" 2>/dev/null) == "$SID" ]] && rm -f "$LOCK"
    say "exit code $code"
    exit "$code"
}
# Peak usage. memory.peak counts page cache, which fills the cgroup during heavy I/O (it sat at
# the 64 GiB limit in the first IC10 run with no OOM), so the RSS-like figure is the cgroup's
# anonymous memory, sampled every 30 s. The workdir size is sampled every 5 min.
peak_update() { local f=$LOGDIR/$1 v=$2; [[ $v =~ ^[0-9]+$ ]] || return 0; (( v > $(cat "$f" 2>/dev/null || echo 0) )) && echo "$v" > "$f"; return 0; }
anon_now() { awk '$1=="anon"{print $2}' /sys/fs/cgroup/memory.stat 2>/dev/null; }
sample_once() {
    peak_update peak_anon_bytes "$(anon_now)"
    [[ -n ${WORK:-} && -d ${WORK:-/nonexistent} ]] && peak_update peak_workdir_bytes "$(du -sb "$WORK" 2>/dev/null | cut -f1)"
    return 0
}
timed() { local name=$1; shift; local t; t=$(date +%s); "$@"; local rc=$?; STEP_S[$name]=$(( $(date +%s) - t )); return $rc; }

[[ -n ${CASA_HOME:-} && -x $CASA ]] || { say "CASA not found: CASA_HOME=${CASA_HOME:-unset} CASA=$CASA"; finish 12; }
[[ -z ${VLA_PIPELINE_CONFIG_DIR:-} || -f $VLA_PIPELINE_CONFIG_DIR/master_config.cfg ]] || {
    say "VLA_PIPELINE_CONFIG_DIR has no master_config.cfg"; finish 12; }
[[ -n ${VLA_PIPELINE_CONFIG_DIR:-} ]] && export VLA_PIPELINE_CONFIG_DIR

# --- stage the raw SDM once per track (stage_local): unpack, check, rename -----------
RAW="$TRACK/raw"; mkdir -p "$RAW"
SDM=$(find "$RAW" -mindepth 1 -maxdepth 1 -type d -name '*.sb*.eb*' ! -name '.partial' -printf '%f\n' | head -n1)
if [[ -z $SDM ]]; then
    say "unpacking $TAR"
    rm -rf "$RAW/.partial"; mkdir -p "$RAW/.partial"
    timed stage tar -xf "$TAR" -C "$RAW/.partial" --strip-components=2 || { say "tar failed"; finish 10; }
    SDM=$(find "$RAW/.partial" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | head -n1)
    [[ -n $SDM ]] || { say "no SDM directory in the tar"; finish 10; }
    mv "$RAW/.partial/$SDM" "$RAW/$SDM" && rm -rf "$RAW/.partial"
fi
SDMDIR="$RAW/$SDM"
[[ -f $SDMDIR/ASDM.xml && -f $SDMDIR/Main.xml ]] || { say "SDM lacks ASDM.xml/Main.xml"; finish 11; }
MAIN_ROWS=$(grep -o '<row>' "$SDMDIR/Main.xml" | wc -l); BDFS=$(ls "$SDMDIR/ASDMBinary" | wc -l)
SDM_BYTES=$(du -sb "$SDMDIR" | cut -f1)
say "SDM $SDM: $MAIN_ROWS Main rows, $BDFS BDFs, $SDM_BYTES bytes"
[[ $MAIN_ROWS == "$BDFS" ]] || { say "BDF count does not match Main.xml rows"; finish 11; }

# --- fresh work directory per attempt (stale MS/context/splits break re-runs, plan §2) --
WORK="$IDIR/work"
[[ -e $WORK ]] && mv "$WORK" "$WORK.stale.a$ATTEMPT.$(date +%s)"
[[ -e $IDIR/products ]] && mv "$IDIR/products" "$IDIR/products.stale.a$ATTEMPT.$(date +%s)"
mkdir -p "$WORK"; cd "$WORK" || finish 13
if [[ ${SDM_MODE:-link} == copy ]]; then cp -a "$SDMDIR" "$WORK/$SDM"; else ln -s "$SDMDIR" "$WORK/$SDM"; fi
say "workdir $WORK, SDM via ${SDM_MODE:-link}"

# Reviewer inputs, frozen by the controller when the restart was applied.
declare -A INPUT_SHA=()
for f in manual_flagging.txt refant_ignore.txt; do
    if [[ -f $IDIR/inputs/$f ]]; then
        cp "$IDIR/inputs/$f" "$WORK/$f"
        INPUT_SHA[$f]=$(sha256sum "$WORK/$f" | cut -d' ' -f1)
        say "input $f ($(grep -vc '^[[:space:]]*#' "$WORK/$f") non-comment lines, sha256 ${INPUT_SHA[$f]:0:12})"
    fi
done
if [[ -n ${ANTCORR_DIR:-} ]]; then cp -r "$ANTCORR_DIR" "$WORK/VLA_antcorr_tables"; say "offline antenna tables from $ANTCORR_DIR"; fi

( while true; do for _ in 1 2 3 4 5 6 7 8 9 10; do sleep 30; peak_update peak_anon_bytes "$(anon_now)"; done
  peak_update peak_workdir_bytes "$(du -sb "$WORK" 2>/dev/null | cut -f1)"; done ) &
SAMPLER_PID=$!

export PYTHONPATH="$PIPELINE:${PYTHONPATH:-}"
has_exports() {
    compgen -G "products/casa_piperestorescript.py" >/dev/null ||
        compgen -G "products/*pipeline_manifest.xml" >/dev/null ||
        compgen -G "products/*.calapply.txt" >/dev/null
}
rc=1
for pass in 1 2; do
    if [[ $pass == 2 && $rc != 0 ]] && ! has_exports; then say "pass 2 skipped: pass 1 failed before export"; break; fi
    rc=0
    say "=== CASA pass $pass"
    timed "casa_pass$pass" env CASA="$CASA" bash "$CASA_WRAPPER" "$WORK" --pipeline \
        -c "$PIPELINE/lband_pipeline/lband_reduction_pipeline.py" "$SDM" || rc=$?
    say "=== pass $pass exit code $rc"
done
sample_once

# --- verify calibration outputs ---------------------------------------------------------
missing=()
has_exports || missing+=("products/ export markers")
compgen -G "pipeline*.context" >/dev/null || missing+=("pipeline*.context")
[[ -f products/weblog.tgz ]] || missing+=("products/weblog.tgz")
outs=()
for s in "$SDM.speclines.ms.split" "$SDM.speclines.ms.split_calibrators" "$SDM.continuum.ms.split" "$SDM.continuum.ms.split_calibrators"; do
    if [[ -d $s && -n $(ls -A "$s" 2>/dev/null) ]]; then outs+=("work/$s"); else missing+=("$s"); fi
done
if (( ${#missing[@]} )); then
    say "verification failed; missing: ${missing[*]}"
    finish $(( rc ? rc : 20 ))
fi

# --- products out of work/ (a rename within /arc), then QA -------------------------------
mv "$WORK/products" "$IDIR/products" || { say "could not move products/ out of work/"; finish 20; }
say "=== QA (QAPlotter) in $IDIR/products"
timed qa env CASA_HOME="$CASA_HOME" QA_DEPS="$QA_DEPS" bash "$QA_SCRIPT" "$IDIR/products" "${QA_FLAG_LINK:-}"
qa_rc=$?
if [[ $qa_rc != 0 || ! -f $IDIR/products/index.html ]]; then
    say "QA step failed (exit $qa_rc); calibration outputs are kept, no result.json"
    finish 21
fi
outs+=("products/weblog.tgz" "products/index.html")

json_outs=$(printf '"%s",' "${outs[@]}"); json_outs="[${json_outs%,}]"
json_in=""; for f in "${!INPUT_SHA[@]}"; do json_in+="\"$f\": \"${INPUT_SHA[$f]}\", "; done
printf '{"ok": true, "eb": "%s", "iteration": %s, "attempt": %s, "session": "%s", "sdm": "%s", "inputs": {%s}, "outputs": %s, "qaplotter": "%s", "finished": "%s"}\n' \
    "$EB" "$ITERATION" "$ATTEMPT" "$SID" "$SDM" "${json_in%, }" "$json_outs" \
    "$(cat "$QA_DEPS/QAPLOTTER_SHA" 2>/dev/null)" "$(now)" > "$IDIR/partial-result.json"
mv "$IDIR/partial-result.json" "$IDIR/result.json"
say "outputs verified, QA written; wrote result.json"
finish 0
