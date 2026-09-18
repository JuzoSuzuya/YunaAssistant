<template>
  <section class="settings">
    <div v-if="loading" class="hint">Загружаю настройки…</div>
    <div v-else class="grid">
      <div class="card">
        <h3>🔊 Голос</h3>
        <label class="f"><span class="lbl">Движок</span>
          <select v-model="form.voice.engine">
            <option v-for="e in catalog.engines" :key="e" :value="e">{{ e }}</option>
          </select>
        </label>
        <label class="f"><span class="lbl">Диктор Silero</span>
          <select v-model="form.voice.silero_speaker">
            <option v-for="s in catalog.silero_speakers" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <label class="toggle"><input v-model="form.voice.use_rvc" type="checkbox" /> Аниме-конвертация (RVC)</label>
        <div v-if="form.voice.use_rvc">
          <label class="f"><span class="lbl">RVC-модель</span>
            <select v-model="form.voice.rvc_model">
              <option v-for="m in catalog.rvc_models" :key="m" :value="m">{{ m }}</option>
            </select>
          </label>
          <label class="f"><span class="lbl">Высота тона: {{ form.voice.rvc_pitch }}</span>
            <input v-model.number="form.voice.rvc_pitch" type="range" min="-6" max="12" step="1" />
          </label>
          <label class="f"><span class="lbl">Характер: {{ form.voice.rvc_index }}</span>
            <input v-model.number="form.voice.rvc_index" type="range" min="0" max="1" step="0.02" />
          </label>
        </div>
        <label class="toggle"><input v-model="form.voice.cleanup_on" type="checkbox" /> Чистка эха/шума</label>
        <label class="toggle"><input v-model="form.voice_autospeak" type="checkbox" /> Озвучивать ответы</label>
        <button class="ghost" :disabled="testing" style="width:100%" @click="testVoice">
          {{ testing ? 'Говорю…' : '▶ Прослушать голос' }}
        </button>
      </div>

      <div class="card">
        <h3>⚙ Поведение</h3>
        <label class="f"><span class="lbl">Стиль ответов</span>
          <select v-model="form.reply_style">
            <option value="short">Коротко</option>
            <option value="balanced">Сбалансированно</option>
            <option value="detailed">Подробно</option>
          </select>
        </label>
        <label class="f"><span class="lbl">Активный проект</span>
          <input v-model="form.active_project" type="text" placeholder="/home/.../проект" />
        </label>
        <label class="toggle"><input v-model="form.screen_watch" type="checkbox" /> Следить за экраном</label>
        <label class="f"><span class="lbl">Интервал слежения: {{ form.screen_interval }} сек</span>
          <input v-model.number="form.screen_interval" type="range" min="5" max="60" step="1" />
        </label>
      </div>

      <div class="card">
        <h3>🎵 Действия и вид</h3>
        <label class="f"><span class="lbl">Любимая музыка</span>
          <input v-model="form.favorite_music" type="text" />
        </label>
        <label class="f"><span class="lbl">Любимые обои (тема)</span>
          <input v-model="form.favorite_wallpaper" type="text" />
        </label>
        <label class="f"><span class="lbl">Персона</span>
          <input v-model="form.persona" type="text" />
        </label>
        <label class="f"><span class="lbl">Акцентный цвет</span>
          <input v-model="form.accent" type="color" />
        </label>
        <div class="swatches">
          <button
            v-for="c in swatches"
            :key="c"
            class="sw"
            :style="{ background: c }"
            :title="c"
            @click="form.accent = c"
          />
        </div>
      </div>
    </div>
    <div class="savebar">
      <button :disabled="saving" style="width:100%" @click="save">
        {{ saving ? 'Сохраняю…' : 'Сохранить' }}
      </button>
      <div class="hint ok">{{ saveHint }}</div>
      <div v-if="error" class="hint err">{{ error }}</div>
    </div>
  </section>
</template>

<script setup>
import { reactive, ref, watch } from 'vue'
import { api } from '../api.js'

// Schema is READ-ONLY: every key ever returned by GET /api/settings is kept in
// `raw` and merged back on POST. The form edits known keys; unknown future keys
// pass through untouched (additive-only rule).
const raw = ref({})
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const saveHint = ref('')
const error = ref('')
const catalog = ref({ engines: ['silero', 'piper', 'edge'], silero_speakers: ['baya'], rvc_models: ['yuna'] })
const swatches = ['#7c9eff', '#b794f6', '#ff6ec7', '#4dd0e1', '#69f0ae', '#ffab40']

const form = reactive({
  persona: 'yuna',
  voice_autospeak: true,
  active_project: '',
  accent: '#7c9eff',
  screen_watch: true,
  screen_interval: 18,
  reply_style: 'balanced',
  favorite_music: '',
  favorite_wallpaper: '',
  voice: { engine: 'silero', silero_speaker: 'baya', use_rvc: false, rvc_model: 'yuna', rvc_pitch: 4, rvc_index: 0.6, cleanup_on: true },
})

function applyAccent(hex) {
  if (!hex) return
  document.documentElement.style.setProperty('--accent', hex)
}
watch(() => form.accent, applyAccent)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const s = await api.getSettings()
    raw.value = { ...s }
    delete raw.value.catalog
    if (s.catalog) catalog.value = s.catalog
    for (const k of ['persona', 'voice_autospeak', 'active_project', 'accent', 'screen_watch', 'screen_interval', 'reply_style', 'favorite_music', 'favorite_wallpaper']) {
      if (s[k] !== undefined) form[k] = s[k]
    }
    if (s.voice) Object.assign(form.voice, s.voice)
    applyAccent(form.accent)
  } catch {
    error.value = 'Не смогла загрузить настройки — нет связи с мостом.'
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  saveHint.value = ''
  error.value = ''
  try {
    // Merge: raw passthrough + edited form (form wins on known keys).
    const payload = { ...raw.value, ...form, voice: { ...(raw.value.voice || {}), ...form.voice }, catalog: undefined }
    delete payload.catalog
    await api.saveSettings(payload)
    saveHint.value = 'Сохранено ✓'
    setTimeout(() => (saveHint.value = ''), 2500)
  } catch {
    error.value = 'Не смогла сохранить — нет связи с мостом.'
  } finally {
    saving.value = false
  }
}

async function testVoice() {
  testing.value = true
  try {
    await api.voiceTest({ voice: { ...form.voice }, text: 'Привет, хозяин! Это мой голос. Как тебе звучание?' })
  } catch {
    /* bridge will 500 if no voice daemon — non-fatal */
  } finally {
    testing.value = false
  }
}

defineExpose({ load })
load()
</script>

<style scoped>
.settings { padding: 16px; overflow-y: auto; height: 100%; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
.card h3 { margin: 0 0 12px; font-size: 0.9rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.swatches { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.sw { width: 30px; height: 30px; border-radius: 50%; padding: 0; border: 2px solid transparent; }
.sw:hover { border-color: var(--text); }
.savebar { margin-top: 12px; }
.hint.ok { color: var(--accent); text-align: center; min-height: 18px; margin-top: 6px; }
.hint.err { color: var(--err); text-align: center; margin-top: 6px; }
</style>
