import { agentChatStream, confirmAgent } from '../api/agent.js'

/**
 * Agent 问答的会话状态。和 chatStore 同一套「模块级快照 + 订阅」的写法，
 * 但多了一维：助手消息不止有正文，还有**决策轨迹**（思考 + 工具调用）。
 *
 * 为什么轨迹按消息存、不单独存一个全局列表：
 *   一次问答 = 一条可回看的记录。用户往上翻历史时，那一步调用的是
 *   `kb_search` 还是 `kb_delete`、参数是什么、失败没有，必须跟着那条回答走。
 *
 * 状态形状：
 *   {
 *     sessionId, streaming,
 *     messages: [{
 *       id, role, text, status, error, createdAt,
 *       runId,                                  // 上游一次 run 的 id
 *       steps: [thought | tool],                // 时间序
 *       pendingConfirm: { token, name, args, callId, expiresAt } | null,
 *     }],
 *   }
 */

/** 和 Java / Python 两侧的上限一致；导出给页面做提示 */
export const MAX_MESSAGE_CHARS = 20_000
const MAX_MESSAGES = 200

export const MSG = {
  STREAMING: 'streaming',
  DONE: 'done',
  CANCELED: 'canceled',
  INTERRUPTED: 'interrupted',
  ERROR: 'error',
}

/** Python/Java 两侧的 session_id 都只允许 [A-Za-z0-9_-]{1,128} */
function newSessionId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

let seq = 0
const nextId = () => `a${Date.now().toString(36)}${(seq++).toString(36)}`

let snapshot = {
  sessionId: newSessionId(),
  messages: [],
  streaming: false,
}

const listeners = new Set()

/** 当前在途的流：{ controller, messageId } */
let stream = null

function emit() {
  // 换新引用，useSyncExternalStore 才认为变了
  snapshot = { ...snapshot }
  listeners.forEach((l) => l())
}

export function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getSnapshot() {
  return snapshot
}

// ------------------------------------------------------------------ 状态写入

function updateMessage(messageId, updater) {
  let changed = false
  const messages = snapshot.messages.map((m) => {
    if (m.id !== messageId) return m
    const next = updater(m)
    if (next === m) return m
    changed = true
    return next
  })
  if (!changed) return
  snapshot = { ...snapshot, messages }
  emit()
}

/**
 * 找到 tool_result 对应的 tool_call。
 *
 * 正常情况下 call_id 足够。兜底分支（同名的最后一条 running）是为了
 * 上游事件字段缺一个时轨迹不至于整段丢 —— 它只影响展示，不影响回答。
 */
function findToolIndex(steps, event) {
  if (event.call_id) {
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i].kind === 'tool' && steps[i].callId === event.call_id) return i
    }
  }
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].kind === 'tool' && steps[i].status === 'running'
        && (!event.name || steps[i].name === event.name)) {
      return i
    }
  }
  return -1
}

function clearConfirm(messageId, callId) {
  updateMessage(messageId, (m) => {
    if (!m.pendingConfirm) return m
    if (callId && m.pendingConfirm.callId !== callId) return m
    return { ...m, pendingConfirm: null }
  })
}

/**
 * 把一个上游事件落到消息上。**只做展示层的归纳，不做协议改写**：
 * Java 已经把每一行原样转发了，这里看不懂的类型就忽略。
 */
function applyEvent(messageId, event) {
  switch (event.type) {
    case 'start':
      updateMessage(messageId, (m) => (m.runId === event.run_id ? m : { ...m, runId: event.run_id || '' }))
      break

    case 'answer':
      if (typeof event.delta === 'string' && event.delta) {
        updateMessage(messageId, (m) => ({ ...m, text: m.text + event.delta }))
      }
      break

    case 'thought':
      if (event.text) {
        updateMessage(messageId, (m) => ({
          ...m,
          steps: [
            ...m.steps,
            {
              key: `t${m.steps.length}-${event.step ?? 0}`,
              kind: 'thought',
              step: event.step ?? null,
              text: event.text,
            },
          ],
        }))
      }
      break

    case 'tool_call':
      updateMessage(messageId, (m) => ({
        ...m,
        steps: [
          ...m.steps,
          {
            key: `c${m.steps.length}-${event.call_id ?? ''}`,
            kind: 'tool',
            step: event.step ?? null,
            callId: event.call_id ?? null,
            name: event.name ?? '',
            args: event.args ?? {},
            startedAt: typeof event.ts === 'number' ? event.ts : null,
            elapsedMs: null,
            ok: null,
            status: 'running',
            summary: '',
            errorCode: '',
          },
        ],
      }))
      break

    case 'tool_result':
      updateMessage(messageId, (m) => {
        const idx = findToolIndex(m.steps, event)
        if (idx < 0) return m

        const step = m.steps[idx]
        const ok = event.ok !== false
        // 优先用 Python 实测的 elapsed_ms：它不含网络分帧抖动，也不受两侧时钟偏差影响。
        // 老版本上游没有该字段时才回退到 ts 相减。
        const elapsedMs =
          typeof event.elapsed_ms === 'number' && Number.isFinite(event.elapsed_ms)
            ? Math.max(0, event.elapsed_ms)
            : step.startedAt != null && typeof event.ts === 'number'
              ? Math.max(0, event.ts - step.startedAt)
              : null

        const steps = m.steps.slice()
        steps[idx] = {
          ...step,
          ok,
          status: ok ? 'ok' : 'failed',
          elapsedMs,
          summary: typeof event.summary === 'string' ? event.summary : '',
          errorCode: event.error ?? '',
        }

        // 确认请求已经得到结果（批准、拒绝或超时），弹窗不该再留着
        const pendingConfirm =
          m.pendingConfirm && (!event.call_id || m.pendingConfirm.callId === event.call_id)
            ? null
            : m.pendingConfirm

        return { ...m, steps, pendingConfirm }
      })
      break

    case 'confirm_required':
      updateMessage(messageId, (m) => ({
        ...m,
        pendingConfirm: {
          token: event.token,
          name: event.name ?? '',
          args: event.args ?? {},
          callId: event.call_id ?? null,
          // Python 发的是 epoch 秒（float）；统一换成毫秒给 UI
          expiresAt: typeof event.expires_at === 'number' ? Math.round(event.expires_at * 1000) : null,
        },
      }))
      break

    case 'error':
      // 先把上游的说明挂出来；终态是中断还是失败由 finish 再定
      updateMessage(messageId, (m) => ({
        ...m,
        error: event.message || event.code || '上游 agent 出错',
      }))
      break

    case 'done':
      // 终态在 runStream 里统一处理
      break

    default:
      // 协议扩张后的新事件：Java 已经原样转发了，前端先忽略，不崩溃
      break
  }
}

// ------------------------------------------------------------------ 流生命周期

function finalize(messageId, patch, controller) {
  // 旧流的回落不能覆盖「重新开始」之后的新状态
  if (stream?.controller !== controller) return
  stream = null
  snapshot = {
    ...snapshot,
    streaming: false,
    messages: snapshot.messages.map((m) => (m.id === messageId ? { ...m, ...patch } : m)),
  }
  emit()
}

function fail(messageId, controller, detail) {
  const msg = snapshot.messages.find((m) => m.id === messageId)
  finalize(
    messageId,
    {
      // 有正文就保留 —— 半截回答往往仍然有用，和旧链路同一条规则
      status: msg?.text ? MSG.INTERRUPTED : MSG.ERROR,
      error: detail,
      pendingConfirm: null,
    },
    controller,
  )
}

async function runStream(messageId, message, controller) {
  let sawDone = false
  let upstreamError = ''

  try {
    const result = await agentChatStream(
      { sessionId: snapshot.sessionId, message },
      {
        signal: controller.signal,
        onEvent: (event) => {
          if (event.type === 'done') sawDone = true
          if (event.type === 'error') {
            upstreamError = event.message || event.code || '上游 agent 出错'
          }
          applyEvent(messageId, event)
        },
      },
    )
    sawDone = sawDone || result.sawDone

    if (sawDone) {
      finalize(messageId, { status: MSG.DONE, error: '', pendingConfirm: null }, controller)
      return
    }

    // ★ HTTP 200 / 流正常关闭 都不等于成功：
    //   Python 的 BUSY / MODEL_ERROR / MAX_STEPS 是「发一个 error 再 return」
    fail(messageId, controller, upstreamError || '上游在没有发送 done 事件的情况下结束了本次回答')
  } catch (err) {
    if (err?.isCanceled) {
      finalize(messageId, { status: MSG.CANCELED, error: '', pendingConfirm: null }, controller)
      return
    }
    fail(messageId, controller, err?.message || 'Agent 问答失败')
  }
}

// ------------------------------------------------------------------ 对外 API

/** @returns {'ok'|'empty'|'busy'|'too_long'} 页面据此给出提示 */
export function sendMessage(rawText) {
  const message = (rawText ?? '').trim()
  if (!message) return 'empty'
  if (snapshot.streaming) return 'busy'
  // Java/Python 两侧都限制 20000 字符；前端先挡一次，避免发出去再收一个 400。
  // 用展开计数而不是 .length：JS 的 length 数的是 UTF-16 码元，emoji 会算成 2。
  if ([...message].length > MAX_MESSAGE_CHARS) return 'too_long'

  const now = Date.now()
  const userMsg = {
    id: nextId(),
    role: 'user',
    text: message,
    status: MSG.DONE,
    error: '',
    createdAt: now,
    runId: '',
    steps: [],
    pendingConfirm: null,
  }
  const aiMsg = {
    id: nextId(),
    role: 'assistant',
    text: '',
    status: MSG.STREAMING,
    error: '',
    createdAt: now,
    runId: '',
    steps: [],
    pendingConfirm: null,
  }

  const controller = new AbortController()
  stream = { controller, messageId: aiMsg.id }

  snapshot = {
    ...snapshot,
    streaming: true,
    messages: [...snapshot.messages, userMsg, aiMsg].slice(-MAX_MESSAGES),
  }
  emit()

  runStream(aiMsg.id, message, controller)
  return 'ok'
}

/**
 * 批准 / 拒绝一次写操作。
 *
 * 流不会因为确认而中断：Python 在 confirm.wait() 里阻塞，tool_result 和后续
 * 回答还会从同一条流里继续过来。所以这里只发确认请求，**不要**去动 streaming。
 */
export async function respondConfirm(approved) {
  const msg = snapshot.messages.find((m) => m.pendingConfirm)
  if (!msg) return
  const pending = msg.pendingConfirm

  try {
    await confirmAgent({ token: pending.token, approved })
    clearConfirm(msg.id, pending.callId)
  } catch (err) {
    // 404 = token 过期或已经消费过。上游那边其实已经继续走了，
    // 留着弹窗只会让用户一直点一个永远失败的按钮。
    if (err?.status === 404) clearConfirm(msg.id, pending.callId)
    throw err
  }
}

export function stopStreaming() {
  const controller = stream?.controller
  if (!controller) return
  // 不在这里改状态：abort 异步生效，由 runStream 的 catch 统一收尾
  controller.abort()
}

export function resetConversation() {
  stopStreaming()
  snapshot = { ...snapshot, sessionId: newSessionId(), messages: [], streaming: false }
  emit()
}
