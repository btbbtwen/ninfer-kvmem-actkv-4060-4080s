# MiMo-V2.6-Distill-Qwen-9B 在 NInfer / KVMem 上的 8GB 实践（RTX 4060）

> 把 MiMo-V2.6-Distill-Qwen-9B 的两套 GGUF 转成 `.ninfer`，为 KVMem 引擎补齐 9B 几何，
> 并在 **RTX 4060 8GB** 上跑通 **262,144 上下文**、长 prompt 与 MTP 投机解码。
>
> 本文记录：转换方法、引擎 5 处适配、4 个启动脚本、实测数据与调参建议。
> 不包含模型权重；请从模型发布页自行获取 GGUF。

## 0. 成果一览

| 模型 | 产物 | 组件 | 产物大小 | 启动脚本 | 设备 KV |
|---|---|---|---:|---|---:|
| Q4_K_M + MTP | `MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP.ninfer` | `text,mtp` | 5.67 GiB | `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP-RTX4060.bat` | 6,144 |
| Q4_K_M + MTP | 同上 | 同上 | 同上 | `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP-RTX4060-rk8v4.bat` | 8,192（rk8v4） |
| Abliterated IQ3_M | `…-Abliterated-IQ3_M.ninfer` | `text` | 4.12 GiB | `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M-RTX4060.bat` | 81,920 |
| Abliterated IQ3_M | 同上 | 同上 | 同上 | `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M-RTX4060-98k.bat` | 98,304 |

共同点：

- 均**不含视觉组件**（没有 `--vision`），纯文本。
- `--max-context 262144`，即模型 native 上限；长历史通过 KVMem 的 host 层承载。
- 启动脚本自动选择 `CUDA_VISIBLE_DEVICES`，否则用 GPU 0。

## 1. 测试环境

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 4060，8,188 MiB，sm_89 |
| 内存 | 64 GB |
| 驱动 | 617.42 |
| 引擎 | `C:\AI\infer-actkv-40x-1.4\infer-kvmem-server.exe` |
| 引擎大小 / SHA256 | 824,065,024 B / `0948243B1D9C52C37A78E218F2873D93A850A73FB8CEA9F9B58D6642C0D8A424` |
| 转换工具 | `ninfer-convert` + Python 3.11（numpy / gguf / torch-cpu） |
| 产物 SHA256（IQ3） | `78CAEF808A947E8B4D3D63D367F8B20FBA011F5A13E107BB73E5F3A1F5EF0740` |
| 产物 SHA256（MTP） | `F4047B246EBE7DB33DB14CA80C7846B1E4097CD78F05C633E23609D3B9132439` |

## 2. 快速开始

把下面 4 个 bat 放在引擎目录（`infer-kvmem-server.exe` 旁边）：

| 脚本 | 端口 | 模型 | 关键参数 |
|---|---:|---|---|
| `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M-RTX4060.bat` | 18201 | IQ3_M | int8，B/R=65536/16384，host=12288 |
| `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M-RTX4060-98k.bat` | 18201 | IQ3_M | int8，B/R=81920/16384，host=12288 |
| `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP-RTX4060.bat` | 18200 | Q4_K_M-MTP | int8，B/R=4096/2048，host=8192，`--spec mtp` |
| `start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP-RTX4060-rk8v4.bat` | 18200 | Q4_K_M-MTP | rk8v4，B/R=6144/2048，host=8192，`--spec mtp` |

双击即可；或：

```bat
start-kvmem-ninfer-MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M-RTX4060.bat
```

**不要同时启动两个模型**：5.66 + 4.12 GiB 权重已超过 8GB 显存，必须二选一。

如果你的模型路径不同，只改 bat 里的：

```bat
set "MODEL=D:\models\你的模型.ninfer"
```

### 2.1 命令模板

IQ3（默认 81,920；98k 版把 `--kvmem-budget 65536` 改成 `81920`）：

```bat
"%EXE%" "%MODEL%" ^
  --port 18201 --model-id ninfer-mimo-9b-iq3 ^
  --max-context 262144 --prefill-chunk 256 ^
  --kvmem-budget 65536 --kvmem-gen-reserve 16384 --kvmem-host-mib 12288 ^
  --kvmem-lrfu --kv-dtype int8 ^
  --temperature 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 ^
  --default-reasoning-effort xhigh --default-thinking-budget 16384
```

MTP（int8；rk8v4 版把 `--kv-dtype` 改 `rk8v4`，B/R 改 `6144/2048`）：

```bat
"%EXE%" "%MODEL%" ^
  --port 18200 --model-id ninfer-mimo-9b-mtp ^
  --max-context 262144 --prefill-chunk 256 ^
  --kvmem-budget 4096 --kvmem-gen-reserve 2048 --kvmem-host-mib 8192 ^
  --kvmem-lrfu --kv-dtype int8 ^
  --spec mtp --draft-tokens 4 --ngram-draft-tokens 31 --adaptive-mtp ^
  --temperature 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 ^
  --default-reasoning-effort xhigh --default-thinking-budget 4096
```

## 3. GGUF → ninfer 转换

### 3.1 输入

- `MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP.gguf`（含 `blk.32.nextn.*`）
- `MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M.gguf`（IQ3_S/Q4_K 混合，无 MTP）
- MiMo 官方 `config.json` / tokenizer 等 7 个 metadata 文件

### 3.2 命令

```powershell
# MTP 版：text + mtp
powershell -NoProfile -ExecutionPolicy Bypass -File .\convert-to-ninfer.ps1 `
  -MainGguf .\MiMo-V2.6-Distill-Qwen-9B-Q4_K_M-MTP.gguf -TextOnly -NonInteractive

# IQ3 版：text
powershell -NoProfile -ExecutionPolicy Bypass -File .\convert-to-ninfer.ps1 `
  -MainGguf .\MiMo-V2.6-Distill-Qwen-9B-Abliterated-IQ3_M.gguf -TextOnly -NonInteractive
```

转换配方为 `mimo2_6_9b_gguf`：

- 32 层、hidden 4096、16 attention heads、4 KV heads、GDN 16 key / 32 value heads。
- Q4_K / Q6_K / IQ3_S 块**按原字节搬运**，不重新量化。
- IQ3_M 的 `ssm_alpha` / `ssm_beta` 被量化成 IQ3_S，配方会先反量化再写 BF16。
- MTP 的 `blk.32.*` 作为独立组件接入共享输出头。
- 产物组件与字节均已用 `tools.artifact.inspect` 和逐字节抽样校验。

## 4. 引擎适配（5 处）

原始 KVMem 分支硬编码了 27B/35B 几何。9B 需要：

### ① 融合输入投影：`src/ops/weight_input.cpp`

新增 9B 几何：

- attention：Q `{4096,4096}`、K `{1024,4096}`
- GDN：Q/K `{2048,4096}`、V/Z `{4096,4096}`

放行 `dense9` 到 GGUF-parent / multi-parent / single-parent-format 三条路径；
三输入 QKV 放行 `q=={4096,4096}`；GDN A/B 放行 `{32,4096}` 并允许 32 头分离父矩阵。

### ② GDN gating 投影

`bf16_gdn_gating_proj_kernels.cu/.h`、`plan.cpp`、`wrapper/gdn_gating_proj.cpp`：

- SIMT 内核按 `Heads=32, Hidden=4096` 实例化，新增 `k9N=32 / k9K=4096` 路由。
- 9B 走通用 SIMT，不是 27B/35B 的调优 MMA；正确但偏慢。

### ③ Attention 16q / 4kv

- `geometry.cuh` 新增 `CausalD256H16Kv4 = CausalAttentionGeometry<16,4,2>`（GroupSize=4）。
- `causal_softmax_attention.cpp` 的 `require_causal_geometry` 放行 `(16,4)`。
- `small_t.cu`、`prompt.cu` 及 fp8/k8v4/nvfp4 存储专属路径都按 `q_heads==16 && num_kv_heads==4` 派发到 H16Kv4。
- 补了 12 个 `launch_small_t_i8<CausalD256H16Kv4, W, …>` 显式实例化，并修正 GroupSize=4 下的 warp/`PVNtPerWarp` 选择。

### ④ GGUF GDN profile：`src/ops/wrapper/gdn_input_proj.cpp`

把 27B 常量换成按 hidden 选择：

```cpp
struct GgufGdnProfile { hidden, qkv_rows, z_rows, query_key, value };

case 5120: return {5120, 10240, 6144, 2048, 6144}; // 27B
case 4096: return {4096,  8192, 4096, 2048, 4096}; // MiMo 9B
```

`gdn_projected_conv.cu` 原本已有 `8192/2048/2048/4096` 分支，正好命中 9B，无需改。

### ⑤ MTP split：`mtp_pack.cpp/.cu/.cuh`

把写死的 `14336 / 6144 / 1024 / 24 头` 改成运行时推导：

- `q_rows = q.ne[0]*q.ne[1]`，`kv_rows = k.ne[0]*k.ne[1]`
- `attn_rows = 2*q_rows + 2*kv_rows`
- 9B 得到 `10240 / 4096 / 1024 / 16 头`，27B 仍是 `14336 / 6144 / 1024 / 24 头`。

27B 与 35B 的既有路由未改动；构建目标 `sm_89`，无 `sm_120a`。

> 原始官方 v0.18.0 二进制仍只支持 27B/35B，会报
> `input projection: unsupported logical projection geometry`。
> 必须使用带上述 5 处补丁的引擎构建。

## 5. 实测数据

### 5.1 IQ3 非 MTP

启动：

```
weights ready | 4.11 GiB | 1.5s
capacity | KV 81,920 tokens, int8 | runtime 1.59 GiB | free ~756–890 MiB
```

短请求 decode：**约 40 tok/s**（24 prompt / 100 output）。

长上下文（同一 81,920 设备 KV 配置）：

| prompt tokens | 总耗时 |
|---:|---:|
| 6,917 | 4.2 s |
| 13,817 | 7.7 s |
| 27,617 | 16.1 s |
| 73,617 | 54.2 s |
| 115,017 | 199.6 s |
| **247,267** | **498.8 s** |

结论：**IQ3 的 262,144 native 上下文可用**；247k 已实测返回。

### 5.2 MTP + 投机解码

启动（int8，B/R=4096/2048）：

```
weights ready | 5.66 GiB | 2.1s
capacity | KV 6,144 tokens, int8 | runtime 772.8 MiB | free ~489 MiB
```

生成：

- decode **21–24 tok/s**
- `mtp accepted` 在散文提示下 **7–14%**
- 输出连贯；投机解码路径正常

长上下文：

| prompt tokens | 配置 | 总耗时 |
|---:|---|---:|
| 13,817 | B/R=4096/2048（最终 bat） | 15.9 s |
| 27,617 | B/R=4096/2048 | 33.2 s |
| 73,617 | B/R=4096/2048 | 93.9 s |
| **247,267** | B/R=6144/2048 | 366.4 s |

> 247k 是在 B=6144 下测的；最终 bat 为了 8GB 稳定性用了 B=4096，73.6k 已验证。
> 262,144 的逻辑上限不变，超出设备 B 的历史由 host-mib 8GB 承载。

### 5.3 rk8v4 变体

- `--kv-dtype rk8v4`，B/R=6144/2048 → 8,192 tokens，剩约 530 MiB。
- decode 20.6 tok/s，accepted 7.1%（同一散文提示）。
- 输出连贯；KV 质量有轻微 trade-off，适合想多留设备历史/降低 KV 占用时使用。

## 6. 为什么 MTP 反而更慢？

在这台 4060 上：

| 模型 | decode |
|---|---:|
| IQ3 非 MTP（4.12 GiB） | **~40 tok/s** |
| MTP + spec（5.66 GiB） | 21–24 tok/s |
| MTP + rk8v4 | 20.6 tok/s |

原因是散文提示下 MTP 接受率只有 7–14%，draft + verify 的开销大于省下的步数；同时 5.66 GiB 权重让显存更紧、可用带宽更少。

**日常使用建议优先 IQ3 非 MTP**；MTP 版适合：
- 代码/工具调用等 ngram 命中率更高的场景（未实测）；
- 需要验证 MTP 路径或与 27B 对齐；
- 不在意速度、想保留 MTP 头。

## 7. KVMem 调参

`--kvmem-budget B` 是设备上保留的 selected history，`--kvmem-gen-reserve R` 是 append 工作区；
二者共同占用设备 KV，`--kvmem-host-mib` 是 host 侧历史预算。

### IQ3（4.12 GiB，int8）

| B / R | 设备 KV | 启动 | 启动后剩余 | 建议 |
|---|---:|---|---:|---|
| 65536 / 16384 | 81,920 | OK | 756–890 MiB | 默认，平衡 |
| 81920 / 16384 | 98,304 | OK | ~750 MiB | **98k bat**，更多设备历史 |
| 98304 / 16384 | 114,688 | OK | ~396 MiB | 激进 |
| 114688 / 16384 | 131,072 | OK | ~123 MiB | 不推荐 |
| 131072 / 16384 | 147,456 | 失败 | — | 差约 57 MiB |

### MTP（5.66 GiB）

| KV 格式 | B / R | 设备 KV | 启动后剩余 | 说明 |
|---|---|---:|---:|---|
| int8 | 4096 / 2048 | 6,144 | ~489 MiB | 默认、稳 |
| int8 | 6144 / 2048 | 8,192 | 有时只差约 4 MB | 桌面显存一波动就失败 |
| rk8v4 | 6144 / 2048 | 8,192 | ~530 MiB | rk8v4 bat |
| rk8v4 | 8192 / 2048 | 10,240 | 差 52 KB | 当前环境失败 |
| rk4v4 | 12288 / 4096 | 16,384 | ~444 MiB | 可起，质量 trade-off 更大 |

经验：

- 8GB 上桌面/浏览器会占用 1–2 GB，**启动前预留 300–500 MiB** 才稳。
- `--lm-head-q4` 对 GGUF 输出头不适用（要求 row-split Q8）。
- 想扩大上下文优先加 `--kvmem-host-mib` / 用 IQ3；设备 B/R 不要贴着上限。
- 超过 262,144 需要 YaRN/rope scaling；本文未验证。

## 8. 已知限制

- **8GB 只能二选一**，且 MTP 版余量很小。
- 无视觉组件；要视觉需重新转带 `mmproj` 的产物并加 `--vision`。
- MTP 在散文上比 IQ3 慢；投机解码不是无条件加速。
- 长 prompt prefill 很慢：247k tokens 约 6–8 分钟（4060）。
- rk8v4/rk4v4 是质量换容量，未做严格困惑度对比。
- 引擎补丁是 9B 专用；27B/35B 路径未回归测试（仅代码层保持原分支）。

## 9. 校验

```powershell
# 产物结构与组件
python -m tools.artifact.inspect MiMo-...-MTP.ninfer --json

# 逐字节抽样：token embedding / output head / GDN out / attention key /
# MTP input projection / MTP MLP / MTP attention key
# 直接张量全量：norms / A_log / conv / a,b_projection
```

本文两套产物的组件、块字节与直接张量均已比对通过；转换器侧详见
`ninfer-convert/tools/convert/mimo2_6_9b_gguf.py`。

## 10. Credits / License

- 推理引擎：NInfer（`Neroued/ninfer` 上游）与 KVMem 分支（`iamwavecut/ninfer-all`），
  遵循其各自许可证。
- 模型：MiMo-V2.6-Distill-Qwen-9B 及其 Abliterated/IQ3_M 量化，遵循模型发布方许可证。
- 本文只记录转换、引擎适配与在 RTX 4060 8GB 上的实测配置，不附带权重。
- 与小米、阿里、NInfer/KVMem 作者无隶属关系。

---

## English summary

This repository documents converting MiMo-V2.6-Distill-Qwen-9B GGUF checkpoints to
NInfer `.ninfer` artifacts and running them with the KVMem engine on an 8 GB RTX 4060.

- Two artifacts: Q4_K_M + MTP (text+mtp) and Abliterated IQ3_M (text).
- Five engine patches add the 9B geometry (hidden 4096; attention 16q/4kv;
  GDN 32 value heads; GGUF GDN profile; MTP q/k/v/z split).
- Both artifacts accept `--max-context 262144`; a 247,267-token prompt was verified.
- Four launchers cover IQ3 (81,920 / 98,304 device KV) and MTP (int8 / rk8v4).
- On this 8 GB card IQ3 decodes at ~40 tok/s; MTP with speculative decoding is slower
  (21–24 tok/s) because draft acceptance is only 7–14% on prose.
- Do not run both models at once on 8 GB.
