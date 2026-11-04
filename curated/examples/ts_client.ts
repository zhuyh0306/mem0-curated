/**
 * Thin client for the mem0-curated gateway.
 *
 * Run: GATEWAY_URL=http://localhost:8787 CURATOR_KEY=... WORKER_KEY=... npx tsx curated/examples/ts_client.ts
 */

const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8787";
const CURATOR_KEY = process.env.CURATOR_KEY ?? "";
const WORKER_KEY = process.env.WORKER_KEY ?? "";
const PROJECT_ID = process.env.PROJECT_ID ?? "proj-a";

type Json = Record<string, unknown>;

async function call(method: "GET" | "POST", path: string, apiKey: string, body?: Json, params?: Json) {
  const url = new URL(`${GATEWAY_URL}${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    url.searchParams.set(key, String(value));
  }

  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`${method} ${path} -> ${response.status} ${await response.text()}`);
  }
  return response.json();
}

export const search = (apiKey: string, query: string, tags: string[] = [], topK = 5) =>
  call("POST", "/v1/experiences/search", apiKey, { query, project_id: PROJECT_ID, tags, top_k: topK });

export const dedupCheck = (apiKey: string, query: string, tags: string[] = []) =>
  call("POST", "/v1/experiences/dedup-check", apiKey, { query, project_id: PROJECT_ID, tags });

export const create = (apiKey: string, record: Json) =>
  call("POST", "/v1/experiences", apiKey, { project_id: PROJECT_ID, record });

export const deprecate = (apiKey: string, memoryId: string, reason: string, supersededBy = "") =>
  call("POST", `/v1/experiences/${memoryId}/deprecate`, apiKey, {
    project_id: PROJECT_ID,
    reason,
    superseded_by: supersededBy,
  });

export const getExperience = (apiKey: string, memoryId: string) =>
  call("GET", `/v1/experiences/${memoryId}`, apiKey, undefined, { project_id: PROJECT_ID });

async function main() {
  const record: Json = {
    memory_type: "experience",
    summary: "Qdrant payload 过滤漏掉缺少字段的点，deprecated 必须在插入时显式写 false",
    task_goal: "让已废弃经验从检索结果中消失",
    constraints: ["qdrant 1.12", "mem0 filters ne/must_not"],
    successful_solution: {
      steps: ["插入时 metadata 固定写入 deprecated=false", "检索默认加 filters {'deprecated': false}"],
      verification: "插入两条、废弃其中一条后检索，只返回未废弃的那条",
    },
    failed_attempts: [
      {
        try_content: "只在废弃时写入 deprecated=true，不写 false",
        root_cause: "Qdrant 的 must_not / ne 对缺少该字段的点不匹配",
        lesson: "布尔标记字段必须有显式默认值",
      },
    ],
    applicable_scope: "Qdrant 作为向量库、用 metadata 布尔位做软删除时",
    exclude_scope: "使用 expiration_date 做时间型失效时",
    tags: ["qdrant", "mem0", "metadata-filter"],
    supersedes: "",
    source_agent: "worker-coding-1",
    confidence: 0.9,
  };

  const created = (await create(CURATOR_KEY, record)) as { id: string };
  console.log("created:", created);

  console.log("worker search:", await search(WORKER_KEY, "deprecated 标记 过滤 qdrant"));

  const superseding = (await create(CURATOR_KEY, { ...record, supersedes: created.id })) as { id: string };
  console.log("superseding record:", superseding);

  try {
    await create(WORKER_KEY, record);
  } catch (error) {
    console.log("worker write blocked as expected:", (error as Error).message);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
