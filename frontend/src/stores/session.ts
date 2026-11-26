/**
 * 面试会话状态（Pinia store）。
 *
 * 存什么：当前场次、消息列表、评分历史、进度。
 * 为什么放 store 而不是组件内：面试页可能被离开再回来（看报告再返回），
 * 状态要能保留。
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { sessionApi, type StartParams } from '@/api'
import type { AnswerResult, CoachCard, EvalView, ProgressView, SessionStart } from '@/api/types'

export interface ChatMessage {
  id: string
  kind: 'question' | 'answer'
  text: string
  element?: string
  eval?: EvalView
  /** 评估模式的点评卡（只有开启 coach 的轮次才有） */
  card?: CoachCard | null
  ts: number
}

export const useSessionStore = defineStore('session', () => {
  const sid = ref('')
  const messages = ref<ChatMessage[]>([])
  const progress = ref<ProgressView | null>(null)
  const evals = ref<EvalView[]>([])
  const busy = ref(false)
  const done = ref(false)
  const engine = ref<SessionStart['engine'] | null>(null)
  const lastReport = ref<Record<string, unknown> | null>(null)

  const turn = computed(() => progress.value?.turn ?? 0)
  const maxTurns = computed(() => progress.value?.max_turns ?? 16)

  /** 当前平均分 */
  const avgScore = computed(() => {
    if (!evals.value.length) return 0
    const sum = evals.value.reduce((a, e) => a + (e.avg || 0), 0)
    return Math.round((sum / evals.value.length) * 100) / 100
  })

  /** 各维度平均（雷达图用） */
  const dimAvg = computed(() => {
    const dims = ['accuracy', 'depth', 'tradeoff', 'boundary', 'clarity']
    const out: Record<string, number> = {}
    for (const d of dims) {
      const vals = evals.value.map((e) => e.dims?.[d]).filter((v) => typeof v === 'number') as number[]
      out[d] = vals.length ? Math.round((vals.reduce((a, b) => a + b, 0) / vals.length) * 100) / 100 : 0
    }
    return out
  })

  let seq = 0
  function push(kind: ChatMessage['kind'], text: string, extra?: Partial<ChatMessage>) {
    messages.value.push({
      id: `m${++seq}`, kind, text, ts: Date.now(), ...extra,
    })
  }

  /** 开新面试 */
  async function start(p: StartParams) {
    busy.value = true
    try {
      reset()
      const r = await sessionApi.start(p)
      sid.value = r.session_id
      progress.value = r.progress
      engine.value = r.engine
      push('question', r.question, { element: r.element })
      return r
    } finally {
      busy.value = false
    }
  }

  /** 交一轮答案 */
  async function answer(text: string): Promise<AnswerResult> {
    if (!sid.value) throw new Error('还没有进行中的面试')
    busy.value = true
    try {
      push('answer', text)
      const r = await sessionApi.answer(sid.value, text)
      if (r.eval && r.eval.verdict) evals.value.push(r.eval)

    // 评估模式的点评卡：挂到对应的答案气泡上
    //
    // 为什么不单独存一个数组：卡片和某一轮的回答是一对一的，
    // 用户在对话里看到"我答的这段 + 点评"才连贯。
    // 单独列表会让用户对不上是哪一轮。
    if (r.card) {
      for (let i = messages.value.length - 1; i >= 0; i--) {
        if (messages.value[i].kind === 'answer') {
          messages.value[i].card = r.card
          break
        }
      }
    }
      progress.value = r.progress
      done.value = r.done
      if (r.done) {
        lastReport.value = (r.report as Record<string, unknown>) ?? null
      } else {
        push('question', r.question, { element: r.element })
      }
      return r
    } finally {
      busy.value = false
    }
  }

  /** 提前结束 */
  async function finish(reason = 'user_quit') {
    if (!sid.value) return null
    busy.value = true
    try {
      const r = await sessionApi.finish(sid.value, reason)
      done.value = true
      lastReport.value = (r.report as Record<string, unknown>) ?? null
      return r
    } finally {
      busy.value = false
    }
  }

  async function loadReport(id?: string) {
    const target = id || sid.value
    if (!target) return null
    const r = await sessionApi.report(target)
    lastReport.value = r as Record<string, unknown>
    return r
  }

  function reset() {
    sid.value = ''
    messages.value = []
    progress.value = null
    evals.value = []
    done.value = false
    engine.value = null
    lastReport.value = null
    seq = 0
  }

  return {
    sid, messages, progress, evals, busy, done, engine, lastReport,
    turn, maxTurns, avgScore, dimAvg,
    start, answer, finish, loadReport, reset,
  }
})
