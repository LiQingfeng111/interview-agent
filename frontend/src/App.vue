<script setup lang="ts">
/**
 * 根组件：只做两件事——放顶栏、放路由出口。
 *
 * 为什么登录态恢复（bootstrap）放在路由守卫里而不是这里：
 * 守卫是"进入页面前"执行，能避免"先渲染空白页再跳登录"的闪烁。
 */
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

onMounted(async () => {
  // 首次进入时尝试恢复登录态（守卫里也会调，但这里兜底直接打开首页的情况）
  await auth.bootstrap()
})

async function doLogout() {
  await auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="app-root">
    <!-- 顶栏：登录页不显示 -->
    <header v-if="auth.isLoggedIn" class="app-header">
      <div class="brand">模拟面试 Agent</div>

      <nav class="nav">
        <RouterLink to="/interview" :class="{ active: route.name === 'interview' }">
          面试
        </RouterLink>
        <RouterLink to="/history" :class="{ active: route.name === 'history' }">
          历史
        </RouterLink>
        <RouterLink to="/bank" :class="{ active: route.name === 'bank' }">
          题库
        </RouterLink>
        <RouterLink v-if="auth.isAdmin" to="/admin" :class="{ active: route.name === 'admin' }">
          后台
        </RouterLink>
      </nav>

      <div class="user">
        <span class="name">{{ auth.user }}</span>
        <el-tag v-if="auth.isAdmin" size="small" type="warning">管理员</el-tag>
        <el-button link type="primary" size="small" @click="doLogout">退出</el-button>
      </div>
    </header>

    <main>
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.app-root {
  min-height: 100%;
  display: flex;
  flex-direction: column;
}

.app-header {
  height: 56px;
  padding: 0 24px;
  background: #1f2b3a;
  color: #e8eef6;
  display: flex;
  align-items: center;
  gap: 32px;
  position: sticky;
  top: 0;
  z-index: 100;
}

.brand {
  font-weight: 600;
  font-size: 15px;
  letter-spacing: 0.3px;
}

.nav {
  display: flex;
  gap: 4px;
  flex: 1;
}

.nav a {
  color: #b8c4d4;
  text-decoration: none;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 14px;
  transition: background 0.15s, color 0.15s;
}

.nav a:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.nav a.active {
  background: rgba(255, 255, 255, 0.14);
  color: #fff;
  font-weight: 500;
}

.user {
  display: flex;
  align-items: center;
  gap: 8px;
}

.user .name {
  font-size: 13px;
  color: #cbd6e4;
}

main {
  flex: 1;
}
</style>
