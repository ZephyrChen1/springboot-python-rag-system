/**
 * 错误文案的回归验证：所有失败路径最终吐给用户的**那句话**。
 *
 *   node tools/verify-http-errors.mjs
 *
 * **不需要任何后端**：脚本自己起一个临时 HTTP server 扮演上游，逐条回放真实会
 * 出现的坏响应（空 body 的 500、HTML 错误页、超时、连不上、主动取消）。
 *
 * 为什么值得单独一个脚本：这些文案是分散在多个 if 里的，改一处很容易顺手改坏
 * 另一处 —— 而它们的形态（「无法连接后端服务（HTTP 500）…」）恰恰是排查问题时
 * 唯一的线索，写错了会把人带到完全错误的方向。
 */
import { createServer } from 'node:http'
import { canceledError, http, toHttpError } from '../src/api/http.js'

const server = createServer((req, res) => {
  if (req.url === '/detail') {
    res.writeHead(404, { 'content-type': 'application/json' }).end('{"detail":"任务不存在(仅保留最近一个任务)"}')
  } else if (req.url === '/empty500') {
    res.writeHead(500).end() // 代理连不上后端时的典型形态：空 body 的 5xx
  } else if (req.url === '/html') {
    res.writeHead(502, { 'content-type': 'text/html' }).end('<html><body>502 Bad Gateway</body></html>')
  } else if (req.url === '/slow') {
    // 不响应，让客户端超时
  } else {
    res.writeHead(200, { 'content-type': 'application/json' }).end('{"ok":true}')
  }
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const port = server.address().port
http.defaults.baseURL = `http://127.0.0.1:${port}`

let pass = 0
let fail = 0
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  ok ? pass++ : fail++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`)
  if (!ok) console.log(`      期望 ${JSON.stringify(expected)}\n      实际 ${JSON.stringify(actual)}`)
}

const get = (url, cfg) => http.get(url, cfg).then((res) => res, (err) => err)

// ① 正常响应原样返回（拦截器不能把 data 吞掉）
check('① 成功响应不被包装', (await get('/ok')).data, { ok: true })

// ② 上游 {"detail": ...} → 抽出人话 + 带状态码
{
  const err = await get('/detail')
  check('② detail 抽出来', err.message, '任务不存在(仅保留最近一个任务)')
  check('② 状态码带出来（轮询层全靠它）', err.status, 404)
}

// ③ 空 body 的 500 → 那条专门用于「后端没启动」的文案
check('③ 空 body 的 500', (await get('/empty500')).message, '无法连接后端服务（HTTP 500），请确认后端已在 8080 启动')

// ④ HTML 错误页 → 不能把 HTML 当文案糊给用户
{
  const err = await get('/html')
  check('④ HTML 不糊给用户', err.message, '服务端错误（HTTP 502）')
  check('④ 状态码', err.status, 502)
}

// ⑤ 超时
check('⑤ 超时文案', (await get('/slow', { timeout: 50 })).message, '请求超时（服务端可能仍在处理中）')

// ⑥ 网络不通
{
  http.defaults.baseURL = 'http://127.0.0.1:1'
  check(
    '⑥ 连不上',
    (await get('/ok')).message,
    '无法连接服务，请确认后端已启动（或检查安全软件是否拦截了本地端口）',
  )
  http.defaults.baseURL = `http://127.0.0.1:${port}`
}

// ⑦ 主动取消：必须带 isCanceled，且不能被包装成错误文案
{
  const ac = new AbortController()
  const p = get('/slow', { signal: ac.signal })
  setTimeout(() => ac.abort(), 20)
  check('⑦ 取消标记', (await p).isCanceled, true)
}

// ⑧ SSE 那条路复用的工具函数（见 api/rag.js 的 chatStream）
check('⑧ canceledError', canceledError().isCanceled, true)
check(
  '⑧ toHttpError 没有响应时',
  toHttpError({}).message,
  '无法连接服务，请确认后端已启动（或检查安全软件是否拦截了本地端口）',
)

server.close()
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
