@echo off
setlocal EnableExtensions

REM ====================================================================
REM  KVMem multi-backend launcher - ninfer backend
REM
REM  Same ActKV flags as start-kvmem-server.bat (llama.cpp backend), which
REM  is the point of the port: one option surface, two backends.
REM
REM  This is a thin wrapper over scripts\windows\start-backend.ps1, which
REM  owns the ninfer device/stage/validation rules. The ActKV flags travel
REM  through its -WorkerArgs and are appended verbatim to the worker argv.
REM
REM  NOTE the -Command form below. Calling the script with -File and
REM  -WorkerArgs 'a','b' binds the whole comma list as ONE string, so the
REM  worker would receive a single bogus argument. Verified: -Command makes
REM  each element a separate argv entry (39 args, four ActKV flags intact).
REM
REM  ninfer differences to keep in mind (docs/multi-backend-differences.md):
REM    * page/block size is FIXED at 64 tokens (llama.cpp uses 128 by default),
REM      so B, R, sink and recent are rounded to whole 64-token pages;
REM    * there is no --kvmem-method switch: with a complete query ninfer always
REM      scores by mean-K, so ActKV's LRFU is the only retention policy knob;
REM    * KV dtype names differ: int8/bf16/fp8/nvfp4/k8v4/rk8v4/... (not q8_0);
REM    * cold disk snapshots (--kvmem-disk-*) are disabled for ninfer.
REM
REM  ActKV flags used below:
REM    --kvmem-action-lrfu            action-oriented LRFU retention
REM    --kvmem-lrfu-lambda 0.50       paper's decay
REM    --kvmem-lrfu-top-frac 0.90     paper's top-p hit set
REM    --kvmem-score-agg max          Alg. 3 step 8 max over a GQA group
REM  Remove that one -WorkerArgs group for the A/B baseline; keep the rest.
REM ====================================================================

title KVMem server (ninfer backend) - model.ninfer + ActKV

set "ROOT=%~dp0"
set "PS=%ROOT%scripts\windows\start-backend.ps1"
if not exist "%PS%" (
    echo ERROR: start-backend.ps1 not found:
    echo   %PS%
    echo Run this .bat from the repository root.
    pause
    exit /b 2
)

REM Put a model with text, Vision and MTP in the repository root as
REM model.ninfer, or edit MODEL below. Use English paths and filenames.
set "MODEL=%ROOT%model.ninfer"
if not exist "%MODEL%" (
    echo ERROR: ninfer model not found:
    echo   %MODEL%
    echo Copy your .ninfer artifact there, or edit MODEL in this file.
    pause
    exit /b 2
)

REM GPU UUIDs in stage order, comma separated. Defaults to CUDA_VISIBLE_DEVICES
REM or 0. Uncomment and edit to pin specific cards (UUIDs, not indices):
set "GPU=%CUDA_VISIBLE_DEVICES%"
if not defined GPU set "GPU=0"

echo Starting ninfer backend at http://127.0.0.1:18200/  (Ctrl+C stops it)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%PS%' -Backend ninfer -Model '%MODEL%' -Gpu '%GPU%' -Port 18200 -Context 204800 -Concurrency 1 -Prefill 256 -MaxTokens 16384 -Budget 36864 -Reserve 16384 -HostMiB 12288 -RetainedSessions 1 -KvType int8 -MtpDrafts 4 -AdaptiveMtp -NgramDrafts 31 -Vision -VisionResidency cpu -VisionTokens 1024 -ThinkingBudget 8192 -SinkTokens 0 -RecentTokens 64 -WorkerArgs '--kvmem-action-lrfu','--kvmem-lrfu-lambda','0.50','--kvmem-lrfu-top-frac','0.90','--kvmem-score-agg','max'"

set "RC=%ERRORLEVEL%"
echo.
echo ninfer backend exited with code %RC%.
pause
endlocal & exit /b %RC%
