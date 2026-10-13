/**
 * 前端串行入库的端到端验证脚本（仅开发用，不参与构建）。
 *
 * 跑法： node tools/verify-serial.cjs
 * 前置：Java :8080、FastAPI :8102、Vite :5173 都已启动
 *
 * 验证点：
 *   1. 三个文件按顺序串行入库 —— 任一采样时刻，「活动态」任务数 ≤ 1
 *   2. 三个文件最终都走到终态（成功/跳过/失败/状态未知），且没有中间态卡死
 *   3. 控制台无报错
 */
const { chromium } = require('playwright')

const FILES = process.argv.slice(2)
const URL = 'http://localhost:5173'
const SAMPLE_MS = 400
const MAX_MS = 180000

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function sample(page) {
  return page.locator('.task-row').evaluateAll((rows) =>
    rows.map((row) => {
      const name = row.querySelector('.task-row__name')?.innerText?.trim() ?? '?'
      const phase = row.querySelector('.task-row__phase')
      const tag = row.querySelector('.ant-tag')?.innerText?.trim() ?? ''

      if (!phase) return { name, state: 'T', label: tag || '终态' }

      const text = phase.innerText.replace(/\s+/g, ' ').trim()
      // 排队中不算「在跑」——它只是等在闸门后面
      if (text.includes('排队中')) return { name, state: 'Q', label: text }
      return { name, state: 'A', label: text }
    }),
  )
}

;(async () => {
  const browser = await chromium.launch()
  const page = await browser.newPage()

  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })
  page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))

  await page.goto(URL, { waitUntil: 'networkidle' })
  console.log('页面已加载:', await page.title())

  await page.locator('input[type=file]').setInputFiles(FILES)
  console.log(`已投入 ${FILES.length} 个文件，开始采样…\n`)

  const started = Date.now()
  let maxActive = 0
  let worstSample = null
  let finished = false
  let lastLine = ''

  while (Date.now() - started < MAX_MS) {
    const rows = await sample(page)
    const active = rows.filter((r) => r.state === 'A')

    if (active.length > maxActive) {
      maxActive = active.length
      if (active.length > 1) worstSample = rows
    }

    const line = rows.map((r) => `${r.name}=${r.state}`).join('  ')
    if (line !== lastLine) {
      const secs = ((Date.now() - started) / 1000).toFixed(1)
      console.log(`[${secs}s] ${line}`)
      lastLine = line
    }

    if (rows.length > 0 && rows.every((r) => r.state === 'T')) {
      finished = true
      break
    }
    await sleep(SAMPLE_MS)
  }

  await page.screenshot({ path: 'tools/verify-serial.png', fullPage: true })

  const finalRows = await sample(page)
  const detail = await page.locator('.task-row').evaluateAll((rows) =>
    rows.map((row) => row.innerText.replace(/\n+/g, ' | ').trim()),
  )

  console.log('\n================ 结果 ================')
  console.log(`最大同时活动任务数: ${maxActive}  ${maxActive <= 1 ? '✅ 串行' : '❌ 并发！'}`)
  if (worstSample) console.log('并发时的采样:', JSON.stringify(worstSample, null, 2))
  console.log(`是否全部走到终态: ${finished ? '✅' : '❌ 超时仍有任务未结束'}`)

  console.log('\n--- 每个任务的最终状态 ---')
  finalRows.forEach((r) => console.log(`  ${r.name.padEnd(10)} ${r.state}  ${r.label}`))
  console.log('\n--- 最终行文本 ---')
  detail.forEach((d) => console.log(`  ${d}`))

  console.log('\n--- 控制台错误 ---')
  console.log(errors.length === 0 ? '  ✅ 无' : errors.map((e) => `  ❌ ${e}`).join('\n'))
  console.log(`\n截图: frontend/tools/verify-serial.png`)

  await browser.close()
  process.exit(maxActive <= 1 && finished && errors.length === 0 ? 0 : 1)
})()
