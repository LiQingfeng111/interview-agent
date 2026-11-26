<script setup lang="ts">
/**
 * 报告页：雷达图 + Markdown 报告 + 分维度明细。
 *
 * 数据来源两条：
 * 1. store.lastReport（刚结束这场，内存里有）
 * 2. sessionApi.report(sid)（从历史进来看，需重新拉）
 */
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { sessionApi } from '@/api'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const store = useSessionStore()

const loading = ref(true)
const markdown = ref('')
const dims = ref<Record<string, number>>({})
const error = ref('')
const chartRef = ref<HTMLElement | null>(null)

const DIM_LABEL: Record<string, string> = {
  accuracy: '准确度',
  depth: '深度',
  tradeoff: '取舍',
  boundary: '边界',
  clarity: '表达',
}

const dimEntries = computed(() =>
  Object.entries(DIM_LABEL).map(([k, label]) => ({
    key: k,
    label,
    value: dims.value[k] ?? 0,
  })),
)

const avg = computed(() => {
  const vals = dimEntries.value.map((d) => d.value).filter((v) => v > 0)
  return vals.length ? Math.round((vals.reduce((a, b) => a + b, 0) / vals.length) * 100) / 100 : 0
})

onMounted(async () => {
  const sid = (route.params.sid as string) || store.sid

  try {
    if (!sid) {
      error.value = '没有指定会话'
      return
    }

    // 先用内存里的（刚结束，不用请求）
    let payload = store.lastReport as Record<string, unknown> | null
    if (!payload || !(payload as { markdown?: string }).markdown) {
      payload = (await sessionApi.report(sid)) as unknown as Record<string, unknown>
    }

    markdown.value = String(
      (payload as { markdown?: string }).markdown ??
      (payload as { report?: string }).report ?? '',
    )
    const d = (payload as { dims?: Record<string, number> }).dims
    if (d && Object.keys(d).length) {
      dims.value = d
    } else if (store.evals.length) {
      // 兜底：从本轮评分算
      dims.value = store.dimAvg
    }

    if (!markdown.value) error.value = '报告为空'
  } catch (e) {
    error.value = '报告加载失败'
  } finally {
    loading.value = false
    setTimeout(drawRadar, 60)
  }
})

function drawRadar() {
  if (!chartRef.value) return
  const chart = echarts.init(chartRef.value)
  chart.setOption({
    radar: {
      indicator: dimEntries.value.map((d) => ({ name: d.label, max: 5 })),
      radius: '66%',
      splitNumber: 5,
      axisName: { color: '#6b7688', fontSize: 12 },
    },
    series: [
      {
        type: 'radar',
        data: [
          {
            value: dimEntries.value.map((d) => d.value),
            name: '本场表现',
            areaStyle: { color: 'rgba(78,121,167,0.18)' },
            lineStyle: { color: '#4e79a7', width: 2 },
            itemStyle: { color: '#4e79a7' },
          },
        ],
      },
    ],
  })
  window.addEventListener('resize', () => chart.resize())
}

function backToInterview() {
  router.push({ name: 'interview' })
}

function goHistory() {
  router.push({ name: 'history' })
}
</script>

<template>
  <div class="ia-page">
    <div class="ia-panel">
      <div class="ia-between">
        <h2 style="margin: 0">面试报告</h2>
        <div>
          <el-button size="small" @click="goHistory">历史记录</el-button>
          <el-button size="small" type="primary" @click="backToInterview">
            再面一场
          </el-button>
        </div>
      </div>
    </div>

    <div v-if="loading" class="ia-panel" style="text-align: center; padding: 48px">
      <el-icon class="is-loading" :size="24"><i class="el-icon-loading" /></el-icon>
      <p class="ia-sub">正在加载报告…</p>
    </div>

    <div v-else-if="error" class="ia-panel ia-empty">
      <h3>{{ error }}</h3>
      <el-button type="primary" @click="backToInterview">回到面试页</el-button>
    </div>

    <template v-else>
      <div class="ia-row">
        <!-- 雷达图 -->
        <div class="ia-panel chart-panel">
          <div class="ia-between" style="margin-bottom: 8px">
            <h3 style="margin: 0">分维度</h3>
            <span class="ia-sub">平均 {{ avg }}</span>
          </div>
          <div ref="chartRef" class="chart"></div>

          <div class="dim-list ia-mt16">
            <div v-for="d in dimEntries" :key="d.key" class="dim-row">
              <span class="dim-label">{{ d.label }}</span>
              <el-progress
                :percentage="(d.value / 5) * 100"
                :stroke-width="6"
                :show-text="false"
                style="flex: 1"
              />
              <span class="dim-val">{{ d.value.toFixed(1) }}</span>
            </div>
          </div>
        </div>

        <!-- Markdown 报告 -->
        <div class="ia-panel ia-grow">
          <h3 style="margin: 0 0 12px">详细报告</h3>
          <div class="ia-markdown">{{ markdown }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.chart-panel {
  width: 380px;
  flex-shrink: 0;
}

.chart {
  height: 280px;
}

.dim-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.dim-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.dim-label {
  width: 48px;
  font-size: 12px;
  color: var(--ia-text-sub);
  flex-shrink: 0;
}

.dim-val {
  width: 30px;
  text-align: right;
  font-size: 12px;
  font-weight: 600;
  color: var(--ia-primary);
}

@media (max-width: 900px) {
  .ia-row {
    flex-direction: column;
  }
  .chart-panel {
    width: 100%;
  }
}
</style>
