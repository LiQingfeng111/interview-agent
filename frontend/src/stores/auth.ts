/**
 * 鉴权状态（Pinia store）。
 *
 * 为什么用 store 而不是组件内局部状态：
 * 登录态被多个地方读（路由守卫、顶部导航、面试页），
 * 而且刷新页面时要"先试着恢复再决定是否跳登录"——这需要全局时序控制。
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api'
import { setAccessToken, setAuthLostHandler } from '@/api/client'

export const useAuthStore = defineStore('auth', () => {
  const user = ref('')
  const role = ref<'admin' | 'user' | ''>('')
  const ready = ref(false) // 是否已完成"尝试恢复登录态"

  const isLoggedIn = computed(() => !!user.value)
  const isAdmin = computed(() => role.value === 'admin')

  /** 登录 */
  async function login(name: string, pwd: string) {
    const data = await authApi.login(name, pwd)
    user.value = data.user
    role.value = data.role
    ready.value = true
    return data
  }

  /** 注册（成功后不自动登录，让用户先看到"注册成功"提示） */
  async function register(name: string, pwd: string) {
    return authApi.register(name, pwd)
  }

  /**
   * 应用启动时尝试恢复登录态。
   *
   * 时序很重要：必须先试 refresh，成功才认为已登录。
   * 如果先跳登录页再 refresh，用户会看到一闪而过的登录界面。
   */
  async function bootstrap() {
    if (ready.value) return
    const data = await authApi.refresh()
    if (data) {
      user.value = data.user
      role.value = data.role
    } else {
      setAccessToken('')
    }
    ready.value = true
  }

  async function logout() {
    try {
      await authApi.logout()
    } finally {
      user.value = ''
      role.value = ''
    }
  }

  /** 登录态彻底失效时的清理（由 client 拦截器触发） */
  function onLost() {
    user.value = ''
    role.value = ''
    setAccessToken('')
  }

  setAuthLostHandler(onLost)

  return { user, role, ready, isLoggedIn, isAdmin, login, register, bootstrap, logout, onLost }
})
