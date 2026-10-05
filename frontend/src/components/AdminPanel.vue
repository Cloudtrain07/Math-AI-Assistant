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
    resetMessage.value = `✅ 已重置账号 ${account} 的密码。`
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
  <details class="admin" :open="open" @toggle="onToggle">
    <summary>🛡️ 管理（仅本机可用）</summary>
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
            <button class="ghost" type="button" :disabled="loading" @click="refresh">
              🔄 刷新统计
            </button>
          </template>
          <template v-else>
            <span class="hint">暂无统计数据</span>
            <button class="ghost" type="button" @click="refresh">🔄 重试</button>
          </template>
        </div>
        <p v-if="loadError" class="error">{{ loadError }}</p>
        <table v-if="stats" class="accounts">
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
              <td>{{ a.enabled ? '✅' : '⛔' }}</td>
              <td class="num">{{ formatTokens(a.used) }}</td>
            </tr>
          </tbody>
        </table>
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
          class="send"
          type="submit"
          :disabled="resetting || statsUnavailable || !resetAccount || !newPassword"
        >
          {{ resetting ? '重置中…' : '确认重置' }}
        </button>
      </form>
      <p v-if="resetMessage" :class="resetOk ? 'hint' : 'error'">{{ resetMessage }}</p>
    </div>
  </details>
</template>

<style scoped>
.admin {
  border: 1px solid #e1e4e8;
  border-radius: 10px;
  margin-top: 8px;
  font-size: 0.88rem;
  background: #fafbfc;
}
.admin summary {
  cursor: pointer;
  user-select: none;
  padding: 6px 10px;
  color: #57606a;
}
.admin-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 2px 10px 10px;
}
.stats-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  color: #43505e;
}
.stats-bar strong {
  font-variant-numeric: tabular-nums;
}
.admin .ghost {
  border: 1px solid #d0d7de;
  background: #fff;
  color: #43505e;
  border-radius: 8px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 0.85rem;
}
.admin .ghost:hover:not(:disabled) {
  border-color: #1a73e8;
  color: #1a73e8;
  background: #f2f7ff;
}
.admin .ghost:disabled {
  opacity: 0.5;
  cursor: default;
}
.accounts {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
  border: 1px solid #e1e4e8;
  border-radius: 8px;
}
.accounts th,
.accounts td {
  text-align: left;
  padding: 5px 10px;
  border-bottom: 1px solid #eef1f4;
}
.accounts th {
  color: #57606a;
  font-weight: 500;
  background: #fafbfc;
}
.accounts tr:last-child td {
  border-bottom: none;
}
.accounts .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.display {
  color: #8b949e;
}
.reset {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
.reset label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #57606a;
}
.reset select,
.reset input {
  border: 1px solid #d0d7de;
  border-radius: 8px;
  padding: 5px 8px;
  background: #fff;
  font-size: 0.88rem;
  min-width: 160px;
}
.reset .send {
  border: none;
  background: #1a73e8;
  color: #fff;
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 0.88rem;
  cursor: pointer;
  white-space: nowrap;
}
.reset .send:disabled {
  opacity: 0.5;
  cursor: default;
}
.hint {
  margin: 0;
  color: #57606a;
}
.error {
  margin: 0;
  color: #cf222e;
}
</style>
