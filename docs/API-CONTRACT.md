# 接口契约快照与变更清单

> 快照文件：`tests/golden/api_snapshot.json`（27 条，从旧 `webui.py` 真实录制）
> 验证测试：`tests/test_api_snapshot.py`（23 条，全绿）
> 录制工具：`tools/capture_api_snapshot.py`
> 录制日期：2026-11-26

---

## 一、为什么要先录快照

接下来要做的事：`webui.py`（标准库 http.server）→ FastAPI；`web/` 原生 JS → Vue 3。

**最容易出的问题不是报错，而是响应结构悄悄变了**：少一个字段、多一层嵌套、
某个数字从 int 变 str —— 前端不会崩，但行为已经不同，而单元测试看不见。

所以顺序是：

```
录快照 → 写新实现 → 逐字段比对 → 才敢删旧代码
```

这和之前做提示词模板化时"先拍 9 条 golden"是同一个方法。

---

## 二、快照里怎么处理不确定性

| 类型 | 处理 | 原因 |
|---|---|---|
| 时间戳 | `<TIME>` / `<DATE>` | 每次都变 |
| 会话 id（`s_1795...`） | `<SID>` | 每次不同 |
| 长数字 | `<NUM>` | 含时间戳 |
| 哈希 | `<HASH>` | 每次不同 |
| 浮点 | 保留 4 位 | 末位会抖 |
| 模型输出 | **用 mock 模式** | 出题/评分是随机的，不固定就没法比对 |

---

## 三、27 个接口的真实响应

### 3.1 公开接口（无需登录）

| 接口 | 状态 | 真实响应字段 |
|---|---|---|
| `GET /api/health` | 200 | `{ok, sessions}` |
| `GET /api/meta` | 200 | `{roles, personas, default_resume}` |
| `GET /api/bank` | 200 | `{total, topics, items, preview}` |
| `GET /api/registration` | 200 | `{open, first, existing}` |

### 3.2 鉴权相关

| 接口 | 状态 | 真实响应 | 备注 |
|---|---|---|---|
| `POST /api/register`（密码 3 位） | 400 | `{ok:false, error:"密码至少 6 位"}` | 有长度校验 |
| `POST /api/register`（正常） | 200 | `{ok, user, role, first}` | `first=true` 表示第一个用户成为管理员 |
| `POST /api/login`（错密码） | 401 | `{error:"用户名或密码不对"}` | |
| `POST /api/login`（正常） | 200 | `{ok, user, role}` | 同时 Set-Cookie |
| `GET /api/me` | 200 | `{user, role}` | 本机免登录时返回 `local` |
| `POST /api/logout` | 200 | `{ok}` | |

**⚠ 字段名坑**：注册用的是 `password`，不是 `pwd`。
第一次录快照时我用了 `pwd`，结果报"密码至少 6 位"（其实是字段没读到、空字符串）。

### 3.3 面试主链路

| 接口 | 状态 | 真实响应字段 |
|---|---|---|
| `POST /api/start` | 200 | `{question, projects, engine, progress, coach, resume_quality, ...}` |
| `POST /api/answer` | 200 | `{question, eval, progress, done, card}` |
| `POST /api/end` | 200 | `{ok, done, finish_reason, report, dims}` |
| `GET /api/report` | 200 | `{report, dims, profile}` |

### 3.4 数据查询

| 接口 | 状态 | 真实响应字段 |
|---|---|---|
| `GET /api/history` | 200 | `{count, text}` |
| `GET /api/trend` | 200 | `{user, count, points, dim_avg, avg_series}` |
| `GET /api/sessions` | 200 | `{sessions}` |
| `GET /api/users` | 200 | `{users}` |
| `GET /api/admin` | 200 | `{users, sessions}` |

### 3.5 异常路径

| 接口 | 状态 | 真实响应 |
|---|---|---|
| `POST /api/answer`（无 sid） | 200 | `{error:"会话不存在（服务重启或已过期），请重新开始"}` |
| `POST /api/answer`（空答案） | 200 | `{error:"回答为空"}` |
| `POST /api/start`（非法 role） | **500** | `{error:"LLMError: 既没有本地模型，也没有 LLM_API_KEY。"}` |

---

## 四、快照暴露出的 3 个真实问题

### 问题 1：非法 role 返回 500，不是 400

```
POST /api/start {"role": "不存在的岗位"}
→ 500 {"error":"LLMError: 既没有本地模型，也没有 LLM_API_KEY。"}
```

**两层问题**：
1. 客户端传错参数（4xx 范畴）却返回 500；
2. 错误信息泄露内部类名 `LLMError`，且**根因是"没配模型"而不是"role 不对"**——
   说明 role 校验顺序在模型初始化之后，参数错了也是先炸模型。

新实现要改成：先 Pydantic 校验 role → 400 带可选值列表。

### 问题 2：`/api/end` 与 `/api/report` 重复返回同一份报告

```
/api/end.report     → 2108 字符（审计摘要）
/api/report.report  → 2108 字符
两者逐字相同 ==  True
```

**同一个字符串被两个接口各存一份**。新实现拆分：
- `POST /api/sessions/{id}/finish` → 只返回 `{status, finish_reason}`
- `GET /api/sessions/{id}/report` → 返回完整报告

### 问题 3：错误码不统一

```
/api/answer 无 sid   → 200 + {error:...}     ← 业务错误用 200
/api/register 密码短 → 400 + {error:...}     ← 参数错误用 400
/api/login 错密码    → 401 + {error:...}     ← 鉴权错误用 401
/api/start 非法 role → 500 + {error:...}     ← 却是 500
```

同样性质的问题（输入不对），三种状态码。新实现统一成：

```
400  参数不合法（Pydantic 校验失败）
401  未认证 / 凭证错误
403  已认证但无权限
404  资源不存在
409  状态冲突（会话已结束还答题）
422  语义错误（答案为空）
500  未预期异常（不泄露内部类名）
```

---

## 五、我三次写错测试的经过（值得记下来）

`test_report_*` 那组测试我改了三次，每次都是**先入为主**：

| 版本 | 我的假设 | 实际 | 教训 |
|---|---|---|---|
| 第 1 版 | `report` 是 dict（结构化） | 是 str（Markdown） | 别猜，去看 |
| 第 2 版 | 两接口类型不同 | 都是 str | 别推理，去测 |
| 第 3 版 | 两接口长度不同 | 逐字相同（2108 字符） | 别二分，去比对 |

**这三次全是我"根据名字推断内容"**，而快照里明明有真实数据。
最后改成断言 `end_report == get_report`，一次就对了。

这件事本身是这类工作的写照：**快照的价值就是让人不必猜**。

---

## 六、快照里两条"故意断言缺陷"的测试

这两条测试的用途不是验证正确性，而是**把现状钉住**：

```python
def test_known_defect_bad_role_returns_500(snap):
    """已知缺陷：非法 role 时旧实现抛 500（应为 400）。
    这条测试故意断言缺陷存在，作用是：
    1. 防止有人以为它是"正确行为"；
    2. 新实现修成 400 后，这条测试会红——提醒更新快照并写进变更说明。
    """

def test_report_payload_is_identical_on_both_endpoints(snap):
    """三次试错后按事实改的……真实情况是同一个字符串被两个接口重复返回。
    新实现会合并成一处，这条测试就是变更前的证据。
    """
```

它们会在重构时**主动变红**，这是设计意图——红了说明行为变了，需要显式确认。

---

## 七、新接口契约（目标状态）

| 旧 | 新 | 变化 |
|---|---|---|
| `POST /api/start` | `POST /api/sessions` | 路径资源化 |
| `POST /api/answer` `{sid}` | `POST /api/sessions/{id}/answer` | sid 进路径 |
| `POST /api/end` | `POST /api/sessions/{id}/finish` | 不再返回报告全文 |
| `GET /api/report?sid=` | `GET /api/sessions/{id}/report` | |
| `GET /api/history` | `GET /api/sessions?page=&size=` | 分页 |
| `GET /api/bank` | `GET /api/bank` + `GET /api/bank/search?q=` | 新增语义检索 |
| — | `POST /api/auth/login` / `refresh` / `logout` | JWT + refresh |
| — | `POST /api/documents/upload` / `{id}/status` | 简历上传 → 切片 → 向量化 |
| — | `GET /api/sessions/{id}/stream` | SSE 流式 |
| — | `GET /api/profile` | 长期记忆画像 |
| — | `GET /api/rag/search?index=&q=` | RAG 诊断 |

统一响应包：

```json
{"code": 0, "data": {...}, "message": ""}
```

错误：`{"code": 40001, "data": null, "message": "role 必须是 ai_algo/backend/..."}`
