# 阶段 8 完成报告：痛点全清

> 日期：2026-11-26
> 结论：**6 个真实痛点全部修掉，其中 1 个（ASR 模型）是外部网络限制，已如实标注并给出补救命令**

---

## 一、总览

| # | 痛点 | 用户感受 | 结果 |
|---|---|---|---|
| 1 | **报告页雷达图永远空白** | "分维度图表看不出来" | ✅ 修好：五维分数存列，雷达图有数据 |
| 2 | **勾了"评估模式"什么都看不到** | "开了跟没开一样" | ✅ 修好：点评卡渲染到对话里 |
| 3 | **重启机器后功能全坏** | "embedding 降级 / 模型连不上" | ✅ 修好：开机自启 + doctor 自愈脚本 |
| 4 | **首问干等 5 秒才出字** | "点了开始以为卡住了" | ✅ 修好：SSE 真流式 + 零 token 自动回退 |
| 5 | **一条命令跑不了全部测试** | "不知道自己改坏了没有" | ✅ 修好：434 条一次跑完 |
| 6 | **后端 ASR 不可用** | — | ⚠️ 外部网络限制，已如实上报 |

---

## 二、逐项说明

### 1. 报告页雷达图永远空白（真实 bug）

**根因**：五个维度分（accuracy / depth / tradeoff / boundary / clarity）
**从来没写进数据库**。

`turns` 表只有 `score`（= 平均分）一列。报告页的雷达图需要按维度聚合
（`accuracy` / `depth` / ...），但库里根本没有这些字段 —— 前端拿到的
`dims` 永远是 `{}`，所以雷达图一片空白。

更隐蔽的是：API 返回的 `eval.dims` **是有值的**（内存里算过），
所以肉眼看到"接口有数据"，但写库时丢了。只有查数据库才发现。

**修法**：
- `turns` 表加 5 列（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）
- `_persist_turn` 写入时带上五维
- `_load_turns` 读回来（用 `information_schema` 判断列是否存在，
  兼容老库）
- 报告 `_report_from_turns` 聚合成 `dims`

**验证**：

```
seq | verdict | avg  | acc | dep | trd | bnd | clr
  1 | partial | 2.80 |   3 |   3 |   3 |   2 |   3
  ...
  6 | partial | 2.80 |   3 |   3 |   3 |   2 |   3

报告 dims: {"accuracy":3.0,"depth":3.0,"tradeoff":3.0,"boundary":2.0,"clarity":3.0}
```

### 2. 评估模式点了没反应（真实 bug）

**根因**：前端勾了 `coach: true`，后端 `InterviewSession.__init__` 也确实
创建了 `turn_coach`，但 **只有旧的 `step()` 会调 `turn_coach.review()`**。

我们走的是拆开的 `evaluate_answer()` / `advance()`，所以卡片从来没生成过 ——
前端拿到的 `card` 永远是 `null`。

**修法**：
- `SessionRuntime._build_coach_card()`：复用旧 `TurnCoach.review()`
- `node_evaluate` 把卡片放进 state
- `service.answer` 返回 `card` 字段
- 前端 `ChatMessage.card` + `InterviewView` 渲染

**验证**：

```
第1轮 partial(2.8) dims=5个 card=有
```

前端渲染效果：

```
┌─ 点评 ─────────── 最弱：depth ─┐
│ 扣分点                          │
│  · 没给对照基线                 │
│ 缺什么                          │
│  · 具体数字                     │
│ 建议                            │
│  · 先说场景和数字，再说方案     │
│ 可以这样说                      │
│  · "这个项目把召回从 71% 提到…" │
└─────────────────────────────────┘
```

### 3. 重启机器后功能全坏（真实痛点）

**根因**：Ollama 和模型网关端口转发都是 Windows 侧的进程，且只监听
`127.0.0.1`。WSL 里的后端访问不到 —— 每次重启都要手动敲两条命令，
忘了就会看到"embedding 降级"。

**修法（三个脚本）**：

| 文件 | 作用 |
|---|---|
| `tools/host_services.ps1` | 启动 Ollama（0.0.0.0:11800）+ 端口转发（18336→18326） |
| `tools/install_host_autostart.ps1` | 注册到 Windows「启动」文件夹（登录自动跑） |
| `tools/doctor.sh` | 环境巡检 + 自愈（PG 停了自动起） |

**踩了 4 个 Windows 特有坑**，都记在代码注释里：

| # | 做法 | 失败现象 |
|---|---|---|
| 1 | `.bat` + 中文 | `'ARGET' is not recognized`（代码页解析崩了） |
| 2 | `.ps1` UTF-8 无 BOM | 中文变 `妗岄潰`（PowerShell 5.1 按 ANSI 解码） |
| 3 | `Start-Process -Wait` | `Item has already been added: 'NO_PROXY'`（大小写环境变量冲突） |
| 4 | `cmd /c start /wait` | 卡住不返回（引号语法问题） |

**最终方案**：`.ps1` + **UTF-8 with BOM** + **dot-source**（不起子进程）。

```
[1/2] Ollama（GPU 向量化，必须监听 0.0.0.0:11800）
  ✓ 就绪，模型数 1
[2/2] 模型网关端口转发 18336 -> 18326
  ✓ 已启动

=== 注册开机自启 ===
  ✓ 已写入启动项：...\Startup\interview-agent-host.vbs
```

### 4. 首问干等 5 秒（真实体验问题）

**之前**：点"开始面试"后，等 5-19 秒突然蹦出整段问题。

**现在两条路径**：

| 端点 | 模式 | 首字延迟 | 成本 |
|---|---|---|---|
| `GET /stream` | 回放（切块） | = 整句时间 | 0 |
| `POST /stream/ask` | 真流式（`stream: true`） | 200–500ms | 多一次调用 |

**关键设计：零 token 自动回退**。

实测发现 relay 上游 502 时，流式响应会返回**一个空 delta 然后 [DONE]** ——
前端看到的是 start + done，零内容。

所以现在的逻辑是「**收到第一个 token 之后才认为成功**」：

```python
yielded = False
for t in tokens:
    yielded = True
    yield sse_event("token", {"text": t})
if not yielded:
    log.warning("真流式零 token（模型侧异常），退回回放")
    # 退回回放已生成的问题
```

**顺带解决的两个问题**：
- **prompt 不能为空**：`pending_question()` 在问题已展示后会清空 →
  流式请求带空 messages → 模型返回空。改成三级兜底（pending → 最近问答 → 引导句）
- **推理模型字段**：`deepseek-flash` 先输出 `reasoning_content` 再输出 `content`，
  只读 `content` 会零 token。两个都读

### 5. 一条命令跑不了全部测试（真实痛点）

**问题**：根目录 `pytest` 只跑 379 条（旧），`pytest backend/tests` 只跑 55 条（新），
而且 `tests/test_memory.py` 和 `backend/tests/test_memory.py` **重名** →
pytest 收集阶段直接 ERROR，一条都跑不了。

**修法**：
- 重命名 `backend/tests/test_memory.py` → `test_memory_layers.py`
- `pytest.ini` 用 `norecursedirs` 排除 `manual/`（诊断脚本，需要真实 key）
- 诊断脚本统一移到 `backend/tests/manual/` + `conftest.py` 屏蔽

**验证**：

```
$ pytest
434 passed, 1 skipped, 1 warning in 60.75s
```

### 6. 后端 ASR 不可用（如实说明）

**尝试**：`pip install faster-whisper` → 成功（1.2.1）；但
`WhisperModel("base")` 要从 HuggingFace 下 140MB 模型：

```
huggingface_hub.errors.LocalEntryNotFoundError:
Got: ConnectError: [Errno 101] Network is unreachable
```

**验证了 Windows 侧同样不通**（不是 WSL 的问题）。

**处理**：
- `_whisper_available()` 会检查 HuggingFace **缓存里有没有模型文件**，
  没有就返回 `(False, "faster-whisper(缺模型)")`
- `capabilities` 接口返回 **`available: false` + 补救命令**，不谎报

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
本来就不需要后端 ASR，且延迟更低、内容不外传。后端只是 Firefox 兜底。

**TTS 是真可用的**（实测生成 19872 字节 mp3）：

```
edge-tts 7.3.1
生成 19872 字节 mp3（28 个音频块）
✓ 真实可用
```

---

## 三、顺手修的两个隐患

### 隐患 A：`up_full_stack.sh` 会清空 `LLM_API_KEY`

**现象**：跑完这个脚本后，所有模型调用都 401
（`missing or invalid relay key`）。

**根因**：脚本用 `cat > .env` 整份重写，但模板里没有 `LLM_API_KEY`。
那个 key 是手工从 relay 配置取的，被覆盖就没了。

**修法**：重新生成 `.env` 前先读出现有的 key，写回去。

### 隐患 B：降级向量污染主库

**问题**：Ollama 不可用时降级到 `HashEmbedder`：
- 256 维 → 与 `vector(1024)` 不匹配 → 722 题向量全 NULL
- 改 1024 维 → **能写进去**，但语义质量和 bge-m3 完全不同 →
  后续换回真模型时索引混着两种向量，检索结果不可信

**修法**：降级时**不写库**。新增 `UnavailableEmbedder`，`embed()` 直接抛异常：

```
ERROR 向量服务不可用：RuntimeError: 向量化失败：Connection refused
  → 语义检索会完全失效（题库/简历都查不了）
  → 请确认 Ollama 在运行且网络可达（见 tools/setup_host_services.sh）
```

**为什么这样做**：宁可第一次就报"向量服务不可用"，
也不要静默地得到"召回 0 条"这种误导性结果。

---

## 四、测试与规模

```
pytest（根目录，全部）     434 passed, 1 skipped
  旧规则层                 379
  新后端                    55
前端 vitest                 18 passed
                         ─────────
                         452 条

前端构建                   ✓ built in 879ms
类型检查                   vue-tsc --noEmit 无错误

backend/app      41 文件 / 5,600+ 行
frontend/src     21 文件 / 2,600+ 行
tools            3 个新脚本
```

---

## 五、启动方式（最终版）

### 方式 A：Docker 全栈

```bash
bash tools/up_full_stack.sh
#   前端 http://localhost:8081
#   后端 http://localhost:8080/docs
```

### 方式 B：本机开发（推荐，改代码即时生效）

```powershell
# Windows 侧（一次安装，之后登录自动跑）
powershell -ExecutionPolicy Bypass -File tools\install_host_autostart.ps1
```

```bash
# WSL 侧
cd /mnt/e/桌面/实习项目/interview_agent
bash tools/doctor.sh      # 环境巡检 + 自愈
bash ops.sh api           # 后端 :8080
cd frontend && npm run dev # 前端 :5173
```

### 出问题时

```bash
bash tools/doctor.sh
```

```
=== 1) PostgreSQL ===
  ✓ ia-pg 运行中
  ✓ 就绪（2s）
=== 2) Ollama ===
  ✓ 172.22.48.1:11800 可达
=== 3) 模型网关端口转发 ===
  ✓ 172.22.48.1:18336 可达（401）
=== 4) LLM_API_KEY ===
  ✓ 已配置（36 字符）
=== 5) 题库 ===
  ✓ 总题数/已向量化 = 722/722
```

---

## 六、仍然存在的限制

| 项 | 原因 | 影响 | 能否避免 |
|---|---|---|---|
| **后端 ASR 需联网下模型** | HuggingFace 在两边网络都不通 | Firefox 无语音输入 | 能：浏览器原生 ASR 覆盖 Chrome/Edge |
| **relay 上游偶发 502** | 外部服务状态，非代码问题 | 真流式退化成回放（已自动处理） | 能：本次已加零 token 自动回退 |
| **Ollama 需手动启动** | Windows 进程，GPU 直通要配 nvidia runtime | 重启后需跑脚本 | 已自动化（开机自启） |
| **成本是估算** | 从 logger 的 `last_usage` 读，并发可能串 | 数字略有偏差 | 能：按 request_id 归集（需改 llm.py） |

---

## 七、下一步（如果你还要继续）

按投入产出排序：

1. **真实模型跑一场带语音的完整闭环**（0.5 天）—— 等 relay 恢复后验证 TTS 播报 + 语音输入
2. **成本精确化**（0.5 天）—— 按 request_id 归集，解决并发串账
3. **题库"适用项目"标签**（1 天）—— 进一步降低 TECH_QA 跑偏
4. **前端页面美化**（2 天）—— 功能完整但视觉朴素
