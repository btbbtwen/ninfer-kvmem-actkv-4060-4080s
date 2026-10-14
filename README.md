启动参数
参考原项目
  


## 7. 复现命令

```powershell
# 1) 启动服务（bat，含 ActKV 参数）
I:\llama\kvmem-llama.cpp-multi-backend-framework\workers\ninfer\start-kvmem-ninfer.bat

# 2) 全量测试（33 针 / 262k / 80 工具调用，约 16 分钟）
python I:\dsh\html\niah33_262k.py

# 3) 快速诊断（7 轮，打印模型原始输出，约 4 分钟）
python I:\dsh\html\niah_diag.py

# 4) 宿主单测
I:\dsh\html\build-host-tests.bat
```

**产物**：
- 分阶段原始指标：`I:\dsh\html\niah33-262k-metrics.json`
- 完整运行日志：`I:\dsh\html\niah33-final.log`
- 服务端日志（含 58 次重选 trace）：`I:\dsh\html\niah33c-server.log`

---

## 8. 测试设计要点（供后续复用）

1. **每轮写入必须 >16384 token** 才能持续压迫 KV 池；本测试用 435 行 × 36.61 token ≈ 16,588。
2. **不要用 450 行/轮 × 15 轮**：会到达 260,948 token，只剩 ~157 token 余量，最终召回的第 2 批必然 `HTTP 400 context length exceeded`。435 行/轮留出 ~11k 余量。
3. **每轮请求必须带相同的 `tools` 列表**。只在召回阶段加 tools 会改变 chat 模板的 system 段，导致前缀失效（实测表现为 `cache 0 (0.0%)` 并重算全部 223k token）。
4. **工具调用必须回 `role=tool` + `tool_call_id`**。丢弃 `tool_calls` 会让对话结构非法，模型随后停止调用工具、召回归零 —— 这是之前误判为"召回崩溃"的真实原因。
5. **判定用精确匹配**，但应同时报告"重试并集"，因为一位数字的笔误会被前者算作完全失败。
