import { createApp } from 'vue'
import 'katex/dist/katex.min.css'
import './style.css'

// 正常聊天页：App.vue（不引用任何 mock / 故障注入）。
// 开发专用原型（公式演示 + 故障注入）：#demo 入口，独立分包，正常入口不含。
// 设计预览（假数据三个视图）：#design 入口，独立分包，仅用于确认设计方案。
async function boot() {
  if (window.location.hash === '#demo') {
    const { default: DemoPage } = await import('./DemoPage.vue')
    createApp(DemoPage).mount('#app')
  } else if (window.location.hash === '#design') {
    const { default: DesignPreview } = await import('./preview/DesignPreview.vue')
    createApp(DesignPreview).mount('#app')
  } else {
    const { default: App } = await import('./App.vue')
    createApp(App).mount('#app')
  }
}

void boot()
