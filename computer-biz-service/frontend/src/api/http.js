import axios from 'axios'

/**
 * 所有请求的公共前缀。只有一处定义 —— 因为 SSE 问答那条路走的是 fetch，
 * 绕过了 axios 实例的 baseURL，不能让它再手写一遍 '/api'。
 */
export const API_PREFIX = '/api'

/**
 * 全项目唯一的 axios 实例。
 *
 * baseURL 用相对路径，开发期由 Vite dev server 代理到 Java :8080，
 * 所以浏览器视角是「同源」，不需要后端配任何 CORS。
 */
export const http = axios.create({
  baseURL: API_PREFIX,
  timeout: 15000,
})

/**
 * 把上游各种形态的错误体压成一句人话。返回 '' 表示没抽出来，交给调用方兜底。
 */
export function extractDetail(data) {
  if (data == null) return ''

  // 上游/中间代理返回 HTML 错误页、或者空 body（502 很常见）
  if (typeof data === 'string') {
    const t = data.trim()
    if (!t || t.startsWith('<')) return ''
    return t.length > 300 ? `${t.slice(0, 300)}…` : t
  }

  if (typeof data === 'object') {
    const d = data.detail

    if (typeof d === 'string' && d.trim()) return d
    if (typeof d === 'number' || typeof d === 'boolean') return String(d)

    // FastAPI 的参数校验失败（RequestValidationError）返回的是数组：
    //   {"detail":[{"loc":["body","file"],"msg":"Field required","type":"missing"}]}
    // 而 Java 侧 ApiErrorResp.detail 是 String 字段，Jackson 无法把 JSON 数组
    // 反序列化成 String，会抛异常并被 RagClientConfig 的 catch 吞掉，
    // 最终退化成「整个原始 body 字符串」——也就是前端可能收到一个
    // 被 JSON 转义过的 JSON。所以数组和字符串两种形态都要能吃下。
    if (Array.isArray(d)) {
      const text = d
        .map((item) =>
          item && typeof item === 'object' ? item.msg ?? JSON.stringify(item) : String(item),
        )
        .join('；')
      return text.length > 300 ? `${text.slice(0, 300)}…` : text
    }
    if (d != null) return JSON.stringify(d).slice(0, 300)

    // 万一后端某天换成 Spring 默认的错误体格式
    if (typeof data.message === 'string') return data.message
    if (typeof data.error === 'string') return data.error
  }

  return ''
}

/**
 * 把「一次失败的请求」压成一个统一形状的 Error：
 * message / detail 是可以直接显示的人话，status 是真实 HTTP 状态码，
 * isCanceled 表示是我们自己主动取消的。
 *
 * 为什么抽成导出的函数、而不是只写在拦截器里：SSE 问答走的是 fetch
 * （axios 在浏览器里没有流式读的口子，见 api/rag.js），那条路也必须产出
 * **一模一样**的 Error 形状 —— 否则「错误只翻译一次」就不成立了，
 * 早晚会出现两份文案，其中一份先腐烂。
 */
export function toHttpError({ status, rawBody, code, cause } = {}) {
  let detail = extractDetail(rawBody)

  // 响应体是不是空的？空 body 的 5xx 通常不是后端自己返回的，
  // 而是中间代理（Vite dev proxy / nginx）连不上后端时生成的
  const hasBody =
    rawBody != null &&
    (typeof rawBody !== 'string' || rawBody.trim() !== '') &&
    !(typeof rawBody === 'object' && Object.keys(rawBody).length === 0)

  if (!detail) {
    if (code === 'ECONNABORTED' || code === 'ETIMEDOUT') {
      detail = '请求超时（服务端可能仍在处理中）'
    } else if (status == null) {
      // 断网 / fetch 直接 reject / 被 abort：压根没拿到响应
      detail = '无法连接服务，请确认后端已启动（或检查安全软件是否拦截了本地端口）'
    } else if (status >= 500 && !hasBody) {
      // 最容易被误读的一种情况：后端没启动时，Vite 代理会返回一个
      // 空 body 的 500。如果只说「服务端错误」，排查时会以为是后端崩了
      detail = `无法连接后端服务（HTTP ${status}），请确认后端已在 8080 启动`
    } else if (status === 413) {
      // 超限时 Tomcat 常常直接掐断连接，前端通常读不到这个 413 的 body，
      // 能走到这里算运气好。真正的保护靠上传前按 file.size 拦截。
      detail = '文件过大，超出服务端限制'
    } else if (status >= 500) {
      detail = `服务端错误（HTTP ${status}）`
    } else {
      detail = `请求失败（HTTP ${status}）`
    }
  }

  const wrapped = new Error(detail)
  // 必须把状态码带出去。轮询层要靠它区分
  // 「404 = 任务已失效，重试无意义」和「502 = 暂时故障，应该重试」
  wrapped.status = status
  wrapped.detail = detail
  wrapped.raw = cause ?? null
  return wrapped
}

/**
 * 主动取消（组件卸载 / 关标签页 / 点「停止生成」）不是错误，必须单独标记，
 * 否则会被当成"入库失败"或"回答失败"弹给用户。SSE 那条路用它。
 */
export function canceledError() {
  const err = new Error('已取消')
  err.isCanceled = true
  err.detail = '已取消'
  return err
}

http.interceptors.response.use(
  (res) => res,
  (error) => {
    // ① 主动取消：原样透出去，但打上标记。
    //    这里刻意不换成 canceledError() —— 保持既有行为不变
    if (axios.isCancel?.(error) || error.code === 'ERR_CANCELED') {
      error.isCanceled = true
      return Promise.reject(error)
    }

    // ② error.response 在断网 / 超时 / 被 abort 时是 undefined，必须用可选链，
    //    否则这里会抛 TypeError —— 你看到的就变成"拦截器自己崩了"而不是真实错误
    return Promise.reject(
      toHttpError({
        status: error.response?.status,
        rawBody: error.response?.data,
        code: error.code,
        cause: error,
      }),
    )
  },
)
