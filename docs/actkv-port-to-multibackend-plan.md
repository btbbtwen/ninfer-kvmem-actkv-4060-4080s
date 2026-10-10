# ActKV → multi-backend-framework 移植分析

| 项目 | 内容 |
|---|---|
| 源 | `I:\llama\kvmem-llama.cpp-0.17.0\kvmem-actkv`（ActKV tree，304 文件，含 LRFU + 置信度监控） |
| 目标 | `I:\llama\kvmem-llama.cpp-multi-backend-framework`（328 文件，driver + llamacpp/ninfer 双后端） |
| 日期 | 2026-10-10 |

---

## 0. 结论先说

**可移植，而且比预想的好。** 关键原因：

> multi-backend 把**检索打分抽成了共享模块** `kvmem/{include,src}/…/mean_key_retrieval.{hpp,cpp}`，llama.cpp 和 ninfer **都调它**（ninfer 补丁里就是 `state.selector->set_retrieval_scores(kvmem::mean_key_retrieval_scores(...))`）。
> 所以 ActKV 最核心的"**打分更准**"那部分，**改一处、两个后端同时受益**；真正要分两遍做的只有"接线 + 命令行选项"。

**但 mb 里 ActKV 符号为 0**，是从零移植：

| 符号 | 在 mb 出现文件数 |
|---|---|
| `accumulate_lrfu` / `action_lrfu` / `lrfu_score` | **0** |
| `score_agg` | **0** |
| `confidence_monitor` / `observe_confidence` | **0** |

---

## 1. 两棵树的距离

| 指标 | 值 |
|---|---|
| 文件数 | ActKV 304 / mb 328 |
| 仅 ActKV 有 | 38（ActKV 文档、NIAH 脚本、Windows 构建脚本、confidence_monitor…） |
| 仅 mb 有 | 62（driver、`backends/`、`mean_key_retrieval`、`memory_contract`、lane 相关…） |
| 两边都有但不同 | 71 |
| 完全相同 | 195 |

关键文件行差（`+ 是 ActKV 侧独有，− 是 mb 侧独有`）：

| 文件 | + | − | 含义 |
|---|---:|---:|---|
| `kvmem/include/kvmem/kvmem_store.hpp` | 72 | 33 | 结构近似，落点都在 |
| `kvmem/src/host/kvmem_store.cpp` | 85 | 53 | 同上 |
| `kvmem/tests/kvmem_store_test.cpp` | 199 | 0 | ActKV 侧纯新增（8 个 LRFU 测试） |
| `kvmem/CMakeLists.txt` | 8 | 20 | 需手工对齐 |
| `src/adapter/llama-memory-kvmem.cpp` | 522 | **777** | mb 侧改动大，需按函数块定位 |
| `src/adapter/llama-kvmem-hooks.h` | 47 | 40 | 参数结构需按字段对齐 |
| `tools/llama-kvmem-cli.cpp` | 130 | 16 | 选项链需合并 |
| `tools/llama-kvmem-server.cpp` | 1776 | **337** | mb 侧大改（driver 拆分） |

**落点确认**（mb 里都还在）：`score_retrieval()` @ `src/adapter/llama-memory-kvmem.cpp:4226`、`pick_topk_ungrouped` @ `kvmem_store.cpp:487`、`set_retrieval_scores` @ `kvmem_store.hpp:442`、`select_budget_tokens()` / `runtime_select_budget_` @ `kvmem_store.hpp:362-371`、`KvMemMethod` @ `:145`。

---

## 2. 目标架构：一半共享、一半每后端

```
kvmem/ 宿主库（两后端共用）
├── kvmem_store.{hpp,cpp}          ← 选择 / 驱逐 / LRFU 的天然落点
├── mean_key_retrieval.{hpp,cpp}   ← 【新】打分内核，score_agg/精确质量改这里
├── memory_contract / kv_content / native_kv_payload / request_control
└── …

适配层（每个后端一份）
├── llama.cpp 后端（in-tree）
│   ├── src/adapter/llama-memory-kvmem.{h,cpp}   ← score_retrieval / apply_selection 接线
│   ├── src/adapter/llama-kvmem-hooks.h          ← llama_kvmem_params 新字段
│   ├── tools/llama-kvmem-cli.cpp                ← 选项链 eq(arg,"--kvmem-…")  cli:153-213
│   └── tools/llama-kvmem-server.cpp             ← 同一风格选项链  server:950+
└── ninfer 后端（patch 级）
    └── backends/patches/ninfer-kvmem.patch      ← 900 KB：自带 44 处 --kvmem-* 选项 + 自己的 selector 调用
```

---

## 3. ActKV 每一项 → 落点映射

| # | ActKV 改动 | ActKV 侧位置 | mb 落点 | 难度 |
|---|---|---|---|---|
| 1 | `lrfu_score` 字段 + `action_lrfu`/`lrfu_lambda`/`lrfu_top_frac` 配置 | `kvmem_store.hpp` | 同文件（结构近似） | 低 |
| 2 | `accumulate_lrfu()` / `reset_lrfu()` + pick 比较器改读 KS | `kvmem_store.cpp` | 同文件 | 低 |
| 3 | 8 个 LRFU 单测 | `kvmem_store_test.cpp`（+199 行） | 同文件，直接追加 | 低 |
| 4 | 置信度监控（3 个新文件） | 新增 | 新增同名 3 文件 + `kvmem/CMakeLists.txt` 注册 | **零冲突** |
| 5 | `llama_kvmem_params` 新字段（action_lrfu / lrfu_* / score_agg / conf_*） | `llama-kvmem-hooks.h` | 同文件，按字段对齐 | 中 |
| 6 | `make_runtime_cfg` 接线 | `llama-memory-kvmem.cpp` | 同函数 | 中 |
| 7 | **`score_agg=max`（GQA 组内取 max）** | `score_retrieval()` | **`kvmem/mean_key_retrieval.cpp`（共享！）** | 中 |
| 8 | 调 `accumulate_lrfu` + `lrfu_round_id()` | `score_retrieval` 尾 + `apply_selection` | llama.cpp：沿用；ninfer：patch:5752 附近 | 中 |
| 9 | `observe_confidence()` + 导出 API | `llama-memory-kvmem.{h,cpp}` + hooks.h | 同 | 中 |
| 10 | 4 个 CLI 旗标 | `llama-kvmem-cli.cpp` / `server.cpp` | 同文件同类选项链（插在 `--kvmem-method` 附近） | 低 |
| 11 | ActKV 文档 / NIAH 脚本 / Windows 构建脚本 | 新增 | 直接拷 | **零冲突** |
| 12 | CUDA 架构 89+120a | `configure-msvc.bat` | mb 走 `scripts/windows/build-backends.ps1`，改那里 | 低 |
| 13 | **ninfer 后端也要 ActKV** | — | `backends/patches/ninfer-kvmem.patch` 内部 | **高（另一套接线）** |

---

## 4. 关键风险与坑（都已在代码里核实）

1. **block 粒度不同**：llama.cpp 默认 **128**（可配）；ninfer **固定 64**。top-p 命中比例、预算、recent 的语义要按粒度重算（ninfer 的 `--kvmem-recent-tokens` 必须 ≥64 且按 64 向下取整）。
2. **ninfer 没有 `--kvmem-method`**（`docs/multi-backend-differences.md` 明说）：ninfer 在 query 完整时总是 mean-K 检索。所以 LRFU 在 ninfer 上**要么无条件开启，要么新加开关**，不能照抄 `--kvmem-action-lrfu` 的语义。
3. **ActKV 里的两个 bug 必须一起带过去**（否则在两个后端都放大）：
   - `kvmem_store.cpp:296` `lrfu_score += scores[i]`，`scores` 是**可为负**的余弦 → KS 可被推成负数且**永不恢复**（`reset_lrfu()` 在 adapter/tools 里 **0 次调用**）；
   - 同轮第二次评分时 `set_retrieval_scores` 覆盖了 KS（`accumulate_lrfu` 去重早退跳过了写回）→ **同一输入两次 preview 可能选出不同集合**。
4. **置信度预算在 ActKV 里是死代码**：`llama_kvmem_observe_confidence` 零调用，且 `active == ceiling` 恒成立（永不增长）。移植时**要么做对（B_upper + 每轮门控 + 冷却），要么先别搬**，否则搬过去还是个死的。
5. **选项面已经分叉**（移植旗标时要分别加）：
   - llama.cpp 独有：`--kvmem-query-replay`、`--kvmem-query-policy`、`--kvmem-query-max-tokens`、`--kvmem-mtp-state`、`--kvmem-gpu-ratio/high/low`…
   - ninfer 独有：`--kvmem-sessions`、`--kvmem-disk-path/mib`、`--kvmem-host-mib`、`--kvmem-verify-transfers`
6. **patch 双轨**：mb 同时有旧的 `patches/llama-kvmem-current.patch` 和新的 `backends/patches/llamacpp-kvmem.patch`。若移植触动 llama.cpp 子模块（例如为了拿真注意力质量去改 FA），**必须同步对应 patch**，否则别人按 `versions.json` 重建时对不上（`patch_sha256` / `patched_tree` 也要更新）。
7. **顺手可带的一个小修**：`prefill_needs_offload()` 在 mb 里**仍是旧的水位线逻辑**（和 ActKV 树一样缺 d9b05a7 的 hysteresis），一行改动可省掉大量 prefill 换出。

---

## 5. 分阶段实施（按风险从低到高）

| 阶段 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **0** | 拷纯新增物：`confidence_monitor.{hpp,cpp}`、`confidence_monitor_test.cpp`、ActKV 文档、NIAH 脚本、`.gitignore` 条目 | 零冲突 | `kvmem` 库能编过 |
| **1** | 共享内核的 LRFU：`kvmem_store.{hpp,cpp}` + 单测；**顺手修负分与轮次幂等** | 低 | `kvmem_store_test` 绿；新增 LRFU 用例覆盖"同轮两次评分结果一致" |
| **2** | 共享打分层：把 `score_agg=max` 做进 `mean_key_retrieval.cpp`（两个后端同时受益） | 中 | `mean_key_retrieval_test` 扩展 GQA 用例 |
| **3** | llama.cpp 后端接线：`hooks.h` → `make_runtime_cfg` → `score_retrieval`（调 `accumulate_lrfu` + 轮次）→ `apply_selection`（提交时推进轮次）→ 导出 API | 中 | 端到端 A/B，关掉时行为与现状逐字节一致 |
| **4** | 命令面：`llama-kvmem-cli.cpp` + `llama-kvmem-server.cpp` 加旗标（`--kvmem-action-lrfu/-lrfu-lambda/-lrfu-top-frac/-score-agg`），写法沿用现有 `eq(arg, "--kvmem-…")` 链 | 低 | `--help` 与 flag 解析单测 |
| **5** | ninfer 后端等价实现：改 `backends/patches/ninfer-kvmem.patch`（选项 + selector 接线），更新 `versions.json` 的 `patch_sha256` / `patched_tree` | **高** | ninfer 侧真实模型 A/B |
| **6**（可选） | 置信度预算做对：接进生成循环 + 真 `B_upper` + 每轮门控 + 冷却 | 中 | 长会话下预算变化次数 ≤ 轮数且 ≤ `B_upper` |

**建议**：先做 0→4（llama.cpp 后端完整可用），ninfer（阶段 5）单独排期。

---

## 6. 验证方案

1. **宿主侧（不需要 GPU）**：`kvmem_store_test`、`confidence_monitor_test`、`mean_key_retrieval_test` —— 新增用例必须覆盖 §4.3 的两个 bug。
2. **端到端 A/B**：用这次写的 `scripts/niah_gkamradt.py`（250k 单点针）与 `scripts/niah_dialog.py`（232k 代码库 + 40 轮多轮），**在两个后端分别跑开/关**。
   - 注：单点针在 250k 已饱和（两边都 100%），要用**多轮 + 高针数（100–200）+ 砍预算（8192/4096）**才拉得开差距。
3. **回归**：默认关闭 ActKV 时，选中集合与现状**逐字节一致**（用 `KVMEM_TRACE` 的 `window=` 与选中块列表比对）。
4. **一致性**：改了 llama.cpp 子模块就必须同步 patch，并让 `versions.json` 的 `patch_sha256` / `patched_tree` 重新计算通过校验。

---

## 7. 一句话

**移植可行，而且共享内核（`mean_key_retrieval` + `kvmem_store`）让"打分"这半边只做一次；要分两遍的是"接线 + 选项"，其中 ninfer 那半边因为写在 900 KB patch 里，是本次唯一的高风险项。**
移植时务必把 ActKV 现有的两个 bug（KS 可为负、同轮不幂等）和那个死掉的置信度模块一并处理，否则等于把问题复制到两个后端。
