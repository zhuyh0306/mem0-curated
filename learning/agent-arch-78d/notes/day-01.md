# Day 1｜环境与工具链基线（120min）

日期：2026-11-09
用时：120 / 120
今日验收物：`lab/day-01/docker-compose.yml` + `env_check.sh`（全项检查通过）+ 一条真实 completion 与对应 Langfuse trace

## 精读笔记（3 条，能复述才算）

1. **Langfuse v3 的最小拓扑是 6 个进程，不是一个容器**
   - 为什么重要：`langfuse/langfuse` 镜像只是 web 前端；trace 由 `langfuse-worker` 从 Redis 异步消费后写入 ClickHouse，事件体和媒体文件还要落到 S3。少了 worker，completion 能返回 200、`/api/public/traces` 却永远是空数组——这正是「网关通了但 trace 不通」的典型现象。
   - 在本项目落在：`lab/day-01/docker-compose.yml` 里 `langfuse-web` + `langfuse-worker` + postgres + redis + clickhouse + minio 六件套，加上 qdrant 与 litellm 共 8 个容器。

2. **LiteLLM 上报 trace 是「配置声明」和「环境注入」两件事**
   - 为什么重要：`litellm_settings.success_callback: ["langfuse"]` 只是声明回调名，能不能真的上报取决于进程里有没有 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`。密钥不能写进 `config.yaml`，要用 `os.environ/XXX` 从环境变量取，否则 yaml 一旦提交就把密钥带出去了。
   - 在本项目落在：`lab/day-01/litellm/config.yaml` 的 `success_callback` / `failure_callback`，密钥走 `.env` → compose `env_file`，`.env` 已进 `.gitignore`。

3. **异步上报意味着验证脚本必须轮询，不能「睡一次」**
   - 为什么重要：completion 返回时 span 还在批量缓冲里，`/api/public/traces` 要几百毫秒到几秒才可见。固定 `sleep 2` 在慢机器上会误判失败，写成「有间隔地重试直到出现或超时」才稳定。
   - 在本项目落在：`lab/day-01/smoke.sh` 的 `SMOKE_WAIT` 轮询循环（默认 60s）。

## 扫读：模块归属

| 资料 | 归属模块 | 一句话判断依据 |
|------|---------|---------------|
| LiteLLM Gateway | 网关层 | 统一协议、托管密钥、多模型路由都在这一层；业务只认 `model_name` |
| Langfuse | 可观测 | trace / observation / 成本都由它存，属于第 12 周的定位与看板 |
| Qdrant | 向量库 | 只存 embedding 与 payload，不参与鉴权和业务判定 |
| MinIO | 存储 | Langfuse 只把它当 S3 用，和业务数据无关 |
| Ollama | 模型供给 | 本机 keyless 推理，等价于云厂商的 completion 端 |

## 产出

- 文件：
  - `lab/day-01/docker-compose.yml`：postgres / redis / qdrant / clickhouse / minio / minio-init / langfuse-web / langfuse-worker / litellm 共 9 个服务定义，8 个常驻容器。
  - `lab/day-01/litellm/config.yaml`：`fast` → `ollama/qwen2.5:0.5b`，`success_callback` 与 `failure_callback` 都挂 langfuse。
  - `lab/day-01/env_check.sh`：9 个端口/HTTP 检查 + LiteLLM `Bearer` 鉴权检查，任一失败退出码 1。
  - `lab/day-01/smoke.sh`：发 completion → jq 校验内容 → 轮询 trace。
- 实测结果：
  - `./env_check.sh` → `10 checks passed`（postgres、redis、qdrant、clickhouse、minio、minio-init 状态、langfuse-web、langfuse-worker、litellm 鉴权、容器列表）。
  - `./smoke.sh` → `model=fast`，中文回答正常返回；trace `85d6cf3cd912496ab9b82160cd701e17`，name `litellm-acompletion`。
  - observation 明细：类型 `GENERATION`，model `ollama/qwen2.5:0.5b`，latency `3.163s`，usage `input 41 / output 29 / total 70`，cost `0`。

## 三行收尾

- 卡点：`minio/minio` 在本机任何镜像源都拉不下来（daocloud 403、1ms/rat not found、quay 401）；同时 langfuse-web 按 768m 内存上限启动会 `Reached heap limit` 直接崩。
- 绕过：MinIO 换成本机已有的 `cgr.dev/chainguard/minio:latest`，用 LiteLLM 镜像里的 boto3 跑 `minio_init.py` 建桶替代官方 `mc`；langfuse-web 提到 `mem_limit 1536m` + `--max-old-space-size=1024`，worker 提到 1024m + 768 堆。
- 下次：先 `docker compose ps` 看哪个容器在重启、再看它自己的日志，而不是先怀疑网络；镜像拉不动时先 `docker images` 找本地可替代的等价镜像。

## 已知限制

- 本机 9000/9001 被一个 ssh 端口转发占着，MinIO 映射到 `19000/19001`；compose 内各服务仍走容器名 + 9000，不受影响。
- 模型用本机 Ollama（`qwen2.5:0.5b`）代替云厂商 key，链路一致、成本为 0；换云模型只需改 `model_list` 里的 `model` 与对应 API key。
- colima VM 只有约 5.7 GiB，叠在其他项目容器之上，所以给每个服务都写了 mem_limit；同时跑更多实验前要先 `docker stats`。

## 验收

- [x] 3 条笔记能不看资料复述
- [x] 能一句话说清项目落点
- [x] 今日验收物真实存在且可复现
- [x] 未完成项已登记为「已知限制」并写明补做日
