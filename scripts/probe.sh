#!/usr/bin/env bash
# E0 probe: what a headless session in this image can do. Writes probes/<session>.txt.
#   canfar create headless images.canfar.net/skaha/base-notebook:latest --name tm-probe -- bash <repo>/scripts/probe.sh
set -uo pipefail
SKEL=$(dirname "$(readlink -f "$0")")
OUT="${PROBE_OUT:-$PWD/probes}/${skaha_sessionid:-nosession}.txt"; mkdir -p "$(dirname "$OUT")"
{
echo "== session ${skaha_sessionid:-?} on $(hostname) at $(date -u +%FT%TZ)"
echo "== id"; id
echo "== python"; command -v python3 && python3 -V; python3 -c "import canfar; print('canfar', canfar.__file__)" 2>&1 | tail -1
echo "== resources"; nproc; free -g | head -2; df -h /scratch /arc 2>&1 | tail -2; echo "OMP_NUM_THREADS=${OMP_NUM_THREADS:-unset}"
echo "== cert"; ls -l --time-style=full-iso ~/.ssl/cadcproxy.pem; openssl x509 -noout -startdate -enddate -in ~/.ssl/cadcproxy.pem 2>&1
echo "== outbound"
for u in https://ws-uv.canfar.net/skaha https://pypi.org/simple/canfar/ https://data-query.nrao.edu/tap https://sheets.googleapis.com https://data.nrao.edu; do
  printf '%s %s\n' "$u" "$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$u" 2>&1)"; done
echo "== env (skaha*)"; env | grep -i '^skaha' | sed -E 's/(token|secret)=.*/\1=<redacted>/I'
} > "$OUT" 2>&1
echo "wrote $OUT"
