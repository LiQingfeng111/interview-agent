/**
 * 自动截图：用 Playwright 走一遍真实用户路径，输出 README 用的截图。
 *
 * ## 为什么用脚本而不是手动截图
 *
 * 1. **可重复**：UI 改了重跑一次就更新，不会忘记更新文档里的图
 * 2. **能造中间态**：脚本知道怎么"停在上传进度 60% 那一刻"，手动很难截到
 * 3. **一次出多种尺寸**：桌面 + 移动，手动要摆两次浏览器
 *
 * ## 关键实现说明
 *
 * ### 复用系统浏览器
 *
 * `npx playwright install chromium` 要下 207MB，本机实测从 CDN 下载极慢
 * （20 分钟到 20%）。而 Windows 自带 Edge 就是 Chromium 内核，
 * Playwright 可以直接驱动：`chromium.launch({ channel: 'msedge' })`。
 *
 * ### 选择器要避开"隐藏元素"
 *
 * Element Plus 的 `<el-tabs>` 会把未激活的面板**保留在 DOM 里但设为不可见**。
 * 所以 `page.locator('input').first()` 拿到的可能是注册面板的输入框（不可见），
 * `fill()` 会超时。
 *
 * 解法：用 `:visible` 前缀，只选当前可见的输入框。
 *
 * ## 用法
 *
 *   # 前后端都要先起来
 *   node scripts/screenshot.mjs
 *
 *   # 自定义地址
 *   BASE_URL=http://127.0.0.1:5173 node scripts/screenshot.mjs
 *
 * ## 输出
 *
 *   ../docs/images/*.png（项目根的 docs/images，不是 frontend/docs）
 */

import { chromium } from 'playwright'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
// 注意：脚本在 frontend/scripts/，输出要放到项目根的 docs/images
const PROJECT_ROOT = join(__dirname, '..', '..')
const OUT_DIR = join(PROJECT_ROOT, 'docs', 'images')
const BASE = process.env.BASE_URL || 'http://localhost:5173'

const VIEWPORTS = {
  desktop: { width: 1440, height: 900, deviceScaleFactor: 2 },
  mobile: { width: 390, height: 844, deviceScaleFactor: 3 },
}

const shots = []
const pageErrors = []

const log = (m) => console.log(`  ${m}`)

async function shot(page, name, opts = {}) {
  const path = join(OUT_DIR, `${name}.png`)
  await page.screenshot({ path, animations: 'disabled', ...opts })
  shots.push(name)
  log(`✓ ${name}.png`)
}

// ============================================================================
// 浏览器启动
// ============================================================================

async function launchBrowser() {
  for (const [channel, label] of [['msedge', '系统 Edge'], ['chrome', '系统 Chrome']]) {
    try {
      const b = await chromium.launch({ channel })
      log(`浏览器: ${label}（复用系统安装）`)
      return b
    } catch {
      /* 试下一个 */
    }
  }
  try {
    const b = await chromium.launch()
    log('浏览器: Playwright 自带 Chromium')
    return b
  } catch {
    throw new Error(
      '找不到浏览器。二选一：\n' +
        '  A) 用系统 Edge/Chrome（Windows 默认有 Edge）\n' +
        '  B) npx playwright install chromium（要下 207MB）',
    )
  }
}

// ============================================================================
// 页面操作
// ============================================================================

/** 只取当前可见的输入框（Element Plus 的隐藏面板会干扰） */
function visibleInputs(page) {
  return page.locator('input:visible')
}

async function switchTab(page, label) {
  const tab = page.locator(`.el-tabs__item:has-text("${label}")`).first()
  if (await tab.count()) {
    await tab.click()
    await page.waitForTimeout(500)
  }
}

async function ensureAccount(page, user, pass) {
  // ---- 诊断：先看页面上有哪些可见控件
  const diag = await page.evaluate(() => {
    const vis = (el) => {
      const r = el.getBoundingClientRect()
      return r.width > 0 && r.height > 0
    }
    return {
      inputs: [...document.querySelectorAll('input')]
        .filter(vis)
        .map((e) => ({ type: e.type, ph: e.placeholder })),
      buttons: [...document.querySelectorAll('button')]
        .filter(vis)
        .map((e) => e.textContent.trim())
        .filter(Boolean)
        .slice(0, 8),
      tabs: [...document.querySelectorAll('.el-tabs__item')].map((e) => e.textContent.trim()),
    }
  })
  log(`  可见 input: ${JSON.stringify(diag.inputs)}`)
  log(`  可见 button: ${JSON.stringify(diag.buttons)}`)
  log(`  tabs: ${JSON.stringify(diag.tabs)}`)

  // ---- 注册
  await switchTab(page, '注册')
  const regInputs = visibleInputs(page)
  const n = await regInputs.count()
  log(`  注册面板可见输入框: ${n} 个`)

  if (n >= 2) {
    await regInputs.nth(0).fill(user)
    await regInputs.nth(1).fill(pass)
    if (n >= 3) await regInputs.nth(2).fill(pass)

    // 注册按钮：精确匹配「注册」，避免匹配到 tab
    const btn = page.locator('button:visible').filter({ hasText: '注册' }).last()
    if (await btn.count()) {
      await btn.click()
      log('  已点注册')
      await page.waitForTimeout(3000)
      // 注册成功后表单会切回登录（代码里 tab.value = 'login'）
    } else {
      log('  ! 找不到注册按钮')
    }
  }

  // ---- 登录
  await switchTab(page, '登录')
  const loginInputs = visibleInputs(page)
  const ln = await loginInputs.count()
  log(`  登录面板可见输入框: ${ln} 个`)
  if (ln < 2) throw new Error('登录表单找不到可见输入框')

  await loginInputs.nth(0).fill(user)
  await loginInputs.nth(1).fill(pass)

  // 用 .last() 避免命中 tab（tab 的 DOM 不是 button，但保险起见）
  const loginBtn = page.locator('button:visible').filter({ hasText: '登录' }).last()
  if (await loginBtn.count()) {
    await loginBtn.click()
    log('  已点登录')
  } else {
    await loginInputs.nth(1).press('Enter')
    log('  已回车登录')
  }

  // 等路由跳转（history 模式下 path 会变成 /interview）
  let path = ''
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(500)
    path = await page.evaluate(() => location.pathname)
    if (!path.includes('login')) break
  }

  if (path.includes('login')) {
    // 抓一下页面上的错误提示，比只报"登录失败"有用
    const msg = await page
      .evaluate(() => {
        const el = document.querySelector('.el-message')
        return el ? el.textContent.trim() : ''
      })
      .catch(() => '')
    throw new Error(`登录后仍在 ${path}${msg ? `（页面提示：${msg}）` : ''}`)
  }
  log(`登录成功（${path}）`)
}

async function uploadResume(page) {
  const input = page.locator('input[type="file"]').first()
  if (!(await input.count())) {
    log('  没找到 file input')
    return false
  }

  const resume = [
    '姓名：演示用户   求职方向：AI 应用开发',
    '',
    '项目一：课程问答助手（2026.03 - 2026.09）',
    '背景：同学找课程答案要翻 300 份 PDF，平均 8 分钟',
    '做法：RAG 检索，切块 500 字，top20 重排，bge-m3 做 embedding',
    '结果：召回从 71% 提到 82%，P99 延迟 1.8 秒',
    '代价：索引体积增加 30%',
    '我负责：切块策略实验、评测集标注、重排模块实现',
    '',
    '项目二：智能客服工单分类（2026.01 - 2026.02）',
    '做法：规则兜底 + 小模型微调',
    '结果：准确率 0.86',
  ].join('\n')

  await input.setInputFiles({
    name: 'demo_resume.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(resume, 'utf-8'),
  })
  log('  文件已选择')
  return true
}

async function waitResumeReady(page, maxMs = 25000) {
  const deadline = Date.now() + maxMs
  while (Date.now() < deadline) {
    // "已就绪"标签出现即算好
    const ready = page.locator('text=已就绪').first()
    if (await ready.count()) return true
    await page.waitForTimeout(500)
  }
  return false
}

async function startInterview(page) {
  const btn = page.locator('button:visible').filter({ hasText: /开始面试/ }).first()
  if (!(await btn.count())) {
    log('  找不到"开始面试"')
    return false
  }
  if (await btn.isDisabled()) {
    log('  "开始面试"不可点')
    return false
  }
  await btn.click()
  log('  已点击，等首问…')
  await page.waitForTimeout(8000)
  return true
}

async function answerOnce(page) {
  const ta = page.locator('textarea:visible').last()
  if (!(await ta.count())) {
    log('  找不到回答框')
    return false
  }
  await ta.fill(
    '切块 500 字是 300/500/800 三档实测选的。召回从 71% 提到 82%，' +
      '代价是索引体积多 30%。P99 延迟 1.8 秒，主要在重排那一步。',
  )
  const btn = page.locator('button:visible').filter({ hasText: /提交回答/ }).first()
  if (await btn.count()) {
    await btn.click()
    await page.waitForTimeout(7000)
    return true
  }
  return false
}

async function finishAndReport(page) {
  // 策略 1：直接从页面内存里拿 sid（最可靠）
  //
  // 为什么要这样：点"提前结束 → 确认"这条路有随机性
  // （弹窗动画、按钮文案匹配、会话是否已自然结束）。
  // 而 session store 本来就是 pinia 挂在全局的，能直接读。
  try {
    const sid = await page.evaluate(() => {
      // pinia 实例挂在 app 上；退而求其次读 localStorage / 全局变量
      const w = window
      // @ts-ignore
      const pinia = w.__PINIA__ || w.pinia
      if (pinia && pinia.state && pinia.state.value && pinia.state.value.session) {
        return pinia.state.value.session.sid || ''
      }
      return ''
    })
    if (sid) {
      log(`  从 store 拿到 sid: ${sid}`)
      return sid
    }
  } catch {
    /* 继续走策略 2 */
  }

  // 策略 2：点"提前结束"
  const btn = page.locator('button:visible').filter({ hasText: /提前结束/ }).first()
  if (!(await btn.count())) {
    log('  找不到"提前结束"按钮')
    return null
  }
  await btn.click()
  await page.waitForTimeout(1200)

  const confirm = page
    .locator('.el-message-box button:visible')
    .filter({ hasText: /^结束$|确定/ })
    .first()
  if (await confirm.count()) {
    await confirm.click()
    await page.waitForTimeout(4000)
  }

  let path = await page.evaluate(() => location.pathname)
  for (let i = 0; i < 20; i++) {
    if (path.includes('/report')) break
    await page.waitForTimeout(500)
    path = await page.evaluate(() => location.pathname)
  }

  const m = path.match(/report\/([^/?]+)/)
  if (m) return m[1]

  // 策略 3：从历史列表里取最近一场
  try {
    await page.goto(`${BASE}/history`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(2000)
    const link = page.locator('a[href*="/report/"]').first()
    if (await link.count()) {
      const href = await link.getAttribute('href')
      const mm = (href || '').match(/report\/([^/?]+)/)
      if (mm) {
        log(`  从历史列表拿到 sid: ${mm[1]}`)
        return mm[1]
      }
    }
  } catch {
    /* 放弃 */
  }

  return null
}

// ============================================================================
// 主流程
// ============================================================================

async function main() {
  await mkdir(OUT_DIR, { recursive: true })
  log(`目标地址: ${BASE}`)
  log(`输出目录: ${OUT_DIR}`)
  console.log('')

  const browser = await launchBrowser()

  try {
    const ctx = await browser.newContext({
      viewport: { width: VIEWPORTS.desktop.width, height: VIEWPORTS.desktop.height },
      deviceScaleFactor: VIEWPORTS.desktop.deviceScaleFactor,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      reducedMotion: 'reduce',
    })
    const page = await ctx.newPage()
    page.on('pageerror', (e) => pageErrors.push(e.message))

    // ---- 1. 登录页
    log('1) 登录页')
    await page.goto(BASE, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1000)
    await shot(page, '01-login')

    // ---- 2. 登录（注册 + 登录）
    log('2) 注册并登录')
    const user = `demo_${Date.now().toString(36)}`
    const pass = 'demo123456'
    await ensureAccount(page, user, pass)
    await page.waitForTimeout(1500)

    // ---- 3. 面试配置页
    //
    // 顺序很重要：先上传简历，**再**截配置页。
    // 否则截到的是"简历框空着、开始按钮灰色"的状态，
    // 看起来像页面没做完。
    log('3) 面试页（配置态）')
    await page.goto(`${BASE}/interview`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1500)

    // ---- 4. 上传简历
    log('4) 简历上传')
    let uploaded = false
    try {
      uploaded = await uploadResume(page)
      if (uploaded) {
        // 先截"上传中/就绪"那一张（有进度条或绿色已就绪标签）
        await page.waitForTimeout(1200)
        await shot(page, '03-upload-progress')
        const ready = await waitResumeReady(page)
        log(`  向量化${ready ? '完成' : '超时（继续）'}`)
      }
    } catch (e) {
      log(`  上传失败: ${e.message.slice(0, 60)}`)
    }

    // 上传完再截配置页：这时按钮可点、简历已就绪，界面是"完整状态"
    if (uploaded) {
      await page.waitForTimeout(800)
      await shot(page, '02-interview-setup')
    } else {
      // 没上传成功也要有一张，至少展示表单结构
      await shot(page, '02-interview-setup')
    }

    // ---- 5. 面试对话
    let sid = null
    if (uploaded) {
      log('5) 面试对话')
      try {
        if (await startInterview(page)) {
          await answerOnce(page)
          await page.waitForTimeout(1500)
          await shot(page, '04-interview-chat')
        }
      } catch (e) {
        log(`  对话失败: ${e.message.slice(0, 60)}`)
      }

      // ---- 6. 报告
      log('6) 报告页')
      try {
        sid = await finishAndReport(page)
        if (sid) {
          await page.goto(`${BASE}/report/${sid}`, { waitUntil: 'networkidle' })
          await page.waitForTimeout(2500) // 等雷达图 canvas
          await shot(page, '05-report')
        } else {
          log('  拿不到会话 id，跳过')
        }
      } catch (e) {
        log(`  报告失败: ${e.message.slice(0, 60)}`)
      }
    }

    // ---- 7. 题库
    log('7) 题库')
    try {
      await page.goto(`${BASE}/bank`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(1800)
      await shot(page, '06-bank')
    } catch (e) {
      log(`  题库失败: ${e.message.slice(0, 60)}`)
    }

    // ---- 8. 历史
    log('8) 历史')
    try {
      await page.goto(`${BASE}/history`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(1500)
      await shot(page, '07-history')
    } catch {
      log('  历史页跳过')
    }

    await ctx.close()

    // ---- 9. 移动端
    log('9) 移动端')
    const mctx = await browser.newContext({
      viewport: { width: VIEWPORTS.mobile.width, height: VIEWPORTS.mobile.height },
      deviceScaleFactor: VIEWPORTS.mobile.deviceScaleFactor,
      locale: 'zh-CN',
      isMobile: true,
      hasTouch: true,
      reducedMotion: 'reduce',
    })
    const mp = await mctx.newPage()
    await mp.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
    await mp.waitForTimeout(1000)
    await shot(mp, '08-mobile-login')
    await mctx.close()

    // ---- 汇总
    console.log('')
    if (pageErrors.length) {
      console.log(`页面报错 ${pageErrors.length} 条：`)
      for (const e of pageErrors.slice(0, 5)) console.log(`  ! ${e.slice(0, 110)}`)
      console.log('')
    }

    console.log(`完成 ${shots.length} 张：${shots.join(', ')}`)
    await writeFile(
      join(OUT_DIR, 'index.json'),
      JSON.stringify({ base: BASE, demoUser: user, shots, at: new Date().toISOString() }, null, 2),
    )
    console.log(`目录: ${OUT_DIR}`)
  } finally {
    await browser.close()
  }
}

main().catch((e) => {
  console.error(`\n截图失败: ${e.message}`)
  if (pageErrors.length) {
    console.error('页面报错：')
    for (const x of pageErrors.slice(0, 5)) console.error(`  ! ${x.slice(0, 120)}`)
  }
  process.exit(1)
})
