<script setup lang="ts">
/**
 * 题库诊断页：三个用途
 * 1. 看题库规模和分布（是不是全向量化了）
 * 2. 语义检索（验证"问法不同也能召回同一考点"）
 * 3. 嵌入相似度（人工核对阈值合不合理）
 */
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { bankApi, diagApi } from '@/api'
import type { BankHit, BankStats } from '@/api/types'

const stats = ref<BankStats | null>(null)
const thresholds = ref<Record<string, number | string>>({})

const query = ref('多头注意力里每个头的维度怎么算')
const role = ref('ai_algo')
const hits = ref<BankHit[]>([])
const searching = ref(false)
const lastThreshold = ref<number | null>(null)

const textA = ref('召回从71%提到82%')
const textB = ref('recall 从 0.71 提升到 0.82')
const cosine = ref<number | null>(null)
const dim = ref(0)

onMounted(async () => {
  try {
    stats.value = await bankApi.stats()
    thresholds.value = await diagApi.thresholds()
  } catch {
    // 拦截器提示
  }
})

async function search() {
  if (!query.value.trim()) return
  searching.value = true
  try {
    const r = await bankApi.search(query.value, 8, role.value)
    hits.value = r.hits || []
    lastThreshold.value = r.threshold ?? null
    if (!hits.value.length) ElMessage.info('没有召回结果（可能都低于阈值）')
  } finally {
    searching.value = false
  }
}

async function checkEmbed() {
  if (!textA.value.trim() || !textB.value.trim()) return
  const r = await diagApi.embed([textA.value, textB.value])
  dim.value = r.dim
  cosine.value = r.pairs?.[0]?.cosine ?? null
}

function scoreColor(s: number) {
  if (s >= 0.7) return '#1f9d55'
  if (s >= 0.5) return '#b7791f'
  return '#c53030'
}
</script>

<template>
  <div class="ia-page">
    <!-- 概览 -->
    <div class="ia-panel">
      <div class="ia-between">
        <h2 style="margin: 0">题库诊断</h2>
      </div>

      <div v-if="stats" class="stat-row ia-mt16">
        <div class="stat-box">
          <div class="num">{{ stats.total }}</div>
          <div class="lbl">总题数</div>
        </div>
        <div class="stat-box">
          <div class="num">{{ stats.embedded }}</div>
          <div class="lbl">已向量化</div>
        </div>
        <div class="stat-box">
          <div class="num">{{ stats.total ? Math.round((stats.embedded / stats.total) * 100) : 0 }}%</div>
          <div class="lbl">覆盖率</div>
        </div>
      </div>

      <div v-if="stats" class="ia-mt16">
        <div class="ia-sub" style="margin-bottom: 6px">按岗位分布</div>
        <div class="tag-row">
          <el-tag
            v-for="(n, r) in stats.by_role"
            :key="r"
            size="small"
            style="margin: 0 6px 6px 0"
          >
            {{ r }}：{{ n }}
          </el-tag>
        </div>
      </div>

      <div v-if="Object.keys(thresholds).length" class="ia-mt16">
        <div class="ia-sub" style="margin-bottom: 6px">当前检索阈值</div>
        <div class="tag-row">
          <el-tag
            v-for="(v, k) in thresholds"
            :key="k"
            size="small"
            type="info"
            style="margin: 0 6px 6px 0"
          >
            {{ k }}：{{ v }}
          </el-tag>
        </div>
      </div>
    </div>

    <!-- 语义检索 -->
    <div class="ia-panel">
      <h3 style="margin: 0 0 12px">语义检索</h3>
      <div class="ia-flex">
        <el-select v-model="role" style="width: 150px">
          <el-option label="AI 算法" value="ai_algo" />
          <el-option label="Agent 应用" value="agent_app" />
          <el-option label="后端" value="backend" />
          <el-option label="前端" value="frontend" />
          <el-option label="通用" value="general" />
        </el-select>
        <el-input
          v-model="query"
          placeholder="输入一个口语化的问题，看召回哪些题"
          style="flex: 1"
          @keyup.enter="search"
        />
        <el-button type="primary" :loading="searching" @click="search">检索</el-button>
      </div>

      <div v-if="hits.length" class="ia-mt16">
        <div class="ia-sub" style="margin-bottom: 10px">
          召回 {{ hits.length }} 条
          <span v-if="lastThreshold !== null">· 阈值 {{ lastThreshold }}</span>
        </div>
        <div v-for="h in hits" :key="h.id" class="hit">
          <div class="hit-head">
            <el-tag size="small">{{ h.project || '未分类' }}</el-tag>
            <span class="hit-score" :style="{ color: scoreColor(h.score) }">
              {{ h.score.toFixed(3) }}
            </span>
            <span class="ia-sub mono">{{ h.id }}</span>
          </div>
          <div class="hit-text">{{ h.text }}</div>
          <div v-if="h.meta?.points?.length" class="hit-points">
            考点：{{ h.meta.points.join('；') }}
          </div>
        </div>
      </div>
      <div v-else-if="!searching" class="ia-empty" style="padding: 24px">
        输入问题后点检索
      </div>
    </div>

    <!-- 相似度核对 -->
    <div class="ia-panel">
      <h3 style="margin: 0 0 4px">嵌入相似度核对</h3>
      <p class="ia-sub" style="margin: 0 0 12px">
        用来判断阈值合不合理。实测原地：同义句约 0.70，无关句约 0.40。
      </p>
      <div class="ia-flex">
        <el-input v-model="textA" placeholder="文本 A" />
        <el-input v-model="textB" placeholder="文本 B" />
        <el-button @click="checkEmbed">比对</el-button>
      </div>
      <div v-if="cosine !== null" class="ia-mt16 result">
        <span class="cos" :style="{ color: scoreColor(cosine) }">{{ cosine.toFixed(4) }}</span>
        <span class="ia-sub" style="margin-left: 10px">（{{ dim }} 维）</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stat-row {
  display: flex;
  gap: 12px;
}

.stat-box {
  flex: 1;
  text-align: center;
  background: #f7f9fc;
  border-radius: 10px;
  padding: 14px 8px;
}

.stat-box .num {
  font-size: 22px;
  font-weight: 600;
  color: var(--ia-primary);
}

.stat-box .lbl {
  font-size: 12px;
  color: var(--ia-text-sub);
  margin-top: 2px;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
}

.hit {
  border: 1px solid var(--ia-border);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 10px;
  background: #fbfcfe;
}

.hit-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.hit-score {
  font-weight: 600;
  font-size: 13px;
}

.hit-text {
  font-size: 13.5px;
  line-height: 1.65;
}

.hit-points {
  font-size: 12px;
  color: var(--ia-text-sub);
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--ia-border);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11px;
}

.result {
  font-size: 15px;
}

.cos {
  font-size: 20px;
  font-weight: 600;
}
</style>
