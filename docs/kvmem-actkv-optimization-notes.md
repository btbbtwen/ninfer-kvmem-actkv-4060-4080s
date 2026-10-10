# KVMem + ActKV 优化工作记录（ninfer 后端）

> 本文件记录 **2026-10-11 01:09** 的代码备份点、已实测的性能基线、以及待做的优化清单。
> 后续所有优化改动请以本文件的"备份点"为回滚基准。

---

## 一、备份点（优化起点）

### 1.1 三种备份方式

| 方式 | 标识 | 说明 |
|---|---|---|
| git tag | `backup/actkv-optimization-start-20261011-0100` | 指向 `e8bf14f`，只回滚历史 |
| 目录副本 | `I:\llama\kvmem-llama.cpp-multi-backend-framework-BACKUP-20261011-0100` | 4195 文件 / **5.14 GB**，含子模块与构建产物 |
| 状态快照 | `I:\dsh\html\backup-20261011-0100-info.txt` | 哈希与文件数记录 |

更早的原始状态备份仍然保留：`backup/pre-actkv-20261010`（= `eb746f7`，**ActKV 移植之前**）。

### 1.2 备份时的精确状态

```
HEAD               e8bf14f  chore(actkv): record the window-reselection trace and the rebuild trap
ninfer 子模块      8319e8f512477d0cfdef89e16c17c1ba9f9bc7b3
ninfer patched_tree c28ab5b3126d8c2c939ca77e34352cb205410625
patch_sha256       32ae3925a3aee3f00ab888c9aa1475b5d04088ece2cdf71019b84d7b41897e26
已跟踪文件          350
staged worker sha  84982758a1d57a365b8c0fa24e8e5033c5bd8e8908ba9ece32d047e085158128
构建产物            build-backends\ninfer\apps\ninfer-serve.exe  (512.5 MB)
```

### 1.3 回滚命令

```powershell
# 方式 1：只回滚 git 历史（保留构建产物，快）
git -C I:\llama\kvmem-llama.cpp-multi-backend-framework reset --hard backup/actkv-optimization-start-20261011-0100

# 方式 2：整目录还原（含构建产物与子模块，慢但彻底）
robocopy I:\llama\kvmem-llama.cpp-multi-backend-framework-BACKUP-20261011-0100 `
         I:\llama\kvmem-llama.cpp-multi-backend-framework /MIR
```

> `git status` 显示 `M backends/ninfer` 是**正常状态**：ninfer 子模块被打过 KVMem 补丁，
> 父仓库记录的 HEAD 与子模块实际 HEAD 不同。不是未提交的脏改动。

---

## 二、⚠️ 构建铁律（必读，否则会踩已踩过的坑）

**改动 `kvmem/include/kvmem/*.hpp` 中任何类的成员布局后，必须强制全量重编 ninfer 后端。**

`backends/ninfer` 通过 `-DNINFER_KVMEM_SOURCE_DIR` 引用仓库外的 `kvmem/` 目录，
其增量依赖**没有跟踪该目录头文件的变化**。只重编 `kvmem_store.cpp.obj` 会导致：

- ninfer 的 TU 按**旧 `sizeof(KvMemStore)`** 分配对象；
- 共享库代码按**新成员偏移**访问 → 写到对象边界之外；
- 症状：`0xC0000005`，且只在启用新功能时崩溃。

```powershell
$bd = 'I:\llama\kvmem-llama.cpp-multi-backend-framework\build-backends\ninfer'
Get-ChildItem $bd -Recurse -Filter '*.obj' | Remove-Item -Force
Get-ChildItem "$bd\kvmem-core" -Recurse -Filter '*.lib' | Remove-Item -Force
# 然后正常构建（约 25–35 分钟全量）
scripts\windows\build-backends.ps1 -Backend ninfer -CudaPath <cuda> -Jobs 10
```

只改 `.cpp` 实现（不动布局）时，增量重编是安全的（约 1 分钟）。

完整调查过程见 [kvmem-header-rebuild-trap.md](kvmem-header-rebuild-trap.md)。

---

## 三、性能基线（实测）

### 3.1 硬件与配置

| 项 | 值 |
|---|---|
| GPU | RTX 5060 Ti 16 GB（sm_120a，compute_cap 12.0） |
| 模型 | `Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.ninfer`（11.3 GiB 权重） |
| 后端 | ninfer（`infer-kvmem-server.exe`，512.5 MB，仅 sm_120a） |
| KV | `--kv-dtype int8`，KVMem `B=36864 R=16384`（物理池 53,248 token / 832 pages） |
| 推测 | `--spec mtp --draft-tokens 4 --ngram-draft-tokens 31 --ngram-min-match 12 --adaptive-mtp` |
| ActKV | `--kvmem-action-lrfu --kvmem-lrfu-lambda 0.50 --kvmem-lrfu-top-frac 0.90` |
| 采样 | `--temperature 1.0 --top-p 0.95 --top-k 20 --min-p 0.0` |

### 3.2 不同上下文长度的 prefill / decode

| 上下文（实际 token） | prefill | decode | MTP 接受 | ngram 接受 | 内容类型 |
|---|---|---|---|---|---|
| 5,686 | 929.7 tok/s | **190.2 tok/s** | 559/1,105 (50.6%) | 547/1,093，36 轮 | 可复制（重复行） |
| 13,583 | 984.6 tok/s | **198.8 tok/s** | 562/994 (56.5%) | 550/982，33 轮 | 可复制 |
| **27,349** | **916.7 tok/s** | **158.1 tok/s** | 652/1,289 (50.6%) | 640/1,277，43 轮 | 可复制 |
| 30,981 | 891.0 tok/s | 59.3 tok/s | 172/247 (69.6%) | — | 自由生成 |
| 46,843 | 850.7 tok/s | 59.3 tok/s | 71.4% | — | 自由生成 |
| 79,523 | 393.4 tok/s | 57.2 tok/s | 70.9% | — | 自由生成 |
| 101,049 | ≈376 tok/s | — | — | — | 单针 |
| 135,407 | 378.3 tok/s | — | — | — | 单针 |
| 254,557 | 370–372 tok/s | — | — | — | 单针 |

### 3.3 关键结论

1. **prefill 在 ~47k token 内保持 850–985 tok/s**；超过 `B+R = 53,248` 后掉到 **~390 tok/s**（约 2.3× 变慢）。
   掉速点是 **KVMem 物理池上限**，不是 ActKV。
2. **decode 速度几乎只取决于推测解码命中率，与上下文长度无关**：
   - ngram 有料可复制 → **158–199 tok/s**；
   - 自由生成（ngram 无料）→ **45–69 tok/s**。
   - 工作集被 `B+R` 钉住，注意力开销不随逻辑上下文增长。
3. **ngram 满载运行**：每轮复制约 **30 token**（上限 31），接受率 **50–56%**。
4. **MTP 接受率 50–70%**，波动取决于内容。

### 3.4 端到端召回测试（同配置，ActKV 全开）

| 测试 | 规模 | 结果 |
|---|---|---|
| 单针 250k | 254,557 token | ✅ 1/1，684 s |
| **25 针 · 多轮 · 工具调用** | 首轮 256,299 → 末轮 260,986 token | ✅ **25/25 召回，25/25 工具**，862.7 s（首轮 678 s，后续每轮 7–11 s） |

多轮前缀复用生效：末轮服务端日志 `cache 260,863 (100.0%, turn closure)`，TTFT 1.3 s。

---

## 四、待优化清单

> 全部基于**代码审查**；标注 `[实测]` 的已有数据支撑，`[推断]` 的需先验证。

### 4.1 高优先级

| 编号 | 项 | 位置 | 预期收益 | 成本 | 状态 |
|---|---|---|---|---|---|
| **A8** | `--kvmem-recent-tokens` 默认仅 **64（1 page）** | bat 参数 | 多轮场景高 | 改一行 | 待做 |
| **A1** | 打分时 `mean_k` 逐块逐层加锁（832 × layers 次） | `mean_key_retrieval.cpp:75` + `raw_kv_store.cpp:1001` | prefill 高 | 中 | 待做 |
| **A2** | `mean_k` 每次重算归一化（`k_sum[d] * inv`） | `raw_kv_store.cpp:1012` | prefill 中高 | 中 | 待做 |
| **A5** | LRFU 两遍遍历 `scores` + 额外 scratch 缓冲 | `kvmem_store.cpp:284-315` | 每轮一次，中 | 小 | 待做 |

### 4.2 中优先级

| 编号 | 项 | 说明 |
|---|---|---|
| **A3** | 打分内层标量循环显式 SIMD | `mean_key_retrieval.cpp:79-98`，MSVC `/O2` 未必自动向量化，预计 2–4× |
| **A4** | `--kvmem-score-agg max` 的 g 倍开销 | g = 24/4 = **6 倍**。当前已改 `mean`。若要回 `max`，做**两阶段**：mean 粗筛 top-N → 只对候选算 max |
| **A9** | LRFU 年龄粘性未根除 | 只对"本轮实际选入窗口"的块累积 KS（更贴近论文 action region） |
| **D2** | `pick_topk_ungrouped` 每轮重建候选数组 | `kvmem_store.cpp:699-717`，`candidates`/`kept` 提为成员复用 |
| **P1** | 打分路径批量取 mean | 与 A1 同一项 |

### 4.3 待实测决定

| 编号 | 项 | 说明 |
|---|---|---|
| **D3** | decode 重选滞后 | 已加 `KVMEM_WINDOW_TRACE`（提交 `e8bf14f`）但**尚未跑出结论**。若确认每步重选 → 加 `next_selection_at_`；若本来每 ~16384 token 一次 → 跳过 |

**推演（未实测）**：重选后 `removed_tokens = text_kv_valid - visible_tokens`，下一步
`compact_position ≈ visible_tokens ≈ B(36864) < 53248` → 可能**不是每步重选**。
用 trace 数 `KVMEM_WINDOW_TRACE reselect` 行数即可定性（约 3 分钟）。

### 4.4 参数实验（零代码改动）

| 参数 | 当前 | 建议试 | 理由 |
|---|---|---|---|
| `--prefill-chunk` | 256 | 512 | 减少 chunk 循环开销 |
| `--ngram-min-match` | 12 | 8 | 提高 ngram 命中率（接受率可能下降，需权衡） |
| `--kvmem-recent-tokens` | 未指定（=64） | 512–1024 | 保住最近内容，减少跨轮重选 |
| `--device-profile` | off | `calibrate` | 生成设备画像后可能提速 |

### 4.5 已完成的优化（供参考，勿重复）

| 项 | 提交 | 内容 |
|---|---|---|
| 轮次戳幂等 | `4377515` | `round_id == 0` 时由块前沿派生，避免同轮重复衰减 |
| 去掉每轮分配 | `4377515` | `lrfu_scratch_` 复用成员缓冲 |
| hit 需正证据 | `9533c2d` | 只在正分上取 top-p 阈值；零分块不累积（防止 KS 随页面年龄增长） |
| `score-agg` → `mean` | bat | 打分开销降到 1/6 |

---

## 五、建议执行顺序

1. **A8**（改 bat 一行）+ 顺带跑 **D3 trace** → 一次运行拿两个结论；
2. **A1 + A2**（打分路径，改共享库，prefill 直接受益）；
3. **A5 + D2**（小改动、零风险）；
4. 参数实验（4.4）；
5. 视需要做 A3 / A4 / A9。

---

## 六、相关文档索引

| 文档 | 内容 |
|---|---|
| [kvmem-header-rebuild-trap.md](kvmem-header-rebuild-trap.md) | ODR/ABI 错配崩溃的完整定位过程与构建规则 |
| [ninfer-server-build-record.md](ninfer-server-build-record.md) | ninfer 编译依赖（vcpkg）、三个构建坑、启动参数 |
| [actkv-port-implementation-record.md](actkv-port-implementation-record.md) | ActKV 移植到 multi-backend 的实现记录与验证 |
| [actkv-port-to-multibackend-plan.md](actkv-port-to-multibackend-plan.md) | 移植分析与落点映射 |
| [multi-backend-differences.md](multi-backend-differences.md) | llama.cpp 与 ninfer 两个后端的差异 |
