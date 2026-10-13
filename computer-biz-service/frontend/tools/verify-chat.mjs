/**
 * 问答链路的离线验证：SSE 分帧器 + 会话状态机。
 *
 *   node tools/verify-chat.mjs
 *
 * **不需要任何后端**：上游由脚本自己用 ReadableStream 造，专门造那些真实会出现的
 * 脏情况（半个汉字跨块、帧被切断、Spring 不转义换行、错误 JSON 混进流里）。
 *
 * 为什么值得单独一个脚本：这两块逻辑的 bug 在界面上表现都是「回答缺字 / 少一段 /
 * 明明出错了却显示成功」，看起来像玄学。跑一次就知道是哪一层坏的。
 * 和 verify-serial.cjs 的区别：那个开真浏览器验证端到端，这个只压解析和状态。
 */
import { chatStream } from '../src/api/rag.js'
import {
  MSG,
  getSnapshot,
  resetConversation,
  sendMessage,
  stopStreaming,
  subscribe,
} from '../src/chat/chatStore.js'

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

// ================================================================ SSE 分帧

/** 把文本切成 size 字节一段喂进去，专门制造半个汉字 / 半个帧 */
function mockFetch(text, { status = 200, slice = 5 } = {}) {
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

async function stream(text, opts) {
  mockFetch(text, opts)
  const deltas = []
  const res = await chatStream({ message: 'q', sessionId: 's', topK: 5 }, { onDelta: (d) => deltas.push(d) })
  return { ...res, deltas }
}

console.log('\n--- SSE 分帧 ---')

// ① 正常流：JSON 信封 + 裸文本 + [DONE]，字节被切得很碎
{
  const r = await stream('data: {"delta":"你好"}\n\ndata: 世界\n\ndata: [DONE]\n\n')
  check('① 文本拼接（含跨块汉字）', r.text, '你好世界')
  check('① onDelta 收到的增量', r.deltas, ['你好', '世界'])
  check('① 没有悬空内容', r.errorDetail, '')
  check('① 认得结束标记', r.sawDone, true)
}

// ② Spring 的 SseEmitter 不转义内容里的换行：多行文本块会被写成裸行
{
  const r = await stream('data:第一行\n第二行\n\n')
  check('② 不转义的换行要补回正文', r.text, '第一行\n第二行')
}

// ③ 上游失败时：响应已提交成 200，Spring 把错误 JSON 直接写进流里
{
  const r = await stream('data: 半截回答\n\n{"detail":"上游 401 未授权"}')
  check('③ 已收到的正文要保留', r.text, '半截回答')
  check('③ 错误 JSON 要翻译成人话', r.errorDetail, '上游 401 未授权')
}

// ④ \r\n 分帧（中间换过一手代理就会出现）
{
  const r = await stream('data: hello\r\n\r\ndata: world\r\n\r\n')
  check('④ CRLF 分帧', r.text, 'helloworld')
}

// ⑤ 认不出来的 JSON（元数据）不能糊进答案
{
  const r = await stream('data: {"sources":[1,2]}\n\ndata: {"choices":[{"delta":{"content":"OK"}}]}\n\n')
  check('⑤ 元数据丢弃 + OpenAI 信封认得', r.text, 'OK')
}

// ⑥ 流没以空行收尾
{
  const r = await stream('data: tail')
  check('⑥ 尾帧没空行也要收到', r.text, 'tail')
}

// ⑦ 非 2xx：开流之前就失败
{
  mockFetch('{"detail":"message 不能为空"}', { status: 400 })
  const err = await chatStream({ message: '', sessionId: 's', topK: 5 }, {}).catch((e) => e)
  check('⑦ 状态码带出来', err.status, 400)
  check('⑦ detail 抽成人话', err.message, 'message 不能为空')
}

// ⑦b 空 body 的 5xx —— 实测真会这样：Accept 是 text/event-stream，接口 produces 也是，
//     GlobalExceptionHandler 想写 ApiErrorResp 时没有能用的转换器，于是
//     500 的响应既没有 Content-Type 也没有 body（curl 实测 ct=null body=""）。
//     这句文案**不能**沿用「请确认后端已在 8080 启动」：那会把人带去查一个没坏的东西。
{
  globalThis.fetch = async () => new Response(null, { status: 500 })
  const err = await chatStream({ message: 'q', sessionId: 's', topK: 5 }, {}).catch((e) => e)
  check('⑦b 空 body 的 500 带状态码', err.status, 500)
  check(
    '⑦b 空 body 的 500 不能误导成「Java 没启动」',
    err.message,
    '后端返回了 500 但没带错误详情。可能是 Java 没启动，也可能是它调用上游 RAG 服务失败（FastAPI 在 8102 吗？）',
  )
}

// ⑦c 有 body 的 5xx 照旧走 toHttpError 的通用文案（这条分支不能把 detail 吃掉）
{
  mockFetch('{"detail":"服务内部错误"}', { status: 500 })
  const err = await chatStream({ message: 'q', sessionId: 's', topK: 5 }, {}).catch((e) => e)
  check('⑦c 有 body 的 500 仍用后端 detail', err.message, '服务内部错误')
}

// ⑧ 后端没启动（fetch 直接 reject）
{
  globalThis.fetch = async () => {
    throw new TypeError('Failed to fetch')
  }
  const err = await chatStream({ message: 'q', sessionId: 's', topK: 5 }, {}).catch((e) => e)
  check('⑧ 网络失败的文案', err.message, '无法连接服务，请确认后端已启动（或检查安全软件是否拦截了本地端口）')
}

// ⑨ 中止必须是 isCanceled，不能被当成「回答失败」
{
  globalThis.fetch = async (_url, init) => {
    const body = new ReadableStream({
      start(c) {
        c.enqueue(enc.encode('data: 开头'))
        init.signal.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')))
      },
    })
    return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  }
  const ac = new AbortController()
  const p = chatStream({ message: 'q', sessionId: 's', topK: 5 }, { signal: ac.signal }).catch((e) => e)
  setTimeout(() => ac.abort(), 30)
  check('⑨ 中止标记为 isCanceled', (await p).isCanceled, true)
}

// ================================================================ 会话状态机

/** 造一条可控的流：先吐若干帧，然后要么正常结束、要么挂住等 abort */
function mockStream(frames, { hang = false, status = 200, raw = '' } = {}) {
  globalThis.fetch = async (_url, init) => {
    const body = new ReadableStream({
      async start(c) {
        for (const f of frames) {
          c.enqueue(enc.encode(f))
          await sleep(5)
        }
        if (hang) {
          init.signal.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')))
          return
        }
        if (raw) c.enqueue(enc.encode(raw))
        c.close()
      },
    })
    return new Response(body, { status, headers: { 'content-type': 'text/event-stream' } })
  }
}

async function settle(timeout = 2000) {
  const t0 = Date.now()
  while (getSnapshot().streaming && Date.now() - t0 < timeout) await sleep(10)
  await sleep(10)
}
const last = () => getSnapshot().messages.at(-1)

console.log('\n--- 会话状态机 ---')

// ① 正常一轮：流式过程中能看到中间态，结束后是 done
{
  resetConversation()
  mockStream(['data: {"delta":"你"}\n\n', 'data: {"delta":"好"}\n\n', 'data: [DONE]\n\n'])
  const seen = []
  const un = subscribe(() => seen.push({ streaming: getSnapshot().streaming, text: last().text }))
  sendMessage('  你好  ')

  check('① 发出后立刻进入生成中', getSnapshot().streaming, true)
  await settle()
  un()

  check('① 用户消息去掉首尾空格', getSnapshot().messages[0].text, '你好')
  check('① 助手消息文本', last().text, '你好')
  check('① 助手消息状态', last().status, MSG.DONE)
  check('① 收尾后 streaming 归位', getSnapshot().streaming, false)
  check('① 中间态被订阅者看见', seen.some((s) => s.streaming && s.text === '你'), true)
}

// ② 生成中点「停止生成」：已收到的完整帧保留。
//    最后一帧故意没凑齐（没有结尾空行）—— 半截帧会丢，这是有意的取舍（见 rag.js）
{
  resetConversation()
  mockStream(['data: 半句话\n\n', 'data: 半截'], { hang: true })
  sendMessage('问题')
  await sleep(60)
  stopStreaming()
  await settle()

  check('② 完整的帧保留', last().text, '半句话')
  check('② 状态是已停止', last().status, MSG.CANCELED)
  check('② 不算失败（没有错误文案）', last().error, '')
}

// ③ 流里混进错误 JSON：有正文 → 回答中断 + 上游文案；一个字都没有 → 失败
{
  resetConversation()
  mockStream(['data: 前半段\n\n'], { raw: '{"detail":"上游 401 未授权"}' })
  sendMessage('问题')
  await settle()

  check('③ 正文保留', last().text, '前半段')
  check('③ 状态是回答中断', last().status, MSG.INTERRUPTED)
  check('③ 错误文案来自上游 detail', last().error, '上游 401 未授权')

  resetConversation()
  mockStream([], { raw: '{"detail":"模型不可用"}' })
  sendMessage('问题')
  await settle()

  check('③ 一个字都没有则是失败', last().status, MSG.ERROR)
  check('③ 错误文案', last().error, '模型不可用')
}

// ④ 开流前就 4xx：走普通错误通路
{
  resetConversation()
  mockStream([], { status: 422, raw: '{"detail":"user_id: Field required"}' })
  sendMessage('问题')
  await settle()

  check('④ 状态是失败', last().status, MSG.ERROR)
  check('④ 422 的 detail 透传', last().error, 'user_id: Field required')
}

// ⑤ 生成中重复发送要被挡住（界面上按钮已换成「停止」，这里是兜底）
{
  resetConversation()
  mockStream(['data: 慢'], { hang: true })
  sendMessage('第一条')
  await sleep(30)
  sendMessage('第二条')
  check('⑤ 生成中不再追加消息', getSnapshot().messages.length, 2)
  stopStreaming()
  await settle()
}

// ⑥ 清空会话：掐掉在途的流，增量不会落到空列表上
{
  resetConversation()
  mockStream(['data: 正在'], { hang: true })
  sendMessage('问题')
  await sleep(30)
  resetConversation()
  await sleep(60)

  check('⑥ 消息清空', getSnapshot().messages.length, 0)
  check('⑥ 不是生成中', getSnapshot().streaming, false)
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
