<script setup lang="ts">
/**
 * 语音输入/播报组件。
 *
 * ## 策略：浏览器优先，后端兜底
 *
 * | 能力 | 首选 | 兜底 |
 * |---|---|---|
 * | 语音转文字 | `webkitSpeechRecognition`（Chrome/Edge 原生，零延迟） | 后端 `/speech/transcribe`（需装 whisper） |
 * | 文字转语音 | `speechSynthesis`（原生，中文自然） | 后端 `/speech/synthesize`（需装 edge-tts） |
 *
 * 为什么浏览器优先：延迟低（本地识别）、免费、不外传面试内容。
 * Firefox 不支持 webkitSpeechRecognition，所以保留后端路径。
 *
 * ## 为什么语音识别要"连续 + 中间结果"
 *
 * 面试回答通常 100-300 字，说 30-60 秒。如果每句都断开，用户得反复点开始。
 * `continuous=true` 让它一直听，`interimResults=true` 让用户看到实时文字，
 * 有反馈才敢继续说。
 */
import { ref, computed, onUnmounted } from 'vue'

const props = defineProps<{
  /** 当前文字（用于播报） */
  text?: string
  /** 识别到的文字通过这个事件回传 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'transcript', text: string): void
  (e: 'error', message: string): void
}>()

// ---------------------------------------------------------------- ASR
interface SpeechRecognitionLike extends EventTarget {
  lang: string
  continuous: boolean
  interimResults: boolean
  start(): void
  stop(): void
  abort(): void
  onresult: ((ev: Event) => void) | null
  onerror: ((ev: Event) => void) | null
  onend: (() => void) | null
}

const listening = ref(false)
const interim = ref('')
const asrSupported = ref(false)
let recognition: SpeechRecognitionLike | null = null

function getRecognitionCtor(): (new () => SpeechRecognitionLike) | null {
  const w = window as unknown as {
    SpeechRecognition?: new () => SpeechRecognitionLike
    webkitSpeechRecognition?: new () => SpeechRecognitionLike
  }
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

function initRecognition() {
  const Ctor = getRecognitionCtor()
  if (!Ctor) {
    asrSupported.value = false
    return
  }
  asrSupported.value = true
  const r = new Ctor()
  r.lang = 'zh-CN'
  r.continuous = true
  r.interimResults = true

  r.onresult = (ev: Event) => {
    const e = ev as unknown as {
      resultIndex: number
      results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>
    }
    let finalText = ''
    let interimText = ''
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i]
      const t = res[0]?.transcript ?? ''
      if (res.isFinal) finalText += t
      else interimText += t
    }
    interim.value = interimText
    if (finalText) {
      emit('transcript', finalText)
      interim.value = ''
    }
  }

  r.onerror = (ev: Event) => {
    const e = ev as unknown as { error?: string }
    const code = e.error || 'unknown'
    const msgMap: Record<string, string> = {
      'not-allowed': '麦克风权限被拒绝，请在浏览器地址栏允许麦克风',
      'no-speech': '没有检测到声音，请靠近麦克风再说一次',
      'audio-capture': '没有找到麦克风设备',
      'network': '语音识别需要联网（浏览器把音频发到识别服务）',
      'aborted': '',
    }
    const msg = msgMap[code]
    if (msg) emit('error', msg)
    listening.value = false
  }

  r.onend = () => {
    // 连续模式被系统中断时自动续听（除非用户主动停）
    if (listening.value) {
      try {
        r.start()
      } catch {
        listening.value = false
      }
    }
  }

  recognition = r
}

function toggleListen() {
  if (props.disabled) return
  if (!recognition) initRecognition()
  if (!recognition) {
    emit('error', '当前浏览器不支持语音识别，请用 Chrome / Edge，或手动输入')
    return
  }

  if (listening.value) {
    listening.value = false
    try {
      recognition.stop()
    } catch {
      /* 已停止 */
    }
    interim.value = ''
  } else {
    try {
      recognition.start()
      listening.value = true
    } catch (e) {
      emit('error', '启动麦克风失败，可能被其他程序占用')
    }
  }
}

// ---------------------------------------------------------------- TTS
const speaking = ref(false)
const ttsSupported = computed(
  () => typeof window !== 'undefined' && 'speechSynthesis' in window,
)

function speak(text?: string) {
  const content = (text ?? props.text ?? '').trim()
  if (!content) {
    emit('error', '没有可播报的文字')
    return
  }
  if (!ttsSupported.value) {
    emit('error', '当前浏览器不支持语音播报')
    return
  }

  window.speechSynthesis.cancel() // 打断上一句，避免排队
  const u = new SpeechSynthesisUtterance(content)
  u.lang = 'zh-CN'
  u.rate = 1.0
  u.pitch = 1.0
  u.onstart = () => { speaking.value = true }
  u.onend = () => { speaking.value = false }
  u.onerror = () => { speaking.value = false }
  window.speechSynthesis.speak(u)
}

function stopSpeak() {
  if (ttsSupported.value) window.speechSynthesis.cancel()
  speaking.value = false
}

onUnmounted(() => {
  if (recognition && listening.value) {
    listening.value = false
    try {
      recognition.abort()
    } catch {
      /* ignore */
    }
  }
  stopSpeak()
})

defineExpose({ speak, stopSpeak })
</script>

<template>
  <div class="speech-bar">
    <el-tooltip
      :content="asrSupported
        ? (listening ? '点击停止' : '点击开始说话（Chrome/Edge 支持）')
        : '当前浏览器不支持语音识别，请用 Chrome / Edge'"
      placement="top"
    >
      <el-button
        size="small"
        :type="listening ? 'danger' : 'default'"
        :disabled="props.disabled || !asrSupported"
        @click="toggleListen"
      >
        <span v-if="listening" class="rec-dot" />
        {{ listening ? '停止' : '语音输入' }}
      </el-button>
    </el-tooltip>

    <el-tooltip content="朗读当前问题" placement="top">
      <el-button
        size="small"
        :disabled="!ttsSupported || !(props.text || '').trim()"
        @click="speaking ? stopSpeak() : speak()"
      >
        {{ speaking ? '停止朗读' : '朗读' }}
      </el-button>
    </el-tooltip>

    <span v-if="interim" class="interim">{{ interim }}</span>
    <span v-else-if="listening" class="listening-hint">正在听…</span>
  </div>
</template>

<style scoped>
.speech-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.rec-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #fff;
  margin-right: 5px;
  animation: pulse 1.1s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.8); }
}

.interim {
  font-size: 12.5px;
  color: var(--ia-text-sub);
  font-style: italic;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.listening-hint {
  font-size: 12.5px;
  color: var(--ia-fail);
}
</style>
