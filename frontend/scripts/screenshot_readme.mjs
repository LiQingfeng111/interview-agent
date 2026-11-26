/**
 * 截取 GitHub 上的 README 渲染效果，用来验证排版。
 *
 * ## 为什么直接截 GitHub 页面
 *
 * 本地渲染要处理 marked 的 ESM import、图片相对路径、CSS 模拟，
 * 折腾半天还可能和 GitHub 的真实渲染不一致（它会跑自己的 sanitizer
 * + tables 样式）。
 *
 * 直接打开真实页面截图，看到的就是别人看到的样子。
 *
 * ## 用法
 *
 *   node frontend/scripts/screenshot_readme.mjs
 *
 * 输出：docs/images/readme-preview.png（整页长图）
 */

import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'docs', 'images')
const URL = process.env.README_URL ||
  'https://github.com/LiQingfeng111/interview-agent'

const log = (m) => console.log(`  ${m}`)

async function launch() {
  for (const [channel, label] of [['msedge', '系统 Edge'], ['chrome', '系统 Chrome']]) {
    try {
      const b = await chromium.launch({ channel })
      log(`浏览器: ${label}`)
      return b
    } catch { /* 下一个 */ }
  }
  const b = await chromium.launch()
  log('浏览器: Playwright Chromium')
  return b
}

async function main() {
  await mkdir(OUT, { recursive: true })
  log(`目标: ${URL}`)

  const browser = await launch()
  try {
    const ctx = await browser.newContext({
      viewport: { width: 1280, height: 900 },
      deviceScaleFactor: 1.5,
      locale: 'zh-CN',
    })
    const page = await ctx.newPage()

    // `networkidle` 在 GitHub 上会超时：页面有长轮询和埋点请求，
    // 永远不会"网络空闲"。改成 domcontentloaded + 显式等元素。
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 90000 })

    // 等 README 真正渲染出来（GitHub 是异步注入的）
    try {
      await page.waitForSelector('article.markdown-body', { timeout: 30000 })
      log('README 区域已出现')
    } catch {
      log('! 等不到 README 区域，可能需要登录或页面结构变了')
    }
    await page.waitForTimeout(2000)

    // 等图片加载完（README 里 8 张图）
    await page.evaluate(async () => {
      const imgs = [...document.querySelectorAll('article img')]
      await Promise.all(
        imgs.map((img) =>
          img.complete
            ? Promise.resolve()
            : new Promise((r) => {
                img.onload = r
                img.onerror = r
                setTimeout(r, 5000)
              }),
        ),
      )
    })
    await page.waitForTimeout(1000)

    // 只截 README 区域（不要导航栏和侧边栏）
    const article = page.locator('article.markdown-body').first()
    const hasArticle = await article.count()

    if (hasArticle) {
      const stat = await page.evaluate(() => {
        const a = document.querySelector('article.markdown-body')
        if (!a) return null
        return {
          height: a.scrollHeight,
          imgs: a.querySelectorAll('img').length,
          tables: a.querySelectorAll('table').length,
        }
      })
      log(`README 区域: ${stat.height}px 高, ${stat.imgs} 张图, ${stat.tables} 个表格`)

      await article.screenshot({
        path: join(OUT, 'readme-preview.png'),
        animations: 'disabled',
      })
      log('✓ readme-preview.png')
    } else {
      log('! 找不到 README 区域，截整页')
      await page.screenshot({ path: join(OUT, 'readme-preview.png'), fullPage: true })
      log('✓ readme-preview.png（整页）')
    }

    // 另外截顶部（badge + 首图），用来快速看开头效果
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.waitForTimeout(500)
    await page.screenshot({
      path: join(OUT, 'readme-top.png'),
      clip: { x: 0, y: 0, width: 1280, height: 900 },
      animations: 'disabled',
    })
    log('✓ readme-top.png')
  } finally {
    await browser.close()
  }
}

main().catch((e) => {
  console.error(`失败: ${e.message}`)
  process.exit(1)
})
