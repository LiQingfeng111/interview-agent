/**
 * 把 README 里的 Mermaid 图渲染成图片，肉眼确认。
 *
 * ## 为什么光校验语法不够
 *
 * 语法正确不等于渲染正确：可能是布局塌成一坨、节点重叠、
 * 或者 GitHub 的 mermaid 版本不认某个语法糖。
 * 只有真渲染一次才能确认。
 *
 * ## 做法
 *
 * 用 Playwright 打开一个本地 HTML，里面引入 mermaid 的 CDN，
 * 把 README 里的图渲染出来，然后截图。
 *
 * 注意：需要网络加载 mermaid CDN。如果加载失败会明确报错。
 *
 * ## 用法
 *
 *   node frontend/scripts/render_mermaid.mjs
 *
 * 输出：docs/images/architecture.png
 */

import { chromium } from 'playwright'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = join(__dirname, '..', '..')
const OUT_DIR = join(ROOT, 'docs', 'images')
const TMP = join(ROOT, 'frontend', '.mermaid-preview.html')

const md = readFileSync(join(ROOT, 'README.md'), 'utf-8')
const blocks = []
const re = /```mermaid\n([\s\S]*?)```/g
let m
while ((m = re.exec(md)) !== null) blocks.push(m[1].trim())

if (!blocks.length) {
  console.log('README 里没有 Mermaid 图')
  process.exit(0)
}

console.log(`找到 ${blocks.length} 个图，开始渲染…`)

// 写一个临时 HTML，用 mermaid CDN 渲染
const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body { margin: 0; padding: 24px; background: #fff;
         font-family: -apple-system, 'Segoe UI', sans-serif; }
  .mermaid { margin: 0 auto; text-align: center; }
  .mermaid svg { max-width: 100%; height: auto; }
  .err { color: #c00; font-size: 13px; white-space: pre-wrap; text-align: left; }
</style>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({
    startOnLoad: false,
    theme: 'default',
    // 关键：不要 useMaxWidth: false。
    // 那个设置会让 SVG 用 intrinsic 尺寸（很宽），
    // 在固定 viewport 里会被裁掉右边 —— 实测第一版就是这么塌的。
    // 用 true 让它自适应容器宽度。
    flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis' },
    securityLevel: 'loose',
  });
  window.__mermaid = mermaid;
  window.__ready = true;
</script>
</head><body>
${blocks.map((b, i) => `<div class="mermaid" id="d${i}">${b.replace(/</g, '&lt;')}</div>`).join('\n')}
</body></html>`

writeFileSync(TMP, html, 'utf-8')

async function launch() {
  for (const [channel, label] of [['msedge', 'Edge'], ['chrome', 'Chrome']]) {
    try {
      const b = await chromium.launch({ channel })
      console.log(`  浏览器: ${label}`)
      return b
    } catch { /* next */ }
  }
  return chromium.launch()
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true })
  const browser = await launch()

  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1400 }, deviceScaleFactor: 2 })

    // CDN 可能慢，给足时间
    await page.goto('file://' + TMP.replace(/\\/g, '/'), {
      waitUntil: 'domcontentloaded', timeout: 30000,
    })

    // 等 mermaid 模块加载
    try {
      await page.waitForFunction(() => window.__ready === true, { timeout: 30000 })
      console.log('  mermaid 已加载')
    } catch {
      console.log('  ! mermaid CDN 加载失败（网络问题），跳过渲染')
      await browser.close()
      process.exit(1)
    }

    // 渲染
    const result = await page.evaluate(async (n) => {
      const errors = []
      for (let i = 0; i < n; i++) {
        const el = document.getElementById(`d${i}`)
        try {
          const { svg } = await window.__mermaid.render(`svg${i}`, el.textContent)
          el.innerHTML = svg
        } catch (e) {
          errors.push(`图 ${i + 1}: ${e.message}`)
          el.innerHTML = `<pre class="err">${e.message}</pre>`
        }
      }
      return errors
    }, blocks.length)

    if (result.length) {
      console.log('  渲染错误：')
      result.forEach((e) => console.log(`    ✗ ${e}`))
    } else {
      console.log('  ✓ 全部渲染成功')
    }

    await page.waitForTimeout(1500)

    // 逐个截图
    for (let i = 0; i < blocks.length; i++) {
      const el = page.locator(`#d${i}`)
      const name = blocks.length === 1 ? 'architecture' : `architecture-${i + 1}`
      await el.screenshot({ path: join(OUT_DIR, `${name}.png`) })
      const box = await el.boundingBox()
      console.log(`  ✓ ${name}.png  ${Math.round(box.width)}×${Math.round(box.height)}`)
    }

    await browser.close()
    process.exit(result.length ? 1 : 0)
  } catch (e) {
    console.error(`失败: ${e.message}`)
    await browser.close()
    process.exit(1)
  }
}

main()
