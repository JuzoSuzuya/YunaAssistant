<template>
  <section class="chat">
    <div ref="listEl" class="msgs">
      <div v-if="!messages.length" class="empty hint">
        Пока тихо. Напиши Юне что-нибудь ↓
      </div>
      <div v-for="(m, i) in messages" :key="m.at + '-' + i" class="msg" :class="m.role">
        <div class="meta">{{ m.role === 'user' ? 'Ты' : 'Юна' }} · {{ m.at || '' }}</div>
        <div class="txt">{{ m.text }}</div>
      </div>
      <div v-if="sending" class="msg assistant pending">
        <div class="meta">Юна</div>
        <div class="txt">печатает…</div>
      </div>
    </div>
    <div class="composer">
      <textarea
        v-model="draft"
        rows="1"
        placeholder="Спроси что угодно… (Enter — отправить)"
        @keydown.enter.exact.prevent="send"
      />
      <label class="screen-toggle hint" title="Прикрепить скриншот к сообщению">
        <input v-model="withScreen" type="checkbox" /> скрин
      </label>
      <button :disabled="sending || !draft.trim()" @click="send">→</button>
    </div>
  </section>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import { api } from '../api.js'

const messages = ref([])
const draft = ref('')
const sending = ref(false)
const withScreen = ref(false)
const listEl = ref(null)
const seen = ref(0)

function scrollBottom() {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

// Incremental sync: append only messages we haven't drawn yet.
async function pollChat() {
  try {
    const data = await api.session()
    const list = data.messages || []
    if (list.length < seen.value) {
      messages.value = []
      seen.value = 0
    }
    for (const m of list.slice(seen.value)) {
      messages.value.push({ role: m.role === 'user' ? 'user' : 'assistant', text: m.text || '', at: m.at || '' })
    }
    seen.value = list.length
    scrollBottom()
  } catch {
    /* offline banner is owned by App.vue health check */
  }
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  sending.value = true
  messages.value.push({ role: 'user', text, at: new Date().toLocaleTimeString() })
  seen.value += 1
  draft.value = ''
  scrollBottom()
  try {
    await api.sendChat(text, withScreen.value)
  } catch {
    messages.value.push({ role: 'assistant', text: 'Не смогла отправить — нет связи с мостом.', at: '' })
  } finally {
    sending.value = false
  }
  // Fire-and-forget queue: the reply lands via session poll.
  setTimeout(pollChat, 2000)
  setTimeout(pollChat, 8000)
}

watch(messages, scrollBottom)

defineExpose({ pollChat })
</script>

<style scoped>
.chat { display: flex; flex-direction: column; min-height: 0; height: 100%; }
.msgs {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
.empty { text-align: center; margin-top: 30px; }
.msg {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 14px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.user { align-self: flex-end; background: linear-gradient(135deg, #3d5afe, #7c4dff); }
.msg.assistant {
  align-self: flex-start;
  background: var(--glass);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--border);
}
.msg.pending { opacity: 0.7; font-style: italic; }
.msg .meta { font-size: 0.7rem; color: var(--muted); margin-bottom: 4px; }
.composer {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 12px;
  border-top: 1px solid var(--border);
  background: var(--glass);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}
.composer textarea { flex: 1; resize: none; min-height: 42px; max-height: 120px; }
.screen-toggle { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
</style>
