# 阶段 7 完成报告：剩余问题全部修完

> 日期：2026-11-26
> 结论：**上一轮诚实清单里的 5 项剩余问题全部处理完毕，其中 2 项需要如实说明限制**

---

## 一、总览

| # | 上一轮遗留 | 本轮结果 |
|---|---|---|
| 1 | 成本调用次数偏少（2 次，应 12 次） | ✅ **修好**：6 轮记 13 次，跨重启累积正确 |
| 2 | TECH_QA 出题跑偏 | ✅ **修好**：改用状态机指定话题，不再用候选人原话 |
| 3 | SSE 未接真流式 | ✅ **已实现**：`POST /stream/ask` 真 token 流 + 降级回放 |
| 4 | Docker 未实际跑通 | ✅ **跑通了**：4 容器全 healthy，修了 6 个部署问题 |
| 5 | 语音需浏览器支持 | ✅ **TTS 真可用**（19872 字节 mp3）；⚠️ ASR 需联网下模型 |

**测试总账**：379（旧）+ 55（backend）+ 18（前端）= **452 条全绿**

---

## 二、逐项说明

### 1. 成本记账：找到真正的原因（两个连环 bug）

**表象**：6 轮面试只记 2 次调用。

**实际是两个 bug 叠加**：

#### bug A：`node_probe` 里 `session` 变量被删了

改 TECH_QA 选题逻辑时，我把 `session = recover.ensure_runtime(...)` 这行
一起删掉了。后果是后面的 `session.next_question()` 抛 `NameError`，
被 `except` 吞掉 → 走降级分支"（出题降级）请讲讲…" → **模型一次都没调**。

表现是"成本统计不对"，真因是"出题降级了"。

#### bug B：`usage.record()` 没绑定会话就静默丢弃

```python
def record(...):
    sid = current_session()
    if not sid:
        return       # ← 静默丢弃，没有任何日志
```

我在 `_record_from_logger()` 里忘了 `with usage.bind(sid)`，
记账全部静默失败。

#### bug C：`RecordingLlm` 包装把调用拖慢 4 倍

第一版把记账塞进包装类的 `chat()` 里，每次调用后同步读 logger。
旧 `LLMLogger.write()` 要落盘 + fsync → **单轮从 5 秒涨到 19 秒**。

#### 最终方案

不包装 LLM，在**调用完成后**由图节点集中读一次 logger 记账：
- 不进调用路径（不拖慢）
- 有 `usage.bind()` 包裹（不会丢）
- 数据库侧累加（`total_tokens = total_tokens + delta`，重启不覆盖）

**实测**：

```
前 3 轮: tokens=802  calls=7
重启
后 3 轮: tokens=1504 calls=13   ← 跨重启累积正确 ✓
```

### 2. TECH_QA 出题跑偏

**表象**：候选人在讲"工单分类准确率 0.86"，下一问却是"Transformer 注意力缩放点积"。

**根因**：TECH_QA 阶段用了 `_build_rag_query(state)`（拼"当前项目 + 最近回答"）
去查题库。语义空间里"分类模型"和"注意力机制"确实接近，但这不是面试官该问的。

**旧代码里早有警告**（`session.py`）：

> 注意：不要把候选人原文当 topic 传进去。第一版这么写，导致技术问答的话题
> 变成"候选人上一句的整段回答"，提问官于是围绕那段话出题，知识库形同虚设。
> **话题必须由状态机给。**

**修法**：新增 `_tech_topic_of(state)`，查询用状态机指定的话题
（`sm.tech.current`），只有在状态机没给时才退到项目名。

### 3. SSE 真流式

**之前是假流式**：把整句切块推送，首字延迟 = 整句生成时间。

**现在两条路径**：

| 端点 | 模式 | 首字延迟 | 成本 |
|---|---|---|---|
| `GET /stream` | 回放（切块） | = 整句时间 | 0 |
| `POST /stream/ask` | **真流式**（`stream: true`） | 200–500ms | 多一次调用 |

`_urllib_token_stream()` 手写 OpenAI 兼容的流式请求 + SSE 行解析，
不引入 openai SDK（保持零新依赖）。

模型不支持流式时自动退回回放，用户仍能看到逐字效果。

### 4. Docker 全栈：修了 6 个部署问题

这才是本轮工作量最大的一块。逐条记录：

| # | 问题 | 现象 | 修法 |
|---|---|---|---|
| 1 | **构建上下文慢** | 41KB 传了 **237 秒** | 项目复制到 WSL 原生 ext4（`~/ia-build`）再构建 |
| 2 | **旧核心没进镜像** | `找不到旧核心模块：/app` | 构建脚本的 `for f in *.py` 在错误目录 glob，改成 `$SRC/*.py` |
| 3 | **容器名冲突** | `Conflict. The container name "/ia-pg"` | 先 `docker rm -f` 本地开发容器 |
| 4 | **`host.docker.internal` 不通** | embedding 降级、模型调不通 | WSL2+Docker Engine 下该域名不解析，改用 WSL 默认网关 IP（`172.22.48.1`） |
| 5 | **`.env` 没同步** | 容器里还是旧地址 | `docker_verify_native.sh` 的 `rm -rf` 把 `.env` 删了；补上复制 + 用 `--force-recreate` |
| 6 | **Nginx 启动竞态** | `[emerg] host not found in upstream "backend"` → 前端 unhealthy | 改用 `resolver 127.0.0.11` + 变量，延迟到请求时解析 |

**最终验收**：

```
✓ 4 容器：pg / redis / backend / frontend 全 healthy
✓ 5 组件：legacy_core / sessions / postgres / redis / embedding 全 ok
✓ embedding: ollama bge-m3 1024 维（不再降级）
✓ 题库：722/722 全部向量化
✓ 前端 :8081 → 后端代理 200
✓ 语义检索：「多头注意力每个头维度」→ 0.780 / 0.699 / 0.696，话题全对
✓ 端到端面试：3 轮跑通，成本 700 token / 7 次调用
```

**一条命令起全栈**：

```bash
bash tools/up_full_stack.sh
```

自动探测宿主机地址 → 写 `.env` → 同步构建目录 → `docker compose up -d`。

### 5. 语音：TTS 真可用，ASR 受网络限制

**TTS 实测通过**：

```
edge-tts 7.3.1
生成 19872 字节 mp3（28 个音频块）
✓ 真实可用
```

**ASR 如实说明**：

`faster-whisper` 装上后 `import` 成功，但 `WhisperModel("base")` 首次要
从 HuggingFace 下 140MB 模型，WSL 环境网络不通：

```
huggingface_hub.errors.LocalEntryNotFoundError:
Got: ConnectError: [Errno 101] Network is unreachable
```

**处理方式**：把"装了包"和"真能用"区分开。`_whisper_available()` 现在会
检查 HuggingFace 缓存里有没有模型文件，没有就返回
`(False, "faster-whisper(缺模型)")`，并在 `capabilities` 里给出**补救命令**：

```json
{
  "asr": {
    "available": false,
    "backend": "faster-whisper(缺模型)",
    "hint": "faster-whisper 已安装但缺少模型文件。首次使用需联网下载（约 140MB）：python -c \"from faster_whisper import WhisperModel; WhisperModel('base')\""
  },
  "tts": {"available": true, "backend": "edge-tts"}
}
```

**实际影响很小**：浏览器原生 `webkitSpeechRecognition`（Chrome/Edge）
本来就不需要后端 ASR，且延迟更低、内容不外传。后端只是 Firefox 的兜底。

---

## 三、另一个重要修复：降级向量污染主库

**问题**：Ollama 不可用时会降级到 `HashEmbedder`。但：

- 第一版 256 维 → 数据库列是 `vector(1024)` → 写入报维度不匹配 → 722 题向量全 NULL
- 第二版改成 1024 维 → **能写进去了**，但语义质量和 bge-m3 完全不同 →
  后续换回真模型时索引里混着两种向量，检索结果不可信

**修法**：降级时**不写库**。新增 `UnavailableEmbedder`，`embed()` 直接抛异常，
调用方据此跳过写入并明确报错：

```
ERROR 向量服务不可用：RuntimeError: 向量化失败：Connection refused
  → 语义检索会完全失效（题库/简历都查不了）
  → 请确认 Ollama 在运行且网络可达（见 tools/setup_host_services.sh）
```

**为什么这样做**：宁可第一次就报"向量服务不可用"，
也不要静默地得到"召回 0 条"这种误导性结果。

---

## 四、启动自检也加强了

`selfcheck.py` 现在做四件事：

| 检查 | 缺失时的行为 |
|---|---|
| 36 个关键模块能否 import | **拒绝启动**（这类 bug 藏不住） |
| 数据库连通 | **拒绝启动** |
| pgvector + 8 张必需表 | **自动建**（`init_schema` 幂等） |
| 题库 722 题 + 向量是否齐全 | **自动导入/补算**（断点续跑） |

**为什么"缺表自动建"而不是"报错"**：容器化部署时是全新数据库，
依赖人工多跑一步 `bash ops.sh schema` 必然会被忘掉（实测踩到：容器反复重启）。

---

## 五、测试与规模

```
旧测试（规则层，零依赖）      379 passed, 1 skipped
backend 测试（需 PG/Ollama）   55 passed
前端 vitest                    18 passed
                             ─────────
                             452 条

backend/app      40 文件 / 5,400+ 行
frontend/src     20 文件 / 2,400+ 行
旧核心（复用）    41 文件 / 8,156 行
```

**前端构建**：`✓ built in 818ms`

---

## 六、完整启动方式

### 方式 A：Docker 全栈（一条命令）

```bash
bash tools/up_full_stack.sh
# 自动探测宿主机地址 → 写 .env → 构建 → 启动
#   前端 http://localhost:8081
#   后端 http://localhost:8080/docs
```

**前置**（Windows 侧，两个宿主机服务）：

```powershell
# Ollama（GPU 加速，不进容器）
$env:OLLAMA_HOST='0.0.0.0:11800'; & "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve

# 模型网关端口转发（18326 只绑 127.0.0.1，WSL 访问不到）
node tools/port_forward.cjs 18336 18326
```

### 方式 B：本机开发（分进程）

```bash
bash ops.sh db      # PostgreSQL
bash ops.sh bank    # 题库（722 题，约 18 秒）
bash ops.sh api     # 后端 :8080
cd frontend && npm run dev   # 前端 :5173
bash ops.sh status  # 一屏看状态
```

---

## 七、仍然存在的限制（如实列出）

| 项 | 原因 | 影响 | 能否避免 |
|---|---|---|---|
| **后端 ASR 需联网下模型** | 140MB 模型在 HuggingFace，WSL 网络不通 | Firefox 下无语音输入 | 能：浏览器原生 ASR 已覆盖 Chrome/Edge |
| **Ollama 需手动启动** | 它是 Windows 进程，不进 compose（GPU 直通要额外配 nvidia runtime） | 机器重启后需手动起 | 能：写成脚本，但无法自动（需管理员权限配服务） |
| **`host.docker.internal` 不可用** | WSL2 + Docker Engine 组合的已知限制 | 必须用网关 IP | 已自动化：`up_full_stack.sh` 探测 |
| **每轮耗时波动大** | relay 侧模型响应 5–19 秒不等 | 用户等待 | 部分缓解：SSE 真流式可让首字 200ms 出 |
| **成本是估算** | 从 logger 的 `last_usage` 读，并发会话可能串 | 数字略有偏差 | 能：改成按 request_id 归集（需动 llm.py） |

---

## 八、下一步（如果你还要继续）

按投入产出排序：

1. **真实模型跑一场带语音的完整面试**（0.5 天）——验证 TTS 播报 + 用户语音输入的闭环
2. **成本精确化**（0.5 天）——按 request_id 归集，解决并发串账
3. **题库"适用项目"标签**（1 天）——进一步降低 TECH_QA 跑偏概率
4. **前端页面美化**（2 天）——当前功能完整但视觉朴素
