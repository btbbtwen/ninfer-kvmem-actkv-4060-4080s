# 构建陷阱：改 `kvmem_store.hpp` 后必须强制重编

## 症状

ninfer server 在 250k 上下文下崩溃：

| 时间 | 退出码 | 崩溃位置 |
|---|---|---|
| 22:00 | `0xC0000005` | `VCRUNTIME140.dll+0x1dc10`（memcpy） |
| 22:17 | `0xC0000005` | `infer-kvmem-server.exe+0x46eaec` = **`KvMemStore::accumulate_lrfu+0x16C`** |

同一 prompt 在 **ActKV 关闭**时能完整跑完（254,557 token / 689 s）。崩溃点总在
**prefill 结束、第一次检索打分**时（两臂 prefill 速率曲线逐点相同，ON 臂在 ~350 s 死亡）。

## 定位过程

1. Windows 事件日志给出崩溃偏移：`Application Error` 事件 → `Fault offset: 0x46eaec`。
2. 用 `/MAP` 重链（**只影响链接、不改变代码生成**，所以地址与崩溃二进制一致）：

   ```powershell
   scripts\windows\build-backends.ps1 -Backend ninfer -CudaPath <cuda> -Jobs 10 -CMakeArgs '-DCMAKE_EXE_LINKER_FLAGS=/MAP'
   ```

3. 在 `infer-serve.map` 里按 `Rva+Base = 0x140000000 + 0x46eaec` 二分查找最近的符号
   （MAP 格式：`段:偏移  符号  Rva+Base  对象`，基址见文件头 `Preferred load address`）。
4. 结果落在 `?accumulate_lrfu@KvMemStore@kvmem@@...` 内，`+0x16C`。
5. `dumpbin /DISASM` 全量反汇编（515 MB）后定位到崩溃指令：

   ```asm
   14046EA9D: lea  rbx,[rcx+180h]      ; rbx = &lrfu_scratch_  (this+0x180)
   14046EAA4: mov  rax,[rbx]           ; begin
   14046EAAD: mov  [rbx+8],rax         ; clear(): end = begin
   14046EAE2: mov  rdx,[rbx+8]         ; rdx = end
   14046EAE6: cmp  rdx,[rbx+10h]       ; end vs capacity
   14046EAEC: movsd [rdx],xmm0         ; ← 崩溃：写野指针
   ```

## 根因：增量构建的 ABI 错配（ODR 违规）

`KvMemStore` 的成员布局变了，但**只有 `kvmem_store.cpp.obj` 被重编**：

| 文件 | 时间戳 | 状态 |
|---|---|---|
| `kvmem_store.hpp`（加了 `lrfu_last_round_`/`lrfu_round_seen_`，随后又加 `lrfu_scratch_`） | 21:25 / 22:08 | 已改 |
| `kvmem/src/host/kvmem_store.cpp.obj` | 21:26 / 22:09 | ✅ 重编 |
| `backends/ninfer/.../memory_statistics.cpp.obj`（**构造** `KvMemStore` 的地方） | **20:24** | ❌ 未重编 |
| `backends/ninfer/.../kvmem_window.cpp.obj` | **20:24** | ❌ 未重编 |

构建日志印证：那两次"重编"都是 `[2/4]`，**只编译了 1 个 obj**，完全没碰 ninfer 侧包含该头的 TU。

于是：ninfer 的 TU 按**旧 `sizeof(KvMemStore)`**（少一个 24 字节的 `std::vector`）分配对象，
`accumulate_lrfu` 却按新偏移 `0x180` 访问 `lrfu_scratch_` → **写到对象边界之外** → 崩溃。
这也解释了为什么只有 ActKV 打开才崩：只有它会读那个成员。

## 修复

```powershell
# 清掉所有过时 obj，强制全量重编
$bd = 'I:\llama\kvmem-llama.cpp-multi-backend-framework\build-backends\ninfer'
Get-ChildItem $bd -Recurse -Filter '*.obj' | Remove-Item -Force
Get-ChildItem "$bd\kvmem-core" -Recurse -Filter '*.lib' | Remove-Item -Force
scripts\windows\build-backends.ps1 -Backend ninfer -CudaPath <cuda> -Jobs 10
```

## 规则（重要）

**任何改动 `kvmem/include/kvmem/*.hpp` 中类成员布局的提交，都必须强制全量重编 ninfer 后端。**
`backends/ninfer` 是通过 `-DNINFER_KVMEM_SOURCE_DIR` 引用仓库 `kvmem/` 的**外部**目录，
它的增量依赖没有正确跟踪该目录下头文件的变化。只改 `.cpp` 实现（不碰布局）时增量重编是安全的。
