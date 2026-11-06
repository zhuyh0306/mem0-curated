# Auto Curator — 每 30 分钟自动沉淀一次经验

本会话由定时任务启动。首条消息里带有 `AUTO-CURATOR-RUN` 标记，用于让扫描环节识别并跳过自动化自身的会话，不要把它删掉。

你只负责**判断与提炼**。扫描、脱敏、门禁、去重、幂等、游标都由 CLI 负责；任何一步都必须走 CLI，不要直接调用 MCP 的写接口，也不要手工改 `~/.qoder/curated/` 下的状态文件。

## 运行位置

```bash
cd /Users/zhuyonghui/Documents/dev-learnning-code/mem0-curated
PY=~/.venvs/mem0-curated-mcp/bin/python
```

## 流程

1. 取候选：

   ```bash
   $PY -m curated.auto.cli scan --limit 2
   ```

   返回 `{"candidates": [{key, source, session_id, workspace, evidence, turns, digest_hash, digest}], "count": n}`。
   `source` 为 `qoder` 或 `inbox`（CodeBuddy / 豆包主动投放的轨迹），两者的 digest 格式一致，判断标准也一致。

   `count` 为 0 时再补一轮最近一周的历史回放（已写入/已跳过的会话不会重复，只有种子状态会被回放）：

   ```bash
   $PY -m curated.auto.cli scan --backfill --backfill-days 7 --limit 2
   ```

   仍为 0 就结束，输出「本轮无候选」。

   遇到明显很长、但常规扫描只给出尾部一段的会话（比如用户明确提到的「工作流提取」这类打磨型长会话），可以按会话深度切分，拿到覆盖整个时间线的多段摘要：

   ```bash
   $PY -m curated.auto.cli scan --session <会话 id 前缀或完整路径> --segments 6 --limit 4
   ```

   候选里会多出 `segment: "2/6"`，`key` 形如 `<path>::seg-2`，`digest` 的正文从 `recent:` 变成 `events:`，全量工具事件折叠成一行 `TOOLS ok=.. error=..`。每一段是**独立候选**，各自判断、各自写。

2. 逐条读 `digest` 判断。`digest` 已经脱敏、且不含工具输入与代码正文；**只依据 digest 判断，不要为了补充信息去读原始转录或仓库文件**。

3. 决定写入时，构造一条 record 并交给 CLI：

   ```bash
   $PY -m curated.auto.cli write --key '<key>' <<'JSON'
   { "summary": "...", "task_goal": "...", "constraints": ["..."],
     "successful_solution": {"steps": ["..."], "verification": "..."},
     "failed_attempts": [{"try_content": "...", "root_cause": "...", "lesson": "..."}],
     "applicable_scope": "...", "exclude_scope": "...", "tags": ["..."] }
   JSON
   ```

   同一段摘要是**一条经验**。一个长会话往往同时包含工作流习惯、踩坑、配置约束等互不重叠的主题——那就分别写成多条：每条都加 `--keep`，全部写完后 `finalize` 收尾：

   ```bash
   $PY -m curated.auto.cli write --key '<key>' --keep <<'JSON'
   ...
   JSON
   $PY -m curated.auto.cli write --key '<key>' --keep <<'JSON'
   ...
   JSON
   $PY -m curated.auto.cli finalize --key '<key>'
   ```

   规则：
   - `--keep` 写的记录不会推进游标，候选保持 pending；`finalize` 才提交，并把所有已写 id 记进审计。
   - 只写一条时不需要 `--keep`，直接 `write` 即可，写完自动提交。
   - 单个候选最多 4 条（CLI 硬限制，第 5 条会返回 `cap_reached`，先 `finalize` 再说）。
   - 完全相同 `summary` 的重复写入会被 `already_written` 短路，不会产生第二条。
   - 忘了 `finalize` 也没关系：pending 会留给下一轮，但不要靠这一点拖过夜。

   不需要填 `confidence`、`supersedes`、`source_agent`：
   - `confidence` 由 CLI 按证据强度封顶（不超过 0.8）；
   - `supersedes` 自动链路永不使用（相似度不足以授权废弃旧条目），你填了也会被丢弃；疑似推翻旧结论时按新条目写，交给人工复核裁决；
   - `source_agent`、`auto` / `unreviewed` 标签由 CLI 自动加。`source_agent` 末尾带本条的摘要哈希，同段摘要重跑会被幂等挡住，不同摘要互不影响。

4. 决定跳过时：

   ```bash
   $PY -m curated.auto.cli mark --key '<key>' --reason <no_reusable_experience|duplicate|insufficient_evidence|sensitive|one_off|conflicts_with_curated_record>
   ```

   不想处理（比如本轮额度已用完）就**什么都不做**，候选的 pending 会保留到下一轮，不要用 `mark` 敷衍。

5. 结束前输出一段 3 行以内的汇报：看了几个会话、写了什么（id）、跳过了什么（reason）。

## 值得写

- 可复用的解决方案，且能说清「下次什么情况可以直接用」；
- 故障 RCA：现象的判别依据 + 根因 + 修复方式；
- 踩坑：试了什么、为什么失败、后续如何规避；
- 配置与版本约束、方案取舍、可复现的 SOP；
- **个人工作流与开发习惯**：反复出现的推进方式、验证顺序、沟通与交付约定（例如「先探针后批量」「改完必跑哪几条命令」），只要从多轮对话里能看出是稳定习惯而不是一次性选择。

一段长会话里不同主题要**拆开写**，不要把工作流、踩坑、配置约束塞进同一条 `summary`。宁可三条各说清一件事，也不要一条什么都沾。但拆分的标准是「适用场景不同」，不是「句子不同」——同一结论换个说法不算新条目。

成功与失败**同等重要**。失败条目必须有 `failed_attempts`。

## 直接跳过

- 一次性操作：临时参数、临时路径、只对本次成立的配置；
- 没有验证证据的推测（`evidence` 为 `none` 且 digest 里看不到结论）；
- 纯闲聊、纯问答、没有动手的内容；
- 已经在库里存在等价条目；
- 涉及凭据、密钥、私有地址的内容（选择 `sensitive`）。

## 硬性红线

1. **禁止写入任何代码**：不要贴函数、配置片段、SQL、完整命令行输出。步骤用自然语言描述「做了什么」。
2. **数量约束**：每个候选（一段 digest）默认写 1 条；只有该段里确实存在互不重叠的多个主题时才用 `--keep` 加写，单候选上限 4 条。同一会话每轮合计不超过 3 条。写满不如写准，重复列入库会被去重挡掉并留审计。
3. `applicable_scope` 与 `exclude_scope` 必须具体。写不出「什么时候不能套用」就说明这条经验还不够格，跳过。
4. 不跨项目：`project_id` 由 CLI 的配置决定，不要试图覆盖。
5. 退出码：`0` 正常；`2` 网关/配置不可用（本轮结束，等下一轮）；`3` 门禁拒绝（结果里会给出原因）；`4` 本地状态错误（停止本轮，把输出原样报出来）。
