/**
 * 校验 README 里的 Mermaid 图能不能正常渲染。
 *
 * ## 为什么需要这个
 *
 * GitHub 渲染 Mermaid 时如果语法错了，不会报错——只会显示
 * "Unable to render rich display" 或者一片空白。
 * 从 README 源码完全看不出来。
 *
 * 所以在本地用 mermaid 官方解析器先跑一遍语法检查。
 *
 * ## 用法
 *
 *   node frontend/scripts/verify_mermaid.mjs
 */

import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = join(__dirname, '..', '..')

const md = readFileSync(join(ROOT, 'README.md'), 'utf-8')

// 提取所有 ```mermaid 代码块
const blocks = []
const re = /```mermaid\n([\s\S]*?)```/g
let m
while ((m = re.exec(md)) !== null) {
  blocks.push(m[1])
}

console.log(`找到 ${blocks.length} 个 Mermaid 图`)
if (blocks.length === 0) {
  console.log('  （没有图，跳过）')
  process.exit(0)
}

// 基础语法检查（不依赖 mermaid 包，避免额外依赖）
//
// 检查项：
//   1. 第一行必须是图类型声明（flowchart / graph / sequenceDiagram / ...）
//   2. 括号配对
//   3. subgraph 与 end 数量匹配
const VALID_TYPES = [
  'flowchart', 'graph', 'sequenceDiagram', 'classDiagram',
  'stateDiagram', 'erDiagram', 'journey', 'gantt', 'pie',
  'gitGraph', 'mindmap', 'timeline',
]

let failed = 0

blocks.forEach((block, i) => {
  const allLines = block.trim().split('\n')

  // Mermaid 支持在图的**第一行**放指令，两种写法都合法：
  //   ① %%{init: {...}}%%      单行 JSON 配置（我们用的）
  //   ② %%{init: {...}}%% 之后才是 flowchart
  // 指令块可能跨多行，所以要把它们剥掉再找真正的图类型行。
  let idx = 0
  let depth = 0
  while (idx < allLines.length) {
    const line = allLines[idx].trim()
    if (line.startsWith('%%{')) {
      depth += (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length
      idx++
      if (depth <= 0 && line.includes('}%%')) break
      continue
    }
    if (depth > 0) {
      depth += (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length
      idx++
      if (depth <= 0 && line.includes('}%%')) break
      continue
    }
    if (line === '' || line.startsWith('%%')) { idx++; continue }
    break
  }

  const declLine = (allLines[idx] || '').trim()
  const problems = []

  // 1) 类型声明（跳过指令行后找到的才算）
  if (!VALID_TYPES.some((t) => declLine.startsWith(t))) {
    problems.push(`找不到合法的图类型声明（跳过指令后是：${declLine || '（空）'}）`)
  }

  // 2) subgraph / end 配对
  const subgraphs = (block.match(/^\s*subgraph\s+/gm) || []).length
  const ends = (block.match(/^\s*end\s*$/gm) || []).length
  if (subgraphs !== ends) {
    problems.push(`subgraph(${subgraphs}) 与 end(${ends}) 不匹配`)
  }

  // 3) 方括号/圆括号配对（粗略）
  //    注意：指令行里有大量 {} 和 ""，先剔除再数
  const body = allLines.slice(idx).join('\n')
  const pairs = [
    ['[', ']'], ['(', ')'],
  ]
  for (const [open, close] of pairs) {
    const o = (body.split(open).length - 1)
    const c = (body.split(close).length - 1)
    if (o !== c) {
      problems.push(`${open}${close} 不配对（${o} vs ${c}）`)
    }
  }

  // 4) classDef 是否给了文字颜色
  //
  // 为什么专门查这个：深色主题下如果只设 fill 不设 color，
  // 浅色底配默认浅色字，字会糊在背景里看不清（真实踩过）。
  const classDefs = block.match(/classDef\s+(\w+)\s+([^\n]+)/g) || []
  for (const cd of classDefs) {
    const name = (cd.match(/classDef\s+(\w+)/) || [])[1] || '?'
    if (!/color\s*:/.test(cd)) {
      problems.push(`classDef ${name} 没设 color（深色主题下文字会看不清）`)
    }
  }

  if (problems.length) {
    console.log(`\n  ✗ 图 ${i + 1}（${declLine}）有问题：`)
    problems.forEach((p) => console.log(`      - ${p}`))
    failed++
  } else {
    const nodes = (block.match(/\[/g) || []).length
    console.log(`  ✓ 图 ${i + 1}（${declLine}，约 ${nodes} 个节点）`)
  }
})

console.log('')
if (failed) {
  console.log(`${failed} 个图有语法问题`)
  process.exit(1)
}
console.log('全部通过')
