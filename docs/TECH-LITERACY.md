# 技术扫盲：这些名字分别是什么、长什么样、为什么用它

写给"看到 Java / TypeScript / YAML / Lua / Redis / Flyway 就卡住"的人。
每个名词都按四件事讲：**是什么 → 长什么样 → 为什么用它 → 我们用了什么**。

---

## 1. Java 是什么，后端业务是什么，为什么不用 Python

### 1.1 Java 是一种编程语言

和 Python 同类，都是"写程序的语言"。区别在运行方式：

| | Python | Java |
|---|---|---|
| 怎么跑 | 解释器直接读源码执行 | 先编译成字节码，再由 JVM 执行 |
| 类型 | 动态（写错类型运行时才报） | 静态（编译期就报错） |
| 速度 | 慢一些 | 快（尤其长跑服务） |
| 并发 | 有 GIL，多线程不能真并行跑 CPU 活 | 真多线程；Java 25 还有"虚拟线程"（几万并发线程很便宜） |
| 生态 | AI/数据科学最强 | 企业后端、支付、电商最强 |
| 启动 | 秒级 | JVM 启动慢，内存占用大 |

**Java 代码长这样**（跟 Python 对比）：

```java
// Java：每个变量都要写类型
public class Hello {
    public static void main(String[] args) {
        String name = "YG";
        int turns = 16;
        System.out.println(name + " 有 " + turns + " 轮");
    }
}
```

```python
# Python：不用写类型
name = "YG"
turns = 16
print(f"{name} 有 {turns} 轮")
```

### 1.2 "后端业务"是什么

一个网站分两半：

```
浏览器（前端）  ←→  服务器（后端）  ←→  数据库
   看得见的界面        看不见的逻辑        存数据
```

**后端业务 = 没有界面的那部分逻辑**：校验参数、查数据库、调模型、算分、写报告、发队列。
用户看不到它，但所有"真正发生的事"都在这里。

### 1.3 为什么 interview-guide 用 Java

| 原因 | 具体 |
|---|---|
| 并发 | 它要同时处理很多用户 + 长任务（解析简历、跑评估），Java 虚拟线程扛得住 |
| 生态 | Spring 全家桶把 Web/DB/缓存/队列都封装好了，少写胶水代码 |
| 类型安全 | 大项目多人协作，编译期报错比运行时崩好 |
| 招聘/教学 | Java 后端是国内岗位最多的方向，用它做项目"更好讲" |

### 1.4 我们为什么用 Python

| 原因 | 具体 |
|---|---|
| 形态不同 | 我们是**单机工具**，不是多用户平台，没有高并发压力 |
| 零依赖 | 只用标准库，拿到机器就能跑（不用装 JVM、不用 Gradle） |
| AI 生态 | 调模型、算指标、跑评估，Python 最顺手 |
| 改起来快 | 一个人维护，动态语言省事 |

**我们确实用 Python**：现场统计 `*.py` 95 个文件、`*.js` 1 个（就 `web/app.js`）、**没有 Java**。

> 一句话：**不是 Java 更好，是"多用户平台"和"单机工具"选了不同的工具。**

---

## 2. TypeScript 是什么，怎么"控制浏览器界面"

### 2.1 浏览器只认三样东西

```
HTML  结构（有哪些元素）
CSS   样式（长什么样）
JS    行为（点击后干什么）
```

**浏览器不认 TypeScript**。TypeScript 是 JavaScript 的"加强版"：加了类型，写完要**编译成 JS** 才能跑。

```typescript
// TypeScript
function greet(name: string): string {
  return `你好，${name}`;
}
```

```javascript
// 编译后的 JavaScript（类型被抹掉）
function greet(name) {
  return `你好，${name}`;
}
```

### 2.2 "控制界面"具体是怎么发生的

不是 TS 直接画像素，而是三步：

```
① TS 代码编译成 JS
② 浏览器执行 JS
③ JS 通过 DOM API 改页面（增删元素、改文字）
```

举例（真实机制）：

```javascript
document.getElementById('start').onclick = () => {
  fetch('/api/start', {method:'POST', body: '...'})   // 调后端
    .then(r => r.json())
    .then(data => { document.getElementById('q').textContent = data.question; });  // 改页面文字
};
```

**所以"控制界面"= 用 JS 改 DOM + 用 fetch 和后端说话。**

### 2.3 为什么用它（而不是纯 JS）

| 好处 | 说明 |
|---|---|
| 类型检查 | 拼错字段名，编译时就报错 |
| 自动补全 | 编辑器知道对象有哪些字段 |
| 重构安全 | 改一个接口名，全部引用一起改 |

我们的前端是**手写 JS**（`web/app.js` 约 17KB），没有 TypeScript——因为只有 6 个页面，引入构建链不值得。

---

## 3. YAML 是配置文件，对

它是**给人看的配置格式**，靠缩进表达层级：

```yaml
services:
  interview-agent:
    image: interview-agent:local
    ports:
      - "8770:8770"
    environment:
      OPENAI_MODEL: lobsterai/deepseek-flash
```

同样的意思用 JSON 写：

```json
{"services": {"interview-agent": {"image": "interview-agent:local",
  "ports": ["8770:8770"],
  "environment": {"OPENAI_MODEL": "lobsterai/deepseek-flash"}}}}
```

YAML 少括号、能写注释，所以配置文件爱用它。**它不是编程语言**，不能写逻辑。

我们用 YAML 的地方：`docker-compose.yml`。

---

## 4. Markdown 做提示词，为什么？txt 不行吗

### 4.1 Markdown 只是"纯文本 + 几个符号"

```markdown
# 标题
- 列表项
**加粗**
`代码`
```

它**没有任何魔法**——本质就是 `.txt` 文本，只是约定用 `#` `-` `**` 表示结构，渲染器再把它们显示成标题/列表/加粗。

### 4.2 为什么用它装提示词

| 原因 | 说明 |
|---|---|
| 可读 | 模型和我都能一眼看出层次 |
| 可 diff | 改一个字，git 里就是一行差异，能 review |
| 有结构 | 标题/列表/代码块让"硬性规则"更清楚，模型也吃这套 |
| 不掺代码 | 提示词里全是自然语言，混在 Python 字符串里会被转义符搞乱 |

### 4.3 txt 完全行——我们就是 txt

我们项目的提示词在：

```
prompts/interviewer_v1.txt   0.7 KB
prompts/interviewer_v2.txt   1.2 KB
prompts/interviewer_v3.txt   1.6 KB
```

内容长这样（真实截取）：

```
你是一位严格但公正的 AI 方向技术面试官，正在做「项目深挖」面试。

硬性规则：
1. 一次只问一个问题。禁止把两三个问题用"以及""另外""还有"串在一起。
2. 必须以候选人上一句回答里的具体词或数字为落点。
...
```

**结论**：Markdown 不是必须，`.txt` 一样行。区别只是"有没有 `#`/`-` 这类结构符号、渲染后好不好看"。
真正重要的是你猜到的那件事：**把提示词从代码里拿出来放文件，改的时候不用动代码、能单独 review、能留版本**。

---

## 5. Lua 是什么，Redis 是什么，"原子限流"是什么

### 5.1 Lua：一个很小的嵌入式脚本语言

它只有几十个关键字，常被嵌进别的软件里当"可配置逻辑"（游戏引擎、Nginx、**Redis** 都用它）。

```lua
-- Lua 代码长这样
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
if count > tonumber(ARGV[2]) then
  return 0
end
return 1
```

### 5.2 Redis：一个放在内存里的键值数据库

```
SET user:1:name "YG"      → 存
GET user:1:name           → 取（微秒级，因为在内存）
INCR counter              → 自增
EXPIRE key 60             → 60 秒后自动删
```

它常被用来做：缓存、计数器、队列（Stream）、分布式锁。

### 5.3 "原子限流"是什么

**问题**：要限制"同一个 IP 每分钟最多 5 次登录"。用普通代码写：

```
读 count → 判断 count<5 → count+1 写回
```

两个请求**同时**到达时：都读到 4，都判断"没超"，都写 5 → 实际放进来 6 次。这就是"竞态"。

**原子 = 这串操作不可被打断**。Redis 执行 Lua 脚本时是单线程、整段执行，中间不会有别的命令插进来：

```lua
-- 一条脚本完成：自增 + 首次设过期 + 判断是否超限
local c = redis.call('INCR', KEYS[1])       -- 计数+1（原子）
if c == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return c <= 5 and 1 or 0                     -- 1=放行 0=拒绝
```

`@RateLimit` 注解的作用就是：请求进来 → 自动跑这段脚本 → 返回 0 就拒绝（HTTP 429）。

**我们项目没有 Redis**（现场确认：`redis-server` 不存在）。我们的登录限流还没做（在 P0 清单里），
做的话最简版用进程内字典 + 时间窗就够，单机不需要 Redis。

---

## 6. 数据库：谁在跑、用什么写、存在哪

### 6.1 数据库是"一个常驻的程序 + 一堆磁盘文件"

```
你的程序 ──(网络/本地文件)──> 数据库服务 ──> 磁盘上的数据文件
```

它不是"一个文件"，也**不是"网页上能买的东西"**。它由两部分组成：

1. **服务程序**（常驻进程，监听端口，如 PostgreSQL 的 5432）
2. **数据目录**（磁盘上的一堆文件）

PostgreSQL 是 **C 语言**写的；MySQL 是 C/C++；SQLite 是 C。

### 6.2 三种获取方式

| 方式 | 谁在跑 | 你要做什么 |
|---|---|---|
| 自己装 | 你机器上的进程 | `apt install postgresql` / 装安装包 |
| Docker 起一个 | 容器里的进程 | `docker run postgres:16`（interview-guide 的 compose 就这么干） |
| 用云服务 | 别人机房的机器 | 注册阿里云/腾讯云 RDS，**付钱**，拿到地址+账号 |

**"怎么直接用别人的"**：云厂商给你一个地址（`xxx.rds.aliyuncs.com:5432`）+ 账号密码，你的程序连上去就行——数据在**他们的服务器**上。

**"哪来的服务器"**：租的。机房里的物理机，按配置按月付费（几百到几千元/月）。

### 6.3 interview-guide 的做法

它的 `docker-compose.dev.yml` 里起一个 PostgreSQL 容器，默认账号 `postgres / 123456`，
端口映射到 `localhost:5432`。所以是**作者自己搭的（用 Docker），不是租的云库**。

### 6.4 我们的做法：SQLite，一个文件就是一个数据库

```
data/sessions.db   36 KB   ← 里面有一张表 sessions
data/users.db      12 KB   ← 里面有一张表 users
```

`users` 表的结构（真实输出）：

```sql
CREATE TABLE users (
  user TEXT PRIMARY KEY,
  pw_hash TEXT, ...
)
```

**SQLite 不需要"服务进程"**：没有端口、没有账号，程序直接读写这个文件。
所以我们的项目"拷走文件夹就等于拷走数据"，而 PostgreSQL 要单独导出备份。

---

## 7. Flyway 是什么，"管版本"是什么

### 7.1 问题：表结构会变

第一版 `users` 表只有 `user` 和 `password`；第二版要加 `role`；第三版要加 `last_login`。
如果每个人自己在数据库里手工 `ALTER TABLE`，很快就乱了：**谁改了、改到哪一步、新同事的库少哪些列**全都不知道。

### 7.2 Flyway 的做法：把每次改结构写成一个带编号的 SQL 文件

```
db/migration/
  V1__create_users.sql
  V2__add_role_to_users.sql
  V3__add_last_login.sql
```

启动时 Flyway：

1. 查数据库里的 `flyway_schema_history` 表（记录已执行过哪些版本）
2. 找出**还没执行过**的 V 文件
3. 按编号顺序执行
4. 把执行记录写进历史表

**"管版本"= 像 git 管代码一样管表结构**：有编号、有顺序、有历史、可重复执行（幂等）。

### 7.3 我们怎么做的（没用 Flyway）

`persist.py` 里手写了一段"启动时检查列是否存在，缺了就 ALTER"：

```python
# 旧库没有 user 列 → 补上（等价于一个 V2 迁移）
TABLE_SQL, INDEX_SQL = ..., ...   # 先建表
# 迁移：ALTER TABLE sessions ADD COLUMN user TEXT ...（已存在则跳过）
```

对我们这种单机小项目够用；表一多、多人协作就必须上 Flyway 这类工具。

---

## 8. 一张总表：名词 → 是什么 → 谁用了

| 名词 | 类别 | 一句话 | interview-guide | 我们 |
|---|---|---|---|---|
| **Java** | 编程语言 | 编译型、强类型、企业后端主力 | ✅ 后端全部 | ❌ |
| **Python** | 编程语言 | 解释型、动态类型、AI 生态最强 | ❌ | ✅ 95 个 .py |
| **TypeScript** | 编程语言 | JS 加强版，编译成 JS 才能在浏览器跑 | ✅ 前端 | ❌（手写 JS） |
| **JavaScript** | 编程语言 | 浏览器唯一能直接执行的语言 | ✅（编译产物） | ✅ `web/app.js` |
| **YAML** | 配置格式 | 缩进表达层级，不写逻辑 | ✅ compose/application.yml | ✅ docker-compose.yml |
| **Markdown** | 文本标记 | 纯文本 + `#`/`-` 表示结构 | ✅ skills/prompts | 部分（我们提示词用 .txt） |
| **Lua** | 嵌入式脚本语言 | 小、快、常被 Redis/Nginx 内嵌 | ✅ 限流脚本 | ❌ |
| **Redis** | 内存数据库 | 缓存/计数/队列，微秒级 | ✅ 队列+缓存+限流 | ❌ |
| **PostgreSQL** | 关系数据库 | C 写的服务进程 + 数据目录 | ✅ + pgvector | ❌ |
| **SQLite** | 嵌入式数据库 | 一个文件就是整个库 | ❌ | ✅ 两个 .db 文件 |
| **Flyway** | 迁移工具 | 带编号的 SQL 管表结构版本 | ✅ | ❌（手写 ALTER） |
| **Docker** | 容器平台 | 把环境和依赖打包 | ✅ 全家桶 | ✅ 单服务 |

---

## 9. 三句话记住全部

1. **语言只是工具**：Java 适合"多用户长跑服务"，Python 适合"单机工具 + AI"，TS 只为了在浏览器里跑得安全。
2. **配置/提示词/数据都是文件**：YAML 管配置、MD/TXT 管提示词、SQL 管表结构、DB 文件管数据——把它们从代码里拿出来，是为了**能改、能 diff、能 review**。
3. **Redis/PostgreSQL/Flyway 解决的是"多个请求同时来"和"结构会演进"**：单机不需要它们，平台才需要。
