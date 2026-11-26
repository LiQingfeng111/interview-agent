/**
 * 按领域分组的接口封装。
 *
 * 为什么不直接在组件里调 http.get('/sessions')：
 * 接口路径一变要改遍所有组件。集中在这里，改一处即可。
 */
import { http, unwrap, setAccessToken, getAccessToken } from './client'
import type {
  AnswerResult, BankHit, BankStats, DocumentStatus, DocumentUpload,
  HealthSnapshot, Profile, ReportPayload, SessionItem, SessionStart, TokenPayload,
} from './types'

/**
 * SSE 端点要拼完整 URL。
 *
 * 为什么不用 axios 的 baseURL：`EventSource` 是浏览器原生 API，
 * 它不知道 axios 实例上配的 baseURL，必须给绝对/根相对路径。
 * 开发时 `/api` 由 vite proxy 转发，生产时由 nginx 转发，两边都成立。
 */
const baseURL = '/api'

// ---------------------------------------------------------------- 鉴权
export const authApi = {
  async login(user: string, password: string): Promise<TokenPayload> {
    const r = await http.post('/auth/login', { user, password })
    const data = unwrap<TokenPayload>(r)
    setAccessToken(data.access_token)
    return data
  },

  async register(user: string, password: string) {
    const r = await http.post('/auth/register', { user, password })
    return unwrap<{ ok: boolean; user: string; role: string; first: boolean }>(r)
  },

  async refresh(): Promise<TokenPayload | null> {
    try {
      const r = await http.post('/auth/refresh', {})
      const data = unwrap<TokenPayload>(r)
      setAccessToken(data.access_token)
      return data
    } catch {
      return null
    }
  },

  async me() {
    const r = await http.get('/auth/me')
    return unwrap<{ user: string; role: string }>(r)
  },

  async logout() {
    await http.post('/auth/logout', {})
    setAccessToken('')
  },
}

// ---------------------------------------------------------------- 会话
export interface StartParams {
  resume: string
  jd?: string
  role: string
  persona: string
  max_turns: number
  web?: boolean
  mock?: boolean
  coach?: boolean
  document_id?: string
}

export const sessionApi = {
  async start(p: StartParams): Promise<SessionStart> {
    const r = await http.post('/sessions', p)
    return unwrap<SessionStart>(r)
  },

  async answer(sid: string, answer: string): Promise<AnswerResult> {
    const r = await http.post(`/sessions/${sid}/answer`, { answer })
    return unwrap<AnswerResult>(r)
  },

  async finish(sid: string, reason = 'user_quit'): Promise<{
    session_id: string; done: boolean; finish_reason: string; report: ReportPayload
  }> {
    const r = await http.post(`/sessions/${sid}/finish`, { reason })
    return unwrap(r)
  },

  async report(sid: string): Promise<ReportPayload> {
    const r = await http.get(`/sessions/${sid}/report`)
    return unwrap<ReportPayload>(r)
  },

  /**
   * SSE 流式端点 URL。
   *
   * 为什么返回 URL 而不是直接发请求：
   * 浏览器 `EventSource` 只接受 URL，不接受 header，
   * 所以 token 必须拼进 query（见后端 `current_user_optional` 的说明）。
   *
   * @param sid 会话 id
   * @param mode 'replay' 回放已生成的问题（免费）；'ask' 真流式重新出题（多一次模型调用）
   */
  streamUrl(sid: string, mode: 'replay' | 'ask' = 'replay'): string {
    const t = getAccessToken() || ''
    const q = t ? `?token=${encodeURIComponent(t)}` : ''
    const path = mode === 'ask'
      ? `/sessions/${sid}/stream/ask`
      : `/sessions/${sid}/stream`
    return `${baseURL}${path}${q}`
  },

  async state(sid: string) {
    const r = await http.get(`/sessions/${sid}`)
    return unwrap<{ session_id: string; progress: Record<string, unknown> }>(r)
  },

  async list(page = 1, size = 20) {
    const r = await http.get('/sessions', { params: { page, size } })
    return unwrap<{ items: SessionItem[]; total: number; page: number; size: number }>(r)
  },
}

// ---------------------------------------------------------------- 文档
export const documentApi = {
  async upload(file: File, kind: 'resume' | 'jd' = 'resume'): Promise<DocumentUpload> {
    const form = new FormData()
    form.append('file', file)
    form.append('kind', kind)
    const r = await http.post('/documents/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return unwrap<DocumentUpload>(r)
  },

  async status(id: string): Promise<DocumentStatus> {
    const r = await http.get(`/documents/${id}/status`)
    return unwrap<DocumentStatus>(r)
  },

  async list(kind = '') {
    const r = await http.get('/documents', { params: { kind } })
    return unwrap<{ items: unknown[] }>(r)
  },
}

// ---------------------------------------------------------------- 题库
export const bankApi = {
  async stats(): Promise<BankStats> {
    const r = await http.get('/bank/stats')
    return unwrap<BankStats>(r)
  },

  async search(q: string, k = 10, role = '') {
    const r = await http.post('/bank/search', { q, k, role })
    return unwrap<{ query: string; hits: BankHit[]; count: number; threshold: number }>(r)
  },

  async list(page = 1, size = 20, role = '') {
    const r = await http.get('/bank', { params: { page, size, role } })
    return unwrap<{ items: BankHit[]; total: number }>(r)
  },
}

// ---------------------------------------------------------------- 诊断 / 画像
export const diagApi = {
  async health(): Promise<HealthSnapshot> {
    const r = await http.get('/health')
    return unwrap<HealthSnapshot>(r)
  },

  async ragSearch(index: string, q: string, k = 5) {
    const r = await http.post('/diagnostics/rag/search', { index, q, k })
    return unwrap<{
      index: string; query: string; hits: BankHit[]; count: number
      context: string; context_chars: number
    }>(r)
  },

  async thresholds() {
    const r = await http.get('/diagnostics/thresholds')
    return unwrap<Record<string, number | string>>(r)
  },

  async embed(texts: string[]) {
    const r = await http.post('/diagnostics/embed', { texts })
    return unwrap<{ dim: number; count: number; pairs: Array<{ a: string; b: string; cosine: number }> }>(r)
  },
}

export const profileApi = {
  /** 长期画像：没有独立接口，从会话列表 + 后端 profile 间接取 */
  async get(user: string): Promise<Profile | null> {
    try {
      const r = await http.get('/diagnostics/profile', { params: { user } })
      return unwrap<Profile>(r)
    } catch {
      return null
    }
  },
}
