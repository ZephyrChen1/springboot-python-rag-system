/**
 * 工具名 → 中文标签。未知工具原样显示名字（协议扩张时不用改前端）。
 *
 * 前端**不维护工具清单**：真正注册了哪些工具由 Python 决定
 * （写工具还要 AGENT_ALLOW_WRITE_TOOLS 才可见）。这里只是展示用的别名表，
 * 漏掉一个工具最坏的结果是卡片标题显示 `some_tool`，不会坏功能。
 */
const TOOL_LABELS = {
  kb_search: '检索知识库',
  kb_status: '查看入库任务',
  kb_ingest: '写入知识库',
  kb_delete: '从知识库删除',
  biz_query: '查询业务数据',
  web_search: '联网搜索',
  calculator: '计算',
  now: '当前时间',
}

/** 这些工具会改数据，确认弹窗要更醒目地提醒 */
const WRITE_TOOLS = new Set(['kb_ingest', 'kb_delete'])

export function toolLabel(name) {
  return TOOL_LABELS[name] || name || '未知工具'
}

export function isWriteTool(name) {
  return WRITE_TOOLS.has(name)
}

/** 参数压成一行：卡片上只需要「大概在干什么」，细节不展开 */
export function summarizeArgs(args) {
  if (args == null || (typeof args === 'object' && Object.keys(args).length === 0)) return ''
  let text
  try {
    text = JSON.stringify(args)
  } catch {
    text = String(args)
  }
  return text.length > 180 ? `${text.slice(0, 180)}…` : text
}

export function formatElapsed(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export function formatTime(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return ''
  try {
    return new Date(ms).toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    return ''
  }
}
