<script setup lang="ts">
/**
 * 后台页：健康检查 + 用户列表 + 题库导入。
 *
 * 只有 admin 能进（路由守卫拦住）。
 */
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { http, unwrap } from '@/api/client'
import { diagApi } from '@/api'
import type { HealthSnapshot } from '@/api/types'

const health = ref<HealthSnapshot | null>(null)
const users = ref<Array<{ user: string; role: string; created_at: string; last_login: string }>>([])
const loading = ref(true)
const importing = ref(false)

async function loadHealth() {
  try {
    health.value = await diagApi.health()
  } catch {
    health.value = null
  }
}

async function loadUsers() {
  try {
    const r = await http.get('/auth/users')
    users.value = unwrap<{ users: typeof users.value }>(r).users || []
  } catch {
    users.value = []
  }
}

onMounted(async () => {
  loading.value = true
  await Promise.all([loadHealth(), loadUsers()])
  loading.value = false
})

async function importBank(rebuild: boolean) {
  try {
    await ElMessageBox.confirm(
      rebuild
        ? '会清空题库后重新导入并向量化（722 题约 20 秒），确定？'
        : '增量导入：只处理还没向量化的题，确定？',
      '导入题库',
      { type: 'warning' },
    )
  } catch {
    return
  }

  importing.value = true
  try {
    const r = await http.post('/bank/import', {}, { params: { rebuild } })
    const d = unwrap<{ total: number; embedded: number; seconds: number }>(r)
    ElMessage.success(`导入完成：共 ${d.total} 题，本次向量化 ${d.embedded} 题，${d.seconds} 秒`)
    await loadHealth()
  } finally {
    importing.value = false
  }
}

async function deleteUser(name: string) {
  try {
    await ElMessageBox.confirm(`确定删除用户 ${name}？`, '删除用户', { type: 'warning' })
  } catch {
    return
  }
  try {
    await http.post('/auth/users/delete', { user: name })
    ElMessage.success('已删除')
    await loadUsers()
  } catch {
    // 拦截器提示
  }
}

function compColor(ok: boolean) {
  return ok ? '#1f9d55' : '#c53030'
}

function compLabel(k: string) {
  const map: Record<string, string> = {
    legacy_core: '规则层',
    sessions: '内存会话',
    postgres: 'PostgreSQL',
    redis: 'Redis',
    embedding: '向量模型',
  }
  return map[k] || k
}

function fmt(s: string) {
  return s ? s.replace('T', ' ').slice(0, 16) : '—'
}
</script>

<template>
  <div class="ia-page">
    <!-- 健康检查 -->
    <div class="ia-panel">
      <div class="ia-between">
        <h2 style="margin: 0">系统状态</h2>
        <el-button size="small" @click="loadHealth">刷新</el-button>
      </div>

      <div v-if="loading" class="ia-sub ia-mt16">加载中…</div>

      <div v-else-if="health" class="ia-mt16">
        <div class="comps">
          <div v-for="(v, k) in health.components" :key="k" class="comp">
            <div class="dot" :style="{ background: compColor(v.ok) }"></div>
            <div class="comp-body">
              <div class="comp-name">{{ compLabel(String(k)) }}</div>
              <div class="comp-detail">
                <template v-if="v.ok">
                  <span v-for="(vv, kk) in v" :key="kk">
                    <template v-if="kk !== 'ok'">
                      {{ kk }}={{ vv }}&nbsp;
                    </template>
                  </span>
                </template>
                <span v-else style="color: #c53030">{{ v.error || '不可用' }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 题库导入 -->
    <div class="ia-panel">
      <h3 style="margin: 0 0 12px">题库维护</h3>
      <div class="ia-flex">
        <el-button :loading="importing" @click="importBank(false)">增量导入</el-button>
        <el-button type="warning" :loading="importing" @click="importBank(true)">
          全量重建
        </el-button>
        <span class="ia-sub">722 题向量化约 20 秒（bge-m3，GPU）</span>
      </div>
    </div>

    <!-- 用户列表 -->
    <div class="ia-panel">
      <div class="ia-between">
        <h3 style="margin: 0">用户管理</h3>
        <el-button size="small" @click="loadUsers">刷新</el-button>
      </div>
      <el-table :data="users" style="width: 100%; margin-top: 12px" empty-text="没有用户">
        <el-table-column prop="user" label="用户名" width="180" />
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'warning' : 'info'" size="small">
              {{ row.role === 'admin' ? '管理员' : '普通用户' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="170">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="最后登录" width="170">
          <template #default="{ row }">{{ fmt(row.last_login) }}</template>
        </el-table-column>
        <el-table-column label="操作" align="center">
          <template #default="{ row }">
            <el-button
              link
              type="danger"
              size="small"
              @click="deleteUser(row.user)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.comps {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.comp {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 240px;
  flex: 1;
  background: #f7f9fc;
  border-radius: 10px;
  padding: 12px 14px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-top: 6px;
  flex-shrink: 0;
}

.comp-name {
  font-weight: 600;
  font-size: 13px;
}

.comp-detail {
  font-size: 12px;
  color: var(--ia-text-sub);
  margin-top: 2px;
  word-break: break-all;
}
</style>
