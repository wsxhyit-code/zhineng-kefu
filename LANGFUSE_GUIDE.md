# Langfuse 链路追踪：原理 · 接入 · 使用（本项目实操版）

> 目标：从"数据怎么流进去"讲到"怎么用它解决实际问题"，每一步都能在本项目里照做、照验。
> 本机实况：`langfuse 4.14.0` · 自部署栈 6 个容器 · `http://localhost:3000`

---

## 第一部分：原理 —— 先搞懂数据怎么流动

### 1.1 一句话：Langfuse 是个"收结构化事件的服务 + 数据库 + 网页"

**它自己不会追踪任何东西。它是个哑的接收端，全靠别人把事件推给它。**

最贴切的类比：

| | 普通日志 | Langfuse |
|---|---|---|
| 谁产生数据 | 你在代码里 `print(...)` | **框架自动发事件**（你不用写 print）|
| 数据长什么样 | 一行文本 | **结构化事件**（谁开始了/结束了、输入输出、token 数）|
| 存哪 | 日志文件 | **ClickHouse**（大字段另存 MinIO）|
| 怎么看 | `tail -f` / Kibana | **网页画成一棵树** |

**⇒ "零侵入"的全部含义 = 不用你自己写那句 `print`。** 剩下的都是工程细节。

### 1.2 五跳（数据从你的代码到网页）

```
① 业务代码执行：await retrieve_knowledge(state)
        │  框架(LangGraph)在节点开始/结束时自动"发事件"
        ▼
② LangChain 回调层：on_chain_start(name="retrieve_knowledge", inputs=…)
        │  Langfuse 的 CallbackHandler 收到事件 → 翻译成 Langfuse 的记录格式
        ▼
③ Langfuse SDK(在你进程里)：塞进【内存队列】
        │  后台线程攒一批，批量 POST → http://localhost:3000/api/public/ingestion
        ▼
④ Langfuse Web(Docker 容器)：收下、鉴权、塞进 Redis 队列
        │  Worker 容器消费队列 → 写 ClickHouse；大字段(prompt/completion)丢 MinIO
        ▼
⑤ 网页(localhost:3000)：查出来，按 parent id 拼成树
```

**第 ③ 步要记住两件事**：
- SDK **不是每次调用都发 HTTP**，而是攒一批再发 ⇒ **界面上有延迟是正常的**
- 发送在后台线程 ⇒ **Langfuse 挂了业务还能跑**（项目还额外加了一层静默降级）

### 1.3 trace / span / generation 是什么 —— **就是数据库里的几行**

| 概念 | 是什么 | 关键字段 |
|---|---|---|
| **trace** | **一次完整交互**（本项目 = 用户发一句话，图跑完一整轮）| `id`、`name`、`session_id`、`user_id`、`metadata`、`tags` |
| **span** | trace 里的一段工作（一个节点、一次工具调用、一次检索）| `id`、**`trace_id`**、**`parent_observation_id`**、`name`、`start_time`、`end_time`、`input`、`output` |
| **generation** | 一种特殊 span —— **一次 LLM 调用** | 上面那些 + `model`、`prompt`、`completion`、`usage`(→算钱) |

**树结构是靠两个 id 拼出来的，不是靠时间戳**：

```
某行 span 的 parent_observation_id == 另一行 span 的 id   ⇒  它就是那行的子节点
```

**⇒ "自动追踪"的真相极其朴素**：框架发出的事件里**已经带了 id 和 parent id**，Langfuse 只是存下来、再按 id 反查拼成树。**没有 id 就没有树。**

### 1.4 本项目里一棵真实的树长这样

```
trace(session_id=会话1, tags=["intent:退款退货"], metadata={"intent":"退款退货","intent_confidence":"0.9"})
│
├─ span  resolve_reference          ← on_chain_start/end 自动产生
├─ span  classify_intent
│   └─ generation  deepseek-v4-flash   ← token 数、耗时、"钱"都在这一层
│        input:  INTENT_CLASSIFY_SYSTEM + 用户这句话
│        output: {"intent":"退款退货","confidence":0.9}
├─ span  fetch_order                 ← 若没订单号会 interrupt，trace 停在这里
├─ span  retrieve_policy
│   └─ generation  …                 ← 扩写 3 条 query 的那次调用
├─ span  main_agent
│   └─ generation  …
└─ span  log
```

**看这棵树能立刻回答**：这一轮花了多少 token/钱（把所有 generation 的 usage 加起来）· 哪一步最慢（每个 span 的 end−start）· 走了哪条路（trace 的 tags）· 出错在哪步（span 的 error 标记）。

### 1.5 为什么"只挂一次"能覆盖全链路

```
LangChain 里所有东西都是 Runnable(包括 LangGraph 的每个节点)
        ↓
每个 Runnable 执行时都读 config 里的 callbacks
        ↓
这个 config 存在一个 ContextVar 里,会【自动向下传播】到子 Runnable
        ↓
异步下子协程自动继承父协程的 ContextVar ⇒ 不用逐层传参
        ↓
结果:最外层挂 1 个 handler ⇒ 12 个节点全都"看得见"它
```

**不是 Langfuse 找到了你的节点，而是框架主动把事件送到了挂 handler 的地方。**

---

## 第二部分：自部署架构（6 个容器各干什么）

容器清单来自 `docker ps` 实测；镜像 / 端口 / healthcheck 逐项取自 `docker-compose.langfuse.yml`：

| 容器 | 镜像 | 在链路里的角色 | 对外端口 | 探活命令（compose 自带）|
|---|---|---|---|---|
| `langfuse-web` | `langfuse/langfuse:3` | **接收入口**（`/api/public/ingestion`）+ 提供网页与查询 API | **3000** | （无 healthcheck，靠端口探）|
| `langfuse-worker` | `langfuse/langfuse-worker:3` | **消费者** —— 从 redis 取事件、批量写库 | `127.0.0.1:3030` | — |
| `redis` | `redis:7` | **写队列** —— web 收下就丢这里，不让写入拖慢响应 | `127.0.0.1:6380` | `redis-cli ping` |
| `clickhouse` | `clickhouse/clickhouse-server` | **存 trace/observation**（列存，专为快速聚合设计 ⇒ 仪表盘秒出）| `127.0.0.1:8123/9000` | `wget --spider localhost:8123/ping` |
| `postgres` | `postgres:17` | **事务数据**：项目、API key、scores（评分/👎）、prompt 管理 | `127.0.0.1:5432` | `pg_isready -U postgres` |
| `minio` | `cgr.dev/chainguard/minio` | **对象存储**：存大的 prompt/completion 原文，让 ClickHouse 只留小行 | `9090`(API) / `127.0.0.1:9191`(控制台) | `mc ready local` |

**⇒ 标准的"trace 收集 + 分析"架构：写入路径（web→redis→worker→clickhouse）与查询路径（web 直接查）分开。**

### 2.1 为什么不用手动在界面上建项目、拿 key —— **headless 预置**

compose 里给 `langfuse-web` 灌了一组 `LANGFUSE_INIT_*` 环境变量，**首次启动时自动把组织/项目/用户/密钥建好**：

```yaml
LANGFUSE_INIT_ORG_ID: mewhelp
LANGFUSE_INIT_PROJECT_ID: mewhelp-观测
LANGFUSE_INIT_PROJECT_PUBLIC_KEY: pk-lf-mewhelp-local     # ← .env 里那两个 key 的来源
LANGFUSE_INIT_PROJECT_SECRET_KEY: sk-lf-mewhelp-local
LANGFUSE_INIT_USER_EMAIL: admin@mewhelp.local
LANGFUSE_INIT_USER_PASSWORD: mewhelp123
```

**⇒ 这就是 `.env` 里 key 能写死的原因** —— key 是**声明式配置**出来的，不是人肉去网页上点出来的。
（*代价：这两个 key 是明文公开的本地默认值，**生产必须换**，compose 里也标了 `# CHANGEME`*）

### 2.2 端口全是"避让"过的（本机撞端口的解法）

本机同时跑着主 compose（Milvus 等）和这个 Langfuse 栈，**端口冲突是真实发生过的**，
所以 compose 里做了显式避让，注释写明了原因：

| 服务 | 容器内 | 宿主 | 为什么 |
|---|---|---|---|
| `redis` | 6379 | **6380** | 原 6379，让位给本机另一项目的 `bff-redis`（**容器间互联仍走 6379，不受影响**）|
| `minio` 控制台 | 9001 | **9191** | 原 9091，让位给主 compose 的 milvus `healthz` |
| `clickhouse` | 8123/9000 | 127.0.0.1 绑定 | 只本机可访问，不对外暴露 |

**⇒ 学到的通用做法**：起第二个栈前先把端口列出来对一遍，**只给需要对外访问的端口做映射**，其余绑 `127.0.0.1`。

---

## 第三部分：从零接入本项目（照做）

### 步骤 1：起 Langfuse 栈

```powershell
make langfuse-up          # 等价于 docker compose -p mewhelp-langfuse -f docker-compose.langfuse.yml up -d
# 输出会直接打印账号与三个环境变量:
#   http://localhost:3000   (admin@mewhelp.local / mewhelp123)
#   LANGFUSE_PUBLIC_KEY=pk-lf-mewhelp-local
#   LANGFUSE_SECRET_KEY=sk-lf-mewhelp-local
#   LANGFUSE_BASE_URL=http://localhost:3000
```

**6 个容器都起来后**（`make langfuse-up` 输出里明说**首次就绪约 2-3 分钟**，clickhouse/postgres 的 healthy 要等）：

```powershell
docker ps --filter "name=langfuse" --format "{{.Names}}  {{.Status}}"
#   Status 里带 "(healthy)" 才算真就绪,不是 "Up" 就算好
```

**⚠️ `-p mewhelp-langfuse` 这个 project 名不能省**（Makefile 注释里写明了原因）：
不加的话这个栈会和**主 compose 落到同一个 `mewhelp` project**，而**两边都有个叫 `minio` 的服务 ⇒ 同名相撞**。

**探活**：栈里只有 redis/clickhouse/postgres/minio 定义了 healthcheck（见第二部分表格），
**`langfuse-web` 本身没有 healthcheck** ⇒ 用容器状态 + 端口作为就绪判据：

```powershell
Test-NetConnection localhost -Port 3000 -InformationLevel Quiet   # True 即网页端口已监听
```
（*Langfuse 另有 `/api/public/health` 这类 HTTP 探活端点；本次没起栈，未实测，故不写进命令清单*）

### 步骤 2：配 `.env`（三个变量）

```ini
LANGFUSE_PUBLIC_KEY=pk-lf-mewhelp-local
LANGFUSE_SECRET_KEY=sk-lf-mewhelp-local
LANGFUSE_BASE_URL=http://localhost:3000
```

**注意本项目的特殊之处**：它用 `pydantic-settings` 读 `.env`，**不依赖 `os.environ`**，
所以代码里是**显式传参**给 Langfuse 客户端，而不是靠 SDK 自己读环境变量：

```python
# app/core/observability.py
_client = Langfuse(
    public_key=settings.langfuse_public_key,
    secret_key=settings.langfuse_secret_key,
    base_url=settings.langfuse_base_url,      # v4 参数名是 base_url(host 已弃用)
)
```

### 步骤 3：图编译时挂一次回调（**唯一的一处**）

```python
# app/graph/runtime.py  init_graph()
_graph = attach_observability(build_graph(checkpointer=checkpointer))
```

```python
# app/core/observability.py
def attach_observability(graph):
    """编译后挂一次 Langfuse 回调,全图自动 trace(README:「编译时挂一次,节点里一行不用动」)。
    未配置时原样返回——不挂、不 import langfuse。"""
    if not langfuse_enabled():
        return graph
    _init_client()
    return graph.with_config({"callbacks": [_make_handler()]})
```

**⚠️ 两个 API 细节**：
- `with_config()` 是**不可变的**（返回新 graph，不就地修改）⇒ 必须 `_graph = attach_observability(...)` 赋值回去，忘了赋值等于没挂
- `langfuse` 的 import 全在**函数内部** ⇒ 没配 Langfuse 的环境不会因为缺包而崩

### 步骤 4：让多轮串成一组（`session_id`）

```python
# runtime.py  run_turn / stream_turn
config = {"configurable": {"thread_id": str(cid)},
          "metadata": {"langfuse_session_id": str(cid)}}
```

⇒ Langfuse 界面上同一会话的多轮会归到**一个 session**，可以整段回放。

### 步骤 5：给业务维度打标（`tag_intent`）—— 唯一需要手写的地方

```python
# app/graph/nodes.py  classify_intent 里调一行
observability.tag_intent(intent, confidence)
```

**为什么必须手动**：框架知道"这里调了一次 LLM"，但**不知道这次调用属于哪个意图**。
而"按意图分成本"恰恰需要它。

### 步骤 6：验证

```powershell
# ① 起服务并聊一句
uv run uvicorn app.main:app --port 8000
# ② 打开 http://localhost:3000 → Traces → 看到那棵树
# ③ 或直接查账(需先聊过)
make cost-report DAYS=7
```

---

## 第四部分：接入代码逐行讲解

### 4.1 开关：三个键齐全才算启用

```python
def langfuse_enabled() -> bool:
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key
                and settings.langfuse_base_url)
```

**⇒ 只要有一个空，就完全不挂、连 import 都不做。** 测试里也用 `monkeypatch` 把 key 清空（`tests/conftest.py` 的 `_no_langfuse`），**测试不依赖 Langfuse 服务在线**。

### 4.2 客户端单例

```python
_client = None
def _init_client():
    global _client
    if _client is None:
        from langfuse import Langfuse
        _client = Langfuse(public_key=…, secret_key=…, base_url=…)
    return _client
```

### 4.3 handler（**这里有那个坑**）

```python
def _make_handler():
    from langfuse.langchain import CallbackHandler

    class _InlineCallbackHandler(CallbackHandler):
        """run_inline=True:让 LangChain 在调用方协程内联执行回调,而不是丢进独立 task。
        没有它,handler 的 OTel context.attach 挂在别的 task 上,节点代码里没有活动
        span 上下文,tag_intent 的 propagate_attributes 写不进当前 trace(实测踩坑)。"""
        run_inline = True

    return _InlineCallbackHandler()
```

**坑的机制（三步）**：

```
① OTel 判断"我现在在哪个 span 里",靠一个 ContextVar
② 而 ContextVar 是【每个 asyncio 任务一份副本】—— 任务 A 设的值,任务 B 看不到
③ LangChain 默认把回调丢进独立 task 跑 ⇒ span 上下文记在【别人的任务】上
   ⇒ 业务协程回来问"当前 span 是哪个?" → 空 ⇒ tag_intent 写入【静默丢弃】
```

**修法**：`run_inline = True` ⇒ 回调在调用方协程内联执行 ⇒ 同一个任务 ⇒ 同一个 ContextVar ⇒ 写得进去。

**⇒ 这个坑的可怕之处**：trace 本身、各节点 span、LLM 的 token/耗时**全部正常**，只有 `tag_intent` 悄悄失效 —— 界面上一切看起来都好。

### 4.4 打标（v4 的写法）

```python
def tag_intent(intent: str, confidence: float) -> None:
    if not langfuse_enabled():
        return
    try:
        from langfuse import get_client, propagate_attributes
        # 不能只 with...pass 写"当前活动 span"——LangGraph 异步调度下它可能已 ended,
        # OTel 对 ended span 的写入静默丢弃(实测踩坑)。改为在传播上下文内新建一个
        # event 观测:新 span 必然存活,trace 级属性(metadata/tags)随它上提到整条 trace。
        with propagate_attributes(
            metadata={"intent": intent, "intent_confidence": f"{confidence:.2f}"},
            tags=[f"intent:{intent}"],
        ):
            get_client().create_event(name="intent-tagged")
    except Exception:
        logger.warning("langfuse tag_intent 失败(已忽略)", exc_info=True)
```

**三个要点**：
1. **v4 的 API 变了**：`update_current_trace` 在 v3 有、v4 已移除 ⇒ 改用 `propagate_attributes` 上下文管理器
2. **不能只 `with …pass`**：异步调度下"当前活动 span"可能已经结束了，OTel 对 ended span 的写入**不报错、直接丢** ⇒ 所以要在上下文里**新建一个 event**（新 span 必然存活），让 trace 级属性随它上提
3. **观测失败不许影响业务**：任何异常都吞掉 + warning

---

## 第五部分：怎么**用** Langfuse 解决实际问题

### 5.1 按"你想知道什么"来查

| 你想知道 | 在哪看 |
|---|---|
| 这一轮走了哪些节点、哪一步最慢 | **Traces → 点开某条 → 树形 span**（每层带耗时）|
| 哪次 LLM 调用烧了多少 token / 多少钱 | 树里的 **generation** 层；界面上配模型单价后可看"钱" |
| 用户这句话走了哪条路 | trace 的 **tags**（`intent:退款退货`）与 **metadata** |
| 某个会话的多轮完整过程 | **Sessions 视图**（靠 `langfuse_session_id`）|
| 哪个意图最烧钱 | 自己按 tag 聚合（`make cost-report`）或界面按 tag 筛选 |
| 出错了是哪一步、错在什么输入 | span 上的 **error 标记** + 该 span 的 `input`/`output` |
| 某次回答**实际发给模型的完整 prompt** | generation 的 `input`（= 拼装后的全部消息）|
| `cache_read` 命中没有 | generation 的 usage 明细（本项目靠它发现了摘要进 system 的问题，见 5.2）|

### 5.2 一个 Langfuse 帮你抓到的真实问题

项目里有一条不变量：**发给模型的消息列表里只许有一条 SystemMessage**。

**为什么**：上游 chat template 会把多个 system 上提合并成一个头部块 ⇒ 可变内容（摘要）进 system 会把**工具 schema 挤到可变内容之后** ⇒ prompt caching 按渲染后的**前缀精确匹配** ⇒ 整段前缀全 miss。

**怎么发现的**：**Langfuse 记的 `cache_read`**。
> 实测：无摘要的会话 `cache_read=2048`；**摘要一进 system 就掉到 0**。

⇒ **没有 Langfuse 的 usage 明细，这个坑根本看不见**（业务功能完全正常，只是白花钱）。

### 5.3 代码侧查询（`scripts/cost_by_intent.py` 的做法）

```python
client = get_langfuse()                       # 未配置时返回 None,脚本打印"无账可查"并退出 1

resp = client.api.legacy.metrics_v1.metrics(query=json.dumps({
    "view": "observations",                   # ← 见下方口径说明
    "metrics": [{"measure": "totalTokens", "aggregation": "sum"}],
    "dimensions": [{"field": "tags"}, {"field": "traceId"}],
    "fromTimestamp": frm, "toTimestamp": to,
}))
```

**实测口径（脚本注释里写清了，值得照抄这种严谨）**：

| 要点 | 说明 |
|---|---|
| **为什么要走 `api.legacy.metrics_v1`** | v4 typed client 的 metrics 需要 **server v4 write mode**；自部署是本机 v3 服务 ⇒ 走 v1 端点 |
| **为什么用 `observations` 视图** | v1 **只开放 observations / scores 视图，traces 视图不可用** |
| **怎么算"请求数"** | 用 `tags + traceId` 分组拉明细，**客户端聚合**：请求数 = 去重 trace 数，tokens = 求和 |
| **口径边界** | **未打 `intent:` tag 的老 trace 不计入** |
| **单价** | 自定义模型名（glm-5.2 等）在 Langfuse **无内置单价** ⇒ 统计以 **token 数**为准；要看钱得在界面配模型单价 |

### 5.4 「API 可用性由自部署版本决定」—— 踩过两次的同一类限制

**这是本项目使用 Langfuse 最容易卡住的地方**：同一个 SDK 里，有的查询端点在这台自部署服务上**直接 404**。

| 你想用 | 调用 | 在这台服务上的结果 |
|---|---|---|
| 观测(v2，带字段选择) | `client.api.observations.get_many(fields=…)` | ❌ **404** `The observations v2 API is only available in a Langfuse v4 write mode` |
| 观测(v1，legacy) | `client.api.legacy.observations_v1.get_many(trace_id=…)` | ✅ 可用，但 **`limit` 上限 100**（传 200 被 400 拒：`Too big: expected number to be <=100`）|
| metrics(v4 typed) | `client.api.metrics…` | ❌ 需 v4 write mode |
| metrics(v1 legacy) | `client.api.legacy.metrics_v1.metrics(query=…)` | ✅ 可用，但**只开放 observations / scores 视图** |

**⇒ 结论：`langfuse` Python SDK 装的是 4.14.0，但服务端跑在 v3 write mode —— 所以"SDK 有这个方法"不等于"这台服务能答"。**
出 404 先别怀疑配置，先看错误里有没有 `v4 write mode` 字样，然后去 `client.api.legacy.*` 找同名的 v1 端点。

### 5.5 一次真实往返的验证结果（可当基线对照）

2026-09-28 实测，一句话「七天无理由退货怎么操作?」走完一整轮：

```
trace  name=LangGraph   session=12   tags=['intent:商品咨询']
       metadata={intent: 商品咨询, intent_confidence: 0.9, thread_id: 12, …}

├─ CHAIN       LangGraph                  24.16s
  ├─ CHAIN       resolve_reference           3.11s
  │   └─ GENERATION  …ChatOpenAI  1.36s  tokens[input=352 output=7 total=359]
  ├─ CHAIN       classify_intent             2.59s
  │   ├─ GENERATION  …ChatOpenAI  2.56s  tokens[input=892 output=62 total=954]
  │   └─ EVENT       intent-tagged               ← tag_intent 建的 event,看得见
  ├─ CHAIN       route_by_intent             0.00s
  ├─ CHAIN       retrieve_knowledge          5.58s
  │   └─ 2 次 GENERATION(query 扩写 / 自检)  合计 1541 tokens
  ├─ CHAIN       confidence_check            0.00s   ← 空节点:只留形状,不花模型调用
  ├─ AGENT       main_agent                 12.84s  tokens[input=2285 output=249 total=2534]
  │   └─ CHAIN       should_continue         0.00s
  └─ CHAIN       log                         0.02s

共 33 个观测, 该轮 total=5388 tokens, cache_read=0
```

**三个读法**：
1. **`route_by_intent` / `confidence_check` / `should_continue` 耗时 0.00s** —— 它们是**条件边/空节点**，纯逻辑不调模型；**这一眼就看出"哪些节点是免费的"**
2. **`main_agent` 12.84s 占了整轮 24.16s 的一半** —— 要优化延迟先看它
3. **首轮 `cache_read=0` 很正常** —— 前缀还没被写过；**要看缓存命中得聊第二轮**（或让 ReAct 多走几步）

**⇒ 这一棵树的形状本身就是证据**：12 个节点该出现的都出现了，`intent-tagged` 也在 —— 说明"框架自动报告 + 手写打标"两条路都通。

**另外一个工程纪律**：脚本把结果落 `cost_by_intent.{txt,json}`，**页面只读这份 json、不另算一份**
（*"同一个数不许在前端再算一遍"*）。

---

## 第六部分：学习路径（由浅入深，每步都能在本项目验证）

| # | 练什么 | 学到什么 | 怎么验 |
|---|---|---|---|
| 1 | 起栈 + 聊一句 + 打开 trace 树 | **trace / span / generation 三层的区别** | 看到 12 个 span、每个带耗时 |
| 2 | 找到某个 generation，看它的 `input` | **"实际发给模型的 prompt"长什么样** | 能看到 `AGENT_SYSTEM` + 滑窗 + 材料 |
| 3 | 看 tags 与 metadata | **业务维度怎么进观测** | `intent:退款退货` |
| 4 | 切到 Sessions 视图 | **多轮怎么串成一组** | 同一会话的 3 轮在一起 |
| 5 | 在界面配模型单价 | **token → 钱** | 数字从 token 变成金额 |
| 6 | `make cost-report DAYS=7` | **用 API 程序化聚合** | 输出"哪个意图最烧钱" |
| 7 | 故意把 `run_inline` 删掉再聊一句 | **亲手复现"附加能力静默失效"** | trace 有、但 metadata 里没有 intent |
| 8 | 对比改动 prompt 前后两次 trace 的 `input` | **prompt 改动的实际影响** | 逐字 diff |

**第 7 步是这份指南里最值钱的练习** —— 它让你亲身体会：**主线全绿 ≠ 附加能力都在工作。**

---

## 第七部分：排查清单（接不上/看不全时按这个查）

| 症状 | 依次检查 |
|---|---|
| **界面上一条 trace 都没有** | ① `.env` 三个变量是否齐全（`langfuse_enabled()` 为 True？）② 图编译时是否调了 `attach_observability`（**`with_config` 不可变，忘了赋值等于没挂**）③ Docker 栈是否在跑且容器 `(healthy)` ④ **等几秒——批量异步上报有延迟** |
| **trace 有，但缺业务维度**（看不到 intent） | ① `tag_intent` 是否被调用 ② **`run_inline = True` 是否还在**（这是最可能的）|
| **多轮没串起来** | invoke 的 `config["metadata"]` 里有没有 `langfuse_session_id` |
| **界面上看到的时间比实际晚** | SDK 批量上报的固有延迟，不是 bug |
| **观测拖慢了业务** | 回调在热路径上同步跑（尤其 `run_inline=True` 之后）⇒ 生产上要"采样 + 异步批量 + 观测侧限流"，**不能无脑全量同步上报** |
| **某条 trace 只有半个树** | 那次请求 interrupt 了（图停在 `log` 之前），树自然不完整 —— 这是**设计如此**，不是 tracing 坏了 |
| **查询报 404 且带 `v4 write mode` 字样** | 不是你没配对，是**自部署服务端版本限制** ⇒ 换 `client.api.legacy.*` 下的 v1 端点（见 5.4）|

---

## 附：一页速查

```powershell
# 起停(-p 不能省:不加会和主 compose 撞 minio 服务名)
make langfuse-up / make langfuse-down

# 就绪判据:容器带 (healthy) 才算好,web 无 healthcheck,看 3000 端口
docker ps --filter "name=langfuse" --format "{{.Names}}  {{.Status}}"
Test-NetConnection localhost -Port 3000 -InformationLevel Quiet

# 按意图查账(需先聊过)
make cost-report DAYS=7

# 从 API 侧读回 trace 树(界面之外的第二条独立证据)
.venv\Scripts\python.exe .verify_trace.py
```

```ini
# .env 三变量(缺一即完全不挂)
LANGFUSE_PUBLIC_KEY=pk-lf-mewhelp-local
LANGFUSE_SECRET_KEY=sk-lf-mewhelp-local
LANGFUSE_BASE_URL=http://localhost:3000
```

```python
# 接入就三处
① 图编译时:  _graph = attach_observability(build_graph(checkpointer=...))
② 会话分组:  config={"metadata": {"langfuse_session_id": str(cid)}, ...}
③ 业务打标:  observability.tag_intent(intent, confidence)     # 唯一需要手写的
```

---

## 一句话总结

> **Langfuse = 一个"收结构化事件的服务 + 数据库 + 网页"，它自己不追踪任何东西。**
> **LangChain/LangGraph 是那个到处发事件的人；Langfuse 的 `CallbackHandler` 是翻译官；SDK 攒批 POST 到 `/api/public/ingestion`；6 个容器负责收、排队、落库；网页按 `parent_id` 把行拼成树。**
>
> **"只挂一次"能覆盖全链路，是因为 config 里的 callbacks 会通过 `ContextVar` 自动向下传播 —— 而 LangGraph 的每个节点本身就是 Runnable。**
>
> **接入三处：编译时挂回调、invoke 时传 session_id、业务维度调 `tag_intent`。**
> **要避开三个坑：`run_inline=True`（否则业务打标静默失效）、摘要别进 system（否则 `cache_read` 掉到 0，靠 Langfuse 的 usage 才看得见）、ended span 的写入会被静默丢弃（要新建 event）。**
