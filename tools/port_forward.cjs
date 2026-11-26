#!/usr/bin/env node
/**
 * 端口转发：0.0.0.0:18336 → 127.0.0.1:18326
 *
 * ## 为什么用 Node 而不是 Python
 *
 * 这个转发器**必须跑在 Windows 侧**（因为模型网关只监听 Windows 的 127.0.0.1）。
 * 但这台机器的 `python` 解析到 WindowsApps 的占位符（一跑就打开商店），
 * `py` 也没装。Node 是确定可用的（v24.16.0，前端就靠它构建）。
 *
 * ## 为什么不用 netsh portproxy
 *
 * 那是最标准的做法，但需要管理员权限。当前会话没有。
 * Node 的 net 模块做纯 TCP 搬运，效果一样且不需要提权。
 *
 * 用法：
 *   node tools/port_forward.cjs              # 18336 → 18326
 *   node tools/port_forward.cjs 18336 18326
 */
const net = require('net')

const LISTEN_PORT = parseInt(process.argv[2] || '18336', 10)
const TARGET_PORT = parseInt(process.argv[3] || '18326', 10)
const TARGET_HOST = process.argv[4] || '127.0.0.1'

let connections = 0

const server = net.createServer((client) => {
  connections++
  if (connections % 20 === 1) {
    console.log(`  [${connections}] 连接来自 ${client.remoteAddress}:${client.remotePort}`)
  }

  const upstream = net.connect(TARGET_PORT, TARGET_HOST)

  // 任一方向出错都要关掉两边，否则会泄漏 fd
  const cleanup = () => {
    client.destroy()
    upstream.destroy()
  }

  client.on('error', cleanup)
  upstream.on('error', (e) => {
    if (e.code !== 'ECONNRESET') {
      console.log(`  [上游错误] ${e.code}`)
    }
    cleanup()
  })

  client.pipe(upstream)
  upstream.pipe(client)

  client.on('close', () => upstream.destroy())
  upstream.on('close', () => client.destroy())
})

server.on('error', (e) => {
  console.error(`  监听失败：${e.message}`)
  process.exit(1)
})

server.listen(LISTEN_PORT, '0.0.0.0', () => {
  console.log(`端口转发已启动：0.0.0.0:${LISTEN_PORT} → ${TARGET_HOST}:${TARGET_PORT}`)
  console.log(`  WSL 里请用：http://<宿主IP>:${LISTEN_PORT}/v1`)
  console.log('  Ctrl+C 停止')
})
