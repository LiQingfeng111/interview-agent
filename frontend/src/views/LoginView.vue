<script setup lang="ts">
/**
 * 登录 / 注册页。
 *
 * 两个 Tab 而非两个页面：注册完通常立刻要登录，放一起少一次跳转。
 */
import { ref, reactive } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const tab = ref<'login' | 'register'>('login')
const loading = ref(false)

const form = reactive({
  user: '',
  password: '',
  confirm: '',
})

async function doLogin() {
  if (!form.user.trim() || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.user.trim(), form.password)
    ElMessage.success(`欢迎，${auth.user}`)
    const redirect = (route.query.redirect as string) || '/interview'
    router.push(redirect)
  } catch {
    // 拦截器已经提示过具体原因（如"用户名或密码不对"）
  } finally {
    loading.value = false
  }
}

async function doRegister() {
  if (form.password !== form.confirm) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  if (form.password.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  loading.value = true
  try {
    const r = await auth.register(form.user.trim(), form.password)
    ElMessage.success(
      r.first
        ? '注册成功，你是第一个用户，已获得管理员权限'
        : '注册成功，请登录',
    )
    tab.value = 'login'
  } catch {
    // 拦截器提示过
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="head">
        <h1>模拟面试 Agent</h1>
        <p class="ia-sub">项目深挖型面试 · 出题交给模型 · 流程由代码判定</p>
      </div>

      <el-tabs v-model="tab" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form label-position="top" @submit.prevent="doLogin">
            <el-form-item label="用户名">
              <el-input v-model="form.user" placeholder="请输入用户名" autocomplete="username" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="form.password"
                type="password"
                placeholder="请输入密码"
                show-password
                autocomplete="current-password"
                @keyup.enter="doLogin"
              />
            </el-form-item>
            <el-button
              type="primary"
              :loading="loading"
              style="width: 100%"
              @click="doLogin"
            >
              登录
            </el-button>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form label-position="top" @submit.prevent="doRegister">
            <el-form-item label="用户名">
              <el-input
                v-model="form.user"
                placeholder="3–20 位字母、数字、下划线"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="form.password"
                type="password"
                placeholder="至少 6 位"
                show-password
                autocomplete="new-password"
              />
            </el-form-item>
            <el-form-item label="确认密码">
              <el-input
                v-model="form.confirm"
                type="password"
                placeholder="再输一次"
                show-password
                autocomplete="new-password"
                @keyup.enter="doRegister"
              />
            </el-form-item>
            <el-button
              type="success"
              :loading="loading"
              style="width: 100%"
              @click="doRegister"
            >
              注册
            </el-button>
            <p class="ia-sub ia-mt8" style="text-align: center">
              第一个注册的用户自动获得管理员权限
            </p>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2b3a 0%, #35506e 100%);
  padding: 24px;
}

.login-card {
  width: 400px;
  background: #fff;
  border-radius: 16px;
  padding: 32px 28px 24px;
  box-shadow: 0 12px 40px rgba(10, 20, 40, 0.24);
}

.head {
  text-align: center;
  margin-bottom: 20px;
}

.head h1 {
  margin: 0 0 6px;
  font-size: 20px;
  font-weight: 600;
  color: var(--ia-text);
}

.head p {
  margin: 0;
}
</style>
