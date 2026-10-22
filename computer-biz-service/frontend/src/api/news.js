import { http } from './http.js'

/**
 * 抓取动态的只读 API。
 *
 * 后端 DTO 是 snake_case（Spring 侧用 @JsonProperty 对齐 FastAPI），
 * 这里统一归一化成 camelCase，页面不直接处理下划线字段。
 */
const pick = (obj, snake, camel) => obj?.[snake] ?? obj?.[camel]

const numberOrNull = (value) =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

const textOrEmpty = (value) => (typeof value === 'string' ? value : '')

function normalizeSource(raw) {
  return {
    sourceId: textOrEmpty(pick(raw, 'source_id', 'sourceId')),
    name: textOrEmpty(raw?.name),
    enabled: raw?.enabled !== false,
    lastFetchedAt: pick(raw, 'last_fetched_at', 'lastFetchedAt') ?? null,
    lastStatus: numberOrNull(pick(raw, 'last_status', 'lastStatus')),
    lastError: textOrEmpty(pick(raw, 'last_error', 'lastError')),
    consecutiveFailures: numberOrNull(
      pick(raw, 'consecutive_failures', 'consecutiveFailures'),
    ),
  }
}

function normalizeRunSource(raw) {
  return {
    sourceId: textOrEmpty(pick(raw, 'source_id', 'sourceId')),
    sourceName: textOrEmpty(pick(raw, 'source_name', 'sourceName')),
    fetched: numberOrNull(raw?.fetched) ?? 0,
    candidates: numberOrNull(raw?.candidates) ?? 0,
    filtered: numberOrNull(raw?.filtered) ?? 0,
    ingested: numberOrNull(raw?.ingested) ?? 0,
    skipped: numberOrNull(raw?.skipped) ?? 0,
    failed: numberOrNull(raw?.failed) ?? 0,
    error: textOrEmpty(raw?.error),
  }
}

function normalizeRun(raw) {
  return {
    runId: textOrEmpty(pick(raw, 'run_id', 'runId')),
    startedAt: pick(raw, 'started_at', 'startedAt') ?? null,
    finishedAt: pick(raw, 'finished_at', 'finishedAt') ?? null,
    mode: textOrEmpty(raw?.mode),
    exitCode: numberOrNull(pick(raw, 'exit_code', 'exitCode')),
    elapsedS: numberOrNull(pick(raw, 'elapsed_s', 'elapsedS')),
    fetched: numberOrNull(raw?.fetched) ?? 0,
    candidates: numberOrNull(raw?.candidates) ?? 0,
    filtered: numberOrNull(raw?.filtered) ?? 0,
    expired: numberOrNull(raw?.expired) ?? 0,
    ingested: numberOrNull(raw?.ingested) ?? 0,
    skipped: numberOrNull(raw?.skipped) ?? 0,
    failed: numberOrNull(raw?.failed) ?? 0,
    pending: numberOrNull(raw?.pending) ?? 0,
    sourcesOk: numberOrNull(pick(raw, 'sources_ok', 'sourcesOk')) ?? 0,
    sourcesFailed: numberOrNull(pick(raw, 'sources_failed', 'sourcesFailed')) ?? 0,
    fatalError: textOrEmpty(pick(raw, 'fatal_error', 'fatalError')),
    sources: Array.isArray(raw?.sources) ? raw.sources.map(normalizeRunSource) : [],
  }
}

function normalizeItem(raw) {
  return {
    id: numberOrNull(raw?.id),
    sourceId: textOrEmpty(pick(raw, 'source_id', 'sourceId')),
    sourceName: textOrEmpty(pick(raw, 'source_name', 'sourceName')),
    title: textOrEmpty(raw?.title),
    titleZh: textOrEmpty(pick(raw, 'title_zh', 'titleZh')),
    summaryZh: textOrEmpty(pick(raw, 'summary_zh', 'summaryZh')),
    excerpt: textOrEmpty(raw?.excerpt),
    status: textOrEmpty(raw?.status),
    category: textOrEmpty(raw?.category),
    importance: numberOrNull(raw?.importance),
    publishedAt: pick(raw, 'published_at', 'publishedAt') ?? null,
    fetchedAt: pick(raw, 'fetched_at', 'fetchedAt') ?? null,
    docFilename: textOrEmpty(pick(raw, 'doc_filename', 'docFilename')),
    canonicalUrl: textOrEmpty(pick(raw, 'canonical_url', 'canonicalUrl')),
    reason: textOrEmpty(raw?.reason),
    lastError: textOrEmpty(pick(raw, 'last_error', 'lastError')),
    keyFacts: Array.isArray(pick(raw, 'key_facts', 'keyFacts'))
      ? pick(raw, 'key_facts', 'keyFacts').filter((x) => typeof x === 'string')
      : [],
    content: textOrEmpty(raw?.content),
    contentKind: textOrEmpty(pick(raw, 'content_kind', 'contentKind')),
    ingestResult: textOrEmpty(pick(raw, 'ingest_result', 'ingestResult')),
    jobId: textOrEmpty(pick(raw, 'job_id', 'jobId')),
    retryCount: numberOrNull(pick(raw, 'retry_count', 'retryCount')),
    nextRetryAt: pick(raw, 'next_retry_at', 'nextRetryAt') ?? null,
    updatedAt: pick(raw, 'updated_at', 'updatedAt') ?? null,
  }
}

export async function fetchNewsOverview(limit = 10) {
  const { data } = await http.get('/rag/news/overview', {
    params: { limit },
    timeout: 15000,
    headers: { 'Cache-Control': 'no-cache' },
  })
  return {
    available: !!data?.available,
    generatedAt: pick(data, 'generated_at', 'generatedAt') ?? '',
    counts: data?.counts && typeof data.counts === 'object' ? data.counts : {},
    sources: Array.isArray(data?.sources) ? data.sources.map(normalizeSource) : [],
    runs: Array.isArray(data?.runs) ? data.runs.map(normalizeRun) : [],
  }
}

export async function fetchNewsItems({
  status = '',
  sourceId = '',
  q = '',
  page = 1,
  pageSize = 20,
} = {}) {
  const params = { page, page_size: pageSize }
  if (status) params.status = status
  if (sourceId) params.source_id = sourceId
  if (q) params.q = q

  const { data } = await http.get('/rag/news/items', {
    params,
    timeout: 15000,
    headers: { 'Cache-Control': 'no-cache' },
  })
  return {
    available: !!data?.available,
    page: numberOrNull(data?.page) ?? page,
    pageSize: numberOrNull(pick(data, 'page_size', 'pageSize')) ?? pageSize,
    total: numberOrNull(data?.total) ?? 0,
    items: Array.isArray(data?.items) ? data.items.map(normalizeItem) : [],
  }
}

export async function fetchNewsItem(id) {
  const { data } = await http.get(`/rag/news/items/${encodeURIComponent(id)}`, {
    timeout: 15000,
    headers: { 'Cache-Control': 'no-cache' },
  })
  return normalizeItem(data)
}
