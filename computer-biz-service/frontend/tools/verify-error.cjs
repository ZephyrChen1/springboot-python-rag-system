/**
 * 错误路径验证（仅开发用，不参与构建）。
 *
 * 跑法： node tools/verify-error.cjs <file>
 *
 * 用途：在后端**故意停掉**的情况下上传，验证前端不会白屏 / 不会
 * 因为拦截器读 error.response.data 抛 TypeError，而是给出一句人话。
 */
const { chromium } = require('playwright')

const FILE = process.argv[2]
const URL = 'http://localhost:5173'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  const browser = await chromium.launch()
  const page = await browser.newPage()

  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })
  page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))

  await page.goto(URL, { waitUntil: 'networkidle' })
  await page.locator('input[type=file]').setInputFiles([FILE])

  // 等任务行出现并稳定
  await page.waitForSelector('.task-row', { timeout: 15000 })
  await sleep(3000)

  await page.screenshot({ path: 'tools/verify-error.png', fullPage: true })

  const rows = await page.locator('.task-row').evaluateAll((els) =>
    els.map((e) => e.innerText.replace(/\n+/g, ' | ').trim()),
  )

  console.log('--- 任务行 ---')
  rows.forEach((r) => console.log(`  ${r}`))

  console.log('\n--- 是否出现原生崩溃特征 ---')
  const crashed = errors.some(
    (e) => /TypeError|Cannot read|is not a function|undefined/.test(e),
  )
  console.log(crashed ? `  ❌ 疑似拦截器崩溃:\n${errors.join('\n')}` : '  ✅ 无 TypeError 类崩溃')

  console.log('\n--- 控制台错误（未捕获异常） ---')
  console.log(errors.length === 0 ? '  ✅ 无' : errors.map((e) => `  ${e}`).join('\n'))

  await browser.close()
  process.exit(crashed ? 1 : 0)
})()
