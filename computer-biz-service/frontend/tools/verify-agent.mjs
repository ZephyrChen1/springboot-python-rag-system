/**
 * Agent 问答链路的离线验证：NDJSON 事件 → 状态机。
 *
 *   node tools/verify-agent.mjs
 *
 * **不需要任何后端**：上游用 ReadableStream 自己造，专门造真实会出现的脏情况
 * （半个汉字跨 chunk、事件挤在一个包里、流报错、写操作人工确认、用户中止）。
 *
 * 和 verify-chat.mjs 的分工：那个验旧链路的 SSE 文本增量；这个验新链路的事件
 * 归纳（thought / tool_call / tool_result / confirm_required / done）。
 */
import { http } from '../src/api/http.js'
import {
  MSG,
  getSnapshot,
  resetConversation,
  respondConfirm,
  sendMessage,
  stopStreaming,
  subscribe,
} from '../src/agent/agentStore.js'

const enc = new TextEncoder()
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

let pass = 0
let fail = 0
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  ok ? pass++ : fail++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`)
  if (!ok) console.log(`      期望 ${JSON.stringify(expected)}\n      实际 ${JSON.stringify(actual)}`)
}

const frame = (obj) => `data: ${JSON.stringify(obj)}\n\n`
const last = () => getSnapshot().messages.at(-1)

async function settle(timeout = 3000) {
  const t0 = Date.now()
  while (getSnapshot().streaming && Date.now() - t0 < timeout) await sleep(10)
  await sleep(20)
}

async function waitFor(pred, timeout = 2000) {
  const t0 = Date.now()
  while (!pred() && Date.now() - t0 < timeout) await sleep(10)
  return pred()
}

/** 把事件串成 SSE 帧，并按 5 字节切块 —— 逼出跨块解码 */
function mockSse(frames, { status = 200, slice = 5 } = {}) {
  const text = frames.join('')
  const bytes = enc.encode(text)
  globalThis.fetch = async () => {
    const body = new ReadableStream({
      start(c) {
        for (let i = 0; i < bytes.length; i += slice) c.enqueue(bytes.slice(i, i + slice))
        c.close()
      },
    })
    return new Response(body, { status, headers: { 'content-type': 'text/event-stream' } })
  }
}

// ================================================================ 正常一轮

console.log('\n--- 事件归纳 ---')

{
  resetConversation()
  mockSse([
    frame({ v: 1, type: 'start', run_id: 'run-abc123', ts: 1000 }),
    frame({ v: 1, type: 'thought', step: 1, text: '先查知识库', ts: 1001 }),
    frame({ v: 1, type: 'tool_call', step: 1, call_id: 'c1', name: 'kb_search', args: { query: 'x' }, ts: 1010 }),
    frame({ v: 1, type: 'tool_result', step: 1, call_id: 'c1', ok: true, summary: '找到 2 条', ts: 1260 }),
    frame({ v: 1, type: 'answer', delta: '你好' }),
    frame({ v: 1, type: 'answer', delta: '世界' }),
    frame({ v: 1, type: 'done', steps: 1 }),
  ])

  const seen = []
  const un = subscribe(() => seen.push({ text: last()?.text, streaming: getSnapshot().streaming }))
  sendMessage('  问题  ')
  await settle()
  un()

  const m = last()
  check('① 状态 done', m.status, MSG.DONE)
  check('① 正文按 delta 拼接（跨块汉字）', m.text, '你好世界')
  check('① run_id 落到消息上', m.runId, 'run-abc123')
  check('① 轨迹两条（thought + tool）', m.steps.map((s) => s.kind), ['thought', 'tool'])
  check('① thought 文案', m.steps[0].text, '先查知识库')
  check('① tool 名字/参数', [m.steps[1].name, m.steps[1].args], ['kb_search', { query: 'x' }])
  check('① tool 成功与耗时（ts 差）', [m.steps[1].status, m.steps[1].elapsedMs], ['ok', 250])
  check('① tool 结果摘要', m.steps[1].summary, '找到 2 条')
  check('① 用户消息去空格', getSnapshot().messages[0].text, '问题')
  check('① 中间态可见', seen.some((s) => s.streaming && s.text === '你好'), true)
  check('① 结束后 streaming 归位', getSnapshot().streaming, false)
}

// ================================================================ 失败终态

console.log('\n--- 失败终态 ---')

// ② 先发 error 再正常关流：HTTP 200 不等于成功
{
  resetConversation()
  mockSse([
    frame({ v: 1, type: 'start', run_id: 'r2' }),
    frame({ v: 1, type: 'error', code: 'MODEL_ERROR', message: '模型不可用' }),
  ])
  sendMessage('问题')
  await settle()

  const m = last()
  check('② 没有 done → 失败', m.status, MSG.ERROR)
  check('② error 事件文案透传', m.error, '模型不可用')
}

// ③ 收到一半正文再报错：保留正文，标成中断
{
  resetConversation()
  mockSse([
    frame({ v: 1, type: 'answer', delta: '半截回答' }),
    frame({ v: 1, type: 'error', code: 'MODEL_ERROR', message: '生成失败' }),
  ])
  sendMessage('问题')
  await settle()

  const m = last()
  check('③ 半截正文保留', m.text, '半截回答')
  check('③ 状态是中断', m.status, MSG.INTERRUPTED)
  check('③ 错误文案', m.error, '生成失败')
}

// ④ 流正常关闭但既没有 done 也没有 error —— 上游异常退出
{
  resetConversation()
  mockSse([frame({ v: 1, type: 'start', run_id: 'r4' })])
  sendMessage('问题')
  await settle()

  check('④ 没有 done 不算成功', last().status, MSG.ERROR)
  check('④ 给出可读原因', last().error, '上游在没有发送 done 事件的情况下结束了本次回答')
}

// ⑤ 开流前就 502（Java 连不上 FastAPI）
{
  resetConversation()
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: '无法连接上游 RAG 服务' }), {
      status: 502,
      headers: { 'content-type': 'application/json' },
    })
  sendMessage('问题')
  await settle()

  check('⑤ 502 状态落成失败', last().status, MSG.ERROR)
  check('⑤ detail 透传', last().error, '无法连接上游 RAG 服务')
}

// ================================================================ 人工确认

console.log('\n--- 写操作确认 ---')

{
  resetConversation()

  // 工具名 → 中文标签的能力由 UI 负责，这里只验状态机
  let releaseConfirm
  const gate = new Promise((r) => {
    releaseConfirm = r
  })
  const before = [
    frame({ v: 1, type: 'start', run_id: 'r5' }),
    frame({ v: 1, type: 'tool_call', step: 1, call_id: 'w1', name: 'kb_ingest', args: { file_name: 'a.txt' } }),
    frame({
      v: 1,
      type: 'confirm_required',
      step: 1,
      call_id: 'w1',
      name: 'kb_ingest',
      args: { file_name: 'a.txt' },
      token: 'tok-1',
      expires_at: 1_700_000_000.5,
    }),
  ]
  const after = [
    frame({ v: 1, type: 'tool_result', step: 1, call_id: 'w1', ok: true, summary: '已入库', ts: 1200 }),
    frame({ v: 1, type: 'answer', delta: '已为你入库。' }),
    frame({ v: 1, type: 'done', steps: 1 }),
  ]

  globalThis.fetch = async () => {
    const body = new ReadableStream({
      async start(c) {
        for (const f of before) {
          c.enqueue(enc.encode(f))
          await sleep(5)
        }
        await gate
        for (const f of after) {
          c.enqueue(enc.encode(f))
          await sleep(5)
        }
        c.close()
      },
    })
    return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  }

  // 确认接口不走 fetch（走 axios 实例），直接换掉 post
  const calls = []
  const originalPost = http.post
  http.post = async (url, body) => {
    calls.push({ url, body })
    return { data: { ok: true } }
  }

  sendMessage('把 a.txt 入库')
  const got = await waitFor(() => !!last()?.pendingConfirm)

  check('⑥ confirm_required 进入待确认', got, true)
  check('⑥ token / 工具名', [last().pendingConfirm.token, last().pendingConfirm.name], ['tok-1', 'kb_ingest'])
  check('⑥ epoch 秒换成毫秒', last().pendingConfirm.expiresAt, 1_700_000_000_500)
  check('⑥ 等待确认时流仍开着', getSnapshot().streaming, true)

  const p = respondConfirm(true)
  releaseConfirm()
  await p
  await settle()

  check('⑦ 确认请求参数', calls, [
    { url: '/rag/agent/confirm', body: { token: 'tok-1', approved: true } },
  ])
  check('⑦ 确认后弹窗清掉', last().pendingConfirm, null)
  check('⑦ 工具轨迹更新为成功', last().steps.at(-1).status, 'ok')
  check('⑦ 后续回答继续到达', last().text, '已为你入库。')
  check('⑦ 终态 done', last().status, MSG.DONE)

  http.post = originalPost
}

// ⑧ 确认被拒绝（approved=false）也要发请求，工具不执行但 Agent 继续回答
{
  resetConversation()
  let release
  const gate = new Promise((r) => {
    release = r
  })
  globalThis.fetch = async () => {
    const body = new ReadableStream({
      async start(c) {
        // 真实顺序：tool_call 先到，confirm_required 随后
        c.enqueue(enc.encode(frame({
          v: 1, type: 'tool_call', step: 1, call_id: 'w2', name: 'kb_delete', args: {}, ts: 1000,
        })))
        c.enqueue(enc.encode(frame({
          v: 1, type: 'confirm_required', call_id: 'w2', name: 'kb_delete',
          args: {}, token: 'tok-2', expires_at: 1_700_000_010,
        })))
        await gate
        c.enqueue(enc.encode(frame({
          v: 1, type: 'tool_result', call_id: 'w2', ok: false,
          summary: '[未确认] 用户未批准写操作', error: 'not_confirmed', ts: 1100,
        })))
        c.enqueue(enc.encode(frame({ v: 1, type: 'answer', delta: '已取消。' })))
        c.enqueue(enc.encode(frame({ v: 1, type: 'done', steps: 1 })))
        c.close()
      },
    })
    return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  }
  const calls = []
  const originalPost = http.post
  http.post = async (url, body) => {
    calls.push(body)
    return { data: { ok: true } }
  }

  sendMessage('删掉它')
  check('⑧ 弹窗出现', await waitFor(() => !!last()?.pendingConfirm), true)
  const p = respondConfirm(false)
  release()
  await p
  await settle()

  check('⑧ 拒绝也调用确认接口（approved=false）', calls, [{ token: 'tok-2', approved: false }])
  check('⑧ 弹窗已清掉', last().pendingConfirm, null)
  check('⑧ 工具轨迹标记失败', last().steps.at(-1).status, 'failed')
  check('⑧ 拒绝后回答继续', last().text, '已取消。')
  check('⑧ 终态仍是 done', last().status, MSG.DONE)

  http.post = originalPost
}

// ================================================================ 中止

console.log('\n--- 中止 ---')

{
  resetConversation()
  globalThis.fetch = async (_url, init) => {
    const body = new ReadableStream({
      start(c) {
        c.enqueue(enc.encode(frame({ v: 1, type: 'answer', delta: '开头' })))
        init.signal.addEventListener('abort', () =>
          c.error(new DOMException('Aborted', 'AbortError')),
        )
      },
    })
    return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  }

  sendMessage('问题')
  await sleep(50)
  stopStreaming()
  await settle()

  check('⑨ 已收到的正文保留', last().text, '开头')
  check('⑨ 状态是已停止', last().status, MSG.CANCELED)
  check('⑨ 不算失败（无错误文案）', last().error, '')
}

// ⑩ 未知事件类型忽略，不影响终态
{
  resetConversation()
  mockSse([
    frame({ v: 1, type: 'start', run_id: 'r10' }),
    frame({ v: 1, type: 'future_event', payload: { x: 1 } }),
    frame({ v: 1, type: 'answer', delta: 'OK' }),
    frame({ v: 1, type: 'done', steps: 1 }),
  ])
  sendMessage('问题')
  await settle()

  check('⑩ 未知事件不崩', last().status, MSG.DONE)
  check('⑩ 正文正常', last().text, 'OK')
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
