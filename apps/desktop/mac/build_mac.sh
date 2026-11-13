#!/bin/bash
# Builds the Mac apps on a Mac: dist/Atlas PO ERP.app and dist/Atlas PO ERP (Sample Data).app,
# plus a zip of each in dist/mac that can be copied to another Mac without breaking the app.
# The app runs on Macs with the same chip as the Mac that built it (Apple silicon or Intel).
# Apple silicon Macs can also run an Intel build through Rosetta.
#
#   bash mac/build_mac.sh          (run in apps/desktop; needs Python 3.12 or newer)
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
ENV_DIR="build/mac-env"
"$PY" -m venv "$ENV_DIR"
# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"
python -m pip install --upgrade pip
# Pillow lets PyInstaller turn app.png into the Mac icon.
python -m pip install -r requirements.txt pillow

common=(
  --noconfirm --windowed
  --icon "atlaspo/ui/assets/app.png"
  --add-data "atlaspo/ui/assets:atlaspo/ui/assets"
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module matplotlib
  --exclude-module pandas --exclude-module numpy --exclude-module pytest
)
python -E -m PyInstaller "${common[@]}" --name "Atlas PO ERP" \
  --osx-bundle-identifier "com.atlaspo.erp" "Atlas PO ERP.pyw"
python -E -m PyInstaller "${common[@]}" --name "Atlas PO ERP (Sample Data)" \
  --osx-bundle-identifier "com.atlaspo.erp.sample" "Atlas PO ERP (Sample Data).pyw"

case "$(uname -m)" in
  arm64) chip="Apple silicon" ;;
  *) chip="Intel" ;;
esac
mkdir -p dist/mac
rm -f dist/mac/*.zip
# ditto keeps the app's links and signature intact; a plain zip would break the app.
for app in "Atlas PO ERP" "Atlas PO ERP (Sample Data)"; do
  ditto -c -k --sequesterRsrc --keepParent "dist/$app.app" "dist/mac/$app - Mac $chip.zip"
done
echo
echo "Built:"
ls -1 dist/mac
