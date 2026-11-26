#!/usr/bin/env bash
# 把 README 渲染成 HTML，肉眼检查排版
set -u
ROOT=/mnt/e/桌面/实习项目/interview_agent
OUT=/tmp/readme-preview

cd "$ROOT/frontend" || exit 1
rm -rf "$OUT" && mkdir -p "$OUT"

# 图片目录要能被 HTML 找到（相对路径 docs/images/...）
ln -sfn "$ROOT/docs" "$OUT/docs" 2>/dev/null || cp -r "$ROOT/docs" "$OUT/"

cat > /tmp/render_readme.mjs <<'NODE'
import { marked } from 'marked'
import { readFileSync, writeFileSync } from 'node:fs'

const ROOT = '/mnt/e/桌面/实习项目/interview_agent'
const md = readFileSync(`${ROOT}/README.md`, 'utf-8')

const css = `
  body { font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
         max-width: 1000px; margin: 0 auto; padding: 32px 40px; line-height: 1.7; color: #1f2328; }
  h1 { font-size: 2em; border-bottom: 1px solid #d0d7de; padding-bottom: 8px; }
  h2 { font-size: 1.5em; border-bottom: 1px solid #d0d7de; padding-bottom: 6px; margin-top: 36px; }
  h3 { font-size: 1.15em; margin-top: 24px; }
  img { max-width: 100%; height: auto; border: 1px solid #d0d7de; border-radius: 6px; display: block; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; }
  td { padding: 10px; vertical-align: top; }
  code { background: #f6f8fa; padding: 2px 6px; border-radius: 4px; font-size: 0.88em; }
  pre { background: #f6f8fa; padding: 14px; border-radius: 6px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  blockquote { border-left: 4px solid #d0d7de; margin: 0; padding-left: 16px; color: #656d76; }
  a { color: #0969da; }
  hr { border: none; border-top: 1px solid #d0d7de; margin: 28px 0; }
  strong { color: #1f2328; }
`

const html = marked.parse(md, { async: false, gfm: true, breaks: false })
writeFileSync('/tmp/readme-preview/index.html',
  `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
   <title>README preview</title><style>${css}</style></head><body>${html}</body></html>`)

console.log('  已生成 index.html')

// 统计
const tables = (html.match(/<table>/g) || []).length
const rows = (html.match(/<tr>/g) || []).length
const imgs = (html.match(/<img[^>]+src="([^"]+)"/g) || [])
console.log(`  table: ${tables} 个, tr: ${rows} 行, img: ${imgs.length} 张`)
NODE

node /tmp/render_readme.mjs

echo ""
echo "=== 图片路径检查 ==="
grep -oE 'src="[^"]*docs/images/[^"]+"' "$OUT/index.html" 2>/dev/null | sed 's/^/  /' | head -12

echo ""
echo "=== badge 检查 ==="
grep -oE 'src="https://[^"]*badge[^"]*"' "$OUT/index.html" 2>/dev/null | sed 's/^/  /' | head -6
