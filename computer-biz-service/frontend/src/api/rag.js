import { API_PREFIX, canceledError, extractDetail, toHttpError, http } from './http.js'

/**
 * 后端 DTO 用的是 snake_case（@JsonProperty("job_id") 等）。
 *
 * 已确认当前项目跑的是 Jackson 3，而 Jackson 3 的注解包名仍是
 * com.fasterxml.jackson.annotation（官方为平滑迁移刻意保留），
 * 所以 @JsonProperty 照常生效，字段确实是 snake_case。
 *
 * 这里同时兼容 camelCase 只是零成本的保险：万一将来有人往 pom 里
 * 加了 spring-boot-starter-jackson2，前端不会因此白屏。
 */
const pick = (obj, snake, camel) => obj?.[snake] ?? obj?.[camel]

export function normalizeUploadResp(raw) {
  const jobId = pick(raw, 'job_id', 'jobId')
  if (!jobId) {
    throw Object.assign(new Error('上传接口未返回 job_id，请检查后端序列化配置'), {
      status: 200,
    })
  }
  return {
    jobId: String(jobId),
    fileName: pick(raw, 'file_name', 'fileName') ?? '',
    done: !!raw?.done,
  }
}

export function normalizeTaskResp(raw) {
  const elapsed = pick(raw, 'elapsed_sec', 'elapsedSec')
  return {
    jobId: pick(raw, 'job_id', 'jobId') ?? '',
    fileName: pick(raw, 'file_name', 'fileName') ?? '',
    done: !!raw?.done,
    result: typeof raw?.result === 'string' ? raw.result : '',
    stage: typeof raw?.stage === 'string' ? raw.stage : '',
    // elapsed_sec 在 Python 侧是 float、Java 侧是 Double，但也可能是 null
    elapsedSec: typeof elapsed === 'number' && Number.isFinite(elapsed) ? elapsed : null,
  }
}

/**
 * 阶段 A：上传文件字节。返回 job_id。
 * 注意上传接口是「立即返回」的，真正的入库是异步的。
 */
export function uploadDocument(file, { signal, onUploadProgress } = {}) {
  const form = new FormData()
  // 字段名必须是 file —— 要和后端 @RequestPart("file") 严格对齐
  form.append('file', file, file.name)

  return http
    .post('/rag/documents', form, {
      signal,
      // 大文件不设超时，生命周期完全交给 signal 控制
      timeout: 0,
      // 不要手动设 Content-Type —— axios 检测到 FormData 会让浏览器自己设置
      // 带 boundary 的头。手写会导致 boundary 丢失，后端直接 400
      onUploadProgress: (e) => {
        // e.total 在 lengthComputable=false 时是 0，不判断会算出 Infinity/NaN
        if (!e || !e.total) return
        const percent = Math.min(100, Math.round((e.loaded / e.total) * 100))
        onUploadProgress?.(percent)
      },
    })
    .then((res) => normalizeUploadResp(res.data))
}

/**
 * 阶段 B：用 job_id 查进度。
 */
export function queryTask(jobId, { signal } = {}) {
  return http
    .get(`/rag/tasks/${encodeURIComponent(jobId)}`, {
      signal,
      // Java 侧 spring.http.client 的读超时完全没配（见 docs/rag对接实现说明.md 遗留问题 #1），
      // FastAPI 一卡这个请求会永远挂着。前端必须自己兜底，否则 UI 静默冻死。
      timeout: 10000,
      // 防止某些中间代理缓存这个 GET
      headers: { 'Cache-Control': 'no-cache' },
    })
    .then((res) => normalizeTaskResp(res.data))
}

// -------------------------------------------------------------------- AI 问答

const CHAT_PATH = `${API_PREFIX}/rag/chat`

/** 结束标记。可能是 [DONE]，也可能是被 JSON 编码过一层的 "[DONE]" */
const DONE_SENTINEL = '[DONE]'

const isDoneSentinel = (data) =>
  String(data ?? '')
    .trim()
    .replace(/^"|"$/g, '') === DONE_SENTINEL

/**
 * 增量正文可能被包成 JSON 信封（{"delta":"..."}，或 OpenAI 风格的
 * choices[0].delta.content），也可能就是裸文本 —— 两种都要能吃下。
 *
 * 认不出来的 JSON 一律返回 ''（当它不是正文），**绝不把 JSON 原文糊进答案**：
 * 上游开始推 {"sources":[...]} 这类元数据时，用户看到的应该是什么都不多，
 * 而不是屏幕上一串花括号。
 */
const TEXT_KEYS = ['delta', 'content', 'text', 'answer', 'token', 'message', 'response', 'reply']

export function parseChatChunk(raw) {
  const text = String(raw ?? '')
  const trimmed = text.trim()
  if (!trimmed) return ''
  if (trimmed[0] !== '{') return text // 裸文本，最常见的一条路

  try {
    const obj = JSON.parse(trimmed)
    if (!obj || typeof obj !== 'object') return ''

    for (const key of TEXT_KEYS) {
      if (typeof obj[key] === 'string') return obj[key]
    }
    const nested = obj.choices?.[0]?.delta?.content ?? obj.choices?.[0]?.message?.content
    return typeof nested === 'string' ? nested : ''
  } catch {
    // 不是完整 JSON（可能被代理截断），当裸文本
    return text
  }
}

// SSE 事件之间用空行分隔。\r\n 和 \n 都会出现 —— 中间换过一手代理就会混
const FRAME_SEP = /\r?\n\r?\n/

/**
 * SSE 分帧器。
 *
 * 为什么手写而不用浏览器的 EventSource：EventSource 只支持 GET，
 * 既不能带自定义请求头、也不能发 JSON 请求体，而问答接口是 POST。
 * 所以只能 fetch + ReadableStream 手动分帧，这是唯一的办法。
 */
export function createSseParser() {
  let buffer = ''
  // 「悬空行」：不是 data:、不是已知字段、且当前帧里还没有出现过 data:。
  // 正常协议下不该存在，出现了就说明**有非 SSE 的内容被写进了这个流**，
  // 见 chatStream 里对 stray 的说明。
  const stray = []

  function frameData(frame) {
    const dataLines = []
    let sawData = false

    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith(':')) continue // 注释/心跳，规范里就是靠它保活的
      if (line.startsWith('data:')) {
        // 规范：data: 后面的第一个空格是分隔符，不算内容
        dataLines.push(line.slice(5).replace(/^ /, ''))
        sawData = true
        continue
      }
      if (/^(event|id|retry):/.test(line)) continue // 用不上

      if (sawData) {
        // ⚠️ 本文件最不显然的一行。Java 侧 Spring 的 SseEmitter 写 data: 时
        // **不转义内容里的换行**（这是它的已知问题）：一个含 \n 的文本块会被
        // 写成
        //     data:第一行
        //     第二行
        // 而不是规范要求的 `data:第一行\ndata:第二行`。
        // 所以「data: 之后、空行之前的裸行」其实是正文的一部分，必须补回去 ——
        // 否则 markdown 回答会只剩第一行。
        dataLines.push(line)
      } else {
        stray.push(line)
      }
    }

    return dataLines.length ? dataLines.join('\n') : null
  }

  return {
    /** 喂一段解码后的文本，返回本次凑齐的完整事件（每个事件是它的 data 内容） */
    push(text) {
      buffer += text
      const out = []
      // 非全局正则：每次都从头找，避免 lastIndex 状态在多次调用间泄漏
      let m
      while ((m = FRAME_SEP.exec(buffer)) !== null) {
        const frame = buffer.slice(0, m.index)
        buffer = buffer.slice(m.index + m[0].length)
        const data = frameData(frame)
        if (data !== null) out.push(data)
      }
      return out
    },

    /** 流结束时调用：最后一个事件可能没以空行收尾（很多实现不发尾随空行） */
    flush() {
      const rest = buffer.trim()
      buffer = ''
      if (!rest) return []
      const data = frameData(rest)
      return data === null ? [] : [data]
    },

    /** 被丢弃的那些行。流异常结束时用它捞错误信息 */
    strayText() {
      return stray.join('\n').trim()
    },
  }
}

/**
 * 请求体用 snake_case，和后端 AiChatReq 上的 @JsonProperty 对齐。
 */
function buildChatBody({ userId, sessionId, message, topK }) {
  const body = { message, session_id: sessionId }

  // user_id 是可空的 Integer：null 时**干脆不发这个字段**。
  // 发 null 的话 FastAPI 收到的是 None，若它声明成 int 而不是 Optional[int]
  // 会直接 422，而那条报错文案会把人带到完全错误的方向。
  if (userId != null) body.user_id = userId

  // top_k 必须发。后端 AiChatReq.topK 是基本类型 int，字段缺失时 Jackson 留 0，
  // 上游拿到 top_k=0 很可能检索不到任何东西 —— 这种「空答案」最难查
  if (topK != null) body.top_k = topK

  return body
}

/**
 * 把错误响应读成 JSON 或文本，交给 toHttpError 统一翻译。
 *
 * 导出给 api/agent.js 复用：agent 那条路同样走 fetch + ReadableStream，
 * 错误体只有这一种读法。
 */
export async function readErrorBody(res) {
  try {
    const raw = await res.text()
    if (!raw) return null
    try {
      return JSON.parse(raw) // 后端/上游自己的 {"detail": ...}
    } catch {
      return raw // 代理给的 HTML 或纯文本
    }
  } catch {
    return null
  }
}

/**
 * 把「悬空内容」翻译成一句人话。
 *
 * 两个坑叠在一起：extractDetail 是按 axios 的形态设计的 —— axios 已经把
 * JSON 响应体解析成对象了，所以它只把**字符串**当成「不是 JSON 的兜底」。
 * 而这里拿到的是流里读出来的原始字节，`{"detail":"..."}` 到这儿还是一根字符串，
 * 直接丢给 extractDetail 会原样返回，用户就看到一串带花括号和转义的原文。
 *
 * 所以先按 JSON 解一次。解析失败（被截断、或者是代理生成的 HTML）再退回纯文本。
 */
export function strayDetail(stray) {
  if (!stray) return ''
  let detail = ''
  try {
    detail = extractDetail(JSON.parse(stray))
  } catch {
    detail = extractDetail(stray)
  }
  // 兜底不能放在调用方：调用方只能看到「有没有 detail」，
  // 分不清「没有悬空内容」和「有悬空内容但翻译不出人话」，前者不该报错
  return detail || '生成中断（服务端提前关闭了连接）'
}

/**
 * AI 问答（SSE 流式）。
 *
 * 为什么绕开 axios：浏览器的 XHR 没有流式读接口，axios 的
 * onDownloadProgress 给的是「累积到目前的整段文本」，拿它硬拼增量既笨又容易错。
 * SSE 只能用 fetch + ReadableStream 手动读。代价是错误翻译要自己做一遍，
 * 所以上面复用了 http.js 导出的 toHttpError / canceledError，保证两条路的
 * 文案完全一致。
 *
 * 返回值 { text, sawDone, errorDetail }：
 *   - 每段增量已经通过 onDelta 推给调用方了，text 是全文（留作兜底）
 *   - errorDetail 非空 = 这个流没有正常结束，值是**已经翻译好的一句人话**。
 *     它来自「悬空内容」：上游失败时 Java 侧的响应已经提交成 200，状态码改不了了，
 *     Spring 只能把错误 JSON 直接写进流里。调用方据此把这条消息标成失败。
 *     翻译放在这里而不是调用方，是为了和 toHttpError 走同一套文案规则
 *
 * 中止（点「停止生成」）靠 signal —— fetch 原生支持 AbortController。
 */
export async function chatStream(req, { signal, onDelta } = {}) {
  let res
  try {
    res = await fetch(CHAT_PATH, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        // 让任何一层中间代理都别缓冲这个响应，
        // 否则「流式」会变成「等全部生成完再一次性吐出来」
        'Cache-Control': 'no-cache',
      },
      body: JSON.stringify(buildChatBody(req)),
      signal,
    })
  } catch (err) {
    // fetch 只有两种情况会 reject：网络层失败（TypeError），或 signal 被 abort
    if (signal?.aborted) throw canceledError()
    throw toHttpError({ cause: err })
  }

  if (!res.ok) {
    // 开流之前就失败了（400 / 404 / 502…）。错误体还是 {"detail": ...}，
    // 但中间代理可能给的是 HTML 或空 body，所以先按文本读再翻译
    const rawBody = await readErrorBody(res)
    // ★ 这条分支专属于 SSE：JavaScript 的 Accept 是 text/event-stream，而接口的
    //   produces 也是 text/event-stream，于是 GlobalExceptionHandler 想返回
    //   ApiErrorResp 时**找不到能写这个对象的转换器** —— 结果 5xx 变成了
    //   「没有 Content-Type、body 为空」的裸响应（实测 500 ct=null body=""）。
    //   空 body 的 5xx 在 axios 那条路上等价于「Java 没起来」，所以 toHttpError
    //   会给出「请确认后端已在 8080 启动」。这句话在这里是**误导**：Java 明明活着，
    //   真相是它后面那一步炸了（最常见的是 FastAPI 没起）。所以这里单独改写文案。
    if (!rawBody && res.status >= 500) {
      throw toHttpError({
        status: res.status,
        rawBody: `后端返回了 ${res.status} 但没带错误详情。可能是 Java 没启动，也可能是它调用上游 RAG 服务失败（FastAPI 在 8102 吗？）`,
      })
    }
    throw toHttpError({ status: res.status, rawBody })
  }

  if (!res.body) {
    // 极端情况：fetch 被 polyfill 掉，没有流式 body
    throw toHttpError({ status: res.status, rawBody: '当前浏览器不支持流式响应（response.body 为空）' })
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = createSseParser()
  let text = ''
  let sawDone = false

  const consume = (frames) => {
    // 已经见过结束标记：后面剩下的字节一律不当正文（很可能就是错误体的残渣）
    if (sawDone) return true
    for (const data of frames) {
      if (isDoneSentinel(data)) {
        sawDone = true
        return true // 上游明确说结束了，后面就算还有字节也不再当正文
      }
      const piece = parseChatChunk(data)
      if (!piece) continue
      text += piece
      onDelta?.(piece)
    }
    return false
  }

  try {
    read: for (;;) {
      const { value, done } = await reader.read()
      if (done) break

      // stream: true 不能省 —— 一个 UTF-8 汉字占 3 字节，很可能正好被切在
      // 两个 chunk 之间。不加这句会把半个字符解码成「」，而且越界还会把
      // 下一个字符也带歪
      if (consume(parser.push(decoder.decode(value, { stream: true })))) break read
    }

    // 收尾：不带 stream 再 decode 一次，把解码器里可能残留的半截字符吐出来
    consume([...parser.push(decoder.decode()), ...parser.flush()])
  } catch (err) {
    if (signal?.aborted) {
      // 中止时缓冲区里那半个没凑齐的帧**故意丢弃**，不 flush：
      // 它最多是最后一个增量（用户已经按了停止，本来就不想要了），
      // 但它也可能是半截 JSON 信封 —— 补进答案里会变成一串花括号，比少一个字难看得多
      throw canceledError()
    }
    // 读到一半连接断了。**已收到的内容不回滚** —— 由调用方决定保留还是丢弃
    throw toHttpError({ rawBody: '连接中断，回答可能不完整' })
  } finally {
    // 不管正常结束、提前 [DONE] 还是出错，都主动取消读端，
    // 万一上游还在生成，别让它继续白算
    reader.cancel().catch(() => {})
  }

  return { text, sawDone, errorDetail: strayDetail(parser.strayText()) }
}
