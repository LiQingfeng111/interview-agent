<script setup lang="ts">
/**
 * 面试页：左侧配置 + 简历，右侧对话。
 *
 * ## 上传简历的踩坑记录（重要）
 *
 * 第一版用 Element Plus 的 `<el-upload :on-change="onFileChange">`，
 * 但把回调写成了 DOM 的 `(e: Event) => e.target.files[0]`。
 *
 * Element Plus 的 on-change 签名是 `(uploadFile, uploadFiles)`，
 * 不是 DOM Event。所以 `(e.target as HTMLInputElement).files` 是 `undefined`，
 * 函数开头就 return —— 用户点上传后**什么都不会发生**，也不报错。
 *
 * 这正是"简历传不上去"的真实原因：功能没坏，是回调签名错了。
 *
 * 修法：改用原生 `<input type="file">` + 显式绑定。
 * 好处是完全可控（原生 input 的 change 事件签名是标准的），
 * 代价是要自己画按钮样式——但换来的是不会再有这种"框架签名猜错"的问题。
 */
import { ref, reactive, computed, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { documentApi, bankApi, sessionApi } from '@/api'
import { useQuestionStream } from '@/composables/useQuestionStream'
import SpeechBar from '@/components/SpeechBar.vue'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const store = useSessionStore()

// ---------------------------------------------------------------- 配置
const ROLES = [
  { value: 'ai_algo', label: 'AI 算法' },
  { value: 'agent_app', label: 'Agent 应用' },
  { value: 'backend', label: '后端' },
  { value: 'frontend', label: '前端' },
  { value: 'general', label: '通用' },
]

const PERSONAS = [
  { value: 'balanced', label: '均衡' },
  { value: 'strict', label: '严格' },
  { value: 'warm', label: '温和' },
  { value: 'academic', label: '学术' },
  { value: 'engineering', label: '工程' },
  { value: 'efficient', label: '高效' },
]

const cfgOpen = ref(true)
const cfg = reactive({
  resume: '',
  role: 'ai_algo',
  persona: 'balanced',
  max_turns: 12,
  web: false,
  mock: false,
  coach: false,
})

// ---------------------------------------------------------------- 上传状态
const fileInput = ref<HTMLInputElement | null>(null)
const docId = ref('')
const uploading = ref(false)
const uploadPhase = ref('')          // 人话：当前在做什么
const uploadPct = ref(0)             // 0-100
const uploadedName = ref('')
const docReady = ref(false)

const bankTotal = ref(0)
const answerText = ref('')
const chatRef = ref<HTMLElement | null>(null)
/** 首问正在流式打字（用来显示光标动画） */
const starting = ref(false)
const streamed = ref(false)
const stream = useQuestionStream()

const canStart = computed(
  () => (docReady.value || cfg.resume.trim().length > 20) && !store.busy,
)
const inSession = computed(() => !!store.sid && !store.done)
const progressPct = computed(() =>
  store.maxTurns ? Math.round((store.turn / store.maxTurns) * 100) : 0,
)

/** 最近一条问题的文本（给语音朗读用） */
const lastQuestion = computed(() => {
  for (let i = store.messages.length - 1; i >= 0; i--) {
    if (store.messages[i].kind === 'question') return store.messages[i].text
  }
  return ''
})

/** 首问的 id（流式光标只挂在它上面） */
const firstQId = computed(() => {
  const q = store.messages.find((m) => m.kind === 'question')
  return q?.id ?? ''
})

/** 语音识别结果追加到回答框 */
function onTranscript(text: string) {
  answerText.value = (answerText.value + text).trim()
}

bankApi.stats().then((s) => { bankTotal.value = s.total || 0 }).catch(() => {})

watch(
  () => store.messages.length,
  async () => {
    await nextTick()
    chatRef.value?.scrollTo({ top: chatRef.value.scrollHeight, behavior: 'smooth' })
  },
)

// ---------------------------------------------------------------- 上传简历
function pickFile() {
  if (uploading.value) return
  fileInput.value?.click()
}

/**
 * 原生 input 的 change 处理。
 *
 * 这里刻意不用 el-upload：它的 on-change 签名是 (uploadFile, uploadFiles)，
 * 容易和 DOM 事件混淆（第一版就踩了，导致上传静默失败）。
 * 原生 input 的 `e.target.files` 是标准 DOM，不会猜错。
 */
async function onFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''            // 允许重复选同一个文件
  if (!f) return

  // ---- 前置校验：不合法立刻说，不让用户等上传完才失败 ----
  const suffix = f.name.slice(f.name.lastIndexOf('.')).toLowerCase()
  const allowed = ['.pdf', '.docx', '.md', '.markdown', '.txt']
  if (!allowed.includes(suffix)) {
    ElMessage.error(`只支持 ${allowed.join(' / ')}，当前是 ${suffix || '无后缀'}`)
    return
  }
  if (f.size > 10 * 1024 * 1024) {
    ElMessage.error(`文件 ${(f.size / 1024 / 1024).toFixed(1)}MB 超过 10MB 上限`)
    return
  }

  uploading.value = true
  docReady.value = false
  uploadedName.value = f.name
  uploadPct.value = 5
  uploadPhase.value = `正在上传 ${f.name}（${(f.size / 1024).toFixed(0)} KB）…`

  try {
    // ---- 阶段 1：上传（0 → 40%）----
    uploadPct.value = 15
    const up = await documentApi.upload(f, 'resume')
    docId.value = up.id
    uploadPct.value = 40
    uploadPhase.value = `已收到，正在解析文本（${up.text_chars} 字）…`

    // ---- 阶段 2：切片 + 向量化（40 → 100%）----
    const totalGuess = Math.max(1, Math.ceil(up.text_chars / 400))
    let lastPct = 40

    for (let i = 0; i < 90; i++) {
      const st = await documentApi.status(up.id)

      if (st.status === 'ready') {
        uploadPct.value = 100
        uploadPhase.value = `已就绪 · ${st.total} 个片段完成向量化`
        docReady.value = true
        ElMessage.success('简历已就绪，可以开始面试')
        break
      }

      if (st.status === 'failed') {
        uploadPct.value = 0
        uploadPhase.value = `处理失败：${st.error || '未知原因'}`
        ElMessage.error(st.error || '简历处理失败')
        break
      }

      // 细化进度文案
      if (st.status === 'parsing') {
        uploadPhase.value = '正在解析文档结构…'
        uploadPct.value = Math.max(lastPct, 45)
      } else if (st.status === 'embedding') {
        const t = st.total || totalGuess
        const p = st.progress || 0
        uploadPct.value = Math.max(lastPct, 45 + Math.round((p / t) * 50))
        uploadPhase.value = `正在向量化 ${p}/${t} 个片段…`
      } else {
        uploadPhase.value = '排队处理中…'
      }
      lastPct = uploadPct.value

      await new Promise((r) => setTimeout(r, 500))
    }
  } catch (err) {
    uploadPct.value = 0
    // 拦截器已经弹过具体原因（如"简历解析失败：..."），这里补一句可操作的建议
    uploadPhase.value = '上传失败，可把简历文本直接粘到下方输入框'
  } finally {
    uploading.value = false
  }
}

// ---------------------------------------------------------------- 开始 / 答题
async function start() {
  if (!canStart.value) return
  starting.value = true
  try {
    await store.start({
      resume: cfg.resume,
      role: cfg.role,
      persona: cfg.persona,
      max_turns: cfg.max_turns,
      web: cfg.web,
      mock: cfg.mock,
      coach: cfg.coach,
      document_id: docId.value,
    })
    cfgOpen.value = false

    // 首问用真流式重新"打字"一遍，让面试官看起来像刚开口。
    //
    // 为什么首问值得多花一次模型调用：用户刚点"开始面试"，屏幕上
    // 立刻逐字出现问题，比等 5 秒突然蹦出一整段要自然得多。
    // 后续轮次的下一问已在 /answer 里生成，不再走真流式（省钱）。
    if (store.sid) {
      const firstQ = store.messages.find((m) => m.kind === 'question')
      if (firstQ) {
        // 先清空已显示的内容，用流式 token 重新填
        const full = firstQ.text
        firstQ.text = ''
        streamed.value = true
        stream.start(
          sessionApi.streamUrl(store.sid, 'ask'),
          {
            onToken: (t) => {
              firstQ.text += t
              scrollChat()
            },
            onDone: (s) => {
              // 用后端给的完整文本校正（防止丢帧）
              if (s) firstQ.text = s
              streamed.value = false
            },
            onError: () => {
              // 流式失败 → 恢复原文（已经拿到过完整问题，不影响使用）
              firstQ.text = full
              streamed.value = false
            },
          },
        )
      }
    }
    ElMessage.success('面试开始')
  } catch {
    // 拦截器提示
  } finally {
    starting.value = false
  }
}

function scrollChat() {
  chatRef.value?.scrollTo({ top: chatRef.value.scrollHeight, behavior: 'smooth' })
}

async function submit() {
  const text = answerText.value.trim()
  if (!text) {
    ElMessage.warning('请先输入回答')
    return
  }
  answerText.value = ''
  try {
    const r = await store.answer(text)
    if (r.done) {
      ElMessage.success('面试结束，正在生成报告')
      router.push({ name: 'report', params: { sid: store.sid } })
    }
  } catch {
    // 拦截器提示
  }
}

async function endEarly() {
  try {
    await ElMessageBox.confirm('确定提前结束吗？结束后会生成本场报告。', '结束面试', {
      confirmButtonText: '结束',
      cancelButtonText: '继续答',
      type: 'warning',
    })
  } catch {
    return
  }
  await store.finish('user_quit')
  router.push({ name: 'report', params: { sid: store.sid } })
}

function resetSession() {
  store.reset()
  cfgOpen.value = true
  docId.value = ''
  docReady.value = false
  uploadPhase.value = ''
  uploadPct.value = 0
  uploadedName.value = ''
}
</script>

<template>
  <div class="ia-page">
    <div class="ia-row">
      <!-- ======================= 左：配置 ======================= -->
      <div class="left">
        <div class="ia-panel">
          <div class="ia-between">
            <h3 style="margin: 0">面试设置</h3>
            <el-button link type="primary" @click="cfgOpen = !cfgOpen">
              {{ cfgOpen ? '收起' : '展开' }}
            </el-button>
          </div>

          <template v-if="cfgOpen">
            <el-form label-position="top" class="ia-mt16">
              <!-- 简历上传 -->
              <el-form-item label="简历（上传文件，或直接粘贴文本）">
                <input
                  ref="fileInput"
                  type="file"
                  accept=".pdf,.docx,.md,.markdown,.txt"
                  style="display: none"
                  @change="onFilePicked"
                />

                <div class="upload-box" :class="{ ready: docReady, busy: uploading }">
                  <div class="upload-head">
                    <el-button
                      size="small"
                      :loading="uploading"
                      :disabled="uploading"
                      @click="pickFile"
                    >
                      {{ uploading ? '处理中…' : (docReady ? '重新上传' : '选择文件') }}
                    </el-button>
                    <span v-if="uploadedName" class="fname">{{ uploadedName }}</span>
                    <el-tag v-if="docReady" size="small" type="success">已就绪</el-tag>
                  </div>

                  <!-- 进度条 + 人话状态：用户永远知道现在在干什么 -->
                  <div v-if="uploading || uploadPhase" class="upload-progress">
                    <el-progress
                      :percentage="uploadPct"
                      :stroke-width="6"
                      :status="docReady ? 'success' : (uploadPct === 0 && !uploading ? 'exception' : undefined)"
                    />
                    <div class="upload-phase" :class="{ err: uploadPct === 0 && !uploading }">
                      <span v-if="uploading" class="spin">◌</span>
                      {{ uploadPhase }}
                    </div>
                  </div>

                  <div class="upload-hint">
                    支持 PDF / DOCX / MD / TXT，最大 10MB。上传后会自动切片并向量化（约 2–5 秒）。
                  </div>
                </div>

                <el-input
                  v-model="cfg.resume"
                  type="textarea"
                  :rows="5"
                  placeholder="也可以直接粘贴简历文本（至少 20 字）"
                  class="ia-mt8"
                  :disabled="docReady"
                />
                <div v-if="docReady" class="ia-sub" style="margin-top: 4px">
                  已用上传的文件，粘贴框已锁定
                </div>
              </el-form-item>

              <el-form-item label="岗位方向">
                <el-select v-model="cfg.role" style="width: 100%">
                  <el-option
                    v-for="r in ROLES"
                    :key="r.value"
                    :label="r.label"
                    :value="r.value"
                  />
                </el-select>
              </el-form-item>

              <el-form-item label="面试官风格">
                <el-select v-model="cfg.persona" style="width: 100%">
                  <el-option
                    v-for="p in PERSONAS"
                    :key="p.value"
                    :label="p.label"
                    :value="p.value"
                  />
                </el-select>
              </el-form-item>

              <el-form-item :label="`最多轮次：${cfg.max_turns}`">
                <el-slider v-model="cfg.max_turns" :min="6" :max="30" :step="2" />
              </el-form-item>

              <div class="switches">
                <el-checkbox v-model="cfg.web">开启联网检索</el-checkbox>
                <el-checkbox v-model="cfg.mock">mock 模式（不调模型）</el-checkbox>
                <el-checkbox v-model="cfg.coach">评估模式（给评分理由）</el-checkbox>
              </div>

              <el-button
                type="primary"
                style="width: 100%"
                :disabled="!canStart"
                :loading="store.busy"
                class="ia-mt16"
                @click="start"
              >
                开始面试
              </el-button>
              <div v-if="!canStart && !store.busy" class="ia-sub ia-mt8">
                请先上传简历，或粘贴至少 20 字
              </div>
            </el-form>
          </template>

          <!-- 进行中：显示进度 -->
          <template v-else>
            <div class="ia-mt16">
              <div class="ia-between">
                <span class="ia-sub">进度</span>
                <span class="ia-sub">{{ store.turn }} / {{ store.maxTurns }} 轮</span>
              </div>
              <el-progress :percentage="progressPct" :stroke-width="8" class="ia-mt8" />
            </div>

            <div class="stats ia-mt16">
              <div class="stat">
                <div class="num">{{ store.avgScore || '—' }}</div>
                <div class="lbl">平均分</div>
              </div>
              <div class="stat">
                <div class="num">{{ store.progress?.phase || '—' }}</div>
                <div class="lbl">阶段</div>
              </div>
              <div class="stat">
                <div class="num">{{ store.progress?.ladder_name || '—' }}</div>
                <div class="lbl">追问层级</div>
              </div>
            </div>

            <div v-if="store.progress?.current_project" class="ia-sub ia-mt16">
              当前项目：{{ store.progress.current_project }}
            </div>

            <el-button class="ia-mt16" style="width: 100%" @click="endEarly">
              提前结束并看报告
            </el-button>
            <el-button
              link
              type="info"
              style="width: 100%; margin-top: 8px"
              @click="resetSession"
            >
              放弃这轮，重新配置
            </el-button>
          </template>
        </div>

        <div v-if="bankTotal" class="ia-panel ia-sub" style="text-align: center">
          题库已载入 {{ bankTotal }} 道题
        </div>
      </div>

      <!-- ======================= 右：对话 ======================= -->
      <div class="right ia-grow">
        <div class="ia-panel">
          <div class="ia-between" style="margin-bottom: 12px">
            <h3 style="margin: 0">
              对话
              <span v-if="store.engine?.mock" class="ia-badge degraded" style="margin-left: 8px">
                mock
              </span>
            </h3>
            <span v-if="inSession" class="ia-sub">第 {{ store.turn + 1 }} 轮</span>
          </div>

          <div v-if="!store.messages.length" class="ia-empty">
            <h3>还没有开始</h3>
            <p>在左侧上传简历并点击「开始面试」</p>
          </div>

          <div v-else ref="chatRef" class="ia-chat">
            <div
              v-for="m in store.messages"
              :key="m.id"
              class="ia-bubble"
              :class="m.kind"
            >
              <div>
                {{ m.text
                }}<span v-if="m.kind === 'question' && streamed && m.id === firstQId" class="caret" />
              </div>
              <div v-if="m.kind === 'question' && m.element" class="ia-meta">
                落点：{{ m.element }}
              </div>
              <div v-if="m.kind === 'answer' && m.eval" class="ia-meta">
                <span class="ia-badge" :class="m.eval.verdict">
                  {{ m.eval.verdict }} {{ m.eval.avg.toFixed(1) }}
                </span>
                <span v-if="m.eval.degraded" class="ia-badge degraded" style="margin-left: 6px">
                  降级
                </span>
              </div>

              <!-- 评估模式的点评卡 -->
              <div v-if="m.kind === 'answer' && m.card" class="coach-card">
                <div class="coach-head">
                  <span class="coach-title">点评</span>
                  <span v-if="m.card.weakest" class="coach-weak">
                    最弱：{{ m.card.weakest }}
                  </span>
                </div>

                <div v-if="m.card.gaps?.length" class="coach-sec">
                  <span class="coach-label">扣分点</span>
                  <ul>
                    <li v-for="(g, i) in m.card.gaps" :key="i">{{ g }}</li>
                  </ul>
                </div>

                <div v-if="m.card.missing?.length" class="coach-sec">
                  <span class="coach-label">缺什么</span>
                  <ul>
                    <li v-for="(g, i) in m.card.missing" :key="i">{{ g }}</li>
                  </ul>
                </div>

                <div v-if="m.card.tip" class="coach-sec">
                  <span class="coach-label">建议</span>
                  <p>{{ m.card.tip }}</p>
                </div>

                <div v-if="m.card.sample" class="coach-sec">
                  <span class="coach-label">可以这样说</span>
                  <p class="coach-sample">{{ m.card.sample }}</p>
                </div>
              </div>
            </div>
          </div>

          <!-- 提交中：给出明确反馈，避免用户以为卡住 -->
          <div v-if="store.busy && inSession" class="thinking ia-mt16">
            <span class="spin">◌</span>
            正在评分并准备下一问…（模型响应通常 2–5 秒）
          </div>

          <div v-if="inSession" class="input-area ia-mt16">
            <div class="ia-between" style="margin-bottom: 8px">
              <SpeechBar
                :text="lastQuestion"
                :disabled="store.busy"
                @transcript="onTranscript"
                @error="(m: string) => ElMessage.warning(m)"
              />
            </div>
            <el-input
              v-model="answerText"
              type="textarea"
              :rows="3"
              placeholder="输入你的回答…（提示：讲清「做了什么 + 具体数字 + 代价」更容易得分）"
              :disabled="store.busy"
              @keydown.ctrl.enter="submit"
            />
            <div class="ia-between ia-mt8">
              <span class="ia-sub">Ctrl + Enter 提交</span>
              <el-button
                type="primary"
                :loading="store.busy"
                :disabled="!answerText.trim() || store.busy"
                @click="submit"
              >
                提交回答
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.left {
  width: 340px;
  flex-shrink: 0;
}

/* ---------------------------------------------------------------- 上传框 */
.upload-box {
  width: 100%;
  border: 1px dashed var(--ia-border);
  border-radius: 10px;
  padding: 12px 14px;
  background: #fbfcfe;
  transition: border-color 0.2s, background 0.2s;
}

.upload-box.busy {
  border-color: var(--ia-primary);
  background: #f4f8fd;
}

.upload-box.ready {
  border-color: var(--ia-pass);
  background: #f3fbf6;
}

.upload-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.fname {
  font-size: 12px;
  color: var(--ia-text-sub);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-progress {
  margin-top: 10px;
}

.upload-phase {
  font-size: 12px;
  color: var(--ia-primary);
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.upload-phase.err {
  color: var(--ia-fail);
}

.upload-hint {
  font-size: 11.5px;
  color: #97a1b0;
  margin-top: 8px;
  line-height: 1.5;
}

/* ---------------------------------------------------------------- 流式光标 */
.caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: currentColor;
  animation: blink 0.9s step-end infinite;
}

@keyframes blink {
  50% { opacity: 0; }
}

/* ---------------------------------------------------------------- 点评卡 */
.coach-card {
  margin-top: 10px;
  padding: 10px 12px;
  background: #fffbf2;
  border: 1px solid #f0e0c0;
  border-radius: 8px;
  font-size: 12.5px;
  line-height: 1.6;
}

.coach-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.coach-title {
  font-weight: 600;
  color: #b07a1e;
}

.coach-weak {
  font-size: 11.5px;
  color: var(--ia-text-sub);
}

.coach-sec {
  margin-top: 6px;
}

.coach-label {
  display: inline-block;
  font-size: 11.5px;
  color: #97a1b0;
  margin-bottom: 2px;
}

.coach-sec ul {
  margin: 2px 0 0 16px;
  padding: 0;
}

.coach-sec li {
  margin: 1px 0;
}

.coach-sec p {
  margin: 2px 0 0;
}

.coach-sample {
  color: #2c7a4b;
  font-style: italic;
}

/* ---------------------------------------------------------------- 其他 */
.spin {
  display: inline-block;
  animation: rotate 1.2s linear infinite;
}

@keyframes rotate {
  to { transform: rotate(360deg); }
}

.thinking {
  font-size: 13px;
  color: var(--ia-primary);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: #f4f8fd;
  border-radius: 8px;
}

.switches {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stats {
  display: flex;
  gap: 8px;
}

.stat {
  flex: 1;
  text-align: center;
  background: #f7f9fc;
  border-radius: 8px;
  padding: 10px 4px;
}

.stat .num {
  font-size: 16px;
  font-weight: 600;
  color: var(--ia-primary);
}

.stat .lbl {
  font-size: 12px;
  color: var(--ia-text-sub);
  margin-top: 2px;
}

.input-area {
  border-top: 1px solid var(--ia-border);
  padding-top: 14px;
}

@media (max-width: 900px) {
  .ia-row {
    flex-direction: column;
  }
  .left {
    width: 100%;
  }
}
</style>
