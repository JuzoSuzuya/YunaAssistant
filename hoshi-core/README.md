# Hoshi

Telegram-бот и локальный демон для управления аниме-каналом через Cursor Agent.

## Быстрый старт

```bash
cd /root/projects/Hoshi
cp .env.example .env   # если ещё нет
chmod +x hoshi_ctl.sh poll_loop.sh
./hoshi_ctl.sh start
./hoshi_ctl.sh status
```

## Команды бота (только владелец)

| Команда | Описание |
|---------|----------|
| `/settings` | Настройки: аккаунт, посты, канал, источники |
| `agent` / `cursor` | Начать диалог с агентом |
| `/exit` | Закрыть диалог (с подтверждением, история сохраняется) |

## Настройки

- **Привязка аккаунта** — нужны `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` в `.env`
- **Писать в чаты** — по умолчанию выключено (только чтение)
- **Посты/день, стиль, канал, биржи** — через `/settings`

## Архитектура

- `tg_bridge.py` — ingress (Telegram)
- `hoshi_daemon.py` — worker (очередь + cursor-agent)
- `data/tg_inbox/` — входящие задачи
- `data/agent_sessions/` — история диалога (не удаляется при /exit)

## Управление

```bash
./hoshi_ctl.sh start|stop|restart|status
./hoshi_ctl.sh bridge start|stop
./hoshi_ctl.sh daemon start|stop
```
