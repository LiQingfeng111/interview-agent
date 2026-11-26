/**
 * 会话 store 的单元测试。
 *
 * 重点测"状态流转是否正确"，不测 UI 渲染
 * （UI 由组件测试覆盖，但组件里大部分逻辑其实是状态驱动的）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// mock 掉 API 层：store 的职责是状态管理，不是发请求
vi.mock('@/api', () => ({
  sessionApi: {
    start: vi.fn(),
    answer: vi.fn(),
    finish: vi.fn(),
    report: vi.fn(),
    list: vi.fn(),
  },
}))

import { useSessionStore } from '@/stores/session'
import { sessionApi } from '@/api'

const START_RESP = {
  session_id: 's_test1',
  turn: 0,
  question: '请做个自我介绍',
  element: '目标',
  phase: 'OPENING',
  projects: ['课程问答助手'],
  progress: {
    turn: 0, max_turns: 12, phase: 'OPENING',
    ladder_level: 0, ladder_name: '是什么',
    current_project: '课程问答助手', topic_counts: {},
  },
  eval: null,
  done: false,
  engine: { graph: 'langgraph', model: 'm', mock: false, web: false, coach: false },
}

function answerResp(over: Partial<Record<string, unknown>> = {}) {
  return {
    session_id: 's_test1',
    turn: 1,
    question: '第二问',
    element: 'baseline',
    phase: 'SELF_INTRO',
    eval: {
      verdict: 'pass', avg: 3.4, weakest: 'depth', evidence: '召回 71→82',
      dims: { accuracy: 4, depth: 3, tradeoff: 4, boundary: 2, clarity: 4 },
      model_verdict: 'pass', verdict_conflict: false, degraded: false,
      covered: ['指标'],
    },
    progress: {
      turn: 1, max_turns: 12, phase: 'SELF_INTRO',
      ladder_level: 0, ladder_name: '是什么',
      current_project: '课程问答助手', topic_counts: {},
    },
    done: false,
    ...over,
  }
}

describe('session store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('初始状态是空的', () => {
    const s = useSessionStore()
    expect(s.sid).toBe('')
    expect(s.messages).toEqual([])
    expect(s.turn).toBe(0)
    expect(s.avgScore).toBe(0)
    expect(s.done).toBe(false)
  })

  it('start() 后写入首问与进度', async () => {
    vi.mocked(sessionApi.start).mockResolvedValue(START_RESP as never)
    const s = useSessionStore()

    await s.start({
      resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12,
    })

    expect(s.sid).toBe('s_test1')
    expect(s.messages).toHaveLength(1)
    expect(s.messages[0].kind).toBe('question')
    expect(s.messages[0].text).toBe('请做个自我介绍')
    expect(s.messages[0].element).toBe('目标')
    expect(s.maxTurns).toBe(12)
  })

  it('answer() 追加"我的回答 + 下一问"，并累计评分', async () => {
    vi.mocked(sessionApi.start).mockResolvedValue(START_RESP as never)
    vi.mocked(sessionApi.answer).mockResolvedValue(answerResp() as never)
    const s = useSessionStore()
    await s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 })

    await s.answer('我的回答')

    // 应该是 3 条：首问、我的回答、第二问
    expect(s.messages.map((m) => m.kind)).toEqual(['question', 'answer', 'question'])
    expect(s.messages[2].text).toBe('第二问')
    expect(s.evals).toHaveLength(1)
    expect(s.evals[0].verdict).toBe('pass')
    expect(s.turn).toBe(1)
  })

  it('avgScore 是各轮平均，且保留两位小数', async () => {
    vi.mocked(sessionApi.start).mockResolvedValue(START_RESP as never)
    const s = useSessionStore()
    await s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 })

    vi.mocked(sessionApi.answer).mockResolvedValue(answerResp() as never)
    await s.answer('a')

    vi.mocked(sessionApi.answer).mockResolvedValue(
      answerResp({
        eval: {
          verdict: 'partial', avg: 2.2, weakest: 'depth', evidence: 'e',
          dims: { accuracy: 2, depth: 2, tradeoff: 3, boundary: 2, clarity: 2 },
          model_verdict: 'partial', verdict_conflict: false, degraded: false, covered: [],
        },
      }) as never,
    )
    await s.answer('b')

    // (3.4 + 2.2) / 2 = 2.8
    expect(s.avgScore).toBe(2.8)
  })

  it('dimAvg 计算各维度平均（雷达图数据源）', async () => {
    vi.mocked(sessionApi.start).mockResolvedValue(START_RESP as never)
    vi.mocked(sessionApi.answer).mockResolvedValue(answerResp() as never)
    const s = useSessionStore()
    await s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 })
    await s.answer('a')

    expect(s.dimAvg.accuracy).toBe(4)
    expect(s.dimAvg.boundary).toBe(2)
    expect(Object.keys(s.dimAvg)).toHaveLength(5)
  })

  it('done=true 时不再追加新问题，并保存报告', async () => {
    vi.mocked(sessionApi.start).mockResolvedValue(START_RESP as never)
    vi.mocked(sessionApi.answer).mockResolvedValue(
      answerResp({ done: true, question: '', report: { markdown: '# 报告' } }) as never,
    )
    const s = useSessionStore()
    await s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 })

    await s.answer('最后一答')

    expect(s.done).toBe(true)
    // 只有首问 + 我的回答，没有"下一问"
    expect(s.messages.map((m) => m.kind)).toEqual(['question', 'answer'])
    expect(s.lastReport).toMatchObject({ markdown: '# 报告' })
  })

  it('busy 在请求期间为 true，结束后复位', async () => {
    let resolve: (v: unknown) => void = () => {}
    vi.mocked(sessionApi.start).mockImplementation(
      () => new Promise((r) => { resolve = r }) as never,
    )
    const s = useSessionStore()
    const p = s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 })

    expect(s.busy).toBe(true)
    resolve(START_RESP)
    await p
    expect(s.busy).toBe(false)
  })

  it('请求失败时 busy 也会复位（不能卡在 loading）', async () => {
    vi.mocked(sessionApi.start).mockRejectedValue(new Error('boom') as never)
    const s = useSessionStore()

    await expect(
      s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 }),
    ).rejects.toThrow('boom')
    expect(s.busy).toBe(false)
  })

  it('reset() 清空全部状态', async () => {
    vi.mocked(sessionApi.start).mockResolvedValue(START_RESP as never)
    vi.mocked(sessionApi.answer).mockResolvedValue(answerResp() as never)
    const s = useSessionStore()
    await s.start({ resume: 'x'.repeat(50), role: 'ai_algo', persona: 'balanced', max_turns: 12 })
    await s.answer('a')

    s.reset()

    expect(s.sid).toBe('')
    expect(s.messages).toEqual([])
    expect(s.evals).toEqual([])
    expect(s.done).toBe(false)
    expect(s.lastReport).toBeNull()
  })

  it('没有 sid 时 answer() 直接抛错（不能悄悄发请求）', async () => {
    const s = useSessionStore()
    await expect(s.answer('x')).rejects.toThrow('还没有进行中的面试')
    expect(sessionApi.answer).not.toHaveBeenCalled()
  })
})
