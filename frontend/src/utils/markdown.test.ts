/**
 * Markdown 渲染 + XSS 防护测试。
 *
 * ## 为什么这批测试重要
 *
 * 报告内容是**模型生成的**，而模型输出属于不可信输入：
 * 如果提示词被注入，或模型自己返回一段 HTML，
 * 直接 `v-html` 就会在当前页面执行任意 JS（偷 token、改数据）。
 *
 * 所以 `renderMarkdown` 必须做两件事，缺一不可：
 *   1. Markdown → HTML（marked）
 *   2. HTML → 清洗（DOMPurify）
 *
 * 这里的测试专门验第 2 步：常见 XSS payload 必须被剥掉。
 */
import { describe, it, expect } from 'vitest'
import { renderMarkdown } from '@/utils/markdown'

describe('renderMarkdown', () => {
  it('渲染标题和粗体', () => {
    const html = renderMarkdown('# 面试报告\n\n**问**：请自我介绍')
    expect(html).toContain('<h1')
    expect(html).toContain('面试报告')
    expect(html).toContain('<strong>问</strong>')
  })

  it('渲染列表', () => {
    const html = renderMarkdown('- 总轮次：6\n- 平均分：2.8')
    expect(html).toContain('<ul>')
    expect(html).toContain('<li>')
    expect(html).toContain('总轮次')
  })

  it('渲染分隔线', () => {
    const html = renderMarkdown('上文\n\n---\n\n下文')
    expect(html).toContain('<hr')
  })

  it('空输入返回空串（不报错）', () => {
    expect(renderMarkdown('')).toBe('')
    expect(renderMarkdown('   ')).toBe('')
  })

  // ==========================================================================
  // XSS —— 这几条是安全边界
  // ==========================================================================

  it('剥掉 <script>', () => {
    const html = renderMarkdown('正常内容\n\n<script>alert(1)</script>')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('alert(1)')
  })

  it('剥掉 onerror 事件属性', () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)">')
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('alert')
  })

  it('剥掉 javascript: 协议', () => {
    const html = renderMarkdown('[点我](javascript:alert(1))')
    expect(html).not.toMatch(/href="javascript:/i)
  })

  it('剥掉 iframe', () => {
    const html = renderMarkdown('<iframe src="//evil.test"></iframe>')
    expect(html).not.toContain('<iframe')
  })

  it('剥掉 onload / onclick 等所有 on* 属性', () => {
    const payloads = [
      '<div onload="alert(1)">x</div>',
      '<svg onclick="alert(1)"></svg>',
      '<body onpageshow="alert(1)">',
    ]
    for (const p of payloads) {
      const html = renderMarkdown(p)
      expect(html).not.toMatch(/on\w+\s*=/i)
    }
  })

  it('保留安全的链接（http/https）', () => {
    const html = renderMarkdown('[文档](https://example.com/doc)')
    expect(html).toContain('href="https://example.com/doc"')
  })

  it('剥掉 style 属性（防 CSS 注入做点击劫持）', () => {
    const html = renderMarkdown('<p style="position:fixed;inset:0">覆盖全屏</p>')
    expect(html).not.toContain('position:fixed')
  })

  it('保留行内代码，但剥掉代码里的 HTML 标签执行', () => {
    const html = renderMarkdown('用 `<script>` 标签不行')
    expect(html).toContain('<code>')
    // 代码内容被转义成文本，不会真的生成 script 标签
    expect(html).not.toMatch(/<script[^>]*>/i)
  })
})
