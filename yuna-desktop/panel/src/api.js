// Thin data layer over the bridge HTTP API.
// Same contracts as the GTK app (yuna_app.py) and the legacy web panel:
//   GET  /health                  -> {ok, pending, messages, ...}  (NOTE: /health, not /api/health)
//   GET  /api/status              -> {mode, detail, voice, agent, screen, pilot, updated_at}
//   GET  /api/session/desktop     -> {messages: [{role, text, at}]}
//   POST /api/chat {text, screen} -> {ok, task_id}  (async queue, reply arrives via session poll)
//   GET/POST /api/settings        -> full settings object (schema is READ-ONLY: never drop keys)
//   POST /api/voice/test {voice, text}
//   POST /api/command {text}      -> {matched, command, results} | {matched:false, error}
//   (GET /api/command does not exist; availability probe uses POST with a no-match string)

async function req(method, path, body, timeoutMs = 10000) {
  const ctrl = new AbortController()
  const t = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const r = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    })
    if (!r.ok) {
      const err = new Error(`HTTP ${r.status} ${path}`)
      err.status = r.status
      throw err
    }
    return await r.json()
  } finally {
    clearTimeout(t)
  }
}

export const api = {
  async health() {
    // Bridge exposes /health (local_bridge.py). Accept /api/health as fallback.
    try {
      return await req('GET', '/health', undefined, 4000)
    } catch (e) {
      return await req('GET', '/api/health', undefined, 4000)
    }
  },
  status() {
    return req('GET', '/api/status', undefined, 3000)
  },
  session() {
    return req('GET', '/api/session/desktop', undefined, 8000)
  },
  sendChat(text, screen = false) {
    return req('POST', '/api/chat', { text, screen }, 15000)
  },
  getSettings() {
    return req('GET', '/api/settings', undefined, 8000)
  },
  saveSettings(payload) {
    return req('POST', '/api/settings', payload, 10000)
  },
  voiceTest(payload) {
    return req('POST', '/api/voice/test', payload, 60000)
  },
  // Command engine (T4 registry + T5 endpoint). The bridge exposes only
  // POST /api/command, so availability is probed with a no-match POST.
  // Returns {available:false} when the bridge is down instead of throwing.
  async commandList() {
    try {
      const r = await req('POST', '/api/command', { text: '__yuna_probe__' }, 5000)
      return { available: true, ...r }
    } catch (e) {
      return { available: false, commands: [] }
    }
  },
  async commandRun(text) {
    try {
      const r = await req('POST', '/api/command', { text }, 30000)
      return { available: true, ...r }
    } catch (e) {
      if (e.status === 404) return { available: false }
      throw e
    }
  },
}

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}
