# ActKV 移植实现记录（→ multi-backend-framework）

| 项目 | 内容 |
|---|---|
| 源 | `I:\llama\kvmem-llama.cpp-0.17.0\kvmem-actkv` |
| 目标 | `I:\llama\kvmem-llama.cpp-multi-backend-framework`（分支 `feat/multi-backend-framework`） |
| 提交 | `56fd63e` 共享内核 + llama.cpp 后端；`b783fdf` ninfer 后端 |
| 日期 | 2026-10-10 |

---

## 1. 备份与回滚

| 备份 | 位置 |
|---|---|
| git tag | `backup/pre-actkv-20261010`（= `eb746f7`，修改前 HEAD） |
| 完整目录副本 | `I:\llama\kvmem-llama.cpp-multi-backend-framework-BACKUP-20261010`（355 文件 / 57.7 MB，已校验收干净） |

回滚：
```powershell
# 只回滚提交，保留后续工作
git -C I:\llama\kvmem-llama.cpp-multi-backend-framework reset --hard backup/pre-actkv-20261010
# 或整目录还原
robocopy I:\llama\kvmem-llama.cpp-multi-backend-framework-BACKUP-20261010 I:\llama\kvmem-llama.cpp-multi-backend-framework /MIR
```

---

## 2. 参数（与 llama ActKV 完全同名同默认值）

| 参数 | 默认 | 含义 |
|---|---|---|
| `--kvmem-action-lrfu` | off | 开启 ActKV 动作导向 LRFU 保留分 |
| `--kvmem-lrfu-lambda F` | `0.50` | `KS_t = λ·KS_{t-1} + hit·score_t`，要求 `0 < F < 1` |
| `--kvmem-lrfu-top-frac F` | `0.90` | 每轮命中比例（论文 p=90%），`0 < F <= 1` |
| `--kvmem-score-agg NAME` | `mean` | GQA 组内聚合：`mean`（历史）/ `max`（ActKV Alg.3 step 8） |

**llama.cpp 后端**：`tools/llama-kvmem-cli.cpp`、`tools/llama-kvmem-server.cpp`（含 `--help`）。
**ninfer 后端**：`backends/patches/ninfer-kvmem.patch` → `src/serve/serve_options.cpp`（含 `--help`），带范围校验。

---

## 3. 关键设计决策

### 3.1 ActKV 做进共享类 `KvMemStore`，而不是每个后端各写一遍

`backends/patches/ninfer-kvmem.patch:4982` 显示 ninfer 的 selector 就是
`std::unique_ptr<kvmem::KvMemStore>`，配置也是 `kvmem::KvMemStoreConfig`。因此把 LRFU 放进
`KvMemStore::set_retrieval_scores()`，**任何后端只要调用这一个入口就自动获得同样行为**：

```cpp
// kvmem/src/host/kvmem_store.cpp
void KvMemStore::set_retrieval_scores(const std::vector<double> &scores, uint64_t round_id) {
    if (cfg_.action_lrfu) { /* 记 one-shot 分 → accumulate_lrfu(scores, round_id) 并把 KS 作为 attn_score */ }
    else                  { /* 原路径，逐字节不变 */ }
}
```

ninfer 侧因此**不需要改调用点**（它原本就是 `state.selector->set_retrieval_scores(...)`），
只需把 3 个配置项接上。

### 3.2 `score_agg` 为什么有两处实现

- **ninfer**：直接调共享函数 `kvmem::mean_key_retrieval_scores(...)` →
  在 `mean_key_retrieval.{hpp,cpp}` 加 `MeanKeyGroupReduce`（默认 `Mean`，旧调用零改动），
  ninfer 传 `Max`。
- **llama.cpp**：`src/adapter/llama-memory-kvmem.cpp::score_retrieval()` **自己算分**，不调共享函数，
  所以 `max` 路径必须在该函数内实现（照搬 ActKV 的 `agg_max` 分支）。

### 3.3 顺带修掉 ActKV 的两个缺陷（否则等于把问题复制到两个后端）

1. **KS 可为负**：原实现 `lrfu_score += scores[i]`，而余弦相似度可为负 → KS 被推成负数且**永不恢复**
   （`reset_lrfu()` 在 ActKV 里 0 处调用）。现改为 **clamp 到 0**，冷块仍能回升。
2. **同轮重复评分**：`set_retrieval_scores(scores, round_id)` 加轮次戳，同一轮内重复评分只重发 KS、
   不再二次衰减。llama.cpp 的轮次戳 = `(attention_epoch_ << 32) | store_n_tokens()`（沿用 ActKV
   `lrfu_round_id()` 语义）；ninfer / 其他单次调用方用默认 `round_id = 0`（每次调用即一轮）。

### 3.4 未接线项（明确说明）

`confidence_monitor.{hpp,cpp}` + 测试已随共享库一起移植并**在单测中通过**，但**没有接进生成循环**。
原因：ActKV 里它本来就是死代码（`llama_kvmem_observe_confidence` 零调用，且 `active == ceiling`
恒成立、预算永不增长）。接线需要同时补齐真实 `B_upper` 与每轮门控，属于独立改动，本记录不声称已完成。

---

## 4. 验证证据

| 检查 | 结果 |
|---|---|
| 宿主单测 `kvmem_store_test` | **OK**（含新增 8 个 LRFU 用例） |
| 宿主单测 `mean_key_retrieval_test` | **MEAN_KEY_RETRIEVAL PASS** |
| 宿主单测 `confidence_monitor_test` | **OK** |
| ninfer `serve_options.cpp` 语法检查 | **rc=0** |
| ninfer `memory_statistics.cpp` / `memory_snapshot.cpp` / `kvmem_window.cpp` 语法检查（对**改过的 kvmem 头文件**） | **rc=0** |
| 新 ninfer 补丁反向应用校验（= `prepare-backends.py --reverse --check`） | **rc=0** |
| `patch_sha256` / `patched_tree` 与补丁实际值一致 | ✅ `390b376b…` / `472e4349…` |

新增的 8 个 LRFU 用例覆盖：默认值、关闭时保持一次性排序、累积与衰减、轮次幂等、top-p 命中掩码、
非负钳制与回升、reset，以及**行为断言**——`test_lrfu_changes_selection_order` 要求
「开 ActKV 时选中历史块 5、关时选中当前分更高的块 6」；若 LRFU 未生效该用例必然失败。

---

## 5. 未验证项（必须知悉）

1. **端到端编译未做**：`backends/llamacpp`、`backends/ninfer` 两个子模块在本机是空目录，
   所以**没有编译** `src/adapter/*` 与 `tools/*`（llama.cpp 后端），也没有真正 link 出可执行文件。
   已完成的是宿主库编译 + 单测 + 对改动文件的语法检查。
2. **未跑真实模型**：没有实测 250k/256k 召回是否与 ActKV 原树一致；也没有对照 `--kvmem-action-lrfu`
   开/关的实际效果。要做的话需先 `git submodule update --init --depth 1` 再整体构建。
3. **ninfer 补丁未通过 `prepare-backends.py` 实跑**（同样因子模块缺失）；仅做了反向应用校验与
   `versions.json` 值一致性校验。
4. **未提交上游**：两个提交只在本地仓库，未 push（用户未要求）。

---

## 6. 下一步（如需完整可用）

```powershell
cd I:\llama\kvmem-llama.cpp-multi-backend-framework
git -c http.sslBackend=openssl submodule update --init --depth 1
python scripts\prepare-backends.py --verify     # 应输出两个 backend ready
# 然后按 scripts/windows/build-backends.ps1 构建两个后端
```

构建后建议的 A/B：同一 256k 多轮会话分别跑
`--kvmem-action-lrfu --kvmem-score-agg max` 与不带这两个参数，对比跨轮记忆问答准确率
（ActKV 原树的基线：多轮 87.5% → 100%，回答长度中位 43 → 6 token）。
