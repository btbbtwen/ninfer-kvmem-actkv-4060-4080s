# A9 回归事故：延后提交让选择器"看不见本轮 query"

**结论一句话**：A9 的第一版实现把 `attn_score` 只设为**上一轮 KS**，导致
`pick_topk_blocks` 排序时**完全看不到本轮检索分**。首轮全 0（退化为 recency，
所以短测试看起来正常），之后被历史 KS 锁死 —— **本轮刚写入的块 KS=0，永远挤不进
窗口，当场被驱逐**。表现是模型能背出几轮前的旧针，却读不到刚刚写入的针。

已修复并验证：40 针测试 **39/40**（修复前 6/40，且 round 5+ 工具调用归零）。

---

## 1. 症状

256k / 40 针 / 每轮写入 >16384 token 的测试中：

| round | 修复前 | 修复后 |
|---|---|---|
| 1–3（未越池） | ✅ 10/10 | ✅ 10/10 |
| 4（刚越池） | ⚠️ 工具参数对，**散文却说"没看到 N11/N12"** | ✅ 3/3 |
| 5–7 | ❌ **0/9，且 tool calls = 0** | ✅ 9/9，tools 9 |
| 8–13 | ❌ 0/3 每轮 | ✅ 2–3/3 |
| 最终 40 针召回 | ❌ 6/40 | ✅ **39/40** |
| 工具调用 | 40 轮里大量为 0 | **80/80 成功** |

**诊断性证据**（模型原文，来自 `niah_diag.py`）：

```
round 5 probe RAW: "I do not see any CRITICAL AUDIT NOTE entries for N14, N15,
or N16 in that text. The only sealed compliance code notes present in the
archives so far were: N01: N01-000000, N02: N02-104729, ... N13: N13-233215"
```

模型能**背出第 1–4 轮的旧针**，却看不到**本轮刚写的针** —— 这不是"召回变差"，
而是**新块根本没进窗口**。且 `tool calls = 0` 说明它连试都不试（没数据可记录）。

---

## 2. 根因

A9 引入延后提交（`deferred_lrfu`），`set_retrieval_scores` 的延后分支当初写成：

```cpp
if (cfg_.deferred_lrfu) {
    lrfu_pending_scores_ = scores;
    lrfu_pending_round_ = round_id;
    lrfu_pending_ = true;
    for (auto & b : blocks_) b.attn_score = b.lrfu_score;   // ← BUG
    return;
}
```

而选择器排序用的正是 `attn_score`：

```cpp
// kvmem_store.cpp:741-742（pick_topk 比较器）
if (blocks_[a].attn_score != blocks_[b].attn_score)
    return blocks_[a].attn_score > blocks_[b].attn_score;
```

于是延后模式下：

1. **本轮检索分被扣住**（存在 `lrfu_pending_scores_`，要到 `commit_lrfu` 才用）；
2. **排序只用历史 KS** → 选择器对"当前 query 关心什么"完全失明；
3. **首轮**所有 KS=0 → 全部并列 → 退化为 recency（tie-break 取更大 id），
   所以前几轮"看起来正常"；
4. **之后**老块的 KS 累积起来，新块 KS=0 竞不过 → **永远选不进** → 写入即被驱逐。

### 为什么短测试没暴露

`niah_diag.py` 前 4 轮（4/4、3/3、3/3、3/3）在**修复前**也是通过的 ——
因为那个阶段 recency 兜底恰好等价于"保留最新块"。**只有轮次多到老块 KS 超过
新块时才会翻转**，所以必须跑到 round 5+ 才能看见。

---

## 3. 修复

排序与提交分离：**排序用"临时 KS"（含本轮分数），提交才收紧到选中集**。

```cpp
if (cfg_.deferred_lrfu) {
    lrfu_pending_scores_ = scores;      // 仍留给 commit_lrfu
    lrfu_pending_round_ = round_id;
    lrfu_pending_ = true;
    // 排序信号必须等于立即路径会用的那个：lambda * KS + 本轮 hit。
    // lrfu_score 保持不动，等 commit_lrfu 用更窄的、受选中集约束的规则更新。
    const LrfuHitMask mask = lrfu_hit_mask(lrfu_scratch_, scores, n_score, p);
    for (uint32_t i = 0; i < n; ++i) {
        double ks = lambda * blocks_[i].lrfu_score;
        if (i < n_score && scores[i] > 0.0 &&
            (!mask.thresholded || scores[i] >= mask.threshold)) {
            ks += scores[i];
        }
        if (ks < 0.0) ks = 0.0;
        blocks_[i].attn_score = ks;     // 只是个临时排序值
    }
    return;
}
```

同时把 top-p 命中规则抽成共享函数 `lrfu_hit_mask()`，避免立即路径与延后路径
对"什么算命中"产生分歧（这是同类 bug 的温床）。

### 新增回归测试

| 测试 | 守住什么 |
|---|---|
| `test_lrfu_deferred_commit_narrows_hits` | `attn_score` 必须已含本轮分；只有选中块 `lrfu_score` 生长 |
| **`test_lrfu_deferred_ranking_sees_current_query`** | **直接钉死本 bug**：老块 KS=10 时，新块以本轮 8.0 的分必须能被选中 |
| `test_lrfu_deferred_off_keeps_score_only_rule` | 关闭延后模式时行为与原来一致 |

---

## 4. 验证结果

### 40 针 / 256k 最终测试（修复后）

```
round  1/13: +17185 prompt tok | prefill 997.7 tok/s | 18.0s | 4/4 | tools 4
round  2/13: +16747 prompt tok | prefill 800.9 tok/s | 21.1s | 3/3 | tools 3
round  3/13: +17097 prompt tok | prefill 696.9 tok/s | 24.8s | 3/3 | tools 3
round  4/13: +17197 prompt tok | prefill 352.1 tok/s | 49.1s | 3/3 | tools 3
round  5/13: +17197 prompt tok | prefill 369.2 tok/s | 46.8s | 3/3 | tools 3
round  6/13: +17197 prompt tok | prefill 343.1 tok/s | 50.4s | 3/3 | tools 3
round  7/13: +17197 prompt tok | prefill 355.0 tok/s | 48.7s | 3/3 | tools 3
round  8/13: +17197 prompt tok | prefill 343.6 tok/s | 50.4s | 3/3 | tools 3
round  9/13: +17197 prompt tok | prefill 359.4 tok/s | 48.2s | 2/3 | tools 3
round 10/13: +17198 prompt tok | prefill 358.8 tok/s | 48.3s | 3/3 | tools 3
round 11/13: +17198 prompt tok | prefill 350.0 tok/s | 49.5s | 3/3 | tools 3
round 12/13: +17198 prompt tok | prefill 337.6 tok/s | 51.3s | 3/3 | tools 3
round 13/13: +17198 prompt tok | prefill 357.9 tok/s | 48.4s | 3/3 | tools 3

=== final recall (4 批 × 10 针, 上下文 232,392 token) ===
  batch 1: 7.5s | prefill 327.5 tok/s | decode 95.4 tok/s | tools 10
  batch 2: 7.6s | prefill 302.2 tok/s | decode 93.8 tok/s | tools 10
  batch 3: 7.4s | prefill 311.3 tok/s | decode 95.5 tok/s | tools 10
  batch 4: 8.0s | prefill 299.5 tok/s | decode 93.9 tok/s | tools 10

same-turn recall (fresh needles):      39/40
final recall  (after all evictions):   39/40
tool calls: same-turn=40 final=40 total=80
```

未命中的是 **N26**（`N26-063352`），同轮与最终均 MISS —— 单点，非系统性。

### 速度

| 阶段 | prefill | decode |
|---|---|---|
| 未越池（round 1–3） | 697–998 tok/s | — |
| 越池后（round 4–13） | **337–369 tok/s** | 58–85 tok/s |
| 最终召回（232k 上下文） | 300–328 tok/s | **94–96 tok/s** |

**越池后 prefill 恒定在 ~350 tok/s**，与之前 72k 测试一致 —— 印证了
"窗口封顶后速率恒定"的结论。

---

## 5. 教训（写进流程）

1. **"排序信号"和"持久分值"必须分开命名与分开构造**。把它们写成同一个字段
   （`attn_score`）时，任何"延后更新"都会静默改变排序语义。
2. **两阶段改动必须为"排序仍看见当前输入"单独写一条测试** —— 只测"最终分值
   对不对"是不够的，本 bug 的所有既有测试都通过。
3. **短测试会骗人**：recency 兜底让前 4 轮全绿。跨池测试至少要跑到
   **老块 KS 超过新块**的轮次（本机约 round 5）。
4. 同类工具的调用计数（`tool calls`）是**很好的健康指标**：召回失败 + 工具调用
   同时归零，几乎一定说明**输入格式或可见性坏了**，而不是"模型答不出"。

---

## 6. 复现与验证命令

```powershell
# 服务（bat 启动，带探针）
I:\llama\kvmem-llama.cpp-multi-backend-framework\workers\ninfer\start-kvmem-ninfer.bat

# 7 轮快速诊断（会打印模型原始输出，约 4 分钟）
python I:\dsh\html\niah_diag.py

# 完整 40 针 / 256k（约 12 分钟）
python I:\dsh\html\niah40_256k.py

# 宿主单测（含回归测试）
I:\dsh\html\build-host-tests.bat
```
