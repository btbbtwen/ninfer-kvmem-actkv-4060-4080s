# ninfer server 编译与启动记录（Windows / RTX 5060 Ti sm_120a）

| 项目 | 值 |
|---|---|
| 目标 | `backends/ninfer`（pin `8319e8f5` + `backends/patches/ninfer-kvmem.patch`，含本次 ActKV 移植） |
| 构建脚本 | `scripts/windows/build-backends.ps1 -Backend ninfer` |
| 工具链 | VS 2022 Professional 14.44.35207 + CUDA 13.4.59 + CMake 4.3.1 + Ninja 1.13 |
| 依赖 | vcpkg x64-windows：`ffmpeg[zlib] 9.0.2`、`curl 8.22.0`、`pkgconf 3.0.3` |
| 卡 | RTX 5060 Ti 16 GB（`GPU-dba26dd9-930f-1b01-2b05-22c1fca6cdce`），`-DCMAKE_CUDA_ARCHITECTURES=120a` |
| 日期 | 2026-10-10 |

---

## 1. 为什么必须先装 vcpkg 依赖

`backends/ninfer/cmake/Dependencies.cmake` 在 Windows 上**硬性要求**一个预置的 vcpkg triplet 树：

```cmake
if(WIN32 AND NOT VCPKG_TOOLCHAIN AND DEFINED ENV{VCPKG_ROOT} AND DEFINED ENV{VCPKG_TARGET_TRIPLET})
  list(APPEND CMAKE_PREFIX_PATH "$ENV{VCPKG_ROOT}/installed/$ENV{VCPKG_TARGET_TRIPLET}")
endif()
if(WIN32 OR DEFINED VCPKG_TARGET_TRIPLET)
  find_package(FFMPEG REQUIRED)   # cmake/FindFFMPEG.cmake 读 $ENV{VCPKG_ROOT}/installed/$ENV{VCPKG_TARGET_TRIPLET}
```

`FindFFMPEG.cmake` 只认 `$VCPKG_ROOT/installed/$VCPKG_TARGET_TRIPLET/{include,lib}` 里的
`libavformat/libavcodec/libavutil/libswscale`；curl 走 vcpkg 的 CMake 包。**没有 vcpkg 就配置不过**。

## 2. 三个坑（都实际踩到过）

### 2.1 `bootstrap-vcpkg.bat` 下载失败
`scripts/tls12-download.exe` 在本机 TLS 环境下拉不下 `vcpkg.exe`。改用 Git 自带的 OpenSSL 版 curl：

```powershell
$gcurl = 'D:\git\Git\mingw64\bin\curl.exe'      # 系统 curl.exe 走 schannel，同样失败
& $gcurl -L -o I:\vcpkg\vcpkg.exe `
  https://github.com/microsoft/vcpkg-tool/releases/download/2026-09-26/vcpkg.exe
Set-Content -Value "" -Path I:\vcpkg\vcpkg.disable-metrics -Force
```

### 2.2 显式设 `VCPKG_VISUAL_STUDIO_PATH` 反而失败
设置后 vcpkg 报 `Unable to find a valid Visual Studio instance`（尽管该 VS 的 `vcvarsall.bat` 完全可用）。
**不要设这个变量**，让 vcpkg 自己用 vswhere 找（它会正确选中 VS 2022 Professional 的 14.44 工具集）。

### 2.3 VS 的 dev shell 会覆盖 `VCPKG_ROOT`（关键）
`vcvarsall.bat` 会设置 `VCPKG_ROOT=<VS>\VC\vcpkg`（VS 自带、**没有安装任何包**）。
而 `build-backends.ps1` 在 `$env:VSCMD_VER` 为空时会自己调用 `Launch-VsDevShell`，
于是我们设的 `VCPKG_ROOT=I:\vcpkg` 被覆盖 →
`FindFFMPEG.cmake` 去找 `d:\...\VC\vcpkg/installed/x64-windows/lib`，报 `missing swscale.lib`。

**正确做法**：自己先把 vcvars 导进当前进程（这同时设置了 `VSCMD_VER`，脚本就会跳过它自己的 dev shell），
然后再覆盖 `VCPKG_ROOT`：

```powershell
$vcvars = 'D:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat'
cmd /c "call `"$vcvars`" >nul 2>&1 && set" | ForEach-Object {
  if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
$env:VCPKG_ROOT = 'I:\vcpkg'          # 必须在导入之后
$env:VCPKG_TARGET_TRIPLET = 'x64-windows'
```

## 3. 安装依赖

```powershell
$env:VCPKG_ROOT='I:\vcpkg'; $env:VCPKG_DEFAULT_TRIPLET='x64-windows'; $env:VCPKG_MAX_CONCURRENCY='12'
I:\vcpkg\vcpkg.exe install ffmpeg[zlib]:x64-windows curl:x64-windows pkgconf:x64-windows
```
实测 **18 分钟**（其中 ffmpeg 源码编译 17 分钟；curl 1.5 分钟）。
注意本机有代理 `127.0.0.1:7890`，vcpkg 会自动使用；msys2 镜像偶尔 TLS 失败会自动换源重试。

> `bootstrap-vcpkg.bat` 失败后**不需要**再跑它；手动放好 `vcpkg.exe` 即可。

## 4. 编译

```powershell
# 在导入了 vcvars 并覆盖 VCPKG_ROOT 的同一个 PowerShell 里
Set-Location I:\llama\kvmem-llama.cpp-multi-backend-framework
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\build-backends.ps1 `
    -Backend ninfer -CudaPath 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4' -Jobs 10
```

脚本会依次：`prepare-backends.py --backend ninfer`（拉子模块 + 校验并应用补丁）→
CMake 配置（`-DNINFER_KVMEM_SOURCE_DIR=<repo>/kvmem`、`-DNINFER_BUILD_APPS=ON`、`120a`、`NINFER_SM120_NATIVE=ON`）→
构建目标 `ninfer-serve`。

产物：`build-backends/ninfer/apps/serve/ninfer-serve.exe`（约 0.5 GB，含整张 kernel image）；
`ninfer_stage_runtime_dlls` 会把 vcpkg 的 FFmpeg/curl DLL 一起铺到该目录。

## 5. 放到启动器期望的位置

`scripts/windows/start-backend.ps1` 与 `start-kvmem-ninfer.bat` 找的是
`workers/ninfer/infer-kvmem-server.exe`（发行包的命名），所以编译后需要 staging：

```powershell
$src = 'build-backends\ninfer\apps\serve'
$dst = 'workers\ninfer'
New-Item -ItemType Directory -Path $dst -Force
Copy-Item "$src\ninfer-serve.exe" "$dst\infer-kvmem-server.exe"
Copy-Item "$src\*.dll" $dst                    # FFmpeg/curl/z 已由 CMake 铺好
Copy-Item 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4\bin\cublas64_13.dll'   $dst
Copy-Item 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4\bin\cublasLt64_13.dll' $dst
```

## 6. 启动（参数）

`start-kvmem-ninfer.bat` 已按本机配置好：模型 `Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.ninfer`、
GPU UUID `GPU-dba26dd9-…`、端口 18200、`-Budget 36864 -Reserve 16384 -HostMiB 12288`、
`-KvType int8`、`-MtpDrafts 4 -AdaptiveMtp -NgramDrafts 31`、Vision（CPU 驻留）、
以及 ActKV 四个标志 `--kvmem-action-lrfu --kvmem-lrfu-lambda 0.50 --kvmem-lrfu-top-frac 0.90 --kvmem-score-agg max`。

ninfer 侧与 llama 侧的差异（`docs/multi-backend-differences.md`）：page/block **固定 64 token**；
**没有 `--kvmem-method`**；KV 类型名是 `int8/bf16/fp8/nvfp4/k8v4/rk8v4/...` 而不是 `q8_0`；冷盘快照禁用。

## 7. 当前状态（2026-10-10）

| 项目 | 状态 |
|---|---|
| 编译 | ✅ 完成。`ninfer-serve.exe` **512.5 MB**，sm_120a / CUDA 13.4 / VS 2022 14.44，构建脚本 exit 0 |
| ActKV 是否进二进制 | ✅ `--help` 列出四个参数；`--kvmem-lrfu-lambda 1.5` 与 `--kvmem-score-agg bogus` 都被新加的范围校验拒绝 |
| staging | ✅ `workers/ninfer/infer-kvmem-server.exe` + FFmpeg/curl DLL + `cublas64_13.dll` / `cublasLt64_13.dll`（cublas 需从 `CUDA\v13.4\bin\x64\` 取，`bin\` 下没有） |
| 独立可运行 | ✅ `workers\ninfer\infer-kvmem-server.exe --help` 正常 |
| **实际加载模型 / 起服务** | ❌ **未做**。本机只有一张 RTX 5060 Ti（16 GB），启动时被另一个进程占满，见下 |

### 启动前的资源检查

编译验证时本机的占用情况：

```
nvidia-smi memory.used = 15891 MiB / 16311 MiB     # 只剩约 420 MiB
18200 端口被 PID 27316 占用
  I:\llama\kvmem-llama.cpp-0.17.0\build-cuda13.4-dll\bin\llama-kvmem-server.exe
```

ninfer 要加载 11.5 GB 的 `Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.ninfer`，**必须先停掉那个进程**（它没有数据损失，
之后用 `I:\llama\kvmem-llama.cpp-0.17.0\build-cuda13.4-dll\bin\start-llama-server.bat` 可原样重启）：

```powershell
Stop-Process -Id 27316                 # 或按 PID 重新确认后停
# 确认显存空出来后：
I:\llama\kvmem-llama.cpp-multi-backend-framework\start-kvmem-ninfer.bat
# 就绪判断：curl http://127.0.0.1:18200/health
```

本次按用户选择**没有启动**，所以"能不能真正加载模型并服务请求"这一点**仍未经验证**（见实现记录里的未验证项）。

