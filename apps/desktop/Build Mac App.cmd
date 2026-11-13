@echo off
rem Builds the Mac apps (Apple silicon and Intel) on GitHub's Macs, because a Mac app can only be built on a Mac.
rem It sends the committed code to the Atlas-Mac repository and starts the build. In about 10 minutes the zips
rem are on a GitHub release, ready to download on the Mac. Uncommitted changes are not included: commit them first.
setlocal
cd /d "%~dp0"
set "REPO=https://github.com/shaunyu01-hash/Atlas-Mac"

git remote get-url mac >nul 2>&1 || git remote add mac %REPO%.git

for /f %%c in ('git status --porcelain ^| find /c /v ""') do set CHANGES=%%c
if not "%CHANGES%"=="0" (
  echo You have %CHANGES% uncommitted change^(s^). Only committed work goes into the Mac app.
  choice /m "Build from the last commit anyway"
  if errorlevel 2 goto end
)

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "TAG=mac-%%t"

echo.
echo Sending the code to Atlas-Mac...
git push mac HEAD:main || goto failed
echo Starting the Mac build %TAG%...
git push mac HEAD:refs/tags/%TAG% || goto failed

echo.
echo The Mac build has started. It takes about 10 minutes.
echo Watch it:     %REPO%/actions
echo Download it:  %REPO%/releases/tag/%TAG%
echo               (Atlas PO ERP (Sample Data) - Mac Apple silicon.zip, or the Intel one for older Macs)
start "" "%REPO%/actions"
goto end

:failed
echo.
echo The code could not be sent to GitHub. Check the internet connection and your GitHub sign-in, then try again.
echo If it says the push was rejected, Atlas-Mac has commits this PC doesn't have.

:end
echo.
pause
