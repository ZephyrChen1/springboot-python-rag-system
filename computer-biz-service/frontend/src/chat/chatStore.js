import { chatStream } from '../api/rag.js'

/**
 * 问答会话的唯一数据源。和 ingestStore 是同一个套路：状态活在模块里，
 * 不活在组件里。
 *
 * 但这里**故意没有** ingestStore 那一整套调度（上传队列、串行闸门、轮询定时器）：
 * 一次问答就是「一条请求 + 一条流」，不排队、不受并发上限约束、不需要轮询。
 * 留在模块级只因为同一条理由 —— 流的生命周期属于这个标签页，不属于某个组件：
 * 用 Tabs 切走再切回来、或者被 StrictMode 卸载再挂载，流都不该断。
 *
 * 组件侧通过 useSyncExternalStore 订阅（见 hooks/useChat.js）。
 */

/** 每次问答带上的检索条数。后端 AiChatReq.topK 是基本类型 int，必须给值 */
const DEFAULT_TOP_K = 5

/**
 * user_id：后端 AiChatReq 里有这个字段，但本服务目前没有登录体系，所以默认不发。
 * 上游若把它定义成必填，会在界面上回一条 422 "user_id: Field required" ——
 * 那时把这里填成一个真实用户 id 即可，请求体会自动带上（见 api/rag.js 的 buildChatBody）。
 */
const USER_ID = null

/** 一个会话最多留多少条消息。长会话无限增长会把内存和 DOM 一起拖死 */
const MAX_MESSAGES = 200

export const MSG = {
  STREAMING: 'streaming', // 正在逐段生成
  DONE: 'done', // 正常结束
  CANCELED: 'canceled', // 用户点了「停止生成」，已收到的内容保留
  INTERRUPTED: 'interrupted', // 流没正常结束，但已经收到一部分（上游报错 / 连接被掐）
  ERROR: 'error', // 一个字都没收到就失败了
}

/**
 * 后端的 session_id 决定服务端记住的上下文，所以它是「一次会话」的身份，
 * 由前端生成、跟着每条消息发回去。
 */
function newSessionId() {
  // crypto.randomUUID 只在安全上下文（https / localhost）里可用 ——
  // 用局域网 IP 打开页面时它可能不存在，必须兜底
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

// ------------------------------------------------------------------ 内部状态

let seq = 0
const nextId = () => `m${Date.now().toString(36)}${(seq++).toString(36)}`

let snapshot = {
  sessionId: newSessionId(),
  messages: [], // { id, role, text, status, error, createdAt }
  streaming: false,
  topK: DEFAULT_TOP_K,
}

const listeners = new Set()

/** 当前在途的那条流：{ controller, messageId }。同一时刻只会有一条 */
let stream = null

function emit() {
  // 每次换新的对象引用，useSyncExternalStore 才会认为状态变了
  snapshot = { ...snapshot }
  listeners.forEach((l) => l())
}

function setState(patchObj) {
  snapshot = { ...snapshot, ...patchObj }
  emit()
}

export function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getSnapshot() {
  return snapshot
}

// ------------------------------------------------------------------ 流式写入

/** 把一段增量拼到指定的助手消息上 */
function appendDelta(messageId, piece) {
  snapshot = {
    ...snapshot,
    messages: snapshot.messages.map((m) => (m.id === messageId ? { ...m, text: m.text + piece } : m)),
  }
  emit()
}

/**
 * 收尾。必须先确认 controller 还是当前那条流 ——
 * 用户点了「重新开始」之后，旧流的回落不能反过来覆盖新状态。
 */
function finalize(messageId, patchObj, controller) {
  if (stream?.controller !== controller) return
  stream = null
  snapshot = {
    ...snapshot,
    streaming: false,
    messages: snapshot.messages.map((m) => (m.id === messageId ? { ...m, ...patchObj } : m)),
  }
  emit()
}

/**
 * 失败，但**不丢已经收到的内容**：有正文就是「回答中断」（那半截答案往往仍然有用），
 * 一个字都没有才算「失败」。和 ingestStore 里「轮询失败 ≠ 入库失败」是同一条原则 ——
 * 把已经拿到的信息扔掉，用户只会重新问一遍。
 */
function fail(messageId, controller, detail) {
  const msg = snapshot.messages.find((m) => m.id === messageId)
  finalize(
    messageId,
    { status: msg?.text ? MSG.INTERRUPTED : MSG.ERROR, error: detail },
    controller,
  )
}

async function runStream(messageId, message, controller) {
  try {
    const { errorDetail } = await chatStream(
      { userId: USER_ID, sessionId: snapshot.sessionId, message, topK: snapshot.topK },
      {
        signal: controller.signal,
        // 每个增量直接落状态，不做 rAF 节流：一次 emit 只触发一次 React 重渲染，
        // 而 delta 的到达频率（上游逐 token 生成 + 网络往返）远低于 60fps，
        // 节流只会让画面看起来更卡
        onDelta: (piece) => appendDelta(messageId, piece),
      },
    )

    if (errorDetail) {
      // 流没有正常结束（上游报错时 Spring 把错误 JSON 直接写进了流里，
      // 见 api/rag.js 的 strayDetail）。errorDetail 已经是翻译好的一句人话
      fail(messageId, controller, errorDetail)
      return
    }

    finalize(messageId, { status: MSG.DONE, error: '' }, controller)
  } catch (err) {
    // 用户点的「停止生成」不是错误
    if (err?.isCanceled) {
      finalize(messageId, { status: MSG.CANCELED }, controller)
      return
    }
    fail(messageId, controller, err?.message || 'AI 问答失败')
  }
}

// ------------------------------------------------------------------ 对外 API

export function sendMessage(rawText) {
  const message = (rawText ?? '').trim()
  if (!message) return

  // 生成中不让再发。UI 上这时发送按钮已经换成「停止生成」，这里是兜底
  if (snapshot.streaming) return

  const now = Date.now()
  const userMsg = {
    id: nextId(),
    role: 'user',
    text: message,
    status: MSG.DONE,
    error: '',
    createdAt: now,
  }
  // 助手消息先建出来再开流 —— 增量是往这条消息上追加的，
  // 不能等流结束才建（否则用户会盯着一个没反应的界面）
  const aiMsg = {
    id: nextId(),
    role: 'assistant',
    text: '',
    status: MSG.STREAMING,
    error: '',
    createdAt: now,
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
}

export function stopStreaming() {
  const controller = stream?.controller
  if (!controller) return
  // 这里**不**改状态：abort 会让 chatStream 抛出 isCanceled，由 runStream 的
  // catch 统一收尾。两处都写状态迟早会打架，而且 abort 是异步生效的
  controller.abort()
}

export function resetConversation() {
  // 先掐掉在途的流，否则它的增量会落到已经清空的列表上
  stopStreaming()
  setState({ sessionId: newSessionId(), messages: [], streaming: false })
}

// 这里刻意**没有** ingestStore 那样的 pagehide 清理：那个是为了掐掉在途的
// 大文件上传，而问答流在页面卸载时浏览器自己会断，多一个监听只会在
// bfcache 往返（前进/后退）时制造意外。
