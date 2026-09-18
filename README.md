# Yuna by.Hoshi

Локальный AI-ассистент для рабочего стола: голос, зрение, память и управление компьютером — всё работает локально, только на бесплатных моделях.

## Что это

**Юна** — десктоп-ассистент, который живёт на твоём ПК:

- **Чат и голос** — разговаривай с Юной текстом или голосом (whisper для распознавания, silero/piper для синтеза).
- **Зрение** — Юна видит экран (`qwen2.5vl:7b`) и отвечает на вопросы по скриншотам.
- **Управление компьютером** — команды: скриншоты, громкость, окна, файлы и 1000+ встроенных семейств команд через реестр команд.
- **Память** — единая история диалога и контекст между ПК и Telegram-ботом.
- **Нативный десктоп** — Tauri-оболочка с треем, автозапуском и супервизией стека.

## Стек

| Слой | Технология |
|------|------------|
| Десктоп-оболочка | **Tauri v2** (Rust), трей, автозапуск |
| Панель управления | **Vue 3 SPA** (Vite), `yuna-desktop/panel/` |
| HTTP-мост | **Python** `local_bridge.py`, `http://127.0.0.1:8765` |
| Мозг | **ollama** на `127.0.0.1:11435` — `qwen2.5:14b` (основной), `llama3.1:8b` (быстрый) |
| Зрение | `qwen2.5vl:7b` |
| Речь | whisper (STT) + silero/piper (TTS) |

**Только бесплатные локальные модели.** Никаких платных API и ключей в рантайме — это железное правило проекта (см. `AGENTS.md`).

## Как запустить

```bash
# 1. Подготовка (один раз)
cp .env.example .env          # заполни при необходимости
python3 -m venv .venv
.venv/bin/pip install -r hoshi-core/requirements.txt python-dotenv aiohttp

# 2. Запуск стека (ollama → мост → голосовой демон)
bash scripts/start_stack.sh

# 3. Панель
#   SPA:  http://127.0.0.1:8765/
#   Или собери и запусти нативную оболочку:
cd src-tauri && cargo tauri build
./target/release/yuna-desktop
```

Управление стеком: `yuna-desktop/yuna_ctl.sh start|stop|restart|status|sync|panel`.

## Структура

- `yuna-desktop/` — бэкенд-модули: `local_bridge.py` (HTTP API), `ollama_brain.py`, `voice_agent.py`, `memory_hub.py`, `command_registry.py`, `computer_control.py` и др.
  - `yuna-desktop/panel/` — Vue 3 SPA панель управления.
- `src-tauri/` — нативная оболочка (Tauri v2).
- `hoshi-core/` — серверная логика (единая память, Telegram, видео, голос).
- `scripts/` — вспомогательные скрипты (`start_stack.sh`, деплой, установка).

## Требования

- Python 3, Node.js (для сборки SPA), Rust (для сборки Tauri)
- ollama с моделями из раздела «Стек»
- `ffmpeg`, `grim` (скриншоты Hyprland), `pw-play` / `mpv` (голос)