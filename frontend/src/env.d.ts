/// <reference types="vite/client" />

/**
 * Vue SFC 类型声明。
 *
 * 为什么需要这个文件：TypeScript 不认识 `.vue` 后缀，
 * 没有它 `import X from './X.vue'` 一律报 TS2307。
 * vite-plugin-vue 提供了 `vue/dist/vue.d.ts`，但显式声明更稳。
 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
