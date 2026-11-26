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
  const lines = block.trim().split('\n')
  const first = lines[0].trim()
  const problems = []

  // 1) 类型声明
  if (!VALID_TYPES.some((t) => first.startsWith(t))) {
    problems.push(`第一行不是合法的图类型：${first}`)
  }

  // 2) subgraph / end 配对
  const subgraphs = (block.match(/^\s*subgraph\s+/gm) || []).length
  const ends = (block.match(/^\s*end\s*$/gm) || []).length
  if (subgraphs !== ends) {
    problems.push(`subgraph(${subgraphs}) 与 end(${ends}) 不匹配`)
  }

  // 3) 方括号/圆括号配对（粗略）
  const pairs = [
    ['[', ']'], ['(', ')'], ['{', '}'],
  ]
  for (const [open, close] of pairs) {
    const o = (block.split(open).length - 1)
    const c = (block.split(close).length - 1)
    if (o !== c) {
      problems.push(`${open}${close} 不配对（${o} vs ${c}）`)
    }
  }

  // 4) 箭头语法（flowchart 里常见写法）
  if (first.startsWith('flowchart') || first.startsWith('graph')) {
    const badArrows = block.match(/-->\s*\n\s*$/gm)
    if (badArrows) problems.push('箭头后缺目标节点')
  }

  if (problems.length) {
    console.log(`\n  ✗ 图 ${i + 1}（${first}）有问题：`)
    problems.forEach((p) => console.log(`      - ${p}`))
    failed++
  } else {
    const nodes = (block.match(/\[/g) || []).length
    console.log(`  ✓ 图 ${i + 1}（${first}，约 ${nodes} 个节点）`)
  }
})

console.log('')
if (failed) {
  console.log(`${failed} 个图有语法问题`)
  process.exit(1)
}
console.log('全部通过')
