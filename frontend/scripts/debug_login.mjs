/**
 * 用真实浏览器走一遍登录，看用户到底会遇到什么。
 *
 * ## 为什么必须用浏览器
 *
 * curl 测接口是通的（注册/登录/带 token 访问全 200），
 * 但用户说"登不进去"。差异只可能在浏览器侧：
 *   - 表单校验没过（前端就没发请求）
 *   - 请求发出但响应处理出错
 *   - 登录成功但路由没跳转
 *   - 控制台有报错
 *
 * 所以要真的点一遍，同时抓 console 和 network。
 *
 * ## 用法
 *
 *   node frontend/scripts/debug_login.mjs
 */

import { chromium } from 'playwright'

const BASE = process.env.BASE_URL || 'http://localhost:5173'

async function launch() {
  for (const [channel, label] of [['msedge', 'Edge'], ['chrome', 'Chrome']]) {
    try {
      const b = await chromium.launch({ channel })
      console.log(`浏览器: ${label}`)
      return b
    } catch { /* next */ }
  }
  return chromium.launch()
}

async function main() {
  const browser = await launch()
  const ctx = await browser.newContext({ locale: 'zh-CN' })
  const page = await ctx.newPage()

  const logs = []
  const requests = []
  const responses = []

  page.on('console', (m) => {
    logs.push({ type: m.type(), text: m.text() })
  })
  page.on('pageerror', (e) => {
    logs.push({ type: 'pageerror', text: e.message })
  })
  page.on('request', (r) => {
    if (r.url().includes('/api/')) {
      requests.push({ method: r.method(), url: r.url(), body: r.postData() })
    }
  })
  page.on('response', async (r) => {
    if (r.url().includes('/api/')) {
      let body = ''
      try { body = (await r.text()).slice(0, 300) } catch { }
      responses.push({ status: r.status(), url: r.url(), body })
    }
  })

  console.log(`\n打开 ${BASE}`)
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForTimeout(2000)

  // ---- 如果提供了已有账号，就跳过注册直接登录 ----
  const useExisting = !!process.env.LOGIN_USER

  // ---- 先注册一个账号 ----
  const user = process.env.LOGIN_USER || `dbg_${Date.now().toString(36)}`
  const pass = process.env.LOGIN_PASS || 'debug123456'
  console.log(`\n准备账号: ${user} / ${pass}`)

  console.log('\n=== 步骤 1: 切到注册标签 ===')
  const regTab = page.locator('.el-tabs__item').filter({ hasText: '注册' }).first()
  if (useExisting) {
    console.log('  跳过（用已有账号直接登录）')
  } else
  if (await regTab.count()) {
    await regTab.click()
    await page.waitForTimeout(600)
    console.log('  已点击')
  } else {
    console.log('  ! 找不到注册标签')
  }

  const inputs = page.locator('input:visible')
  const n = await inputs.count()
  console.log(`  可见输入框: ${n} 个`)

  if (n >= 3) {
    await inputs.nth(0).fill(user)
    await inputs.nth(1).fill(pass)
    await inputs.nth(2).fill(pass)

    const btn = page.locator('button:visible').filter({ hasText: '注册' }).last()
    if (await btn.count()) {
      console.log('\n=== 步骤 2: 点注册 ===')
      await btn.click()
      await page.waitForTimeout(3000)
      console.log(`  注册后 URL: ${await page.evaluate(() => location.pathname)}`)
    }
  }

  // ---- 登录 ----
  console.log('\n=== 步骤 3: 登录 ===')
  const loginTab = page.locator('.el-tabs__item').filter({ hasText: '登录' }).first()
  if (await loginTab.count()) {
    await loginTab.click()
    await page.waitForTimeout(600)
  }

  const li = page.locator('input:visible')
  const ln = await li.count()
  console.log(`  可见输入框: ${ln} 个`)

  if (ln >= 2) {
    await li.nth(0).fill(user)
    await li.nth(1).fill(pass)
    console.log(`  已填入: ${user} / ${pass}`)

    const btn = page.locator('button:visible').filter({ hasText: '登录' }).last()
    if (await btn.count()) {
      await btn.click()
      console.log('  已点击登录')
      await page.waitForTimeout(4000)
    }
  }

  const finalPath = await page.evaluate(() => location.pathname)
  console.log(`\n  最终 URL: ${finalPath}`)

  // ---- 结果 ----
  console.log('\n' + '='.repeat(60))
  console.log('  网络请求')
  console.log('='.repeat(60))
  if (requests.length === 0) {
    console.log('  ⚠ 没有任何 /api/ 请求 —— 前端根本没发请求！')
  } else {
    requests.forEach((r, i) => {
      console.log(`  ${i + 1}. ${r.method} ${r.url}`)
      if (r.body) console.log(`     请求体: ${r.body.slice(0, 150)}`)
    })
  }

  console.log('\n' + '='.repeat(60))
  console.log('  响应')
  console.log('='.repeat(60))
  if (responses.length === 0) {
    console.log('  （无）')
  } else {
    responses.forEach((r, i) => {
      const mark = r.status >= 400 ? '✗' : '✓'
      console.log(`  ${mark} ${i + 1}. HTTP ${r.status}  ${r.url.split('/api')[1] || ''}`)
      if (r.status >= 400) console.log(`     响应: ${r.body}`)
    })
  }

  console.log('\n' + '='.repeat(60))
  console.log('  控制台错误')
  console.log('='.repeat(60))
  const errs = logs.filter((l) => l.type === 'error' || l.type === 'pageerror')
  if (errs.length === 0) {
    console.log('  （无）')
  } else {
    errs.forEach((l) => console.log(`  [${l.type}] ${l.text.slice(0, 200)}`))
  }

  console.log('\n' + '='.repeat(60))
  console.log('  结论')
  console.log('='.repeat(60))
  if (!finalPath.includes('login')) {
    console.log(`  ✓ 登录成功，跳到 ${finalPath}`)
  } else if (requests.length === 0) {
    console.log('  ✗ 前端没发登录请求 → 表单校验或按钮绑定有问题')
  } else if (responses.some((r) => r.status >= 400)) {
    console.log('  ✗ 请求发出但失败 → 看上面响应体')
  } else {
    console.log('  ✗ 请求成功但没跳转 → 路由或状态处理有问题')
  }

  await browser.close()
}

main().catch((e) => {
  console.error(`失败: ${e.message}`)
  process.exit(1)
})
