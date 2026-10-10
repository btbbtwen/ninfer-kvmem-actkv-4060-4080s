@echo off
setlocal EnableExtensions

REM ====================================================================
REM  KVMem multi-backend launcher - llama.cpp backend
REM
REM  Mirror of I:\llama\kvmem-llama.cpp-0.17.0\build-cuda13.4-dll\bin\
REM  start-llama-server.bat, for the feat/multi-backend-framework tree.
REM  ONE command line, every parameter written out inline: edit a value,
REM  save, run it. Ctrl+C stops the server.
REM
REM      start-kvmem-server.bat          this file - ActKV ON  (default)
REM      start-kvmem-server-noactkv.bat  same line, ActKV OFF (A/B baseline)
REM      start-kvmem-ninfer.bat          the ninfer backend, same ActKV flags
REM
REM  The exe is looked up next to this script first (so the .bat can be
REM  copied into the build's bin\ directory), then in the usual build
REM  directories of the repository this script sits in.
REM
REM --------------------------------------------------------------------
REM  ActKV retrieval block (arXiv:2609.31395) - the flags below the model
REM  line. Ported into the shared kvmem core, so the llama.cpp and ninfer
REM  backends take the SAME names and defaults:
REM
REM  (1) Action-oriented eviction  --kvmem-action-lrfu
REM      Blocks are kept by a temporal LRFU keep score instead of a one-shot
REM      attention score:  KS_t = lambda*KS_{t-1} + (top-p hit ? score : 0)
REM      lambda 0.50, top-p 0.90 are the paper's tuned values.
REM          lambda -> 0  pure recency (chases the newest query)
REM          lambda -> 1  pure frequency (working set freezes)
REM      The keep score is clamped at 0, so a block that scores badly can
REM      still come back; and two scoring passes inside one round no longer
REM      decay the pool twice. Remove all three flags to revert.
REM
REM  (2) GQA head aggregation      --kvmem-score-agg max
REM      ActKV Alg. 3 step 8: take the MAX attention over a GQA group's query
REM      heads instead of the folded mean probe. Better recall on multi-topic
REM      queries; cost is g x (g = n_head / n_head_kv) on the once-per-turn
REM      scoring pass, NOT per token. Change "max" to "mean" to revert.
REM
REM  (3) Confidence-driven budget  --kvmem-conf-*   [NOT ported / off]
REM      The ActKV monitor was dead code upstream (never called, budget never
REM      grew). The class is ported and unit-tested, but it is NOT wired into
REM      the generation loop, so there is no --kvmem-conf-* flag here yet.
REM --------------------------------------------------------------------
REM  Thinking: the external template defaults to medium when a request sends
REM  no reasoning_effort, so --reasoning-effort medium below matches it.
REM  FIXED FLAG NAME: there is NO --thinking-level option; the flag is
REM  --reasoning-effort LEVEL (none disables thinking).
REM ====================================================================

title KVMem server (llama.cpp backend) - Qwen3.8-27B-IQ3_S-mtp + ActKV

REM ---- locate the server binary --------------------------------------
set "ROOT=%~dp0"
set "EXE="
for %%D in ("%~dp0" "%~dp0bin" "%~dp0build-win\bin" "%~dp0build\bin" "%~dp0..\bin" "%~dp0..\build-win\bin" "%~dp0..\build\bin") do (
    if not defined EXE if exist "%%~fD\llama-kvmem-server.exe" set "EXE=%%~fD\llama-kvmem-server.exe"
)
if not defined EXE (
    echo ERROR: llama-kvmem-server.exe was not found.
    echo   Looked next to this script and in bin\, build-win\bin\, build\bin\.
    echo.
    echo Build it first, for example:
    echo   cd /d "%ROOT%"
    echo   git submodule update --init --depth 1
    echo   scripts\windows\build.ps1
    echo.
    pause
    exit /b 2
)
for %%F in ("%EXE%\..") do set "BIN=%%~fF"
cd /d "%BIN%"

echo Using %EXE%
echo.

"%EXE%" ^
  -m "I:\model\Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf" ^
  --mmproj "I:\model\mmproj-Qwen3.8-27B-Q5_K-MIX.gguf" --no-mmproj-offload --image-max-tokens 256 ^
  -lm none ^
  --jinja ^
  --chat-template-file "I:\templates\chat_template.jinja" ^
  --host 0.0.0.0  --port 18200 ^
  -c 262144 -n 16384  -b 512  -ngl 99 ^
  --kvmem-budget 36864 --kvmem-gen-reserve 16384 ^
  --kvmem-action-lrfu --kvmem-lrfu-lambda 0.50 --kvmem-lrfu-top-frac 0.90 ^
  --kvmem-score-agg max ^
  -ctk q8_0 -ctv q8_0 ^
  --spec-type draft-mtp --spec-draft-n-max 3 --spec-kv-dtype f16 ^
  --enable-thinking --reasoning-effort medium --reasoning-budget 8192 ^
  --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 ^
  --presence-penalty 0.0 --frequency-penalty 0.0 --repeat-penalty 1.0  --webui

set "RC=%ERRORLEVEL%"
echo.
echo Server exited with code %RC%.
pause
endlocal & exit /b %RC%
