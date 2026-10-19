import { API_PREFIX, canceledError, http, toHttpError } from './http.js'
import { createSseParser, readErrorBody, strayDetail } from './rag.js'

/**
 * Agent 问答链路：Java 把 Python 的 NDJSON 行原样放进 SSE 的 data: 帧，
 * 所以前端收到的每一帧**就是一个完整的事件 JSON**，不需要再猜信封。
 *
 * 和旧 /api/rag/chat 的区别只在这里：
 *   旧链路  data: {"delta":"..."} / 裸文本 / [DONE]
 *   新链路  data: {"v":1,"type":"thought",...}
 * 分帧、半汉字、fetch 取消、错误翻译全部复用 rag.js —— 两条路只在
 * 「帧内容怎么解释」上不同，读流的代码不该有两份。
 */

const AGENT_CHAT_PATH = `${API_PREFIX}/rag/agent/chat`
const AGENT_CONFIRM_PATH = `${API_PREFIX}/rag/agent/confirm`

/**
 * 发起一次 agent 问答。
 *
 * @param {{sessionId: string, message: string}} req
 * @param {{signal?: AbortSignal, onEvent?: (event: object) => void}} options
 * @returns {Promise<{sawDone: boolean, lastType: string|null}>}
 *   sawDone 只是给调用方的一个便利标记。**它不代表成功**：
 *   Python 的 BUSY / MODEL_ERROR / MAX_STEPS 都是先发 error 事件再正常关流。
 *   终态判定见 agentStore（必须同时看 error 事件）。
 */
export async function agentChatStream(req, { signal, onEvent } = {}) {
  let res
  try {
    res = await fetch(AGENT_CHAT_PATH, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        // 防中间代理把流缓冲成「全部生成完再一次性吐出来」
        'Cache-Control': 'no-cache',
      },
      // Java 的 AgentChatReq 只认 session_id / message：
      // user_id 由 BFF 从服务端身份注入（当前固定 1），前端传了也会被忽略，
      // 但**不发**才是对契约的准确表达。
      body: JSON.stringify({ session_id: req.sessionId, message: req.message }),
      signal,
    })
  } catch (err) {
    if (signal?.aborted) throw canceledError()
    throw toHttpError({ cause: err })
  }

  if (!res.ok) {
    const rawBody = await readErrorBody(res)
    // 和 chatStream 同一条已知坑：produces=text/event-stream 时
    // GlobalExceptionHandler 的 JSON 体可能被协商掉，留下一个空 body 的 5xx。
    // 这时「请确认后端已启动」是误导 —— Java 可能活着，是上游（8102）没起。
    if (!rawBody && res.status >= 500) {
      throw toHttpError({
        status: res.status,
        rawBody: `后端返回了 ${res.status} 但没带错误详情。可能是 Java 没启动，也可能是它调用上游 RAG 服务失败（FastAPI 在 8102 吗？）`,
      })
    }
    throw toHttpError({ status: res.status, rawBody })
  }

  if (!res.body) {
    throw toHttpError({ status: res.status, rawBody: '当前浏览器不支持流式响应（response.body 为空）' })
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = createSseParser()
  let sawDone = false
  let lastType = null

  const consume = (frames) => {
    for (const data of frames) {
      const text = data.trim()
      if (!text) continue

      let event
      try {
        event = JSON.parse(text)
      } catch {
        // 正常流里不会出现：Java 转发的是 Python 已经序列化好的一行。
        // 出现了大概率是异常时被写进流的错误正文 —— 丢弃，别让它变成一条假消息。
        continue
      }
      if (!event || typeof event.type !== 'string') continue

      lastType = event.type
      if (event.type === 'done') sawDone = true
      onEvent?.(event)
    }
  }

  try {
    read: for (;;) {
      const { value, done } = await reader.read()
      if (done) break

      // stream:true —— 一个汉字 3 字节，可能正好跨两个 chunk
      consume(parser.push(decoder.decode(value, { stream: true })))
    }
    consume([...parser.push(decoder.decode()), ...parser.flush()])
  } catch (err) {
    if (signal?.aborted) throw canceledError()
    throw toHttpError({ rawBody: '连接中断，回答可能不完整' })
  } finally {
    reader.cancel().catch(() => {})
  }

  // 流正常关闭但一个事件都没解析出来、却有非 SSE 的悬空内容：
  // 响应已经提交成 200，Spring 只能把错误正文直接写进流。
  // 翻译成人话抛给调用方，比让 store 去猜「为什么什么都没收到」准确。
  const stray = parser.strayText()
  if (!lastType && stray) {
    throw toHttpError({ rawBody: strayDetail(stray) })
  }

  return { sawDone, lastType }
}

/** 人工确认写操作。404 = token 不存在或已过期。 */
export function confirmAgent({ token, approved }) {
  return http.post('/rag/agent/confirm', { token, approved }).then((res) => res.data)
}

/**
 * 只暂存文件，不入库。返回 { stagedId, fileName, size }。
 *
 * 文件字节直接走 Java → Python，不经过模型；上传成功后 Agent 页会把
 * 草稿填成「请把「xxx」入库」，由用户决定是否真的开始入库。
 */
export function stageAgentFile(file, { signal, onUploadProgress } = {}) {
  const form = new FormData()
  // 字段名必须和后端 @RequestPart("file") / FastAPI UploadFile 的参数名一致
  form.append('file', file, file.name)

  return http
    .post('/rag/agent/stage', form, {
      signal,
      timeout: 0,
      // 不手动设 Content-Type：让浏览器带上 multipart 的 boundary
      onUploadProgress,
    })
    .then((res) => {
      const raw = res.data || {}
      const stagedId = raw.staged_id ?? raw.stagedId
      if (!stagedId) {
        throw Object.assign(new Error('暂存接口未返回 staged_id，请检查后端序列化配置'), {
          status: 200,
        })
      }
      return {
        stagedId: String(stagedId),
        fileName: raw.file_name ?? raw.fileName ?? file.name,
        size: typeof raw.size === 'number' ? raw.size : file.size,
      }
    })
}
