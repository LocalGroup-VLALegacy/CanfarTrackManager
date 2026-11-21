#!/usr/bin/env bash
# Install QAPlotter and its dependencies for CASA's bundled python into a separate folder.
# numpy/astropy/scipy/matplotlib are pinned to CASA's own versions, so the folder never
# changes what CASA itself imports (it is only on PYTHONPATH for the QA step).
#   install_qadeps.sh <CASA_HOME> <QAPlotter checkout> <target dir>
set -euo pipefail
CASA_HOME=$1 SRC=$2 DEST=$3
mkdir -p "$DEST"
cons=$(mktemp)
"$CASA_HOME/bin/python3" -c "import numpy, astropy, scipy, matplotlib
for m in (numpy, astropy, scipy, matplotlib): print(f'{m.__name__}=={m.__version__}')" > "$cons"
"$CASA_HOME/bin/pip3" install --quiet --disable-pip-version-check --upgrade --target "$DEST" -c "$cons" "$SRC"
git -C "$SRC" rev-parse --short HEAD > "$DEST/QAPLOTTER_SHA"
rm -f "$cons"
echo "installed QAPlotter $(cat "$DEST/QAPLOTTER_SHA") into $DEST"
