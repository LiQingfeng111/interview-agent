<script setup lang="ts">
/**
 * 历史记录页：分页列表 + 状态筛选。
 */
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { sessionApi } from '@/api'
import type { SessionItem } from '@/api/types'

const router = useRouter()

const loading = ref(true)
const items = ref<SessionItem[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const filterStatus = ref('')

const filtered = computed(() => {
  if (!filterStatus.value) return items.value
  return items.value.filter((s) => s.status === filterStatus.value)
})

async function load() {
  loading.value = true
  try {
    const r = await sessionApi.list(page.value, size.value)
    items.value = r.items || []
    total.value = r.total || 0
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openReport(sid: string) {
  router.push({ name: 'report', params: { sid } })
}

function fmtDate(s: string) {
  if (!s) return '—'
  // 后端返回 ISO 带时区，直接截断到分钟即可
  return s.replace('T', ' ').slice(0, 16)
}

function statusTag(s: string) {
  if (s === 'completed') return { type: 'success' as const, text: '已完成' }
  if (s === 'running') return { type: 'warning' as const, text: '进行中' }
  return { type: 'info' as const, text: s || '未知' }
}

function onPageChange(p: number) {
  page.value = p
  load()
}
</script>

<template>
  <div class="ia-page">
    <div class="ia-panel">
      <div class="ia-between">
        <h2 style="margin: 0">历史记录</h2>
        <div class="ia-flex">
          <el-select v-model="filterStatus" placeholder="全部状态" clearable size="small" style="width: 130px">
            <el-option label="已完成" value="completed" />
            <el-option label="进行中" value="running" />
          </el-select>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </div>
    </div>

    <div class="ia-panel">
      <el-table
        v-loading="loading"
        :data="filtered"
        style="width: 100%"
        empty-text="还没有面试记录"
      >
        <el-table-column prop="session_id" label="会话 ID" width="220">
          <template #default="{ row }">
            <span class="mono">{{ row.session_id }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="role" label="岗位" width="110" />
        <el-table-column prop="phase" label="末阶段" width="130" />
        <el-table-column prop="total_turns" label="轮次" width="80" align="center" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status).type" size="small">
              {{ statusTag(row.status).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="finish_reason" label="结束原因" min-width="120" />
        <el-table-column label="操作" width="110" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openReport(row.session_id)">
              看报告
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="ia-mt16" style="display: flex; justify-content: flex-end">
        <el-pagination
          :current-page="page"
          :page-size="size"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="onPageChange"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  color: var(--ia-text-sub);
}
</style>
