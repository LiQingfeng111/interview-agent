/**
 * 后端接口类型定义。
 *
 * 为什么手写而不是自动生成：
 * 后端 FastAPI 的 OpenAPI 能生成类型，但我们的响应统一包在
 * `{code, data, message}` 里，生成的类型会多一层嵌套且字段命名是
 * 后端内部名（如 `latest_eval`）。手写这层能把"后端返回什么"
 * 翻译成"前端要什么"，代价是接口改动时要同步——所以字段都加了注释。
 */

/** 统一响应包 */
export interface ApiResponse<T = unknown> {
  code: number
  data: T | null
  message: string
  request_id?: string
  detail?: Record<string, unknown> | null
}

/** 登录/刷新返回 */
export interface TokenPayload {
  access_token: string
  token_type: string
  expires_in: number
  user: string
  role: 'admin' | 'user'
}

/** 评分结果 */
export interface EvalView {
  verdict: '' | 'pass' | 'partial' | 'fail'
  avg: number
  weakest: string
  evidence: string
  dims: Record<string, number>
  model_verdict: string
  verdict_conflict: boolean
  degraded: boolean
  covered: string[]
}

/** 进度 */
export interface ProgressView {
  turn: number
  max_turns: number
  phase: string
  ladder_level: number
  ladder_name: string
  current_project: string
  topic_counts: Record<string, number>
}

/** 开会话返回 */
export interface SessionStart {
  session_id: string
  turn: number
  question: string
  element: string
  phase: string
  projects: string[]
  progress: ProgressView
  eval: null
  done: boolean
  engine: {
    graph: string
    model: string
    mock: boolean
    web: boolean
    coach: boolean
  }
}

/** 答一轮返回 */
export interface AnswerResult {
  session_id: string
  turn: number
  question: string
  element: string
  phase: string
  eval: EvalView
  progress: ProgressView
  done: boolean
  report?: ReportPayload
  /** 评估模式的点评卡（只有开启 coach 时后端才返回） */
  card?: CoachCard | null
}

/**
 * 评估模式的点评卡。
 *
 * 前端勾了"评估模式"就会拿到这个对象，用来在每轮回答后
 * 显示"哪里扣分了 / 缺什么 / 示范回答"。
 *
 * 字段全部可选：后端由旧 `TurnCoach.review()` 生成，
 * 它的返回结构随版本变化过，用可选字段能容忍差异。
 */
export interface CoachCard {
  enabled?: boolean
  score?: number
  verdict?: string
  weakest?: string
  /** 扣分点列表 */
  gaps?: string[]
  /** 缺什么要素 */
  missing?: string[]
  /** 示范回答 */
  sample?: string
  /** 一句话建议 */
  tip?: string
  [key: string]: unknown
}

/** 报告 */
export interface ReportPayload {
  markdown?: string
  report?: string
  dims?: Record<string, number>
  error?: string
}

/** 会话列表项 */
export interface SessionItem {
  session_id: string
  role: string
  phase: string
  status: string
  total_turns: number
  created_at: string
  finish_reason: string
}

/** 文档上传返回 */
export interface DocumentUpload {
  id: string
  kind: string
  filename: string
  size_bytes: number
  text_chars: number
  status: string
}

/** 文档处理状态 */
export interface DocumentStatus {
  id: string
  status: 'pending' | 'parsing' | 'embedding' | 'ready' | 'failed'
  progress: number
  total: number
  error: string
}

/** 题库统计 */
export interface BankStats {
  total: number
  embedded: number
  by_role: Record<string, number>
  by_level: Record<string, number>
}

/** 题库检索命中 */
export interface BankHit {
  id: string
  text: string
  score: number
  rank: number
  project: string
  parent_text: string
  meta: { points?: string[]; verdict?: string }
}

/** 健康检查 */
export interface HealthSnapshot {
  ok: boolean
  version: string
  components: Record<string, { ok: boolean; [k: string]: unknown }>
}

/** 长期画像 */
export interface Profile {
  weak_elements: Record<string, number>
  strong_topics: string[]
  weak_topics: string[]
  sessions_count: number
  updated_at?: string
}
