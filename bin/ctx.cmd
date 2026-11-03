@echo off
rem wscontext launcher for Windows: same contract as bin/ctx; a hook call never fails. Not exercised until the Finalize stage.
setlocal EnableDelayedExpansion
set "R=%~dp0.."
set "D="
:opts
if "%~1"=="--plugin-root" (shift & shift & goto opts)
if "%~1"=="--plugin-data" (set "D=%~2" & shift & shift & goto opts)
set "ARGS="
:collect
if "%~1"=="" goto collected
set ARGS=!ARGS! "%~1"
shift
goto collect
:collected
if not defined D set "D=%USERPROFILE%\.claude\plugins\data\wscontext-wscontext"
set "V=%D%\venv"
set "HOOK="
for /f "tokens=1" %%A in ("!ARGS!") do (if "%%~A"=="hook" set "HOOK=1" & if "%%~A"=="git-hook" set "HOOK=1")
if not exist "%D%" mkdir "%D%"
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%R%\uv.lock" MD5') do if not defined S set "S=%R% %%H"
set "CUR="
if exist "%V%\.lock-hash" set /p CUR=<"%V%\.lock-hash"
if not "!CUR!"=="!S!" (
  where uv >nul 2>nul || (if defined HOOK exit /b 0 & echo wscontext: uv is not installed 1>&2 & exit /b 1)
  set "UV_PROJECT_ENVIRONMENT=%V%"
  uv sync --frozen --no-dev --no-editable --python 3.13 --project "%R%" >nul 2>nul || (if defined HOOK exit /b 0 & echo wscontext: environment build failed 1>&2 & exit /b 1)
  >"%V%\.lock-hash" set /p="!S!"<nul
)
>"%D%\plugin-root" set /p="%R%"<nul
"%V%\Scripts\ctx.exe" --plugin-root "%R%" --plugin-data "%D%" !ARGS!
