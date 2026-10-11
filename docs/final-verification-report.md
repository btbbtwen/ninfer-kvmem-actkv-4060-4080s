# 上游异步移植 最终验证报告

**日期**：2026-10-11
**提交**：`fc26436`（ninfer 异步流水线 + 探针恢复）
**二进制**：`21:30:18`（780/780 全量重编）

---

## 1. 召回：33/33 完美

| 阶段 | 结果 | 工具调用 |
|---|---|---|
| 同轮召回（刚写入立即问） | **33/33** | 33 |
| 最终召回（历经所有驱逐） | **33/33** | 33 |
| 跨工具验证（`send_alert`） | **14/14** | 14 |
| **合计** | **全中** | **80/80** |

上下文峰值 258,467 / 262,144（98.6%）。

---

## 2. 速度

### 2.1 写入 prefill（每轮 ~16,588 token）

| 轮次 | prefill | 阶段 |
|---|---|---|
| 1 | 890.3 | 池未满 |
| 2 | 770.2 | 池未满 |
| 3 | 681.2 | 接近池顶 |
| **4–15** | **310.9 – 330.0** | **池已满（恒定）** |

**双平台特征确认**：池未满 681–890，池满后稳定在 311–330 tok/s。

### 2.2 decode

| 阶段 | decode |
|---|---|
| 同轮探针（短输出 + 工具） | 44.2 – 72.2 |
| **最终召回批次** | **107.4 – 128.6** |
| **跨工具（send_alert）** | **59.0 – 68.3** |

**跨工具 decode 恢复**：`--ngram-min-match` 从 8 改回 12 后，从 40.9–47.2 → **59.0–68.3**。

---

## 3. 探针数据（本次新增）

**68 条 PROBE 样本**，全部 `async=0`（未启用 SSD，符合预期）：

| 阶段 | 中位 | 最大 |
|---|---|---|
| **select** | 135.8 ms | 235.2 ms |
| prepare | 1.0 ms | 441.7 ms |
| **advance** | **415.1 ms** | 1,003.7 ms |

### 关键观察

```
logical=53504 : select=40.5ms  prepare=252.7ms  advance=914.6ms
logical=258346: select=183.4ms prepare=0.8ms    advance=338.7ms
```

**`advance` 是重选的主要成本（中位 415 ms）** —— 它就是同步搬运的阻塞时间。这正是上游异步流水线要解决的问题，但**异步只在 `--kvmem-disk-bytes` 启用时生效**（`program_impl.cpp:465`）。

### 为什么这里不该启用 SSD

| 项 | 值 |
|---|---|
| host 归档需求（258k） | ~11.2 GB |
| 配置 | `--kvmem-host-mib 12288` = 12 GB |
| 物理内存 | 31.9 GB |
| **结论** | ✅ 完全装得下 → **SSD 只会更慢** |

---

## 4. 性能归因（最终版）

| 现象 | 真因 | 证据 |
|---|---|---|
| **跨工具 decode 波动** | **`--ngram-min-match` 参数** | 8 → 1199 草稿/551 接受 (46%)；12 → 982/550 (56%) |
| 256k 慢于 13.6k | **输出长度 → ngram 轮次** | 600 tok = 33 轮 → 165 tok/s；28 tok = 2 轮 → 42 tok/s |
| 越池后 prefill 掉 2.3× | **窗口封顶 53248** | 890 → 311 tok/s |
| ~~Step 4 慢 6–7%~~ | ❌ **测量噪声** | 10 次样本：Step3 均值 171.0 vs Step4 171.7 |
| ~~主机内存换页~~ | ❌ **不存在** | 7.02 GB 空闲页，`pages output/sec = 0` |
| ~~需要 SSD 提速~~ | ❌ **不需要** | 12 GB 归档 < 32 GB 内存 |

---

## 5. 交付清单

| 提交 | 内容 |
|---|---|
| `8c2070e` | 修 `versions.json` 的 llamacpp 哈希 |
| `1ce865e` | Step 1 — 摘取共享库（spill_io / host_kv_storage / snapshot_buffer） |
| `4363582` | 修 bat 续行符断裂（9009 错误） |
| `8c2c4f1` | Step 3 — `raw_kv_store` 换 `HostKvRecord` 池化分配 |
| `2107b34` | 文档 + 性能归因修正 |
| **`fc26436`** | **Step 4 — ninfer 异步流水线 + 探针恢复** |

### 补丁状态

| 项 | 值 |
|---|---|
| `patched_tree` | `8456aa529b16d71f14ac2f78a399d8ab42dfa1b2` |
| `patch_sha256` | `d243d9973df0bb82f38224c1fea56c62360887d7be63841da8f8f5efc66ef791` |
| 补丁大小 | 959.9 KB |
| 校验 | ✅ `ninfer: 8319e8f51247 + ninfer-kvmem.patch ready` |

### ActKV 重插（22 处）

| 文件 | 内容 | 处数 |
|---|---|---|
| `include/ninfer/types.h` | `action_lrfu` / `lrfu_lambda` / `lrfu_top_frac` / `score_agg` | 4 |
| `src/serve/serve_options.cpp` | 4 个 CLI + help 文本 | 8 |
| `.../storage/memory_statistics.cpp` | selector 配置（含 `deferred_lrfu = true`） | 6 |
| `.../storage/kvmem_window.cpp` | 2 处 `commit_lrfu` + `score_agg` 参数 | 4 |

---

## 6. 技术要点（供后续复用）

1. **`patched_tree` 必须在 `core.autocrlf=false` 下计算** —— 否则 LF→CRLF 让 145 个文件哈希全变
2. **`kvmem_window.cpp` 是补丁"新建"文件** —— 用上游完整版 + 重插 ActKV，不需要三方合并
3. **异步路径的开关是 `kvmem_options.disk_bytes`**（`program_impl.cpp:465`）
4. **探针要随补丁一起生成** —— 第一次丢探针就是因为先生成补丁后插探针，重编时补丁重放把它们清掉了
5. **submodule 的 `M` 标记是设计如此** —— gitlink 指向基线，改动由补丁承载

---

## 7. 回滚点

| 备份 | 位置 |
|---|---|
| git 标签 | `backup/pre-ninfer-async` = `8c2c4f1` |
| tree ref | `refs/backup/pre-async-tree` = `efdce9ea...` |
| 文件级 | `I:\llama\ninfer-submodule-backup-pre-step4`（77.8 MB） |
| 补丁/配置 | `I:\dsh\html\pre-step4-backup\` |
| Step3 exe | `I:\dsh\html\exe-step3-1501.exe` |

---

## 8. 产物

| 文件 | 说明 |
|---|---|
| `I:\dsh\html\final-verify-256k.log` | 本次完整测试日志 |
| `I:\dsh\html\probe-final-server.log` | 服务端日志（68 条探针） |
| `I:\dsh\html\niah33-262k-metrics.json` | 分阶段原始指标 |
| `I:\dsh\html\step4-port-record.md` | 移植过程记录 |
