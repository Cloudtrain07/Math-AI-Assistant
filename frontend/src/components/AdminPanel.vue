<script setup lang="ts">
import { computed, ref } from 'vue'
import { adminResetPassword, fetchAdminStats, type AdminStats } from '../api'

/**
 * 管理区域（仅本机直连的登录用户可见，由 /api/me 的 local_admin 决定挂载）。
 * 默认折叠、不占聊天主要空间；首次展开时才请求统计，普通远程用户不加载。
 */

const open = ref(false)
const loading = ref(false)
const loadError = ref('')
const stats = ref<AdminStats | null>(null)

const resetAccount = ref('')
const newPassword = ref('')
const resetting = ref(false)
const resetMessage = ref('')
const resetOk = ref(false)

// 统计不可用（从未加载成功 / 读取失败 / 正在刷新）时重置表单完整禁用；
// 提交处理函数内再次检查，不依赖按钮 disabled
const statsUnavailable = computed(
  () => loading.value || loadError.value !== '' || !stats.value,
)

async function refresh() {
  loading.value = true
  loadError.value = ''
  try {
    stats.value = await fetchAdminStats()
  } catch (err) {
    // 读取失败时清空旧数据，明确显示错误并提供重试按钮，不显示假零值
    stats.value = null
    loadError.value = err instanceof Error ? err.message : '用量数据读取失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

function onToggle(event: Event) {
  open.value = (event.target as HTMLDetailsElement).open
  if (open.value && !stats.value && !loadError.value && !loading.value) {
    void refresh()
  }
}

function formatTokens(value: number): string {
  return value.toLocaleString('en-US')
}

async function submitReset() {
  if (resetting.value || statsUnavailable.value) return
  const account = resetAccount.value
  const password = newPassword.value
  if (!account || !password) return
  resetting.value = true
  resetMessage.value = ''
  try {
    await adminResetPassword(account, password)
    resetOk.value = true
    resetMessage.value = `已重置账号 ${account} 的密码。`
    // 成功后立即清空输入框，页面不留新密码
    newPassword.value = ''
  } catch (err) {
    resetOk.value = false
    resetMessage.value = err instanceof Error ? err.message : '重置失败，请稍后重试'
  } finally {
    resetting.value = false
  }
}
</script>

<template>
  <details class="panel admin" :open="open" @toggle="onToggle">
    <summary>
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path d="M10 2.6l5.8 2.1v4.4c0 3.9-2.5 6.6-5.8 8.1-3.3-1.5-5.8-4.2-5.8-8.1V4.7z" />
        <path d="M7.4 10l1.9 1.9 3.4-3.6" />
      </svg>
      管理（仅本机可用）
      <span class="summary-hint">用量统计 · 密码重置</span>
    </summary>
    <div class="admin-body">
      <section class="stats">
        <div class="stats-bar">
          <span v-if="loading" class="hint">读取中…</span>
          <template v-else-if="stats">
            <span>
              今日（{{ stats.date }}）全局已用
              <strong>{{ formatTokens(stats.global_used) }}</strong>
              / {{ formatTokens(stats.global_daily_limit) }} tokens · 登录
              {{ stats.logins }} 次 · 提问 {{ stats.questions }} 次
            </span>
            <button class="btn ghost small" type="button" :disabled="loading" @click="refresh">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M15.5 10a5.5 5.5 0 1 1-1.6-3.9M15.5 3.5v3h-3" />
              </svg>
              刷新统计
            </button>
          </template>
          <template v-else>
            <span class="hint">暂无统计数据</span>
            <button class="btn ghost small" type="button" @click="refresh">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M15.5 10a5.5 5.5 0 1 1-1.6-3.9M15.5 3.5v3h-3" />
              </svg>
              重试
            </button>
          </template>
        </div>
        <p v-if="loadError" class="error">{{ loadError }}</p>
        <div v-if="stats" class="table-wrap">
          <table class="accounts">
            <thead>
              <tr>
                <th>账号</th>
                <th>状态</th>
                <th class="num">今日用量</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in stats.accounts" :key="a.username">
                <td>
                  {{ a.username }}
                  <span v-if="a.display_name !== a.username" class="display">（{{ a.display_name }}）</span>
                </td>
                <td>
                  <span class="pill" :class="a.enabled ? 'ok' : 'off'">
                    {{ a.enabled ? '正常' : '停用' }}
                  </span>
                </td>
                <td class="num">{{ formatTokens(a.used) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <form class="reset" @submit.prevent="submitReset">
        <label>
          重置密码账号
          <select v-model="resetAccount" :disabled="resetting || statsUnavailable">
            <option value="" disabled>请选择账号</option>
            <option v-for="a in stats?.accounts ?? []" :key="a.username" :value="a.username">
              {{ a.username }}{{ a.display_name !== a.username ? `（${a.display_name}）` : '' }}
            </option>
          </select>
        </label>
        <label>
          新密码
          <input
            v-model="newPassword"
            type="password"
            autocomplete="new-password"
            placeholder="输入新密码"
            :disabled="resetting || statsUnavailable"
          />
        </label>
        <button
          class="btn primary"
          type="submit"
          :disabled="resetting || statsUnavailable || !resetAccount || !newPassword"
        >
          {{ resetting ? '重置中…' : '确认重置' }}
        </button>
      </form>
      <p v-if="resetMessage" :class="resetOk ? 'success' : 'error'">{{ resetMessage }}</p>
    </div>
  </details>
</template>

<style scoped>
/* 与设置面板同族的折叠面板体系（变量来自共享 style.css） */
.panel {
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  margin-top: 10px;
  font-size: 0.88rem;
  background: var(--surface);
  box-shadow: var(--shadow-card);
}
.panel summary {
  cursor: pointer;
  user-select: none;
  padding: 9px 14px;
  color: var(--ink-2);
  display: flex;
  align-items: center;
  gap: 8px;
  list-style: none;
  border-radius: var(--radius-card);
}
.panel summary::-webkit-details-marker {
  display: none;
}
.panel summary::after {
  content: '';
  margin-left: auto;
  width: 8px;
  height: 8px;
  border-right: 1.6px solid var(--ink-3);
  border-bottom: 1.6px solid var(--ink-3);
  transform: rotate(45deg);
  transition: transform 0.15s;
}
.panel[open] summary::after {
  transform: rotate(225deg);
}
.panel summary svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.panel summary:hover {
  color: var(--ink);
  background: var(--surface-2);
}
.summary-hint {
  color: var(--ink-3);
  font-size: 0.78rem;
}
.admin-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 14px 14px;
}
.stats-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  color: var(--ink);
  font-size: 0.86rem;
}
.stats-bar strong {
  font-variant-numeric: tabular-nums;
  color: var(--ink);
}
.btn.small {
  padding: 4px 10px;
  font-size: 0.82rem;
  background: var(--surface);
}
.btn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}
.table-wrap {
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  overflow-x: auto;
  background: var(--surface);
}
.accounts {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.accounts th,
.accounts td {
  text-align: left;
  padding: 7px 12px;
  border-bottom: 1px solid var(--line-soft);
}
.accounts th {
  color: var(--ink-2);
  font-weight: 550;
  background: var(--surface-2);
  font-size: 0.8rem;
}
.accounts tr:last-child td {
  border-bottom: none;
}
.accounts .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.pill {
  display: inline-block;
  font-size: 0.74rem;
  border-radius: 999px;
  padding: 1px 9px;
}
.pill.ok {
  color: var(--ok);
  background: var(--ok-bg);
  border: 1px solid var(--ok-line);
}
.pill.off {
  color: var(--ink-3);
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
}
.display {
  color: var(--ink-3);
}
.reset {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}
.reset label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--ink-2);
  font-size: 0.84rem;
}
.reset select,
.reset input {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 7px 10px;
  background: var(--surface);
  font-size: 0.88rem;
  color: var(--ink);
  min-width: 170px;
}
.reset .btn.primary {
  padding: 8px 16px;
}
.success {
  margin: 0;
  color: var(--ok);
}
.error {
  margin: 0;
  color: var(--danger);
}
.hint {
  margin: 0;
  color: var(--ink-2);
}
</style>
