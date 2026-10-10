# ActKV 的"压缩机制"能在 KVMem 里实现吗？

| 项目 | 内容 |
|---|---|
| 日期 | 2026-10-10 |
| 论文 | ActKV §7 *Page-aware Compression Management*（三原语 + 两个 kernel） |
| 目标代码 | `I:\llama\kvmem-llama.cpp-0.17.0\kvmem-actkv` |
| 结论 | **能实现，但要分两半**：驱逐/预算那半 KVMem 已经实现且更强；**§7.2 压实照搬收益很小**；**§7.1 真注意力值得做，但要改实现位置** |

---

## 1. 先明确 ActKV 的"压缩机制"指什么

论文 §3.3 把压缩统一成三原语：

```
AttnScore  ← CalAttn(targetq, KCache)          # 注意力计算
KeepMask   ← Eviction(AttnScore, Budget)       # 入口驱逐
KVCache    ← Compaction(KVCache, KeepMask)     # 缓存压实
```

两个定制 kernel：
- **§7.1 恢复式注意力计算**：不重算注意力，而用 paged attention 已经产出的 softmax **LSE** 反推每个 token 的注意力概率，再按块求和（GQA 组内取 max）。
- **§7.2 无冲突就地压实**：驱逐后保留项散落，先把保留项规划到**尾部逻辑块**，源/目标槽位互斥 → 一趟并行拷贝、无需辅助 KV buffer，释放的块归还调度器。

---

## 2. KVMem 里逐项可行性

| ActKV 组件 | KVMem 现状（代码事实） | 能实现？ | 收益 |
|---|---|---|---|
| 三原语抽象 | 天然对应：`score_retrieval()` → `pick_topk_blocks()` → `set_selection()` + `apply_plan_to_kv()` | ✅ **已经是这个结构** | 只是文档化价值 |
| **Eviction** | 已有 LRFU（`accumulate_lrfu`）+ top-k/配额选择 | ✅ **已实现** | — |
| **§7.1 LSE 恢复注意力** | 现在是 host 侧 `cos(q_sum, mean_k)`，**不是注意力概率** | ⚠️ **能，但要换位置**（见 §4.A） | **高** |
| **§7.2 就地压实** | 已有块级压实 `layout_gpu_slots_by_orig_pos()`；块内空洞被**刻意保留并 mask** | ✅ 能，但**照搬收益小**（见 §3） | 低→中 |
| 释放块归还调度器 | 已有 `stage_out` + `free_slot` + `pending_gpu_frees_` | ✅ 已有 | — |

---

## 3. 为什么 §7.2 照搬收益很小（四条代码事实）

1. **回收单位是 block slot，不是 token。**
   驱逐走的是整块路径：`seq_rm_logical(0, begin, end)` 整块移出 + `stage_out` 降级到 CPU/NVMe（`llama-memory-kvmem.cpp:1821 / 2154`）。论文里"token 级空洞导致物理块无法归还调度器"的问题，**在 KVMem 不存在**——整块还回池子即可。

2. **块内空洞是刻意保留的，不是泄漏。**
   `llama.cpp/src/llama-kv-cache.cpp:1187-1193` 原文注释：
   > *Non-SWA (including KVMem's bounded slot-pool) may hold holes: resurrected blocks sit at their original pos while the tail stays at high pos. **Purging [pos_min, overwritten] would delete those holes.***

   而且有专门的 `holes` 计数与 `KVMEM_TRACE mask ... holes=%d` 诊断。历史里程碑日志里实测 **holes=1 / 229 cells** → 量级极小，压实省不下多少。

3. **KVMem 已经有块级压实。**
   `layout_gpu_slots_by_orig_pos()`（`llama-memory-kvmem.cpp:1944`，在 `apply_selection` 内调用）把驻留块按 `orig_pos` 重新密集排到 slot 0..n-1，D2D/D2H/raw-K 重建三条路径，**已排序时早退**。这就是"Compaction"的块级版本。

4. **论文 10.41× 的 kernel 提速来自 vLLM/R-KV 的 gather 开销**——把散落 KV 收集到连续 workspace。KVMem 没有这个 gather（它本来就在搬整块），所以**这个性能数字不可复制**。

> 换句话说：论文 §7.2 解决的是"vLLM 式共享块池 + token 级驱逐"的记账问题；KVMem 的记账单位本来就是块，问题已经被消解掉了。

---

## 4. 真正值得做的两个变体

### A.（高收益）§7.1 改造：窗口内直接算**真注意力**，而不是从 LSE "恢复"

**关键洞察：在 KVMem 里不需要论文的"恢复"步骤。** 论文要恢复是因为 vLLM 的注意力窗口巨大且分页、拿不到完整注意力矩阵；而 KVMem 的注意力窗口只有 **36,864 token（288 块）**，完全可以自己算一遍完整 softmax（自己求 LSE），再按块聚合。

**但有一个硬约束**（决定实现位置）：生产路径**没有** per-token、pre-RoPE 的 K。`raw_kv_store.hpp` 明确写着：

> *Product path: ordered K sum (F32) at first write, mean-K computed on read; packed GPU-format K/V (q8_0 etc) copied at stage-out. … `write_layer_k_rows` (unrotated token-major K) **remains for tests only**.*

adapter 实际只写 `write_layer_mean_k` / `write_layer_k_gpu` / `write_layer_v_gpu`（`llama-memory-kvmem.cpp:3401/3487/3604` 等）。所以：

| 可选实现 | 做法 | 代价 | 评价 |
|---|---|---|---|
| **A1. CUDA kernel（推荐）** | 对**候选块**（先 cosine 粗排 top 3×budget）读其 per-token K，算 `exp(q·k/√d − LSE)` 按块求和。放 `src/adapter/llama-kvmem-stagein.cu` 旁边新建 .cu | 中；无需改 ggml | **推荐**：不碰 FA，不碰 patches |
| A2. 扩展 host 保留 per-token 原始 K | 把 `kvmem_capture_k` 从"只存 sum"改成"同时存行" | host RAM ≈ 252k × dim × 2B × 层数（数 GB） | 只在内存宽裕时考虑 |
| A3. 论文原意：让 FA 输出 LSE | 改 `ggml-cuda/fattn*.cu` + 同步 `patches/llama-kvmem-current.patch` | 高；违反 "Flash Attention 不修改" 的既有约束 | 不必要（见上：窗口小，可自算） |

**两级打分设计**（避免 O(全历史 × dim) 爆炸）：
1. 第一级：现有 cosine(mean-K) 粗排 → 取 top `3 × budget` 块作候选；
2. 第二级：只对候选块做精确注意力质量，替换 hit mask 与 KS 增量；
3. 顺带修掉当前 cosine 可为负 → KS 可为负的问题（论文的 AttnScore 是非负的）。

**还需要定义 action region**：现在 query 是 last-user span（**观测**，不是动作）。要按论文取"上一轮 assistant 的 tool-call/尾部 token"或"边生成边累积的 q_sum_"。

### B.（中收益）§7.2 的机制用来**优化现有布局**，而不是新增一套压实

借论文的两点：**① 槽位规划与数据搬移分离；② 源/目标互斥的单趟拷贝**。

- 现在 `layout_gpu_slots_by_orig_pos` 是**整块重排**；改成：只对"打乱的那段 run"做一次 in-place 搬移，其余原地不动。
- 压实释放出来的 slot 交给 `gen_reserve` → 缓解 `docs/architecture.md:49-123` 记录的 **"no free GPU slot for block N"** 硬限制（与已有的 ring buffer 计划互补）。
- 触发要有阈值：先加指标"有效 cell / 已分配 cell"，空洞比例低于阈值就**不做**。

---

## 5. 落地清单

| # | 动作 | 位置 | 预期 |
|---|---|---|---|
| 1 | 加度量：每轮 `holes`、有效/已分配 cell 比、reselect 搬移字节 | `KVMEM_TRACE` / `retr_perf_print` | 决定 B 是否值得做 |
| 2 | 两阶段打分：cosine 粗排 → 精确注意力质量 | `score_retrieval()`（`llama-memory-kvmem.cpp:4191`）+ 新 .cu | **召回率上限 ↑** |
| 3 | 定义 action region（上一轮 action token / 生成期 q_sum） | `kvmem_capture_q` 链路 | 符合 ActKV 前提 |
| 4 | KS 语义修正：非负、同轮幂等、轮次只在提交时推进 | `kvmem_store.cpp` | 选择确定性 ↑ |
| 5 | run 级 layout + 释放 slot 给 gen_reserve | `layout_gpu_slots_by_orig_pos()` | reselect 开销 ↓、生成稳定性 ↑ |
| 6 | 若动 ggml：同步 `patches/llama-kvmem-current.patch` | `patches/` | 保证别人能复现构建 |

**不要做**：把论文的 §7.2 kernel 原样搬进来（没有对应的内存模型，收益≈0）；也不要执着于"从 LSE 恢复"（KVMem 窗口小，自算 LSE 更简单准确）。

---

## 6. 一句话

**能实现，而且 §7.1 值得做——但在 KVMem 里应该改写成"两级打分 + 窗口内自算 softmax"，不是照搬 LSE 恢复；§7.2 应该改写成"run 级就地搬移 + 释放 slot 给 gen_reserve"，而不是新增一套 token 级压实 kernel。**
论文的压缩收益（10.41×）来自 vLLM 的 gather 记账，KVMem 的块级模型已经把它消解了；真正剩下能换召回率的是 **§7.1 换成真注意力质量**。
