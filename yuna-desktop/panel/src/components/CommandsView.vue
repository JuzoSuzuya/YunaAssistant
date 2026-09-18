<template>
  <section class="cmds">
    <div v-if="!engine.available" class="card warn">
      Мост недоступен — команды сохраняются локально и уйдут на мост, когда он будет готов.
    </div>

    <div class="card add">
      <h3>{{ editing ? 'Редактировать команду' : 'Новая команда' }}</h3>
      <label class="f"><span class="lbl">Название</span>
        <input v-model="draft.name" type="text" placeholder="например: открой музыку" />
      </label>
      <label class="f"><span class="lbl">Фразы-триггеры (по одной на строку)</span>
        <textarea v-model="draft.patternsText" rows="2" placeholder="включи музыку&#10;поставь лоуфай" />
      </label>
      <label class="f"><span class="lbl">Действия (JSON-список, порядок важен)</span>
        <textarea v-model="draft.actionsText" rows="2" placeholder='[{"fn":"play_music","args":{"query":"lofi"}}]' />
      </label>
      <label class="toggle"><input v-model="draft.enabled" type="checkbox" /> Включена</label>
      <label class="toggle"><input v-model="draft.stop_on_error" type="checkbox" /> Останавливаться при ошибке</label>
      <div class="row">
        <button :disabled="!canSave" @click="saveDraft">{{ editing ? 'Обновить' : 'Добавить' }}</button>
        <button v-if="editing" class="ghost" @click="cancelEdit">Отмена</button>
      </div>
      <div v-if="draftError" class="hint err">{{ draftError }}</div>
    </div>

    <div v-if="!commands.length" class="hint empty">Своих команд пока нет.</div>
    <div v-for="c in commands" :key="c.name" class="card cmd" :class="{ off: !c.enabled }">
      <div class="head">
        <strong>{{ c.name }}</strong>
        <span class="hint">{{ (c.patterns || []).length }} фраз · {{ (c.actions || []).length }} действий</span>
      </div>
      <div class="hint pats">{{ (c.patterns || []).join(' · ') }}</div>
      <div class="row">
        <button class="ghost" @click="toggle(c)">{{ c.enabled ? 'Выключить' : 'Включить' }}</button>
        <button class="ghost" @click="edit(c)">Изменить</button>
        <button class="ghost" :disabled="running === c.name" @click="run(c)">
          {{ running === c.name ? 'Выполняю…' : '▶ Выполнить' }}
        </button>
        <button class="ghost danger" @click="remove(c)">Удалить</button>
      </div>
      <div v-if="results[c.name]" class="hint res">{{ results[c.name] }}</div>
    </div>
  </section>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { api } from '../api.js'

// Data layer: bridge /api/commands/custom is the source of truth (list/save/
// delete sync to the engine's custom.json); localStorage is the offline
// fallback so the UI survives bridge restarts. Shape mirrors the T4 schema:
// {name, patterns[], actions[], enabled, stop_on_error}.
const LS_KEY = 'yuna-custom-commands'
const engine = reactive({ available: false })
const commands = ref([])
const results = ref({})
const running = ref('')
const draftError = ref('')
const editing = ref('')
const draft = reactive({ name: '', patternsText: '', actionsText: '', enabled: true, stop_on_error: true })

function loadLocal() {
  try {
    commands.value = JSON.parse(localStorage.getItem(LS_KEY) || '[]')
  } catch {
    commands.value = []
  }
}
function persistLocal() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(commands.value))
  } catch { /* private mode — non-fatal */ }
}

async function refresh() {
  loadLocal()
  try {
    const r = await api.commandList()
    engine.available = !!r.available
    if (r.available) {
      commands.value = r.commands
      persistLocal()
    }
  } catch {
    engine.available = false
  }
}

const canSave = computed(() => draft.name.trim().length > 0)

function parseDraft() {
  draftError.value = ''
  const patterns = draft.patternsText.split('\n').map((s) => s.trim()).filter(Boolean)
  let actions = []
  if (draft.actionsText.trim()) {
    try {
      actions = JSON.parse(draft.actionsText)
      if (!Array.isArray(actions)) throw new Error('not-array')
    } catch {
      draftError.value = 'Действия — невалидный JSON-список.'
      return null
    }
  }
  return {
    name: draft.name.trim(),
    patterns,
    actions,
    enabled: draft.enabled,
    stop_on_error: draft.stop_on_error,
  }
}

async function saveDraft() {
  const cmd = parseDraft()
  if (!cmd) return
  const i = commands.value.findIndex((c) => c.name === (editing.value || cmd.name))
  if (editing.value) {
    commands.value[i] = cmd
    editing.value = ''
  } else if (i >= 0) {
    commands.value[i] = cmd
  } else {
    commands.value.push(cmd)
  }
  persistLocal()
  const r = await api.commandSave(cmd)
  if (r.available) {
    commands.value = r.commands
    persistLocal()
  } else {
    engine.available = false
  }
  draft.name = ''
  draft.patternsText = ''
  draft.actionsText = ''
  draft.enabled = true
  draft.stop_on_error = true
}

function edit(c) {
  editing.value = c.name
  draft.name = c.name
  draft.patternsText = (c.patterns || []).join('\n')
  draft.actionsText = JSON.stringify(c.actions || [], null, 1)
  draft.enabled = c.enabled !== false
  draft.stop_on_error = c.stop_on_error !== false
}
function cancelEdit() {
  editing.value = ''
  draft.name = ''
  draft.patternsText = ''
  draft.actionsText = ''
}
async function toggle(c) {
  c.enabled = !c.enabled
  persistLocal()
  const r = await api.commandSave(c)
  if (r.available) {
    commands.value = r.commands
    persistLocal()
  } else {
    engine.available = false
  }
}
async function remove(c) {
  commands.value = commands.value.filter((x) => x.name !== c.name)
  persistLocal()
  const r = await api.commandDelete(c.name)
  if (r.available) {
    commands.value = r.commands
    persistLocal()
  } else {
    engine.available = false
  }
}

async function run(c) {
  running.value = c.name
  results.value[c.name] = ''
  try {
    const r = await api.commandRun(c.patterns?.[0] || c.name)
    if (!r.available) {
      results.value[c.name] = 'Мост не умеет выполнять команды.'
    } else if (r.matched) {
      results.value[c.name] = 'Готово ✓ ' + JSON.stringify(r.results || []).slice(0, 200)
    } else {
      results.value[c.name] = 'Не совпало: ' + (r.error || 'no command matched')
    }
  } catch {
    results.value[c.name] = 'Нет связи с мостом.'
  } finally {
    running.value = ''
  }
}

defineExpose({ refresh })
refresh()
</script>

<style scoped>
.cmds { padding: 16px; overflow-y: auto; height: 100%; display: flex; flex-direction: column; gap: 12px; }
.card h3 { margin: 0 0 12px; font-size: 0.9rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.warn { border-color: var(--warn); }
.cmd.off { opacity: 0.6; }
.head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.pats { margin: 6px 0 10px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
.row button { flex: 1; min-width: 100px; }
.danger { color: var(--err); border-color: var(--err); }
.empty { text-align: center; margin-top: 8px; }
.hint.err { color: var(--err); margin-top: 6px; }
.res { margin-top: 8px; color: var(--text); }
code { background: var(--bg); padding: 1px 5px; border-radius: 5px; }
</style>
