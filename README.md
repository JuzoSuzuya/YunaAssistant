# Yuna by.Hoshi

Локальный ассистент **Юна** с **единой памятью** между ПК и Telegram-ботом на сервере.

Один контекст, одна история диалога, одни настройки — независимо от того, пишешь ты в Telegram или в панели на компьютере.

## Архитектура

```
┌─────────────────┐     rsync/sync      ┌──────────────────┐
│  ПК (Desktop)   │ ◄────────────────► │  Сервер (France) │
│                 │   единая память    │                  │
│ memory_hub      │                    │ tg_bridge        │
│ local_bridge    │                    │ hoshi_daemon     │
│ local_daemon    │                    │ (Telegram)       │
│ voice_daemon    │                    │                  │
└─────────────────┘                    └──────────────────┘
         │                                       │
         └──────── data/agent_sessions ──────────┘
                  data/settings.json
                  data/saved_ideas.json
                  data/local_context.json
```

### Процессы на ПК

| Процесс | Назначение |
|---------|------------|
| `memory_hub` | Фоновая синхронизация памяти с сервером (~1.5 с) |
| `voice_daemon` | TTS отдельно — не блокируется агентом |
| `local_bridge` | HTTP API + панель настроек (`:8765`) |
| `local_daemon` | cursor-agent, локальная очередь `local_inbox` |

## Быстрый старт

```bash
cd "/home/hoshikojima/Documents/Projects/Yuna by.Hoshi"
cp .env.example .env
# Заполни YUNA_SERVER_SSH_PASS или настрой SSH-ключ

python3 -m venv .venv
.venv/bin/pip install -r hoshi-core/requirements.txt python-dotenv aiohttp

chmod +x yuna-desktop/yuna_ctl.sh
./yuna-desktop/yuna_ctl.sh start
./yuna-desktop/yuna_ctl.sh panel
```

### Кнопка в Waybar

```bash
chmod +x scripts/install-waybar-yuna.sh
./scripts/install-waybar-yuna.sh
# Перезапусти waybar
```

## Единая память

Синхронизируются:

- `data/agent_sessions/{owner_id}.json` — история диалога с Юной
- `data/settings.json` — настройки (desktop-блок — с ПК)
- `data/saved_ideas.json` — «запомни …»
- `data/local_context.json` — активный проект, скрины, заметки с ПК

**Сценарий:** работаешь над проектом на ПК → пишешь в Telegram «скинь что мы делали» → бот видит `local_context.json` и историю сессии.

Ручная синхронизация:

```bash
./yuna-desktop/yuna_ctl.sh sync
```

## Патч сервера (контекст ПК в Telegram)

После обновления локального кода:

```bash
./scripts/deploy-server-memory.sh
```

## Управление

```bash
./yuna-desktop/yuna_ctl.sh start|stop|restart|status|sync|panel
```

Панель: http://127.0.0.1:8765/

## Требования

- `cursor-agent` в PATH (или `CURSOR_AGENT_BIN` в `.env`)
- `ffmpeg`, `grim` (скриншоты Hyprland)
- `pw-play` / `mpv` (воспроизведение голоса)
- SSH до сервера
