# mem0-curated：多 Agent 共享经验记忆系统

面向技术类 Worker Agent 集群的**受控共享经验记忆底座**。沉淀编码、排障、RCA 故障复盘、方案选型、踩坑记录、SOP、配置约束，解决多 Agent 之间经验孤岛、记忆污染、旧方案冲突误导 Agent 的问题。

部署形态：Docker Compose 一键部署，数据目录本地挂载，拷贝目录即可跨机器迁移，不依赖任何云端服务。

核心原则：**读写权限强隔离。Worker 只读，只有 Curator 能写。**

## 1. 角色

### Worker Agent（可多个：编码 / 排障 / 架构评审）

- ✅ 只读检索共享经验，辅助决策
- ❌ 不新增、不修改、不删除、不更新任何共享记忆；不做经验提炼、不直接入库
- 行为规则：开工先检索 → 命中后严格校验 `applicable_scope` / `exclude_scope`，环境不匹配不得照搬 → 任务结束输出完整原始轨迹（需求、尝试、报错、方案、结果）交 Curator
- 提示词：[`prompts/worker_agent.md`](prompts/worker_agent.md)

### Curator Agent（唯一写入方）

- ✅ 读取已有记忆、新增记忆、标记旧记忆废弃、建立版本替代关系
- ❌ 不编造日志里没有的信息；不沉淀一次性、无复用价值的内容
- 只保留具备**跨任务复用价值**的内容：可行方案、故障根因、踩坑失败案例、配置约束、版本限制、方案取舍、SOP
- 提示词：[`prompts/curator_agent.md`](prompts/curator_agent.md)

失败经验与成功经验同等重要：必须记录试了什么、为什么失败、后续规避什么。

## 2. 记忆条目 Schema

`memory_type` 固定为 `experience`。字段定义见 [`app/models.py`](app/models.py)，服务端强校验。

| 字段 | 说明 |
|---|---|
| `summary` | 一句话精简摘要，向量检索主要命中点 |
| `task_goal` | 原始任务目标 |
| `constraints` | 技术栈、环境、版本、权限限制 |
| `successful_solution` | `{steps, verification}`；纯踩坑记录可为空 steps |
| `failed_attempts` | `[{try_content, root_cause, lesson}]` |
| `applicable_scope` | **必填非空**，适用场景/环境/版本/前置条件 |
| `exclude_scope` | **必填非空**，明确什么时候不能套用 |
| `tags` | **至少一个**，自动小写去重，过滤检索主键 |
| `supersedes` | 被本条替代的旧记忆 ID，无则空串 |
| `source_agent` | 提交日志的 Worker 名称 |
| `confidence` | 0–1，稳定方案 0.8–0.95，试探方案 0.5–0.7 |

`applicable_scope` / `exclude_scope` 强制非空，是防止旧经验在新环境误导 Agent 的唯一屏障；`tags` 强制非空，因为标签是过滤检索的入口。

无复用价值的日志，Curator 直接输出 `{"skip": true, "reason": "..."}`，不写入。

## 3. 冲突与版本治理

1. Curator 入库前先 `POST /v1/experiences/dedup-check` 查等价条目，再 `POST /v1/experiences/search` 查同主题旧条目。
2. 新结论**推翻**旧方案：新条目 `supersedes` 填旧 ID。服务端在同一次调用里把旧条目标记 `deprecated=true` 并链 `superseded_by`；若废弃失败，新条目会被自动删除，不会留下"两条互相矛盾且都生效"的状态。
3. 只是**补充/扩展**：`supersedes` 留空，作为独立条目新增。
4. **完全等价**：跳过，不重复入库。

检索默认排除 `deprecated`，所以"最新版本优先"是过滤器的结果，不依赖排序技巧。

## 4. 架构

```
用户任务
  ↓
Worker Agent ──X-API-Key(worker)──▶ gateway ──▶ mem0-curated ──▶ qdrant
  │  只读                                  │       │
  │  输出原始轨迹                           │       └─ history.db（add/update 审计链）
  ↓                                        │
Curator Agent ─X-API-Key(curator)──────────┘
   降噪 → 提炼 Schema JSON → dedup/冲突检查 → 写入
```

三个容器：

| 服务 | 作用 | 端口 |
|---|---|---|
| `qdrant` | 向量库，数据挂 `./data/qdrant` | 仅内网 |
| `mem0-curated` | FastAPI + mem0 `Memory`，`add(infer=False)` 原样存储 | 仅内网，**不映射宿主机** |
| `gateway` | 角色鉴权 + method/path allowlist + 反向代理 | `8787` |

`mem0-curated` 不 publish 端口，Worker 在网络上到不了后端；gateway 再叠加 allowlist；后端另有 `X-Internal-Token` 校验。三层独立，任一层配置失误都不会让 Worker 写进去。

单镜像双进程：同一个 Dockerfile，两个 service 用不同 `command` 起 `curated.app.main` 与 `curated.gateway.main`。

## 5. 部署

```bash
cd curated
cp .env.example .env          # 填入随机 key：openssl rand -base64 32
docker compose up -d --build
curl -s localhost:8787/health
```

首次启动会拉取 embedder 模型到 `./data/fastembed`（默认 `BAAI/bge-small-en-v1.5`，384 维），需要一次网络。之后完全离线。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKER_API_KEYS` / `CURATOR_API_KEYS` | — | 逗号分隔。同一个 key 误填进两个列表时按 **Worker** 处理（降级而非提权） |
| `INTERNAL_TOKEN` | — | gateway ↔ 后端共享密钥，不可下发给 Worker |
| `QDRANT_COLLECTION` | `experiences` | 集合名 |
| `EMBEDDER_PROVIDER` | `fastembed` | 可切 `openai`（需 `OPENAI_API_KEY`） |
| `EMBEDDER_MODEL` / `EMBEDDER_DIMS` | `BAAI/bge-small-en-v1.5` / `384` | **两者必须匹配**，启动时会做维度探针校验，不匹配立即报错 |

> `EMBEDDER_DIMS` 必须手填：mem0 不会把 embedder 输出维度同步到 vector store 配置，漏填会导致建出错误维度的集合。

### 迁移到另一台机器

```bash
docker compose down
# 整个 curated/ 目录（含 data/）拷走；目标机放回原位
docker compose up -d
```

`data/` 里是三项全部状态：`qdrant/`（向量+payload）、`history/`（审计链）、`fastembed/`（模型缓存）。

## 6. 接口

全部经 gateway（`http://<host>:8787`），请求头 `X-API-Key`。

| 方法 | 路径 | Worker | Curator |
|---|---|---|---|
| POST | `/v1/experiences` | 403 | ✅ 写入 |
| POST | `/v1/experiences/search` | ✅ | ✅ |
| POST | `/v1/experiences/dedup-check` | ✅ | ✅ |
| GET | `/v1/experiences/{id}?project_id=` | ✅ | ✅ |
| POST | `/v1/experiences/{id}/deprecate` | 403 | ✅ |
| POST | `/mem0/search` | ✅ | ✅ |
| GET | `/mem0/memories?project_id=` | ✅ | ✅ |
| GET | `/health` | 免鉴权 | 免鉴权 |

没有任何 `PUT` / `DELETE` 路由：治理只靠"新增 + 标记废弃"，删除会毁掉替代链。

`project_id` 映射到 mem0 的 `user_id` 作用域，天然获得 Qdrant 的 keyword 索引；跨项目读写一律隔离，`supersedes` 指向别的项目会被拒。

检索排序：`score × (0.5 + 0.5 × confidence)`，`created_at` 做 tie-break。置信度低的经验降权但不被埋掉。

## 7. 示例与测试

```bash
python curated/examples/python_client.py    # 需 httpx，读 GATEWAY_URL/CURATOR_KEY/WORKER_KEY
npx tsx curated/examples/ts_client.ts       # 纯 fetch，无依赖

pytest tests/curated -q
ruff check curated tests/curated --line-length 120
```

测试用真实 `Memory` + 本地 Qdrant + 打桩 embedder，覆盖 Schema 校验、写入检索、`supersedes` 废弃链路、跨项目拒绝、网关 allowlist。

## 8. 排障

| 现象 | 原因 |
|---|---|
| 启动即报 `embedding dims mismatch` | `EMBEDDER_DIMS` 与 `EMBEDDER_MODEL` 不匹配 |
| 首次启动很慢 | 正在下载 embedder 模型到 `data/fastembed` |
| Worker 收到 403 | 正常。写接口对 Worker 永久关闭 |
| 收到 401 `Invalid internal token` | gateway 与后端 `INTERNAL_TOKEN` 不一致 |
| 检索结果里出现已废弃条目 | 说明有绕过本服务直接写库的路径，或 `deprecated` 字段未写入（检查是否有人直接调 mem0 写接口） |
| 搜不到刚写的条目 | `threshold` 太高，或 `tags` 过滤过窄 |

## 9. 已知限制

- 不接 `server/`（终端用户体系，角色模型与本系统的 worker/curator 冲突），`curated/` 完全独立。
- 不提供 `DELETE`/`PUT`：删除会破坏 `supersedes` 链。
- 只支持单一 embedder 维度；更换模型需重建集合。
- 未接入 graph memory 与 reranker。
