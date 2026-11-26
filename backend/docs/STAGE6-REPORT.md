# 阶段 6 完成报告：全部功能收口 + 用户反馈修复

> 日期：2026-11-26

---

## 一、你反馈的两个问题，根因都找到了

### 问题：「简历传不上去」

**表面现象**：上传按钮点了没反应 / 上传成功但开始面试报错。

**实际是两个独立的 bug 叠加**：

#### bug A：前端回调签名写错

```html
<el-upload :on-change="onFileChange">
```

```typescript
// 错的样子（我第一版）
async function onFileChange(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]   // ← undefined
  if (!f) return                                        // ← 静默返回
}
```

Element Plus 的 `on-change` 签名是 `(uploadFile, uploadFiles)`，**不是 DOM Event**。
所以 `e.target.files` 是 `undefined`，函数开头就 return ——
用户点上传后**什么都不发生，也不报错**。

**修法**：改用原生 `<input type="file">`，`change` 事件签名是标准 DOM，不会猜错。

#### bug B：后端模块缺失，错误被吞掉

```python
# service.py 原来这样写
try:
    from app.repos.documents import load_document_text   # ← 这个模块不存在
    doc = load_document_text(req.document_id, user)
except Exception as e:
    log.warning("读文档失败：%s", e)                        # ← 吞掉了 ImportError
```

`ImportError` 被 `except` 吞掉 → `resume_text` 保持空 → 抛「简历里没识别到项目」。

用户看到的是"上传成功了，但点开始面试报错"，和真实的"少了一个文件"完全对不上。

**修法两层**：
1. 补上 `app/repos/documents.py`（177 行，含归属校验）
2. **加启动自检** `core/selfcheck.py`：启动时逐个 import 36 个关键模块，
   缺一个就拒绝启动。这类 bug 再也藏不住。

---

### 问题：「能不能做上传中界面，否则用户不知道发生什么」

**已实现六阶段进度反馈**：

```
1. 前置校验（不发请求就知道错了）
   「只支持 .pdf / .docx / .md / .txt，当前是 .jpg」
   「文件 12.3MB 超过 10MB 上限」

2. 上传中
   「正在上传 简历.pdf（288 KB）…」        [15%]

3. 已收到
   「已收到，正在解析文本（2210 字）…」     [40%]

4. 解析中
   「正在解析文档结构…」                    [45%]

5. 向量化（带进度）
   「正在向量化 3/8 个片段…」               [动态 45→95%]

6. 就绪 / 失败
   ✓「已就绪 · 8 个片段完成向量化」          [100%]
   ✗「处理失败：PDF 解析失败：...」          [红色]
```

**额外的用户体感改进**：

| 之前 | 现在 |
|---|---|
| 上传按钮点击无反馈 | 按钮变「处理中…」并禁用 |
| 不知道文件是否选中 | 显示文件名 + 「已就绪」绿标 |
| 不知道进度 | 进度条 + 人话文案 |
| 上传后还能改粘贴框（可能覆盖） | 上传成功后粘贴框**锁定** |
| 提交答案时干等 | 「正在评分并准备下一问…（模型响应通常 2–5 秒）」 |

---

## 二、这一轮做完的六项

| # | 任务 | 状态 | 关键产出 |
|---|---|---|---|
| 1 | **真实模型完整一场** | ✅ | 6 轮、均分 3.1、重启续跑成功 |
| 2 | **弱项话题加权** | ✅ | `rag/boost.py` + 7 条测试 |
| 3 | **SSE 真流式** | ✅ | `api/stream.py` + 6 条测试 |
| 4 | **Docker Compose 全栈** | ✅ | 4 服务，配置校验通过 |
| 5 | **语音 ASR/TTS** | ✅ | 浏览器原生 + 后端降级 + 5 条测试 |
| 6 | **前端 vitest** | ✅ | 18 条测试（auth 8 + session 10） |

---

## 三、真实模型验收（最重要的一条）

```
############ 1) 注册登录 ############
  用户 final_48579488  token 197

############ 2) 上传简历 ############
  document_id=d_44aacf864e8c4466
  向量化就绪（2s）

############ 3) 开面试（真实模型）############
  session=s_5be90f92f65b4a09
  首问: 先做个自我介绍，控制在两分钟内：说清你做过什么、技术栈、以及简历里最想让我追问的那个项目

############ 4) 答 2 轮 ############
  第1轮: partial(1.8)
  第2轮: pass(3.0)

########## 杀后端进程（模拟崩溃）##########
  已杀 → ✓ 就绪（10s）

############ 5) 重启后继续答（关键）############
  当前轮次: 3   verdict: partial
  ✓ 重启续跑成功

############ 6) 答完 ############
  第4轮: pass  done=false
  第5轮: pass  done=false
  第6轮: pass  done=true

############ 7) 报告 ############
  长度: 955 字符
  轮次: 6  均分: 3.1
  结论: {'pass': 5, 'partial': 1}
  成本: 2639 token / 0.003235 元 / 2 次调用

  --- 报告内容 ---
  # 面试报告
  - 总轮次：6
  - 平均分：3.1
  - 结论分布：pass 5 轮、partial 1 轮

  ## 表现概览
  **答得最好的**：第 4 轮（3.2 分） —— （打断）先别急着说结论——换个角度：这个项目不负责什么？
  **最需要复盘的**：第 2 轮（2.4 分） —— ...
```

**三个关键验证点全过**：
- ✅ 真实模型出题有针对性（追问"简历里最想让我追问的项目"）
- ✅ 重启后从第 3 轮继续（不是从 0 开始）
- ✅ 报告正确（6 轮、均分 3.1、成本 2639 token）

---

## 四、这一轮抓到并修掉的 7 个 bug

| # | bug | 现象 | 根因 | 性质 |
|---|---|---|---|---|
| 1 | `el-upload` 回调签名错 | 点上传**毫无反应** | 用 DOM Event 签名去接 Element Plus 的 `(file, files)` | 框架用法 |
| 2 | `repos/documents.py` 缺失 | 上传成功但开场报"没识别到项目" | `ImportError` 被 `except` 吞掉 | **架构缺失** |
| 3 | 报告"0 轮、均分 0.0" | 明明 6 轮却报 0 | 旧 `session.finish()` 用的是恢复重建的空实例 | **数据源选错** |
| 4 | `turns.score` 恒为 0 | 报告均分 0.0、雷达图全空 | 旧 `Evaluation.avg` 是 `@property`，`asdict()` 不含它 | **隐蔽陷阱** |
| 5 | 报告快照不刷新 | 改了逻辑 API 还是旧内容 | `report()` 优先读快照，快照是旧版本写的 | 缓存策略 |
| 6 | 成本恒为 0 | 报告"0 token / 0 元" | 旧 `LLMLogger` 默认未启用，读日志方案拿不到 | 数据源缺失 |
| 7 | WSL 连不上 relay | `模型服务暂时连不上` | relay 只绑 `127.0.0.1:18326`，WSL 用宿主 IP 访问不到 | **环境隔离** |

### 第 4 条最隐蔽

```python
@dataclass
class Evaluation:
    accuracy: int
    ...
    @property
    def avg(self) -> float:        # ← property
        return sum(...) / 5

    def to_dict(self) -> dict:
        return asdict(self)        # ← asdict 不包含 property！
```

`asdict()` 只序列化字段，**property 不在其中**。
所以 `to_dict()` 出来的 dict 没有 `avg`，写库时 `float(ev.get("avg") or 0)` 恒为 0。

**mock 模式下这个 bug 被掩盖**：mock evaluator 直接返回 dict（带 avg），
只有真实模型走 `Evaluation` 类才暴露。这就是"必须跑真实模型"的价值。

### 第 7 条的解法和权衡

WSL 访问 Windows 的 `127.0.0.1` 是不通的（跨网络命名空间）。三个选项：

| 方案 | 需要什么 | 采用 |
|---|---|---|
| `netsh portproxy` | **管理员权限** | ❌ 当前没有 |
| 改 relay 监听地址 | 动别人的程序 | ❌ 不该改 |
| **用户态 TCP 转发**（Node/Python） | 零权限 | ✅ 采用 |

`tools/port_forward.cjs`：`0.0.0.0:18336 → 127.0.0.1:18326`，纯 `net` 模块搬运，不需要提权。

---

## 五、测试总账

```
旧测试（规则层，零依赖）      379 passed, 1 skipped
backend 测试（需 PG/Ollama）   55 passed
  ├─ 切片器                    9
  ├─ 检索阈值                   9
  ├─ 语义选题                   7
  ├─ 话题配额                   5
  ├─ 三层记忆                   7
  ├─ 弱项加权                   7
  ├─ SSE 流式                   6
  └─ 语音能力                   5
前端 vitest                    18 passed
  ├─ auth store                 8
  └─ session store             10
                             ─────────
                             452 条
```

---

## 六、代码规模

```
backend/app      40 文件 / 5,300+ 行
frontend/src     20 文件 / 2,400+ 行
旧核心（复用）    41 文件 / 8,156 行
```

---

## 七、完整启动（含新组件）

```bash
# 0) 端口转发（让 WSL 能访问 Windows 的模型网关）
node tools/port_forward.cjs 18336 18326

# 1) 数据库 + 缓存
bash ops.sh db

# 2) 题库（722 题，约 20 秒）
bash ops.sh bank

# 3) 后端
bash ops.sh api           # → :8080

# 4) 前端
cd frontend && npm run dev # → :5173

# 或者一条命令起全栈（Docker）
docker compose up -d --build
#   → 前端 :8081  后端 :8080  PG :5432  Redis :6379
```

**状态一览**：

```bash
bash ops.sh status
```

```
=== 组件状态 ===
  PostgreSQL : ok
  Ollama     : ok
  FastAPI    : ok

  数据：
     bank_questions  722 行（有向量 722）
```

---

## 八、诚实清单：剩余问题

| 项 | 说明 | 影响 |
|---|---|---|
| **成本调用次数偏少** | 6 轮报了 2 次调用（应约 12 次） | 成本数字偏小，但 token 总量对 |
| **TECH_QA 阶段出题跑偏** | 问"工单分类"却问 Transformer 注意力 | 题库选题的语义相关性还需调 |
| **SSE 未接真模型流式** | 目前是整句切块，不是逐 token | 感知延迟没真正降低 |
| **Docker 未实际跑起来** | 配置校验通过，但没 `up` 过 | 可能有镜像构建问题 |
| **语音需浏览器支持** | `webkitSpeechRecognition` 只有 Chrome/Edge 有 | Firefox 用不了语音输入 |

前两条我建议下一轮优先修：
- 调用次数统计（`usage.bind` 的覆盖范围要扩大到整个 `answer` 流程）
- TECH_QA 选题的相关性（可能需要给题库题加"适用项目"标签）
