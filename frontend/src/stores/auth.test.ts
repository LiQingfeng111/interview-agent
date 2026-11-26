/**
 * 鉴权 store 的测试。
 *
 * 重点测"刷新页面时的登录态恢复时序"——
 * 这是最容易出 bug 的地方（用户会看到一闪而过的登录页）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/api', () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    refresh: vi.fn(),
    me: vi.fn(),
    logout: vi.fn(),
  },
}))

vi.mock('@/api/client', () => ({
  setAccessToken: vi.fn(),
  setAuthLostHandler: vi.fn(),
}))

import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api'

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('初始未登录，ready=false', () => {
    const a = useAuthStore()
    expect(a.isLoggedIn).toBe(false)
    expect(a.ready).toBe(false)
  })

  it('login() 成功后写入用户与角色', async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'tok', token_type: 'bearer', expires_in: 900,
      user: 'alice', role: 'admin',
    })
    const a = useAuthStore()

    await a.login('alice', 'pwd')

    expect(a.user).toBe('alice')
    expect(a.role).toBe('admin')
    expect(a.isLoggedIn).toBe(true)
    expect(a.isAdmin).toBe(true)
    expect(a.ready).toBe(true)
  })

  it('bootstrap() 用 refresh 恢复登录态（刷新页面场景）', async () => {
    vi.mocked(authApi.refresh).mockResolvedValue({
      access_token: 'tok2', token_type: 'bearer', expires_in: 900,
      user: 'bob', role: 'user',
    })
    const a = useAuthStore()

    await a.bootstrap()

    expect(a.user).toBe('bob')
    expect(a.role).toBe('user')
    expect(a.isLoggedIn).toBe(true)
    expect(a.ready).toBe(true)
  })

  it('refresh 失败时保持未登录，但 ready=true（守卫要能判断）', async () => {
    vi.mocked(authApi.refresh).mockResolvedValue(null)
    const a = useAuthStore()

    await a.bootstrap()

    expect(a.isLoggedIn).toBe(false)
    // ready 必须是 true，否则路由守卫会一直等
    expect(a.ready).toBe(true)
  })

  it('bootstrap() 幂等：ready 后直接返回，不重复请求', async () => {
    vi.mocked(authApi.refresh).mockResolvedValue(null)
    const a = useAuthStore()

    await a.bootstrap()
    await a.bootstrap()
    await a.bootstrap()

    expect(authApi.refresh).toHaveBeenCalledTimes(1)
  })

  it('logout() 即使请求失败也要清空本地状态', async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'tok', token_type: 'bearer', expires_in: 900,
      user: 'alice', role: 'user',
    })
    vi.mocked(authApi.logout).mockRejectedValue(new Error('network') as never)
    const a = useAuthStore()
    await a.login('alice', 'p')

    await expect(a.logout()).rejects.toThrow()

    // 关键：本地状态必须清掉（否则用户以为还登录着）
    expect(a.user).toBe('')
    expect(a.role).toBe('')
    expect(a.isLoggedIn).toBe(false)
  })

  it('onLost() 清空登录态（token 彻底失效时由拦截器调用）', async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'tok', token_type: 'bearer', expires_in: 900,
      user: 'alice', role: 'user',
    })
    const a = useAuthStore()
    await a.login('alice', 'p')

    a.onLost()

    expect(a.isLoggedIn).toBe(false)
    expect(a.user).toBe('')
  })

  it('普通用户 isAdmin 为 false', async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: 'tok', token_type: 'bearer', expires_in: 900,
      user: 'bob', role: 'user',
    })
    const a = useAuthStore()
    await a.login('bob', 'p')

    expect(a.isAdmin).toBe(false)
  })
})
