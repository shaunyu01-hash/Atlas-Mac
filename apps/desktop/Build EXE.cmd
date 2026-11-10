@echo off
rem Builds dist\Atlas PO ERP\Atlas PO ERP.exe and dist\Atlas PO ERP (Sample Data)\Atlas PO ERP (Sample Data).exe. Close the programs first.
cd /d "%~dp0"
python -E -m PyInstaller --noconfirm --windowed --name "Atlas PO ERP" --icon "atlaspo\ui\assets\app.ico" ^
  --add-data "atlaspo\ui\assets;atlaspo\ui\assets" ^
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module matplotlib --exclude-module pandas --exclude-module numpy --exclude-module pytest ^
  "Atlas PO ERP.pyw"
python -E -m PyInstaller --noconfirm --windowed --name "Atlas PO ERP (Sample Data)" --icon "atlaspo\ui\assets\app.ico" ^
  --add-data "atlaspo\ui\assets;atlaspo\ui\assets" ^
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module matplotlib --exclude-module pandas --exclude-module numpy --exclude-module pytest ^
  "Atlas PO ERP (Sample Data).pyw"
pause
