<template>
  <div class="statusbar" title="Живой статус Юны (опрос /api/status каждые 400 мс)">
    <div
      v-for="ind in indicators"
      :key="ind.key"
      class="ind"
      :class="{ on: ind.on }"
      :title="ind.tip"
    >
      <span class="dot">{{ ind.icon }}</span>
      <span class="cap">{{ ind.tip }}</span>
    </div>
    <div class="detail" :title="status.updated_at ? ('обновлено: ' + status.updated_at) : ''">
      {{ status.detail || '…' }}
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: Object, default: () => ({}) },
})

// Same mapping as the GTK app (yuna_app.py::_poll_status):
// listening/speaking <- voice, thinking <- agent, watching <- screen, playing <- pilot.
const indicators = computed(() => {
  const st = props.status || {}
  const playing = st.pilot === 'playing' || st.mode === 'playing'
  return [
    { key: 'listening', icon: '🎤', tip: 'Слушает', on: st.voice === 'listening' },
    { key: 'thinking', icon: '⚙', tip: 'Думает', on: st.agent === 'thinking' },
    { key: 'speaking', icon: '🔊', tip: 'Говорит', on: st.voice === 'speaking' },
    { key: 'watching', icon: '👁', tip: 'Смотрит', on: st.screen === 'watching' },
    { key: 'playing', icon: '🎮', tip: 'Играет', on: playing },
  ]
})
</script>

<style scoped>
.statusbar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 16px;
  background: var(--glass);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}
.ind { display: flex; flex-direction: column; align-items: center; gap: 1px; opacity: 0.35; transition: opacity 0.25s; min-width: 52px; }
.ind.on { opacity: 1; }
.dot { font-size: 1.05rem; line-height: 1.1; }
.ind.on .dot { filter: drop-shadow(0 0 6px rgba(124, 158, 255, 0.9)); }
.cap { font-size: 0.68rem; color: var(--muted); }
.ind.on .cap { color: var(--text); }
.detail {
  margin-left: auto;
  font-size: 0.8rem;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 45%;
}
</style>
