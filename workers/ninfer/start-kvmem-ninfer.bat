@echo off
setlocal EnableExtensions

REM ====================================================================
REM  KVMem multi-backend launcher - ninfer backend
REM
REM  Same shape as start-kvmem-server.bat (llama.cpp backend): ONE command
REM  line, every parameter written out inline. Edit a value, save, run it.
REM  Ctrl+C stops the server.
REM
REM  EDITING RULE: every parameter line except the LAST one must end with " ^".
REM  A trailing "^" followed by a blank line sends an empty argument, and the
REM  server then exits with 'unknown argument: ' before it loads anything.
REM  The last parameter line must have no "^" at all.
REM
REM      start-kvmem-server.bat           llama.cpp backend, ActKV ON
REM      start-kvmem-server-noactkv.bat   same line with the ActKV flags removed
REM      start-kvmem-ninfer.bat           THIS FILE - ninfer backend, ActKV ON
REM
REM  The worker is taken from workers\ninfer\ of the repository this script
REM  sits in (where the ninfer build is staged); CUDA_VISIBLE_DEVICES pins the
REM  card by UUID.
REM
REM  Alternative launcher: scripts\windows\start-backend.ps1 -Backend ninfer
REM  wraps the same worker and adds device/stage/alignment validation. This
REM  .bat trades those checks for the plain inline form, like the llama one.
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
REM      The keep score is clamped at 0 and two scoring passes inside one round
REM      no longer decay the pool twice. Remove all three flags to revert.
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
REM
REM  An invalid ActKV value is rejected at startup, e.g.
REM      --kvmem-lrfu-lambda 1.5      -> "must be in (0, 1)"
REM      --kvmem-score-agg bogus      -> "must be mean or max"
REM --------------------------------------------------------------------
REM  ninfer differs from the llama.cpp backend (docs/multi-backend-differences.md):
REM    * page/block size is FIXED at 64 tokens (llama.cpp defaults to 128), so B,
REM      R, sink and recent round down to whole 64-token pages;
REM    * there is NO --kvmem-method: with a complete query ninfer always scores
REM      by mean-K, so ActKV's LRFU is the only retention-policy knob here;
REM    * KV dtype names are int8/bf16/fp8/nvfp4/k8v4/rk8v4/... - not q8_0;
REM    * cold disk snapshots (--kvmem-disk-path / --kvmem-disk-mib) are refused.
REM ====================================================================

title KVMem server (ninfer backend) - Qwen3.8-27B-IQ3_S-mtp + ActKV

REM ---- locate the worker, and the repository root that owns it ---------
REM This script may sit in the repository root (normal) or next to the worker
REM in workers\ninfer (copied there, the way the llama launcher lives in its
REM bin\ directory). ROOT must still name the repository root either way,
REM because model.ninfer and the error text below refer to it.
REM
REM Plain sequential "if" tests, no "else if": cmd parses "else if" as an else
REM whose command happens to be another if, and the set inside a skipped
REM branch can be lost depending on how the script was invoked. That left
REM %EXE% empty, and an empty %EXE% makes cmd fail with 9009 while the
REM continuation line's remaining tokens are run as commands.
set "EXE="
set "BIN="
set "ROOT="
if exist "%~dp0workers\ninfer\infer-kvmem-server.exe" if not defined BIN (
    set "ROOT=%~dp0"
    set "BIN=%~dp0workers\ninfer"
)
if exist "%~dp0infer-kvmem-server.exe" if not defined BIN (
    set "ROOT=%~dp0..\.."
    set "BIN=%~dp0"
)
if exist "%~dp0..\..\workers\ninfer\infer-kvmem-server.exe" if not defined BIN (
    set "ROOT=%~dp0..\.."
    set "BIN=%~dp0..\..\workers\ninfer"
)
if exist "%~dp0..\workers\ninfer\infer-kvmem-server.exe" if not defined BIN (
    set "ROOT=%~dp0.."
    set "BIN=%~dp0..\workers\ninfer"
)
if defined BIN set "EXE=%BIN%\infer-kvmem-server.exe"
if not defined ROOT set "ROOT=%~dp0"
if not defined EXE (
    echo ERROR: ninfer worker not found. Looked in:
    echo   %~dp0workers\ninfer
    echo   %~dp0
    echo   %~dp0..\..\workers\ninfer
    echo   %~dp0..\workers\ninfer
    echo.
    echo Build and stage it first:
    echo   git submodule update --init --depth 1
    echo   scripts\windows\build-backends.ps1 -Backend ninfer -CudaPath "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4" -Jobs 10
    echo   then copy build-backends\ninfer\apps\ninfer-serve.exe to workers\ninfer\infer-kvmem-server.exe
    echo   ^(docs\ninfer-server-build-record.md has the full recipe^)
    echo.
    pause
    exit /b 2
)

REM ---- model: repo-root model.ninfer, else the artifact this box keeps
set "MODEL=%ROOT%\model.ninfer"
if not exist "%MODEL%" set "MODEL=I:\llama\ninfer-server-rtx50-sm120a-ui\Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.ninfer"
if not exist "%MODEL%" (
    echo ERROR: ninfer model not found:
    echo   %MODEL%
    echo Put a .ninfer artifact in the repository root as model.ninfer,
    echo or edit MODEL above.
    pause
    exit /b 2
)

REM ---- GPU: ninfer wants UUIDs, comma separated in stage order --------
REM An existing CUDA_VISIBLE_DEVICES wins; otherwise this box's RTX 5060 Ti.
if defined CUDA_VISIBLE_DEVICES (set "GPU=%CUDA_VISIBLE_DEVICES%") else (set "GPU=GPU-dba26dd9-930f-1b01-2b05-22c1fca6cdce")
set "CUDA_VISIBLE_DEVICES=%GPU%"

REM ---- last guard before the command line -----------------------------
REM An empty %EXE% would make cmd run "" and then treat the continuation
REM line's tokens as commands, reporting "not recognized ... 9009" for a
REM parameter instead of naming the real problem.
if not defined EXE (
    echo ERROR: worker path resolved empty; cannot start.
    echo   BIN=%BIN%
    echo   ROOT=%ROOT%
    pause
    exit /b 2
)
if not exist "%EXE%" (
    echo ERROR: worker missing at resolved path:
    echo   %EXE%
    pause
    exit /b 2
)
if not defined BIN set "BIN=%~dp0"

cd /d "%BIN%"

"%EXE%" ^
  "%MODEL%" ^
  --port 18200 ^
  --max-context 262144 --kvmem-recent-tokens 1024 ^
  --prefill-chunk 256 ^
  --kvmem-budget 36864 --kvmem-gen-reserve 16384 --kvmem-host-mib 12288 --device-profile off ^
  --kvmem-action-lrfu --model-id ninfer-27b ^
  --kv-dtype int8 --temperature 1.0 --top-p 0.95 ^
  --spec mtp --draft-tokens 4 --ngram-draft-tokens 31 --adaptive-mtp ^
  --vision --vision-residency cpu --vision-max-merged 1024 ^
  --default-reasoning-effort medium --default-thinking-budget 16384
 
set "RC=%ERRORLEVEL%"
echo.
echo Server exited with code %RC%.
pause
endlocal & exit /b %RC%
