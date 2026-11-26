/**
 * HTTP 客户端：拦截器统一处理 401/42004（静默刷新）、错误提示。
 *
 * ## 为什么 access token 存在内存变量而不是 localStorage
 *
 * localStorage 能被页面里任何 JS 读到。一旦有 XSS（哪怕来自第三方统计脚本），
 * 票就被偷走了。存内存变量的话，脚本注入也拿不到（除非它当时正在运行）。
 *
 * 代价：刷新页面就丢。所以配 refresh token（HttpOnly Cookie，JS 读不到），
 * 刷新时静默换新 access —— 用户完全无感。
 *
 * ## 42004 为什么要静默处理
 *
 * access 只有 15 分钟。如果每次过期都弹"登录已过期"，用户会疯。
 * 拦截器收到 42004 就自动调 /auth/refresh，成功则重发原请求。
 * 只有 refresh 也失效（42005）才真的跳登录页。
 */
import axios, { type AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import type { ApiResponse, TokenPayload } from './types'

const http = axios.create({
  baseURL: '/api',
  timeout: 120_000, // 模型调用可能慢，给足 2 分钟
  withCredentials: true, // 带上 refresh 的 HttpOnly Cookie
})

// ---------------------------------------------------------------- token（内存）
let accessToken = ''
let onAuthLost: (() => void) | null = null

export function setAccessToken(t: string) {
  accessToken = t
}

export function getAccessToken() {
  return accessToken
}

/** 登录态彻底失效时由 App 注册回调（跳登录页） */
export function setAuthLostHandler(fn: () => void) {
  onAuthLost = fn
}

// ---------------------------------------------------------------- 请求拦截
http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`
  }
  return config
})

// ---------------------------------------------------------------- 响应拦截
interface RetriableConfig extends InternalAxiosRequestConfig {
  _retried?: boolean
}

let refreshing: Promise<string> | null = null

async function refreshToken(): Promise<string> {
  // 并发请求同时过期时，只发一次 refresh（避免 refresh 被轮换多次而互相失效）
  if (refreshing) return refreshing
  refreshing = (async () => {
    const resp = await axios.post<ApiResponse<TokenPayload>>(
      '/api/auth/refresh', {}, { withCredentials: true },
    )
    const tok = resp.data?.data?.access_token || ''
    if (!tok) throw new Error('refresh failed')
    setAccessToken(tok)
    return tok
  })()
  try {
    return await refreshing
  } finally {
    refreshing = null
  }
}

http.interceptors.response.use(
  (resp) => {
    // 后端统一包 {code, data, message}；成功时把 data 摊出来
    const body = resp.data as ApiResponse
    if (body && typeof body.code === 'number' && body.code !== 0) {
      ElMessage.error(body.message || '请求失败')
      return Promise.reject(new Error(body.message))
    }
    return resp
  },
  async (error: AxiosError<ApiResponse>) => {
    const status = error.response?.status
    const body = error.response?.data
    const code = body?.code
    const cfg = error.config as RetriableConfig | undefined

    // ---- 42004：access 过期 → 静默刷新后重发
    if (code === 42004 && cfg && !cfg._retried) {
      cfg._retried = true
      try {
        await refreshToken()
        return http.request(cfg as AxiosRequestConfig)
      } catch {
        onAuthLost?.()
        return Promise.reject(error)
      }
    }

    // ---- 42005/42006：refresh 也失效 → 跳登录
    if (code === 42005 || code === 42006 || (status === 401 && code !== 42004)) {
      onAuthLost?.()
      return Promise.reject(error)
    }

    // ---- 43xxx：降级类，轻提示（不阻断）
    if (code && code >= 43001 && code <= 43006) {
      ElMessage.warning(body?.message || '服务降级中')
      return Promise.reject(error)
    }

    // ---- 其余：正常报错
    ElMessage.error(body?.message || `请求失败（${status ?? '网络错误'}）`)
    return Promise.reject(error)
  },
)

/** 从包好的响应里取 data */
function unwrap<T>(resp: { data: ApiResponse<T> }): T {
  return (resp.data.data ?? {}) as T
}

export { http, unwrap }
