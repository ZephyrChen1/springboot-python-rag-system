/**
 * Agent 问答页的端到端验证（仅开发用，不参与构建）。
 *
 *   node tools/verify-agent-ui.cjs
 *
 * 前提（三个都在跑）：
 *   - 假上游  python -X utf8 tools/stub-agent-upstream.py   (:8102)
 *   - Java    mvn -w spring-boot:run                         (:8080)
 *   - 前端    npm run dev                                    (:5173)
 *
 * 验证两条真实交互：
 *   ① 普通提问 → 思考 + 工具卡片 + 增量回答 + done
 *   ② 写操作 → confirm_required 弹窗 → 批准/拒绝 → 工具卡片状态正确
 */
const { chromium } = require('playwright')

const URL = process.argv[2] || 'http://localhost:5173'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1000, height: 950 } })

  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })
  page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`))
  page.on('request', (req) => {
    if (req.url().includes('/agent/confirm')) {
      console.log('  [confirm request]', req.postData())
    }
  })

  await page.goto(URL, { waitUntil: 'networkidle' })
  await page.getByRole('tab', { name: 'Agent 问答' }).click()
  await page.waitForSelector('.chat-composer textarea', { timeout: 10000 })

  const pane = page.locator('.ant-tabs-tabpane-active')

  async function send(text) {
    await pane.locator('.chat-composer textarea').fill(text)
    await pane.getByRole('button', { name: '发送' }).click()
  }

  async function waitIdle(timeout = 20000) {
    await page.waitForFunction(
      () => !document.querySelector('.ant-tabs-tabpane-active .chat-composer')
        ?.textContent?.includes('停止生成'),
      null,
      { timeout },
    )
  }

  // ---------------------------------------------------------------- ① 普通提问
  await send('知识库里有什么？')
  await pane.locator('.agent-tool').first().waitFor({ timeout: 10000 })
  await waitIdle()

  const first = pane.locator('.chat-msg--ai').last()
  console.log('--- ① 普通提问 ---')
  console.log('  文本:', (await first.locator('.chat-msg__text').innerText()).replace(/\n/g, ' | '))
  console.log('  轨迹:', (await first.locator('.agent-steps').innerText()).replace(/\n+/g, ' | '))
  console.log('  工具卡片数:', await first.locator('.agent-tool').count())
  console.log('  失败卡片数:', await first.locator('.agent-tool--failed').count())

  const normalOk =
    (await first.locator('.agent-tool').count()) === 1 &&
    (await first.locator('.agent-tool--failed').count()) === 0 &&
    (await first.locator('.chat-msg__text').innerText()).includes('stub')

  // ---------------------------------------------------------------- ② 批准写操作
  await send('把这个文件入库')
  const modal = page.locator('.ant-modal').filter({ hasText: '需要确认写操作' })
  await modal.waitFor({ timeout: 10000 })
  console.log('\n--- ② 写操作确认 ---')
  console.log('  弹窗:', (await modal.innerText()).replace(/\n+/g, ' | '))
  await page.screenshot({ path: 'tools/verify-agent-confirm.png', fullPage: true })

  await modal.getByRole('button', { name: '批准执行' }).click()
  await waitIdle()
  // 等 tool_result 把卡片刷成成功
  await sleep(300)

  const second = pane.locator('.chat-msg--ai').last()
  console.log('  批准后文本:', (await second.locator('.chat-msg__text').innerText()).replace(/\n/g, ' | '))
  console.log('  工具卡片:', (await second.locator('.agent-tool').innerText()).replace(/\n+/g, ' | '))
  const approveOk =
    (await second.locator('.agent-tool--failed').count()) === 0 &&
    (await second.locator('.chat-msg__text').innerText()).includes('已按你的决定')

  // ---------------------------------------------------------------- ③ 拒绝写操作
  await send('再入库一次 demo.txt')
  const modal2 = page.locator('.ant-modal').filter({ hasText: '需要确认写操作' })
  await modal2.waitFor({ timeout: 10000 })
  // antd 会在两个汉字的按钮文案中间插一个空格（「拒 绝」），不能用精确匹配
  await modal2.getByRole('button', { name: /拒\s*绝/ }).click()
  await waitIdle()
  await sleep(300)

  const third = pane.locator('.chat-msg--ai').last()
  console.log('\n--- ③ 拒绝写操作 ---')
  console.log('  拒绝后文本:', (await third.locator('.chat-msg__text').innerText()).replace(/\n/g, ' | '))
  console.log('  工具卡片:', (await third.locator('.agent-tool').innerText()).replace(/\n+/g, ' | '))
  const rejectOk =
    (await third.locator('.agent-tool--failed').count()) === 1 &&
    (await third.locator('.chat-msg__text').innerText()).includes('已取消')

  await page.screenshot({ path: 'tools/verify-agent-ui.png', fullPage: true })

  console.log('\n--- 结果 ---')
  console.log(`  ① 普通提问: ${normalOk ? '✅' : '❌'}`)
  console.log(`  ② 批准写操作: ${approveOk ? '✅' : '❌'}`)
  console.log(`  ③ 拒绝写操作: ${rejectOk ? '✅' : '❌'}`)

  const crashed = errors.some((e) =>
    /TypeError|Cannot read|is not a function|undefined is not/.test(e),
  )
  console.log(`  控制台崩溃: ${crashed ? `❌\n${errors.join('\n')}` : '✅ 无'}`)

  console.log('\n截图： tools/verify-agent-ui.png / tools/verify-agent-confirm.png')
  await browser.close()
  process.exit(normalOk && approveOk && rejectOk && !crashed ? 0 : 1)
})()
