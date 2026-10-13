/**
 * 问答页的可视化验证（仅开发用，不参与构建）。
 *
 * 跑法： node tools/verify-chat-ui.cjs
 *
 * 开着 Vite（:5173）和 Java（:8080）时，打开页面 → 切到「知识库问答」→
 * 打一句话 → 截图，并把气泡上的文字和标签打出来。
 *
 * FastAPI 没起时也应该走通：气泡上是「未收到回答」+ 后端给的那句错误文案，
 * 而不是白屏或 TypeError。**这一条正是要验证的**。
 */
const { chromium } = require('playwright')

// 默认 5173；也可以传一个地址，用来验「Vite 代理指向别的 Java 实例」的情况
const URL = process.argv[2] || 'http://localhost:5173'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1000, height: 900 } })

  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })
  page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))

  await page.goto(URL, { waitUntil: 'networkidle' })

  // ① 页签存在
  const tabs = await page.locator('.ant-tabs-tab').allInnerTexts()
  console.log('--- 页签 ---\n  ' + tabs.join(' / '))

  // ② 切到问答页。注意 Tabs 是 keep-mounted 的：入库页的 DOM 还在，
  //    所以所有选择器都必须限定在当前激活的那个面板里
  await page.getByRole('tab', { name: '知识库问答' }).click()
  await page.waitForSelector('.chat-composer textarea', { timeout: 10000 })
  const pane = page.locator('.ant-tabs-tabpane-active')
  console.log('\n--- 空态 ---\n  ' + (await pane.locator('.ant-empty-description').innerText()))

  // ③ 打一句话发出去
  await pane.locator('.chat-composer textarea').fill('这个知识库里有什么？')
  await pane.getByRole('button', { name: '发送' }).click()
  await sleep(6000) // 真的接上后端时要等回答流完（还接不上时也只是多等几秒）

  await page.screenshot({ path: 'tools/verify-chat-ui.png', fullPage: true })

  const bubbles = await pane.locator('.chat-msg').evaluateAll((els) =>
    els.map((e) => e.innerText.replace(/\n+/g, ' | ').trim()),
  )
  console.log('\n--- 消息气泡 ---')
  bubbles.forEach((b) => console.log(`  ${b}`))

  console.log('\n--- 是否出现原生崩溃特征 ---')
  const crashed = errors.some((e) => /TypeError|Cannot read|is not a function|undefined/.test(e))
  console.log(crashed ? `  ❌ 疑似崩溃:\n${errors.join('\n')}` : '  ✅ 无 TypeError 类崩溃')

  console.log('\n截图： tools/verify-chat-ui.png')
  await browser.close()
})()
