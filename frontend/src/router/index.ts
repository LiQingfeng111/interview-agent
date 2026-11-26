import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

/**
 * 路由表。
 *
 * 守卫逻辑：
 * - meta.public：无需登录（登录/注册页）
 * - meta.admin：需要管理员
 * - 其余：需要登录
 *
 * 为什么用 `beforeEach` 而不是组件内判断：
 * 组件内判断会导致"先渲染再跳转"，用户会看到一闪而过的受保护页面。
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      redirect: '/interview',
    },
    {
      path: '/interview',
      name: 'interview',
      component: () => import('@/views/InterviewView.vue'),
      meta: { title: '模拟面试' },
    },
    {
      path: '/report/:sid?',
      name: 'report',
      component: () => import('@/views/ReportView.vue'),
      meta: { title: '面试报告' },
    },
    {
      path: '/history',
      name: 'history',
      component: () => import('@/views/HistoryView.vue'),
      meta: { title: '历史记录' },
    },
    {
      path: '/bank',
      name: 'bank',
      component: () => import('@/views/BankView.vue'),
      meta: { title: '题库诊断' },
    },
    {
      path: '/admin',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
      meta: { title: '后台', admin: true },
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/interview',
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 每次进受保护页前，先确保"恢复登录态"只跑一次
  if (!auth.ready) {
    await auth.bootstrap()
  }

  if (to.meta.public) {
    // 已登录还去登录页 → 回主页面
    if (auth.isLoggedIn && to.name === 'login') return { name: 'interview' }
    return true
  }

  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (to.meta.admin && !auth.isAdmin) {
    return { name: 'interview' }
  }

  return true
})

router.afterEach((to) => {
  const t = (to.meta.title as string) || ''
  document.title = t ? `${t} · 模拟面试 Agent` : '模拟面试 Agent'
})

export default router
