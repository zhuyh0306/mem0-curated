# Day 1 · 环境与工具链基线

对应原文 Day 1 的验收物：`docker-compose.yml` + `env_check.sh`（全项检查通过）。

本目录是一次性的学习实验栈，和仓库里的 `curated/` 无关，不要混用端口或数据卷。

## 服务与端口

| 服务 | 镜像 | 宿主机端口 | 用途 |
|------|------|-----------|------|
| postgres | `postgres:16-alpine` | 5432 | Langfuse 元数据 |
| redis | `redis:7-alpine` | 6379 | Langfuse 队列/缓存 |
| qdrant | `qdrant/qdrant:v1.12.4` | 6333/6334 | 向量库 |
| clickhouse | `clickhouse/clickhouse-server:24.3-alpine` | 8123 | Langfuse 事件存储（v3 必需） |
| minio | `cgr.dev/chainguard/minio:latest` | 19000/19001 | Langfuse 事件对象存储（v3 必需） |
| langfuse-web | `langfuse/langfuse:3` | 3000 | 控制台与查询 API |
| langfuse-worker | `langfuse/langfuse-worker:3` | — | 异步消费事件 |
| litellm | `ghcr.io/berriai/litellm:main-stable` | 4000 | 模型网关 |

两个说明：

- 原文列了五个服务，但 Langfuse v3 自己还要 ClickHouse 和 S3，所以实际是 8 个容器。
- 本机 9000/9001 被一个 SSH 端口转发占用，MinIO 改到 19000/19001，不要去杀那个转发。
- `docker.io/minio/minio` 和 `quay.io/minio/minio` 在本机都拉不下来（mirror 403 / quay 401），所以用本机已有的 Chainguard 构建；`minio_init.py` 用 LiteLLM 镜像里的 boto3 建桶，替代官方 `mc`。

## 模型来源

本机没有云厂商 key，但装有 Ollama，所以网关路由到本机的 `qwen2.5:0.5b`：

```bash
ollama pull qwen2.5:0.5b
```

换成 OpenAI / DeepSeek 只需要改 `litellm/config.yaml` 的 `model` 与 `api_key`，compose 不用动。

## 运行

```bash
cp .env.example .env      # 首次；填好各项，尤其是 64 位 hex 的 LANGFUSE_ENCRYPTION_KEY
./env_check.sh            # 起服务之前应当全红，用来确认脚本本身有效
docker compose up -d      # 首次会拉镜像，超过 15 分钟按止损规则处理
./env_check.sh            # 期望全部通过
./smoke.sh                # completion + Langfuse trace
```

打开 <http://localhost:3000>，用 `.env` 里的 `LANGFUSE_ADMIN_EMAIL` / `LANGFUSE_ADMIN_PASSWORD` 登录；trace 也可以在 `smoke.sh` 输出的链接直接打开。

停止与清理：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 连数据一起删，下次重新初始化 Langfuse 项目
```

## 止损

原文规则：任何一步卡住超过 15 分钟，停止死磕，改为查文档或换最小简化实现。本目录的两处简化就是这条规则的产物：用本机 Ollama 代替云端模型；如果 Langfuse 起不来，先只保留 postgres/redis/qdrant/litellm 四个服务，把 trace 留到问题定位后再补。
