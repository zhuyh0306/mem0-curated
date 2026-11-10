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

运维看板 127.0.0.1:8788 ─X-API-Key(curator)─▶ gateway   （key 只在服务端）
```

四个容器：

| 服务 | 作用 | 端口 |
|---|---|---|
| `qdrant` | 向量库，数据挂 `./data/qdrant` | 仅内网 |
| `mem0-curated` | FastAPI + mem0 `Memory`，`add(infer=False)` 原样存储 | 仅内网，**不映射宿主机** |
| `gateway` | 角色鉴权 + method/path allowlist + 反向代理 | `8787` |
| `dashboard` | 运维看板，持有 curator key 并逐次调用 gateway | `127.0.0.1:8788` |

`mem0-curated` 不 publish 端口，Worker 在网络上到不了后端；gateway 再叠加 allowlist；后端另有 `X-Internal-Token` 校验。三层独立，任一层配置失误都不会让 Worker 写进去。

`dashboard` 是第四个进程，同样复用这一个镜像。它把 curator key 留在进程里，浏览器只和它通信，所以页面源码、localStorage、网络面板里都不会出现密钥；写操作额外校验 `Origin`，拒绝跨站请求。端口只绑回环地址，局域网访问不到。

单镜像双进程：同一个 Dockerfile，两个 service 用不同 `command` 起 `curated.app.main` 与 `curated.gateway.main`。

## 5. 部署

一个命令，从零到冒烟通过：

```bash
cd curated
./deploy.sh          # 生成 .env → build → up -d → 等健康 → 冒烟测试
```

分步子命令：`./deploy.sh env | up | pull | smoke | down | save FILE | load FILE`。

`env` 不需要 Docker，只需要 `openssl` 与 `curl`：它从 `.env.example` 生成 `.env`（权限 `600`，三个随机密钥），核对无残留 `replace_me` 后建好 `data/` 三个子目录。**密钥只落 `.env`，脚本从不打印它们。**

`smoke` 是冒烟测试，用 `.env` 里的真实密钥逐条断言权限边界：无 key 探活 200、错 key 401、Worker 写入 403、Worker 检索 200、Curator 写入拿到 id、随即用 deprecate 退役这条自测记录（设计上没有 DELETE，不退役就会污染真实检索），最后验一个无关 project 读到自己的空作用域。跑两遍都应通过。

裸命令等价路径：

```bash
cp .env.example .env && $EDITOR .env      # 三个密钥都换成随机值
docker compose up -d --build
curl -s localhost:8787/health
```

首次启动会拉取 embedder 模型到 `./data/fastembed`（默认 `BAAI/bge-small-en-v1.5`，384 维），需要一次网络。之后完全离线。访问 HuggingFace 受限时在 `.env` 里加一行镜像即可：`HF_ENDPOINT=https://hf-mirror.com`（`env_file` 会把它带进容器）。

模型下载失败**不会让服务起不来**：mem0 会退回纯稠密向量检索（BM25 关键词通道关闭），代价是每次模型解析要走约 40 秒的下载重试退避。日志里看到 `Failed to load BM25 encoder` 属正常降级，不是崩溃。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKER_API_KEYS` / `CURATOR_API_KEYS` | — | 逗号分隔。同一个 key 误填进两个列表时按 **Worker** 处理（降级而非提权） |
| `INTERNAL_TOKEN` | — | gateway ↔ 后端共享密钥，不可下发给 Worker。缺失时后端一律 401（fail-closed） |
| `HF_ENDPOINT` | — | 直连 huggingface.co 受限时设 `https://hf-mirror.com` |
| `QDRANT_COLLECTION` | `experiences` | 集合名 |
| `EMBEDDER_PROVIDER` | `fastembed` | 可切 `openai`（需 `OPENAI_API_KEY`） |
| `EMBEDDER_MODEL` / `EMBEDDER_DIMS` | `BAAI/bge-small-en-v1.5` / `384` | **两者必须匹配**，启动时会做维度探针校验，不匹配立即报错 |

> `EMBEDDER_DIMS` 必须手填：mem0 不会把 embedder 输出维度同步到 vector store 配置，漏填会导致建出错误维度的集合。

> 后端容器**没有 `ports`**，只有 `expose: 8000`，宿主上唯一入口是 gateway 的 `8787`。直连后端会因缺少 `X-Internal-Token` 被拒。

### 迁移到另一台机器

镜像的 build context 是**仓库根**（Dockerfile 要 `mem0/` 和 `pyproject.toml`），所以「只拷 `curated/` 目录」在目标机上构建不出来。两条可行路径：

**A. 离线机 —— 传镜像，不构建（推荐）**

```bash
# 源机：两个镜像一起打包，目标机同样拉不到 qdrant 官方镜像
./deploy.sh save /tmp/curated-images.tar
# 拷 /tmp/curated-images.tar + 整个 curated/ 目录（含 data/ 与 .env）
# 目标机
cd curated
./deploy.sh load /tmp/curated-images.tar
./deploy.sh up        # 检测到镜像已存在会跳过 build
./deploy.sh smoke
```

**B. 目标机有网络**：拷**整个仓库**（至少带 `mem0/`、`pyproject.toml`、`curated/`），在目标机 `./deploy.sh up` 现场构建。

`data/` 里是全部状态：`qdrant/`（向量+payload）、`history/`（审计链）、`fastembed/`（模型缓存）。`data/` 与 `.env` 都在 `.gitignore` 里——迁移走文件系统，不要提交，也不要把密钥贴进聊天记录。

### GitHub Actions 自动部署（CD）

[`.github/workflows/curated-cd.yml`](../.github/workflows/curated-cd.yml) 把「构建」和「运行」分开：runner 上构建镜像并推到镜像仓库，服务器只 `docker pull` + `docker compose up -d --no-build`。2 核 2G 的轻量服务器因此不用装编译工具链、不用连 PyPI，也不会倒在 `pip install` 这一步；拉取失败时旧容器保持运行。

**服务器一次性准备**

```bash
# Docker Engine + compose 插件（get.docker.com 的阿里云镜像源）
curl -fsSL https://get.docker.com | sudo bash -s docker --mirror Aliyun
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"          # 重新登录后生效
```

服务器**不需要连 Docker Hub**：CI 会把 qdrant 镜像一起推到 `ghcr.io/<owner>/mem0-curated-qdrant:v1.12.4`，部署时两个镜像都从 GHCR 拉，`docker-compose.yml` 里 qdrant 用 `${QDRANT_IMAGE}` 覆盖。第一次镜像会在 workflow 里自动完成，之后 `docker manifest inspect` 命中就跳过。

新包默认是私有的，二选一：Package settings 里把 `mem0-curated-qdrant` 的可见性改成 Public，或给服务器配 `GHCR_PULL_TOKEN`（workflow 会用它在服务器上 `docker login ghcr.io`）。

部署目录不用预建：workflow 会 `mkdir -p`，并把 `docker-compose.yml`、`deploy.sh`、`.env.example` 传上去。第一次 `./deploy.sh pull` 发现没有 `.env` 会自动生成三个随机密钥（只落盘、不打印），之后每次部署都不覆盖它。取密钥：`grep '^CURATOR_API_KEYS=' /opt/mem0-curated/.env`。

**GitHub 仓库配置**（Settings → Secrets and variables → Actions）

| 类型 | 名称 | 必填 | 说明 |
|---|---|---|---|
| Secret | `DEPLOY_HOST` | ✅ | 服务器公网 IP 或域名 |
| Secret | `DEPLOY_USER` | ✅ | SSH 用户，需要在 `docker` 组里 |
| Secret | `DEPLOY_SSH_KEY` | ✅ | 部署专用私钥，不要复用个人密钥 |
| Secret | `DEPLOY_PORT` | — | SSH 端口，默认 22 |
| Secret | `GHCR_PULL_TOKEN` | — | 仅当 GHCR 包设为私有：需要 `read:packages` 的 PAT |
| Variable | `DEPLOY_PATH` | — | 部署目录，默认 `/opt/mem0-curated` |

公钥放服务器：`ssh-copy-id -i deploy_key.pub user@host`。

**触发方式**

- 推到 `main`，且改动落在 `curated/`、`mem0/`、`pyproject.toml`、`poetry.lock` 时自动执行
- Actions → **Curated CD** → Run workflow，可选分支或标签；feature 分支也能手动部署
- 固定 concurrency group，两次部署不会交叉执行

首轮要拉两个镜像并下载 embedder 模型，留足 10 分钟；网络受限时先往服务器 `.env` 加 `HF_ENDPOINT=https://hf-mirror.com`。`deploy.sh pull` 最后会等网关健康，失败时打印后端日志并以非零码退出。

**回滚**

镜像按 git SHA 打标签，回滚不用重建：

```bash
cd /opt/mem0-curated
MEM0_CURATED_IMAGE=ghcr.io/<owner>/mem0-curated:<old-sha> \
QDRANT_IMAGE=ghcr.io/<owner>/mem0-curated-qdrant:v1.12.4 \
./deploy.sh pull
```

**换成阿里云 ACR（国内拉取更快）**

在 ACR 建好仓库后，设置 Variable `CURATED_IMAGE=registry.cn-hangzhou.aliyuncs.com/<namespace>/mem0-curated`、`CURATED_REGISTRY_USER=<用户名>`，Secret `CURATED_REGISTRY_TOKEN=<密码>`。workflow 会改推 ACR、并在服务器上 `docker login`，其余流程不变。

**安全边界**

- `8787` 是 Worker / Curator 的唯一入口，靠 API key 鉴权；云服务器安全组建议只放行 Agent 所在网段。
- `8788`（dashboard）在服务器上只绑回环地址，看板走隧道访问：`ssh -L 8788:127.0.0.1:8788 user@host`，然后打开 `http://127.0.0.1:8788`。
- `.env` 只在服务器生成；workflow 只上传 `.env.example`，任何密钥都不经过 GitHub。

## 6. 接口

全部经 gateway（`http://<host>:8787`），请求头 `X-API-Key`。

| 方法 | 路径 | Worker | Curator |
|---|---|---|---|
| POST | `/v1/experiences` | 403 | ✅ 写入 |
| POST | `/v1/experiences/search` | ✅ | ✅ |
| POST | `/v1/experiences/dedup-check` | ✅ | ✅ |
| GET | `/v1/experiences/{id}?project_id=` | ✅ | ✅ |
| GET | `/v1/experiences?project_id=` | 403 | ✅ 列表 |
| GET | `/v1/projects` | ✅ | ✅ 已有记录的项目名 |
| POST | `/v1/experiences/{id}/deprecate` | 403 | ✅ |
| POST | `/mem0/search` | ✅ | ✅ |
| GET | `/mem0/memories?project_id=` | ✅ | ✅ |
| GET | `/health` | 免鉴权 | 免鉴权 |

没有任何 `PUT` / `DELETE` 路由：治理只靠"新增 + 标记废弃"，删除会毁掉替代链。

`project_id` 映射到 mem0 的 `user_id` 作用域，天然获得 Qdrant 的 keyword 索引；跨项目读写一律隔离，`supersedes` 指向别的项目会被拒。

检索排序：`score × (0.5 + 0.5 × confidence)`，`created_at` 做 tie-break。置信度低的经验降权但不被埋掉。

### 运维看板（dashboard）

`http://127.0.0.1:8788`，随 `./deploy.sh up` 一起启动。

| 能力 | 说明 |
|---|---|
| 浏览与统计 | 记录数、生效/废弃数、标签分布，按标签筛选 |
| 项目切换 | 输入框挂 `datalist`，候选来自 `GET /v1/projects`；仍可手输尚未存在的项目 |
| 语义搜索 | 走 `/v1/experiences/search`，与 Agent 检索同一套排序 |
| 详情 | 完整记录、失败尝试、`supersedes` / `superseded_by` 链 |
| 新建 | 表单按 Schema 校验，保存前自动查重；发现相似条目需二次确认 |
| 废弃 | 填原因，可指向替代记录；不提供删除，治理模型不变 |

看板默认打开 `CURATED_PROJECT_ID` 指定的项目，右上角可切换。它持有 curator key，因此**只绑定回环地址**，写请求还会校验 `Origin`。需要多人访问时不要直接把端口暴露出去，应放在带认证的反向代理之后。

### 接入编码 Agent（MCP）

`curated/mcp/server.py` 是一个 stdio MCP 服务，把网关的 4 个动作暴露成 tool：`search_experiences` / `dedup_check`（只读）、`record_experience` / `deprecate_experience`（写）。它是手写 JSON-RPC（协议 `2024-11-05`），只用标准库 + `httpx`，**不引入 MCP SDK**。

桥本身不含任何权限判断——每次调用都带着一个角色的 key 走网关，所以「Worker 永不能写」仍然是网关的属性。给 worker key 调 `record_experience`，拿到的是网关原文 `role 'worker' is not allowed to POST /v1/experiences`，模型能读懂并就此收手。

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `CURATED_GATEWAY_URL` | 否（默认 `http://127.0.0.1:8787`） | 只指向网关，绝不指向后端 |
| `CURATED_API_KEY` | 是 | 决定这次会话是只读还是可写 |
| `CURATED_PROJECT_ID` | 是 | 作用域。**不是 tool 参数**，模型无法把读写挪到别的项目上 |

后两者缺一即启动失败（退出码 2，原因写进 stderr），不会留一个每次都报错的僵尸进程。宿主 Python 需要能 `import httpx`。

一个 workspace 对应一个 `CURATED_PROJECT_ID`，因此按用途配成两个条目：日常编码会话挂 worker key，提纯会话单独挂 curator key。

Qoder Desktop —— 入口 左侧栏 **Extensions → Connectors → Add Connector → Add custom MCP**，或直接写 `~/.qoder/settings.json`（CN 版为 `~/.qoder-cn/settings.json`）：

```json
{
  "mcpServers": {
    "curated-read": {
      "command": "python3",
      "args": ["-m", "curated.mcp.server"],
      "cwd": "/绝对路径/mem0-curated",
      "env": {
        "CURATED_GATEWAY_URL": "http://127.0.0.1:8787",
        "CURATED_API_KEY": "<WORKER_KEY>",
        "CURATED_PROJECT_ID": "my-project"
      },
      "timeout": 30000
    },
    "curated-curator": {
      "command": "python3",
      "args": ["-m", "curated.mcp.server"],
      "cwd": "/绝对路径/mem0-curated",
      "env": {
        "CURATED_API_KEY": "<CURATOR_KEY>",
        "CURATED_PROJECT_ID": "my-project"
      },
      "timeout": 30000
    }
  }
}
```

`cwd` 必须是绝对路径；`-m` 从该目录解析包，因此不需要额外设 `PYTHONPATH`。`timeout` 单位是毫秒。

CodeBuddy —— 用户级配置在 `~/.codebuddy/mcp.json`（agent 的 `USER_DATA_DIR_NAME` 就是 `.codebuddy`），形状与上面一致。**编码会话只挂 worker key**；用 `PYTHONPATH` 指向仓库根而不是 `cwd`，这样在任意工作区打开 CodeBuddy 都能 `-m curated.mcp.server`：

```json
{
  "mcpServers": {
    "curated-read": {
      "command": "/绝对路径/.venvs/mem0-curated-mcp/bin/python",
      "args": ["-m", "curated.mcp.server"],
      "env": {
        "PYTHONPATH": "/绝对路径/mem0-curated",
        "CURATED_GATEWAY_URL": "http://127.0.0.1:8787",
        "CURATED_API_KEY": "<WORKER_KEY>",
        "CURATED_PROJECT_ID": "my-project"
      }
    }
  }
}
```

文件权限建议 `600`。改完重启 CodeBuddy（或在 MCP 面板里重连）才会加载；验证方式是让它调一次 `search_experiences`，或按上面的命令手工跑 `initialize` + `tools/list`（应返回 4 个 tool）。需要提纯的会话再单独加一个 `curated-curator` 条目，别和编码会话混在同一个 server 名里。

豆包 PC 版（本机 `Doubao.app` 2.35.4，`DoubaoWork` 同理）—— 入口 左侧「技能·连接器·伙伴」→ 右上「+ 新建」→「新建自定义连接器」，传输类型选 **STDIO**，把上面 CodeBuddy 那组命令、参数、环境变量填进表单。它没有 JSON 入口也没有 `cwd` 字段，所以必须用 `PYTHONPATH` 指向仓库根；连接器跑在豆包的本地 sandbox 里，Python 解释器路径要写 venv 的绝对路径。同样只填 worker key。自定义连接器只在「本地电脑」环境生效，不会同步到云电脑或手机。**尚未实机验证**：入口文案来自官方帮助与第三方接入文档，配完先让它调一次 `search_experiences` 确认工具列表是 4 个。

冷启动后**第一次检索可能超时**：mem0 会惰性加载 BM25 稀疏编码器，模型拉不到时按 3/9/27 秒退避，总时长会盖过网关的 60 秒读超时，Agent 侧表现为一个 504（网关会把上游超时/不可达映射成 504/502，不会抛裸异常）。之后该失败被缓存、不再重复。解法是设 `HF_ENDPOINT=https://hf-mirror.com` 让模型落地，或部署后先手动打一次 `/v1/experiences/search` 预热。


## 7. 示例与测试

```bash
python curated/examples/python_client.py    # 需 httpx，读 GATEWAY_URL/CURATOR_KEY/WORKER_KEY
npx tsx curated/examples/ts_client.ts       # 纯 fetch，无依赖

pytest tests/curated -q
ruff check curated tests/curated --line-length 120
```

测试用真实 `Memory` + 本地 Qdrant + 打桩 embedder，覆盖 Schema 校验、写入检索、`supersedes` 废弃链路、跨项目拒绝、网关 allowlist、转发时 header 剥离与内部 token 注入、上游不可达/超时的 502/504 映射，以及 MCP 桥的协议层与工具层。

## 8. 排障

| 现象 | 原因 |
|---|---|
| 启动即报 `embedding dims mismatch` | `EMBEDDER_DIMS` 与 `EMBEDDER_MODEL` 不匹配 |
| 首次启动很慢 | 正在下载 embedder 模型到 `data/fastembed` |
| 日志 `Failed to load BM25 encoder` / `Could not load model Qdrant/bm25` | 拉不到模型，已降级为纯稠密检索（服务照常）。设 `HF_ENDPOINT=https://hf-mirror.com` 后重启 |
| Agent 侧第一次检索报超时、之后正常 | 冷启动的 BM25 模型下载退避盖过了网关 60s 读超时，见第 6 节「接入编码 Agent（MCP）」 |
| MCP 条目加了但工具列表是空的 | 宿主 `python3` 里装了吗 `httpx`；`cwd` 是不是绝对路径；key/project 两个变量缺一个它会立刻退出（stderr 有原因） |
| Worker 收到 403 | 正常。写接口对 Worker 永久关闭 |
| 容器起不来：`mkdir /Users/xxx/Documents: operation not permitted` | macOS 的 TCC 保护目录，虚拟机里的挂载进程读不到（root 也不行）。给启动 `limactl`/`docker` 的那个 App 授予「完全磁盘访问权」后**完全退出并重开 App**，再 `colima restart` |
| colima 上配了 `registry-mirrors`，重启后失效 | `colima restart` 会用自己托管的配置覆盖 `/etc/docker/daemon.json`。要么每次重启后重配，要么把镜像先 `./deploy.sh save` 下来用 `load` 恢复 |
| 收到 401 `Invalid internal token` | gateway 与后端 `INTERNAL_TOKEN` 不一致 |
| 收到 502 `cannot reach the memory backend` | 后端容器还没 listening（`docker compose up` 的启动窗口）或已崩溃。`docker compose ps` + `docker compose logs mem0-curated` |
| 收到 504 `did not answer within 60.0s` | 网关 60s 读超时；冷启动模型下载最常见。重试一次，或先预热 |
| 检索结果里出现已废弃条目 | 说明有绕过本服务直接写库的路径，或 `deprecated` 字段未写入（检查是否有人直接调 mem0 写接口） |
| 搜不到刚写的条目 | `threshold` 太高，或 `tags` 过滤过窄 |

## 9. 已知限制

- 不接 `server/`（终端用户体系，角色模型与本系统的 worker/curator 冲突），`curated/` 完全独立。
- 不提供 `DELETE`/`PUT`：删除会破坏 `supersedes` 链。
- 只支持单一 embedder 维度；更换模型需重建集合。
- 未接入 graph memory 与 reranker。
- MCP 桥按 `2024-11-05` 基线实现，验证方式是子进程 + 手写 JSON-RPC 驱动（协议握手、四个 tool、UTF-8、错误透传全过，并对着容器化 gateway 跑通了「Curator 中文写入 → Worker 中文检索 → dedup → deprecate 后消失」整链路）。Qoder 侧已实机跑通检索与写入；CodeBuddy 侧已实机跑通：`~/.codebuddy/mcp.json` 里的配置重启后被 MCP 面板识别（`curated-read`，4 tools，连接正常），并从任意 `cwd` 起进程验证了 `initialize` / `tools/list` 与一次真实检索。豆包 PC 版走同一个 STDIO 连接器，入口与参数按官方帮助配置，**尚未实机验证**。
- compose 里 Qdrant 固定在 `v1.12.4`，而镜像装的 `qdrant-client` 是 1.20，启动会打一条版本兼容性 warning。功能实测正常（payload 过滤、`must_not` 语义都对），但**升这个 pin 之前要重跑一遍 `tests/curated` 和 `./deploy.sh smoke`**——过滤语义靠的是 client/server 对 payload 条件的一致理解。

## 10. 自动沉淀（每 30 分钟）

前 9 节的所有写入都由人触发。自动沉淀补的是「任务干完了但没说要沉淀」这一段：定时任务每 30 分钟起来一次，读取对话的脱敏摘要，由模型判断是否值得入库，通过机器门禁后写入共享库。判断归模型，约束归代码。来源有两条：Qoder 的全量转录（全自动），和 CodeBuddy 主动投放的轨迹（半自动，见本节末尾）。

```
~/.qoder/projects/*/<session>.jsonl      Qoder，全自动
~/.codebuddy/curated-inbox/*.jsonl       CodeBuddy，agent 主动投放
        │  scan：流式读尾部，脱敏 + 去代码 + 去工具正文
        ▼
   摘要（≤ 8 KB；长会话可用 --session 按段切分）
        │  定时会话里的模型判断
        ▼
   record JSON  ──write──▶ 门禁 → 幂等 → dedup → 网关 → 经验库
```

命令行（在仓库根目录，用 MCP 那个精简 venv 即可，不依赖 pydantic）：

```bash
PY=~/.venvs/mem0-curated-mcp/bin/python
$PY -m curated.auto.cli scan --limit 2          # 输出候选摘要 JSON
$PY -m curated.auto.cli write --key '<path>'    # 从 stdin 读一条 record
$PY -m curated.auto.cli finalize --key '<path>' # 关闭 --keep 打开的候选
$PY -m curated.auto.cli mark --key '<path>' --reason one_off
```

**一个会话可以沉淀多条经验。** 常规 `scan` 只给会话尾部一段摘要（回答「最后发生了什么」）；长会话的早期与中段需要按会话切分，每段独立成候选：

```bash
$PY -m curated.auto.cli scan --session <id 前缀或完整路径> --segments 6 --limit 4
```

候选 key 是虚拟的 `<path>::seg-N`，与每文件的常规游标互不干扰，不会把定时扫描的 offset 拉回去。每段摘要 ≤ 8 KB，工具事件折叠成 `TOOLS ok=.. error=..`，主题按时间顺序尽量均匀切分；段数会根据文本量自动收敛（短会话仍是 1 段），最多 12 段。

同一段摘要里若确实存在互不重叠的多个主题（工作流习惯、踩坑、配置约束），用 `--keep` 连续写，最后 `finalize` 提交：

```bash
$PY -m curated.auto.cli write --key '<key>' --keep   # 每条都加 --keep
$PY -m curated.auto.cli write --key '<key>' --keep
$PY -m curated.auto.cli finalize --key '<key>'       # 推进游标，审计里记录所有 id
```

单候选最多 4 条（第 5 条返回 `cap_reached`）；只写一条时不用 `--keep`，`write` 自动提交。判断标准与硬性红线写在 [`prompts/auto_curator.md`](prompts/auto_curator.md)，`write` 的门禁按顺序是：字段完整性 → 密钥扫描 → 代码气味 → 置信度封顶（`verified` 0.8 / `error_then_fixed` 0.65 / 其余 0.5）→ 幂等 → 去重。幂等键是 `会话 + 摘要哈希 + 本条 summary 哈希`：同一段摘要重跑同一结论会被挡住，同一摘要里的不同主题、同一会话的后续新主题都能各自入库——否则长会话终生只能写一条。去重分数 ≥ 0.9 直接跳过；0.75–0.9 只是"相关"，自动链路**永不** `supersede`——候选带 `auto` 标签时作为独立条目新增并在审计里记 `near_duplicate`，撞上人工条目则跳过并留审计记录。废弃旧条目必须人工裁决：向量相似度分不清"同一结论"和"只是都在谈门禁"。自动写入的条目固定带 `auto` + `unreviewed` 标签，看板按标签筛出来复核即可。

**状态与配置**（都在 `~/.qoder/curated/`，目录 0700、文件 0600）：

| 文件 | 作用 |
|---|---|
| `auto-cursor.json` | 每个转录的 offset / size / mtime / 待处理摘要，只有写入或显式跳过后才推进；深扫候选用 `<path>::seg-N` 虚拟键，不影响常规游标 |
| `auto-config.json` | 可选：`exclude_workspaces`（按目录 slug 子串排除）、`max_per_run`、`max_read_bytes`、`inbox_dir`（单个路径或数组，覆盖默认的两个投放目录，调试用） |
| `auto-log.jsonl` | 每次 write / mark 的结果与原因，出问题先看这里 |
| `auto.lock` | 防止两次定时任务重叠；45 分钟视为陈旧可接管 |

首次运行只把现有转录的游标种子到文件末尾，避免一次性回灌历史。要回放历史会话用 `scan --backfill --limit 2`，分批跑；加 `--backfill-days 7` 只回放最近一周，更早的文件保持种子状态（不带这个参数时仍可手动全量回放）。停用方式：删掉或禁用 Qoder 里的定时任务；已经入库的条目不会被自动删除，按 `auto` 标签清理即可。

CLI 的网关地址、curator key、project 从环境变量读取；缺省时回退读 `~/.qoder/settings.json` 里 `mcpServers.curated-curator.env`，不额外落一份密钥。Worker 会话仍然只读——写入只发生在定时任务与 CLI 进程内。

**CodeBuddy / 豆包走半自动投放。** 两者都把对话正文放在服务端。CodeBuddy CN 1.106.1 本地只有 `message-queue/*.json`（仅用户输入队列，没有助手回复和工具结果）、`edit-sessions/<conversationId>.json` 与 `file-changes/<conversationId>/*.json`（文件改动）、`todos` / `plans` / `brain/<hash>/*.md`（任务与笔记），以及只存 `current.json` 指针的空 `conversations/<id>/` 目录——没有可扫描的转录正文。所以留了投放目录：CodeBuddy 用 `~/.codebuddy/curated-inbox/`，豆包用 `~/.doubao/curated-inbox/`（`auto-config.json` 的 `inbox_dir` 可覆盖，支持传数组）。客户端侧规则（样例 [`prompts/codebuddy_trajectory.md`](prompts/codebuddy_trajectory.md)，**尚未安装**）让 agent 在任务收尾时把脱敏轨迹写成 JSONL，行类型是 `meta` / `user` / `assistant` / `tool`（只记工具名与 `ok`/`error`）/ `verification`，格式与客户端无关。`scan` 把这两个目录当第二来源，候选里用 `source: "inbox"` 标出；只认各目录直接层的 `*.jsonl`，按 `meta.workspace` 套 `exclude_workspaces`，其余脱敏、摘要、门禁、幂等、去重与 Qoder 同一套。轨迹是 agent 自己写的，只能信任它格式合法、不能信任它内容安全，所以脱敏在扫描时重做一遍；入库后同样带 `auto` + `unreviewed`。

半自动的代价是覆盖率取决于投放行为。衡量方式：取一周内两个投放目录的文件数 ÷ 同一周实际完成的对应客户端会话数；偏低就调规则里的触发条件，不要靠多写凑数。跑通验证：把手写样例 [`examples/inbox_trajectory.jsonl`](examples/inbox_trajectory.jsonl) 放进投放目录（用 `auto-config.json` 的 `inbox_dir` 指到临时目录即可，别动真实状态），先 `scan --limit 1` 让首次运行种子，再 `scan --limit 1 --backfill`，应返回 `source: inbox`、`evidence: error_then_fixed`。
