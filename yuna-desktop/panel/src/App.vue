<template>
  <div class="shell">
    <div v-if="!online" class="banner">⚠ Нет связи с мостом — переподключение…</div>

    <header class="top">
      <img :src="logoSrc" alt="Юна" class="logo" @error="logoGone = true" :style="logoGone ? 'display:none' : ''" />
      <div class="title">
        <h1>Юна <span>by Hoshi</span></h1>
        <span class="sub">{{ healthLine }}</span>
      </div>
      <nav class="tabs">
        <button :class="{ active: view === 'chat' }" @click="view = 'chat'">Чат</button>
        <button :class="{ active: view === 'commands' }" @click="view = 'commands'">Команды</button>
        <button :class="{ active: view === 'settings' }" @click="view = 'settings'">Настройки</button>
      </nav>
    </header>

    <StatusBar :status="status" />

    <main class="body">
      <ChatView v-show="view === 'chat'" ref="chatRef" />
      <CommandsView v-show="view === 'commands'" ref="cmdsRef" />
      <SettingsView v-show="view === 'settings'" ref="settingsRef" />
    </main>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { api } from './api.js'
import StatusBar from './components/StatusBar.vue'
import ChatView from './components/ChatView.vue'
import SettingsView from './components/SettingsView.vue'
import CommandsView from './components/CommandsView.vue'

const view = ref('chat')
const online = ref(true)
const logoGone = ref(false)
// Logo is served by the bridge from project-root assets/ (local_bridge.py),
// not bundled — keep as a plain runtime URL so Vite leaves it alone.
const logoSrc = '/assets/yuna.svg'
const status = ref({ mode: 'idle', detail: 'Подключаюсь…', voice: 'idle', agent: 'idle', screen: 'idle', pilot: 'idle' })
const health = ref({ pending: 0, messages: 0 })
const chatRef = ref(null)
const cmdsRef = ref(null)
const settingsRef = ref(null)

const healthLine = computed(() =>
  online.value
    ? `память синхр. · ${health.value.messages} сообщ. · очередь ${health.value.pending}`
    : 'переподключение…',
)

let statusTimer = null
let chatTimer = null
let healthTimer = null

async function pollStatus() {
  try {
    status.value = await api.status()
  } catch {
    /* health check owns the offline state */
  }
}

async function pollHealth() {
  try {
    const h = await api.health()
    health.value = { pending: h.pending || 0, messages: h.messages || 0 }
    if (!online.value) {
      online.value = true
      chatRef.value?.pollChat()
      settingsRef.value?.load()
      cmdsRef.value?.refresh()
    } else {
      online.value = true
    }
  } catch {
    online.value = false
  }
}

// Same contract as the GTK app: status every 400ms, chat every 8s.
statusTimer = setInterval(pollStatus, 400)
chatTimer = setInterval(() => chatRef.value?.pollChat(), 8000)
healthTimer = setInterval(pollHealth, 5000)
pollStatus()
pollHealth()
chatRef.value?.pollChat()

onBeforeUnmount(() => {
  clearInterval(statusTimer)
  clearInterval(chatTimer)
  clearInterval(healthTimer)
})
</script>

<style scoped>
.shell { display: flex; flex-direction: column; height: 100%; }
.banner {
  background: linear-gradient(135deg, #7a3b00, #7a1f00);
  color: #ffd9a0;
  text-align: center;
  font-size: 0.85rem;
  font-weight: 600;
  padding: 8px;
}
.top {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--glass);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
}
.logo { width: 36px; height: 36px; border-radius: 50%; }
.title h1 { margin: 0; font-size: 1.1rem; font-weight: 600; }
.title h1 span { color: var(--muted); font-size: 0.85rem; font-weight: 400; }
.sub { color: var(--muted); font-size: 0.8rem; }
.tabs { display: flex; gap: 6px; margin-left: auto; }
.tabs button {
  background: var(--bg);
  color: var(--muted);
  border: 1px solid var(--border);
  padding: 8px 14px;
}
.tabs button.active { background: linear-gradient(135deg, #3d5afe, #7c4dff); color: #fff; border-color: transparent; }
.body { flex: 1; min-height: 0; }
</style>
