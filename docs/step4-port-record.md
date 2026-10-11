# Step 3 / Step 4 上游移植记录与性能归因

**日期**：2026-10-11
**目标**：把上游 `feat/multi-backend-framework`（`5bad28c` + `56f5319`）的 SSD 分层 / 异步搬运移植到本地 ActKV 树上
**结果**：功能移植成功（召回 33/33 无回归），**速度无回归**（10 次样本证实）

---

## 1. 提交清单

| 提交 | 内容 |
|---|---|
| `8c2070e` | 修 `versions.json` 的 llamacpp 哈希（原本带着 ninfer 的值） |
| `1ce865e` | **Step 1** — 摘取共享库新增文件（spill_io / host_kv_storage / snapshot_buffer） |
| `4363582` | 修 ninfer `.bat` 的续行符断裂（9009 错误） |
| `8c2c4f1` | **Step 3** — `raw_kv_store` 换 `HostKvRecord` 池化分配 |
| （工作区） | **Step 4** — ninfer 异步流水线（补丁 `patched_tree 4e4ae5fd`） |

---

## 2. Step 4 移植的关键技术点

### 2.1 `patched_tree` 必须匹配：`core.autocrlf=false`

第一次应用上游补丁后 tree 是 `2fcf4bff...`，与上游声明的 `cbf635dc...` **不匹配**：

```
原因：core.autocrlf=true 让 git 把补丁的 LF 转成 CRLF，145 个文件的哈希全变
修复：git -c core.autocrlf=false apply <patch>
结果：tree = cbf635dc8b84c1719fe256682c529dfc7b786b38  ✅ 与上游逐字节相同
```

**后续所有补丁生成/应用都必须在 `core.autocrlf=false` 下**。

### 2.2 `kvmem_window.cpp` 是"新建"文件，不是修改

```
diff --git a/src/models/.../kvmem_window.cpp b/...
new file mode 100644
index 0000000000000000..c24d8c8384295fbf
--- /dev/null
+++ b/src/models/.../kvmem_window.cpp
```

**所以不需要三方合并** —— 用上游完整版 + 重插 ActKV 片段即可。

### 2.3 重插的 ActKV（22 处）

| 文件 | 内容 | 处数 |
|---|---|---|
| `include/ninfer/types.h` | `action_lrfu` / `lrfu_lambda` / `lrfu_top_frac` / `score_agg` | 4 |
| `src/serve/serve_options.cpp` | 4 个 CLI + help 文本 | 8 |
| `.../storage/memory_statistics.cpp` | selector 配置（含 `deferred_lrfu = true`） | 6 |
| `.../storage/kvmem_window.cpp` | 2 处 `commit_lrfu` + `score_agg` 参数 | 4 |

### 2.4 `memory_snapshot.cpp` 无需恢复

上游把它合并进了 `prefix_identity.h`（`KvmemSnapshotCodec`），且**无任何文件引用**。

### 2.5 探针丢失（已知问题）

`KVMEM_WINDOW_PROBE` / `KVMEM_WINDOW_TRACE` **没有加进补丁**，所以 Step 4 二进制里没有它们。
功能不受影响，但后续无法用探针测 `advance` 耗时。

---

## 3. 性能归因（经过多轮实测修正）

### 3.1 最终结论：Step 3 与 Step 4 **速度相同**

**10 次样本，13.6k 上下文，`--ngram-min-match 12`**：

| 二进制 | 最小 | 中位 | 最大 | **均值** |
|---|---|---|---|---|
| Step 3 | 166.1 | 172.9 | 176.0 | **171.0** |
| Step 4 | 164.9 | 175.4 | 179.3 | **171.7** |

**差异 < 0.5%，在噪声范围内。**

**decode 的天然波动范围是 165–179（±4%）** —— 2–3 次样本无法区分 6% 的差异，这正是早期误判的来源。

### 3.2 真正导致掉速的因素

| # | 因素 | 证据 | 影响 |
|---|---|---|---|
| **1** | **`--ngram-min-match` 从 12 降到 8** | min-match=8 时草稿 1199/接受 551 (46%)；=12 时 982/550 (56%) | **-16~19%** |
| **2** | **输出长度 → ngram 轮次** | 600 tok → 33 rounds → 165 tok/s；28 tok → 2 rounds → 42 tok/s | 主要 |
| **3** | **上下文长度** | 3-token 实验：13k vs 64k 只差 7% | **仅 7%** |
| **4** | **越池后窗口封顶** | prefill 993 → 335 tok/s（53248 满） | prefill 掉 2.3× |

### 3.3 被推翻的假说（记录以免重犯）

| 假说 | 推翻证据 |
|---|---|
| ❌ 主机内存换页 | `free & zero page list` 7.02 GB，pagefile 仅用 1.6/26.4 GB，`pages output/sec = 0` |
| ❌ Step 4 代码慢 6–7% | 10 次样本证明均值 171.0 vs 171.7 |
| ❌ 固定开销被短输出摊薄 | 552 token 输出仍只有 68 tok/s（因为该 prompt 的 ngram 无用） |
| ❌ 需要 SSD 才能提速 | 12 GB host 归档在 32 GB 内存内完全装得下，SSD 只会更慢 |

### 3.4 关键机制：ngram 是加速主因，不是 MTP

| 输出要求 | decode | ngram rounds | MTP 接受率 |
|---|---|---|---|
| **复制句子模式**（ngram 可用） | **174.7** | **33** | 562/994 (57%) |
| **生成数字**（ngram 无用） | **68.2** | 极少 | 390/482 (**81%**) |

**MTP 命中率更高（81% vs 57%），速度却慢 2.5 倍** —— 因为：

```
ngram: 每次可复制 31 token（--ngram-draft-tokens 31）
MTP  : 每次只能草稿 4 个 token
杠杆比 31:4 ≈ 7.75:1
```

---

## 4. 测试方法学教训

| 教训 | 说明 |
|---|---|
| **样本量必须足够** | decode 波动 ±4%，判断 6% 差异至少要 10 次样本取中位数 |
| **A/B 必须交替或严格同环境** | 我先测 A 再测 B，中间环境变化会污染结论 |
| **核对实际运行的二进制** | 我曾在 A/B 切换时把 Step 4 覆盖成 Step 3，导致后续测试全部跑错版本 |
| **日志解析要严谨** | 折行、正则跨行匹配都会产生假数据（我一度以为 ngram 失效，实际是解析错误） |
| **区分"参数改动"与"代码改动"** | 把 `min-match` 从 12 改成 8 是我擅自做的，却当成代码差异分析了很久 |

---

## 5. 当前状态

| 项 | 值 |
|---|---|
| workers exe | Step 4 最新（16:57:16） |
| `--ngram-min-match` | **12**（已复原） |
| 补丁 | `patch_sha256 7eac6654` / `patched_tree 4e4ae5fd` |
| 校验 | `ninfer: 8319e8f51247 + ninfer-kvmem.patch ready` |
| 召回 | 33/33（同轮 + 最终 + 跨工具 14/14，80 工具调用） |

## 6. 回滚点

| 备份 | 位置 |
|---|---|
| git 标签 | `backup/pre-ninfer-async` = `8c2c4f1` |
| tree ref | `refs/backup/pre-async-tree` = `efdce9ea...` |
| 文件级 | `I:\llama\ninfer-submodule-backup-pre-step4`（77.8 MB） |
| 补丁/配置 | `I:\dsh\html\pre-step4-backup\` |
| Step3 exe | `I:\dsh\html\exe-step3-1501.exe` |
