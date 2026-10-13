import { queryTask, uploadDocument } from '../api/rag.js'

/**
 * 入库任务的唯一数据源。
 *
 * 为什么把状态和定时器放在 React 之外（模块级单例），而不是塞进 hook：
 *
 * 1. 轮询的生命周期天然属于「这个浏览器标签页」，不属于某个 React 组件。
 * 2. React 18 的 <StrictMode> 在开发模式下会刻意执行「挂载 → 卸载 → 再挂载」
 *    来暴露副作用 bug。如果轮询由一个 useEffect 启动，你会经历
 *    「effect 启动 → cleanup 停掉 → effect 再启动」，写错一点就会漏掉轮询。
 *    放在模块里则完全不受影响。
 * 3. 组件卸载不产生任何副作用 —— 「停止」从「必须记得做的事」变成了「不需要做的事」。
 *
 * 组件侧通过 useSyncExternalStore 订阅（见 hooks/useIngestTasks.js）。
 */

const MAX_POLL_ERRORS = 5
const MAX_POLL_MS = 30 * 60 * 1000

/**
 * 上游（FastAPI）的硬限制，已实测确认：
 *
 *   1. 同一时刻只允许一个入库任务在跑 —— 第二个文件会被拒绝，返回
 *      {"detail":"已有入库任务正在处理：「xxx.txt」，请稍后再试"}
 *   2. 只保留「最近一个」job —— 新任务一提交，旧 job_id 立刻变 404
 *      （{"detail":"任务不存在(仅保留最近一个任务)"}）
 *
 * 所以上传必须**完全串行**：上一个任务到达终态之后，才允许开始下一个。
 * 不能并发，也不能「先把文件都传上去再慢慢轮询」—— 那样只有最后一个能被追踪。
 */
const NEXT_UPLOAD_DELAY_MS = 800

/** 上游忙碌时的报错文案特征。正常串行下不该出现，作为竞态兜底 */
const BUSY_PATTERN = /已有入库任务/
const BUSY_RETRY_DELAY_MS = 2000

export const STATUS = {
  QUEUED: 'queued', // 已入队，等并发槽位
  UPLOADING: 'uploading', // 阶段 A：文件字节在网线上
  COMMITTING: 'committing', // 阶段 A 完成、POST 还没返回（服务端受理中）
  INGESTING: 'ingesting', // 阶段 B：拿到 job_id，轮询中
  SUCCESS: 'success', // result 以 [成功] 开头
  SKIPPED: 'skipped', // result 以 [跳过] 开头
  FAILED: 'failed', // result 以 [失败] 开头 ← 只有服务端能给出这个状态
  UNKNOWN: 'unknown', // 重试耗尽 / 404 / 超时 / 上传中断 —— 绝不等于「入库失败」
}

const TERMINAL = new Set([STATUS.SUCCESS, STATUS.SKIPPED, STATUS.FAILED, STATUS.UNKNOWN])
export const isTerminal = (s) => TERMINAL.has(s)

/**
 * 「正在跑」的状态 —— 串行闸门看的是这个，不是「未终态」。
 * QUEUED 是排队等待，不算在跑，否则排队中的任务会把自己堵死。
 */
const ACTIVE = new Set([STATUS.UPLOADING, STATUS.COMMITTING, STATUS.INGESTING])

/** 把服务端的 result 文案映射成状态。前缀是唯一的契约 */
export function classifyResult(result) {
  const r = typeof result === 'string' ? result.trim() : ''
  if (r.startsWith('[成功]')) return STATUS.SUCCESS
  if (r.startsWith('[跳过]')) return STATUS.SKIPPED
  if (r.startsWith('[失败]')) return STATUS.FAILED
  return STATUS.UNKNOWN
}

// ------------------------------------------------------------------ 内部状态

let seq = 0
const nextId = () => `t${Date.now().toString(36)}${(seq++).toString(36)}`

let snapshot = { tasks: [] }
const listeners = new Set()

const runners = new Map() // taskId -> { timerId, controller, attempt, ticks }
const fileById = new Map() // taskId -> File。File 不可序列化，绝不能进 snapshot
const queue = [] // 待上传的 taskId
let activeUploads = 0

function emit() {
  // 每次换新的对象引用，useSyncExternalStore 才会认为状态变了
  snapshot = { tasks: snapshot.tasks }
  listeners.forEach((l) => l())
}

function setTasks(updater) {
  snapshot.tasks = updater(snapshot.tasks)
  emit()
}

function patch(id, patchObj) {
  let hit = false
  snapshot.tasks = snapshot.tasks.map((t) => {
    if (t.id !== id) return t
    hit = true
    return { ...t, ...patchObj }
  })
  if (hit) emit()
}

export function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getSnapshot() {
  return snapshot
}

// ------------------------------------------------------------------ 轮询调度

function schedule(id, delay) {
  const runner = runners.get(id)
  if (!runner) return
  clearTimeout(runner.timerId)
  runner.timerId = setTimeout(() => poll(id), delay)
}

function nextDelay(runner) {
  // 正常节奏：前 10 次 1s，之后 2s，30 次后 5s
  const base = runner.ticks < 10 ? 1000 : runner.ticks < 30 ? 2000 : 5000
  // 出错时指数退避，上限 15s
  const errBackoff = runner.attempt > 0 ? Math.min(15000, 1000 * 2 ** runner.attempt) : 0
  // 抖动是必需的：一次拖 10 个文件会在同一秒上传完，固定间隔会形成
  // 永久的锁步请求脉冲，全部同时打在 Java 上
  return Math.round(Math.max(base, errBackoff) * (0.85 + Math.random() * 0.3))
}

function stopRunner(id) {
  const runner = runners.get(id)
  if (!runner) return
  clearTimeout(runner.timerId)
  runner.controller?.abort()
  runners.delete(id)
}

// ------------------------------------------------------------------ 上传队列

/** 是否有任务正在跑（上传中/受理中/入库中） */
function hasActiveTask() {
  return snapshot.tasks.some((t) => ACTIVE.has(t.status))
}

/** 串行闸门：队列非空、且没有任务在跑时，才放出**一个**文件 */
function pump() {
  while (queue.length > 0 && !hasActiveTask()) {
    const id = queue.shift()
    const file = fileById.get(id)
    // 文件不可用（理论上不该发生），跳过继续看下一个
    if (!file) continue

    runUpload(id, file)
    return // 一次只放一个，后续由 onTaskSettled / 重试触发
  }
}

/** 任务到达终态后，略等一下再放下一个 */
function onTaskSettled() {
  setTimeout(pump, NEXT_UPLOAD_DELAY_MS)
}

async function runUpload(id, file) {
  const controller = new AbortController()
  runners.set(id, { timerId: null, controller, attempt: 0, ticks: 0 })
  patch(id, { status: STATUS.UPLOADING, percent: 0, error: '' })

  try {
    const resp = await uploadDocument(file, {
      signal: controller.signal,
      onUploadProgress: (percent) => {
        // 100% 只代表字节进了操作系统的 socket 缓冲区，服务端还没受理，
        // 所以这里切到 COMMITTING 而不是直接跳到「入库中」
        patch(id, {
          percent,
          status: percent >= 100 ? STATUS.COMMITTING : STATUS.UPLOADING,
        })
      },
    })

    patch(id, {
      jobId: resp.jobId,
      fileName: resp.fileName || file.name,
      status: STATUS.INGESTING,
      percent: 100,
      stage: '已提交，等待入库…',
      error: '',
    })
    poll(id)
  } catch (err) {
    if (err?.isCanceled) return

    // 上游忙碌：不是这个文件的问题，放回队首等前一个真正结束后重试。
    // 正常串行下不该出现，这是竞态兜底（上游可能还没把上个任务标记成结束）。
    if (BUSY_PATTERN.test(err?.message || '')) {
      runners.delete(id)
      patch(id, {
        status: STATUS.QUEUED,
        percent: 0,
        error: '上游正在处理上一个任务，排队等待…',
      })
      queue.unshift(id)
      setTimeout(pump, BUSY_RETRY_DELAY_MS)
      return
    }

    // 400/413/415/422 是「你传的东西不对」，重试没用，属于确定失败；
    // 其余（网络抖动 / 5xx）保留重试可能，标 UNKNOWN 而不是 FAILED
    const definitive = [400, 413, 415, 422].includes(err?.status)
    patch(id, {
      status: definitive ? STATUS.FAILED : STATUS.UNKNOWN,
      error: err?.message || '上传失败',
    })
    if (definitive) fileById.delete(id)
    onTaskSettled()
  }
}

// ------------------------------------------------------------------ 轮询

function poll(id) {
  const runner = runners.get(id)
  if (!runner) return

  const task = snapshot.tasks.find((t) => t.id === id)
  if (!task || !task.jobId || isTerminal(task.status)) return

  // 兜底：防止一个永远不 done 的任务让定时器跑一整天
  if (Date.now() - task.createdAt > MAX_POLL_MS) {
    stopRunner(id)
    patch(id, { status: STATUS.UNKNOWN, error: '轮询超时（超过 30 分钟），可手动重试' })
    onTaskSettled()
    return
  }

  const controller = new AbortController()
  runner.controller = controller

  queryTask(task.jobId, { signal: controller.signal })
    .then((data) => {
      if (!runners.has(id)) return
      runner.ticks += 1
      runner.attempt = 0

      const next = data.done ? classifyResult(data.result) : STATUS.INGESTING
      patch(id, {
        status: next,
        stage: data.stage,
        result: data.result,
        elapsedSec: data.elapsedSec ?? task.elapsedSec,
        elapsedAt: Date.now(), // 给 UI 的 1s 插值用
        error: '',
      })

      if (isTerminal(next)) {
        stopRunner(id)
        fileById.delete(id)
        onTaskSettled() // 放行队列里的下一个文件
        return // ← 先判断终态再决定是否调度，顺序不能反
      }
      schedule(id, nextDelay(runner))
    })
    .catch((err) => {
      if (err?.isCanceled || !runners.has(id)) return

      // 404 = job_id 在服务端已经不存在了。这是终态，重试没有意义
      if (err?.status === 404) {
        stopRunner(id)
        patch(id, {
          status: STATUS.UNKNOWN,
          error: '任务不存在或已失效（RAG 服务可能已重启）',
        })
        onTaskSettled()
        return
      }

      runner.attempt += 1
      if (runner.attempt >= MAX_POLL_ERRORS) {
        stopRunner(id)
        patch(id, {
          status: STATUS.UNKNOWN,
          error: `${err?.message || '查询失败'}（已重试 ${MAX_POLL_ERRORS} 次）`,
        })
        onTaskSettled()
        return
      }

      // 关键：状态保持 INGESTING，stage / elapsedSec 原样保留，只追加一行提示。
      // 绝不能因为一次 502 就把任务标成「入库失败」—— 上游可能只是在重启，
      // 前端此刻并不知道真实结果，误判会让用户重传导致同一文档入库两次。
      patch(id, {
        error: `查询失败，正在重试（${runner.attempt}/${MAX_POLL_ERRORS}）：${err?.message || ''}`,
      })
      schedule(id, nextDelay(runner))
    })
}

// ------------------------------------------------------------------ 对外 API

export function enqueueFiles(files) {
  const list = Array.from(files || [])
  if (list.length === 0) return

  const now = Date.now()
  const created = list.map((file) => ({
    id: nextId(),
    jobId: null,
    fileName: file.name,
    fileSize: file.size,
    status: STATUS.QUEUED,
    percent: 0,
    stage: '',
    result: '',
    elapsedSec: null,
    elapsedAt: null,
    error: '',
    createdAt: now,
  }))

  created.forEach((task, i) => {
    fileById.set(task.id, list[i])
    queue.push(task.id)
  })

  // 新任务放最上面
  setTasks((tasks) => [...created, ...tasks])
  pump()
}

export function retryTask(id) {
  const task = snapshot.tasks.find((t) => t.id === id)
  if (!task) return

  // 已经有 job_id：直接恢复轮询，不需要重传文件
  if (task.jobId) {
    patch(id, { status: STATUS.INGESTING, error: '' })
    runners.set(id, { timerId: null, controller: null, attempt: 0, ticks: 0 })
    poll(id)
    return
  }

  // 注意：上游只保留最近一个 job。如果这个任务是被后来的上传挤掉的，
  // 它的 job_id 在服务端已经不存在了，重传是唯一出路 —— 但重传会让
  // 那个后来的任务也失效。所以重试前必须先确认没有别的任务在跑。

  // 没有 job_id：需要重传。注意后端没有幂等键，如果上一次上传其实成功了
  // 只是响应丢在路上，重传会造成同一文档入库两次 —— 这一点在 UI 文案里说明
  if (fileById.has(id)) {
    patch(id, { status: STATUS.QUEUED, percent: 0, error: '' })
    queue.push(id)
    pump()
  } else {
    patch(id, { error: '原文件已不可用（页面刷新过），请重新拖入该文件' })
  }
}

export function removeTask(id) {
  stopRunner(id)
  fileById.delete(id)
  setTasks((tasks) => tasks.filter((t) => t.id !== id))
  // 移除的可能正是那个占着闸门的任务，放行队列
  onTaskSettled()
}

export function clearFinished() {
  snapshot.tasks.forEach((t) => {
    if (isTerminal(t.status)) stopRunner(t.id)
  })
  setTasks((tasks) => tasks.filter((t) => !isTerminal(t.status)))
}

export function stopAll() {
  runners.forEach((_, id) => stopRunner(id))
  queue.length = 0
}

// 关标签页 / 进 bfcache 时掐掉在途请求。
// 这是「页面」级别的清理，放在模块里而不是组件里。
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    runners.forEach((runner) => {
      clearTimeout(runner.timerId)
      runner.controller?.abort()
    })
  })
}
