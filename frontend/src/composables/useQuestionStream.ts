/**
 * SSE 流式出题的组合式函数。
 *
 * ## 用途
 *
 * 面试官的第一问（或任何需要"立刻看到字"的场景）用真流式显示，
 * 让用户 200-500ms 就看到面试官开始说话，而不是干等 5 秒。
 *
 * ## 为什么首问才用真流式
 *
 * `POST /sessions/{sid}/stream/ask` 会**再调一次模型**（真流式），
 * 而后续轮次的下一问已经在 `POST /answer` 里生成过了——
 * 为了流式再调一次属于重复付费。
 *
 * 所以：
 * - **首问** → 用真流式（用户刚点"开始"，最需要即时反馈）
 * - **后续轮** → 用回放流式 `GET /stream`（不花钱，秒出全句再逐字显示）
 *
 * ## 降级
 *
 * 后端在模型不支持流式时会自动退回回放模式（发同样的 SSE 帧），
 * 前端不需要区分。若整个 SSE 连接失败，走 `onError` 让调用方
 * 用普通 JSON 接口兜底。
 */
import { ref, onUnmounted } from 'vue'

export interface StreamHandlers {
  /** 收到一个 token */
  onToken?: (text: string) => void
  /** 流结束（带完整文本） */
  onDone?: (full: string) => void
  /** 出错 */
  onError?: (message: string) => void
}

export function useQuestionStream() {
  const streaming = ref(false)
  const partial = ref('')
  let es: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | null = null

  function close() {
    if (es) {
      es.close()
      es = null
    }
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    streaming.value = false
  }

  /**
   * 开始接收流。
   *
   * @param url SSE 端点（相对路径，走 vite 代理或 nginx 代理）
   * @param handlers 回调
   * @param timeoutMs 超时（毫秒）。SSE 是长连接，但"一直没收到 token"
   *                  说明后端卡住了，需要主动放弃，否则用户永远等。
   */
  function start(url: string, handlers: StreamHandlers, timeoutMs = 45000) {
    close()
    partial.value = ''
    streaming.value = true

    // EventSource 无法自定义 header，所以 token 通过 query 传。
    // 这是 SSE 的固有限制（后端也能从 cookie 读，但我们的 access token
    // 存在内存里，不在 cookie 中）。
    es = new EventSource(url)

    // 超时保护：45 秒没有任何 done/error 就放弃
    timer = setTimeout(() => {
      if (streaming.value) {
        close()
        handlers.onError?.('流式响应超时')
      }
    }, timeoutMs)

    es.addEventListener('start', () => {
      // 收到 start 说明连接正常，重置超时
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        if (streaming.value) {
          close()
          handlers.onError?.('流式响应中断')
        }
      }, timeoutMs)
    })

    es.addEventListener('token', (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data) as { text?: string }
        if (data.text) {
          partial.value += data.text
          handlers.onToken?.(data.text)
        }
      } catch {
        /* 单帧解析失败不影响后续 */
      }
    })

    es.addEventListener('done', (ev) => {
      let full = partial.value
      try {
        const data = JSON.parse((ev as MessageEvent).data) as { full?: string }
        if (data.full) full = data.full
      } catch {
        /* 用累积值 */
      }
      close()
      handlers.onDone?.(full)
    })

    es.addEventListener('error', (ev) => {
      // 注意：EventSource 自身的网络错误也走 onerror，但 data 为空。
      // 后端发的业务 error 事件有 data（含 message）。
      let msg = '流式连接中断'
      try {
        const raw = (ev as MessageEvent).data
        if (raw) {
          const data = JSON.parse(raw) as { message?: string }
          if (data.message) msg = data.message
        }
      } catch {
        /* 用默认文案 */
      }
      close()
      handlers.onError?.(msg)
    })

    // 浏览器原生断线重连会自己重试，但我们不想重复消费，
    // 所以 onerror 里统一 close（EventSource 会因 readyState=CLOSED 停止重连）
  }

  onUnmounted(close)

  return { streaming, partial, start, close }
}
