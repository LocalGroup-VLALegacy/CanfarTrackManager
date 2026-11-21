#!/usr/bin/env bash
# QA step for one iteration's products/ (plan §3.4 calibrate, step 4). Ported from
# AutoDataIngest make_qa_products() (ingest_pipeline_functions.py:576-756), with its bugs fixed:
#   - QAPlotter is called directly, not through a quoted `ipython -c` string;
#   - a QAPlotter failure is a failure (no "Ready for QA" without index.html);
#   - nothing is deleted: the txt inputs stay next to the HTML in products/.
#
#   run_qa.sh <products_dir> [flagging_sheet_link]
# Env: CASA_HOME (CASA install; its python3 runs QAPlotter), QA_DEPS (folder with QAPlotter and
# its dependencies installed for that python; see qa/install_qadeps.sh), QA_NAV_FIX=1 to point
# QAPlotter's home links at the controller's index pages (qa/fix_nav.py).
set -uo pipefail
PROD=${1:?usage: run_qa.sh <products_dir> [flagging_sheet_link]}
LINK=${2:-}
: "${CASA_HOME:?CASA_HOME not set}" "${QA_DEPS:?QA_DEPS not set}"
PY=$CASA_HOME/bin/python3
cd "$PROD" || { echo "run_qa: no products dir $PROD" >&2; exit 2; }
[[ -f weblog.tgz ]] || { echo "run_qa: products/weblog.tgz missing" >&2; exit 2; }

# The weblog tarball holds pipeline-<stamp>/html/...; QAPlotter reads weblog/html/.
if [[ ! -d weblog ]]; then
    rm -rf weblog.partial && mkdir weblog.partial
    tar --strip-components=1 -C weblog.partial -xzf weblog.tgz || { echo "run_qa: weblog extraction failed" >&2; exit 3; }
    mv weblog.partial weblog
fi
chmod -R g+rX weblog

echo "run_qa: QAPlotter $(cat "$QA_DEPS/QAPLOTTER_SHA" 2>/dev/null) with $PY in $PROD"
PYTHONPATH=$QA_DEPS MPLBACKEND=Agg QA_LINK=$LINK "$PY" -c '
import os, qaplotter
link = os.environ.get("QA_LINK") or None
qaplotter.make_all_plots(flagging_sheet_link=link, show_target_linesonly=True)
'
rc=$?
[[ $rc == 0 ]] || { echo "run_qa: QAPlotter exit code $rc" >&2; exit 4; }
[[ -f index.html ]] || { echo "run_qa: QAPlotter finished but index.html is missing" >&2; exit 5; }
# The ARC files service serves .css as text/plain, so browsers ignore the weblog's stylesheet link.
# Inline it into the extracted copy (weblog.tgz and the CASA pipeline are untouched).
"$PY" "$(dirname "$0")/inline_weblog_css.py" "$PROD" || { echo "run_qa: inline_weblog_css failed" >&2; exit 7; }
if [[ ${QA_NAV_FIX:-0} == 1 ]]; then
    "$PY" "$(dirname "$0")/fix_nav.py" "$PROD" || { echo "run_qa: fix_nav failed" >&2; exit 6; }
fi
echo "run_qa: wrote $(find . -name '*.html' -newer weblog.tgz | wc -l) html files; index.html present"
