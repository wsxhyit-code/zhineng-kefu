# 智能电商客服助手

一个能查订单物流、答政策 FAQ、走退款子流程、从对话里挖知识补库，
还能微调一个主题分类器的完整电商智能客服系统。

功能逐块叠加、**不分支**：从一个纯对话接口起步，到收尾时是下面这整套东西。
每一块都留了 spec 和 plan（`docs/superpowers/`），这套先写 spec 再拆 plan 的流程可以当范本看。

## 能做什么

- 流式对话 + 结构化信息提取
- 五个业务工具，单轮 Function Calling
- 知识库：切块、嵌入、MySQL 与 Milvus 双写、从对话里挖问答对
- 混合检索、RRF 融合、重排、Query 改写、四策略评估
- LangGraph workflow 骨架 + 主力 Agent 的 ReAct 环
- 意图分流、指代消解、退款子流程的 interrupt / resume
- 上下文管理：滑窗、摘要、前缀缓存
- 工具系统：MCP 动态发现、统一执行引擎、审计日志
- Langfuse 自部署观测、数据飞轮、按意图的成本账
- 主题分类器：造语料、微调、阈值扫描、ONNX 推理服务

## 技术栈

FastAPI + LangGraph / LangChain + SQLAlchemy / MySQL + Milvus。

聊天、嵌入、重排三组上游**各自直连，中间没有网关**那一层。模型名和地址全部在 `.env` 里配
（`CHAT_*` / `EMBED_*` / `RERANK_*` 三组），换供应商、换模型不用改一行代码。
依赖用 [uv](https://docs.astral.sh/uv/) 管，测试用 pytest（443 项，`make test` 全跑一遍不打真实模型）。

## 目录结构

| 位置 | 装的是什么 |
| - | - |
| `app/api/` | HTTP 入口。聊天、Agent、知识库录入、复核、验收页、成本看板 |
| `app/graph/` | LangGraph 那张图。`state` 状态、`nodes` 节点、`routing` 分流规则、`build` 组装 |
| `app/core/` | 单点能力。上游客户端、检索、重排、意图、指代、摘要、置信度、飞轮、可观测 |
| `app/kb/` | 知识怎么进库。切块、嵌入、双写 MySQL 与 Milvus、去重、从对话里挖问答对 |
| `app/tools/` | 工具系统。内置 `@tool`、MCP 客户端、注册表、统一执行引擎 |
| `app/db/` | 表模型与仓储 |
| `app/static/` | 前端页面。聊天、知识库录入、飞轮待审、观测与成本、主题分布、分类器验收 |
| `mcp_servers/` | 两台业务 MCP Server，物流和售后各一台，独立进程 |
| `sql/` | 建表与迁移，容器首启按文件名顺序自动执行 |
| `scripts/` | 建库、评估、飞轮这些离线脚本 |
| `scripts/classifier/` | 主题分类器的语料、训练、导出、量化、推理服务 |
| `data/kb/` | 知识库源文档（markdown），`make kb-build` 的输入 |
| `tests/` | 单元测试，不依赖真实模型与外部服务 |
| `docs/superpowers/` | 每个模块的 spec 和 plan |

## 快速开始

### 前置条件

| 项 | 怎么装 |
| - | - |
| Python 3.12+ | uv 会自己下，不用管 |
| [uv](https://docs.astral.sh/uv/) | macOS `brew install uv`；Linux `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker | Docker Desktop 或 Colima，要能跑 `docker compose` |
| make | macOS / Linux 自带；Windows 用 WSL 或 Git Bash |
| 三个模型密钥 | 见第 2 步 |

内存 8G 起，磁盘 10G 起（要练分类器模型的话再多留 5G）。

### 1. 装依赖

```bash
uv sync
```

要跑几分钟，中间长时间没输出是正常的。

### 2. 配置 .env

```bash
cp .env.example .env
```

然后填这三个密钥位：

```bash
CHAT_API_KEY=sk-xxx
EMBED_API_KEY=sk-xxx
RERANK_API_KEY=sk-xxx
```

`.env.example` 里默认给的是[硅基流动](https://siliconflow.cn)的地址，注册一个账号拿一个 key
填进三个位置就能把整套跑起来。`.env` 已在 `.gitignore` 里，不要提交。

> **`CHAT_THINKING=disabled` 别删。** 思考链开着的话一句客服回复要 22 秒、token 多花 4 倍
> （实测 22.4s/318 → 1.4s/16），流式体验和多步 Agent 都没法看。
> 能不能关取决于模型：硅基流动的 DeepSeek 和 MiniMax-M3 能关，MiniMax-M2.x 关不掉
> （官方文档明确说 M2.x 传了会被静默忽略），那时改用 `CHAT_REASONING_SPLIT=true`
> 把思考拆进独立字段，否则结构化输出会解析失败。

### 3. 起依赖容器

```bash
docker compose up -d
```

拉起 MySQL、Milvus、MinIO、etcd 四个容器。建表不用管 —— `sql/` 已经挂进 MySQL 的
初始化目录，首次启动会自动按文件名顺序执行。

> **头半分钟 MinIO 显示 starting 是正常的**，Docker 每 30 秒才做一次健康检查。
> 最终四个容器都应该是 healthy；一直 unhealthy 才是真出问题。

### 4. 灌测试数据

```bash
make seed        # FAQ（query_faq 的数据源）
make seed-conv   # 历史会话（挖知识用）
```

订单和物流不在这里 —— 它们是 `app/tools/business.py` 里按 `user_id` 稳定生成的模拟数据，
不落库，任何账号名下都有 1001 和 2002 两笔演示单。**数据库里没有 orders 表是正常的。**

### 5. 建知识库

```bash
make kb-build       # 切 chunk 落 MySQL
make kb-vectorize   # 向量化写进 Milvus，这步会调用上游嵌入接口
```

这一步失败通常是密钥或网络问题，不是代码问题。

### 6. 启动

```bash
make dev
```

`make dev` 会依次：拉起依赖容器 → 等 Milvus 健康检查通过 → 拉起两台 MCP Server
（:8101 / :8102）→ 启动应用（:8000）。**它是前台常驻进程**，要等几十秒。
停了用 `make dev-down`，或者 Ctrl+C。

浏览器打开 <http://localhost:8000> 就是聊天页。

### 7. 验收

```bash
printf '{"user_id":"u1","message":"订单 1001 的物流到哪了"}' > /tmp/q.json
curl -s --max-time 90 http://localhost:8000/api/agent \
     -H 'Content-Type: application/json' --data-binary @/tmp/q.json
```

返回的 JSON 里 `tool_calls` 应该同时包含 `query_order` 和 `query_logistics`。
**只出 `answer` 不出 `tool_calls`，不算通过** —— 通常是 `CHAT_MODEL` 写的名字上游不认
（没有网关就没有别名这一层，写错会被上游拒），回到第 2 步核对。

其余的验收命令（检索评估、意图闸门、飞轮、分类器训练等）用 `make help` 看完整清单，
每条都带说明。

## 端口与页面

| 端口 | 是什么 |
| - | - |
| 8000 | 应用 |
| 8101 / 8102 | 业务 MCP Server，物流 / 售后 |
| 8110 | 主题分类器推理服务（`make classifier-up` 之后） |
| 3000 | Langfuse（`make langfuse-up` 之后） |
| 19530 | Milvus |
| 9001 | MinIO 控制台 |

应用里的页面：

| 路径 | 页面 |
| - | - |
| `/` | 聊天 |
| `/admin` | 后台首页，各模块入口收到一处 |
| `/kb` | 知识库录入：贴文档就能切块入库、向量化、当场检索自测 |
| `/rag-eval` | 四策略对照报告，页上看也页上重跑 |
| `/review` | 飞轮待审队列 |
| `/observability` | 意图成本账、评估趋势、置信度阈值校准 |
| `/topics` · `/topics/questions` | 主题分布、某一类下的全部问题 |
| `/acceptance` 及 `/acceptance/{eval,data,errors}` | 九项实证闸门卡、评测详情、数据产物、错例复核 |

## 实测结果

以下数字都有出处，报告在 `data/*/reports/` 下。**注意区分「本机实测」和「随附报告」** ——
后者是跑通一次留下的产物，本轮没有重跑，[EXPERIMENTS.md](EXPERIMENTS.md) §7 把这类局限单列了一节。

**本机实测（可重跑）**

| 实验 | 结果 | 脚本 |
| - | - | - |
| 长对话关键信息保留率 | 纯滑窗基线 **0%（0/60）** → 摘要+锚点滑窗 **100%（60/60）**，与「全量历史」上限臂持平，而上下文体积少 **35%**（1393 字 vs 2150 字） | `.exp_context.py` |
| 置信度信号消融 | 发现一个**与目标负相关**的信号（AUC **0.3083**，比随机还差）；直接移除后 OOF J 0.8833 → **0.8889**，阈值 0.26 → 0.08，**“零误放” 从不可达变为 t=0.20 可达** | `.exp_confidence.py` |
| 阈值稳定性 | 5 折交叉验证**全部选中 0.17**（乐观偏差 ≈ 0）；Bootstrap(B=1000) 95% 区间 **[0.14, 0.29]** | `.exp_confidence.py` |
| 意图识别（188 条九类集） | 无歧义子集 **94.7%（161/170）**，两次独立运行**完全一致**；macro-F1 0.9255 / 0.9306；重放一致性 **100%** | `.exp_intent.py` |
| 去污染对照（**负结果**） | 与 few-shot 逐字重合的 6 条 vs 干净 12 条，差值 **+0.0%** —— 原假设「重合组会虚高」被自己的数据否定 | `.exp_intent.py` |
| 模型压缩（进阶扩展） | 12 层教师 → 6 层学生，再 int8 量化：**390MB → 105MB（-73%）**，CPU 单条延迟 **145ms → 52ms（2.8x）**，micro-F1 仅降 **0.0067**，落在噪声带 ±0.048 内 | [DISTILL.md](DISTILL.md) |

**随附报告（本轮未重跑）**

| 指标 | 结果 | 报告 |
| - | - | - |
| 混合检索 | Recall@5 **0.98** / MRR **0.89** / 答案覆盖度 **0.97** | `data/rag/reports/rag_eval.json` |
| 主题分类（161 条分层留出集） | micro-F1 **0.964** / macro-F1 **0.962**，13 个设线类目全达标 —— ⚠️ **本轮不可复现**，原因见 EXPERIMENTS.md §1.5 | `data/classifier/reports/eval_report.md` |

## 常见问题

**镜像拉不下来（国内常见）**
Docker Hub 和 quay.io 经常连不上。先确认镜像站活着 —— `curl -sI https://docker.m.daocloud.io/v2/`
**返回 401 是正常的**，那是 registry 的鉴权挑战，不是失败。然后走镜像站拉、再打回原名：

```bash
docker pull docker.m.daocloud.io/library/mysql:8
docker tag  docker.m.daocloud.io/library/mysql:8  mysql:8
docker pull docker.m.daocloud.io/milvusdb/milvus:v2.6.22
docker tag  docker.m.daocloud.io/milvusdb/milvus:v2.6.22  milvusdb/milvus:v2.6.22
```

打回原名之后 `docker-compose.yml` 一个字都不用改。mysql 镜像有 1.1GB，**给足时间**。

**3306 端口被占**
本机如果已经有别的 MySQL 容器占着 3306，`docker compose up` 会失败。停掉那个容器，或者改端口。

**中文 Windows 上读文件报 `UnicodeDecodeError`**
中文 Windows 默认 GBK，读仓库里带中文的 markdown / jsonl 会炸。`Makefile` 和
`scripts/dev.sh` 里都设了 `PYTHONUTF8=1`，所以走 `make` 的路径不会有问题；
自己直接跑 Python 的话记得也设上。

**`make dev` 起来了但知识类问答全哑**
先看 Milvus 是不是真就绪了（`curl http://localhost:9091/healthz`）。
`make dev` 会等它，但如果你手动起的服务，可能撞上 Milvus 集合还没 load 完的竞态。

**Windows 上 `.sh` 脚本报 `env: bash^M: No such file or directory`**
行尾被编辑器改成 CRLF 了。`.gitattributes` 已经把 `*.sh` / `Makefile` / `*.py` 等钉死成 LF，
重新 checkout 一次即可。

**分类器跑不起来，说找不到模型**
模型权重（约 2GB）不在仓库里，见下一节。

## 仓库里没有什么

| 缺的东西 | 为什么 | 怎么补 |
| - | - | - |
| `data/classifier/` 下的模型权重 | 基础模型 + 微调产物 + ONNX + 教师缓存约 **2GB**，不进 Git | 见 [REPRO.md](REPRO.md)，或直接跑 `make classifier-train` |
| `.env` | 含真实密钥，已在 `.gitignore` | `cp .env.example .env` 再填 |
| `log/`、`.exp_cache/`、`.hf-cache/` | 运行时产物与可重跑的检索缓存 | 自动生成 |

`data/classifier/reports/` 里的评测报告**保留了**，那是当证据看的。

## 许可

仓库未附开源许可证，默认保留所有权利；转载或二次分发请先联系仓库作者。