#!/usr/bin/env python3
"""Юна — панель управления (GTK4, стиль Jarvis)."""
from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / "hoshi-core"))
sys.path.insert(0, str(ROOT))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from desk_config import BRIDGE_HOST, BRIDGE_PORT, DEFAULT_PERSONA, VOICE_SOCKET  # noqa: E402

API = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
LOGO = PROJECT / "assets" / "yuna.svg"


def _api(method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _voice_talk(persona: str = DEFAULT_PERSONA) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(180)
    sock.connect(str(VOICE_SOCKET))
    sock.sendall(json.dumps({"action": "talk", "persona": persona}, ensure_ascii=False).encode())
    raw = sock.recv(262144)
    sock.close()
    return json.loads(raw.decode() or "{}")


def _content_key(role: str, text: str) -> str:
    """Единый ключ дедупа: role|text (без времени/local — иначе дубли с синка)."""
    return f"{(role or '').strip()}|{(text or '').strip()[:120]}"


class YunaWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Юна")
        self.set_default_size(520, 700)
        self._persona = DEFAULT_PERSONA
        self._busy = False
        self._seen: set[str] = set()
        self._scroll: Gtk.ScrolledWindow | None = None
        self._loaded = False
        self._indicators: dict[str, Gtk.Label] = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        bar = Adw.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if LOGO.exists():
            logo = Gtk.Image.new_from_file(str(LOGO))
            logo.set_pixel_size(28)
            title.append(logo)
        title.append(Gtk.Label(label="Юна", css_classes=["title-2"]))
        bar.set_title_widget(title)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Настройки")
        settings_btn.connect("clicked", self._open_settings)
        bar.pack_end(settings_btn)
        root.append(bar)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, margin_start=16, margin_end=16, margin_top=6)
        for key, label, tip in (
            ("listening", "🎤", "Слушаю"),
            ("thinking", "⚙", "Думаю"),
            ("speaking", "🔊", "Говорю"),
            ("watching", "👁", "Экран"),
            ("playing", "🎮", "Играю"),
        ):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.CENTER)
            icon = Gtk.Label(label=label, opacity=0.35)
            icon.set_tooltip_text(tip)
            cap = Gtk.Label(label=tip, css_classes=["caption"])
            cap.set_opacity(0.5)
            box.append(icon)
            box.append(cap)
            status_row.append(box)
            self._indicators[key] = icon
            self._indicator_caps = getattr(self, "_indicator_caps", {})
            self._indicator_caps[key] = cap
        root.append(status_row)

        self.chat = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.chat.add_css_class("monospace")
        self.chat.set_margin_start(14)
        self.chat.set_margin_end(14)
        self.chat.set_margin_top(8)
        self.chat.set_left_margin(10)
        self.chat.set_right_margin(10)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=self.chat)
        self._scroll = scroll
        root.append(scroll)

        self.status = Gtk.Label(label="Готова · скажи «Юна …»", css_classes=["dim-label"])
        self.status.set_margin_bottom(4)
        root.append(self.status)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_start=12, margin_end=12, margin_bottom=12)
        self.entry = Gtk.Entry(placeholder_text="Или напиши здесь…", hexpand=True)
        self.entry.connect("activate", self._send_text)
        row.append(self.entry)

        send = Gtk.Button(label="Отправить", css_classes=["suggested-action"])
        send.connect("clicked", self._send_text)
        row.append(send)

        self.mic = Gtk.Button(label="🎤 Голос")
        self.mic.set_tooltip_text("Скажи «Юна» + фразу")
        self.mic.connect("clicked", self._send_voice)
        row.append(self.mic)
        root.append(row)

        GLib.timeout_add(8000, self._poll_chat)
        GLib.timeout_add(400, self._poll_status)
        GLib.idle_add(self._load_initial)

    def _set_indicator(self, key: str, active: bool) -> None:
        icon = self._indicators.get(key)
        if icon:
            icon.set_opacity(1.0 if active else 0.3)
        cap = getattr(self, "_indicator_caps", {}).get(key)
        if cap:
            cap.set_opacity(1.0 if active else 0.5)

    def _open_settings(self, *_a: object) -> None:
        try:
            s = _api("GET", "/api/settings", timeout=4)
        except Exception:
            s = {}
        cat = s.get("catalog") or {}
        v = s.get("voice") or {}
        engines = cat.get("engines") or ["silero", "piper", "edge"]
        speakers = cat.get("silero_speakers") or ["baya"]
        models = cat.get("rvc_models") or ["yuna"]
        styles = ["short", "balanced", "detailed"]
        style_ru = {"short": "Коротко", "balanced": "Сбалансированно", "detailed": "Подробно"}

        win = Adw.Window(transient_for=self, modal=True, title="Настройки Юны")
        win.set_default_size(440, 660)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        win.set_content(outer)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=16, margin_bottom=16, margin_start=18, margin_end=18,
        )
        outer.append(Gtk.ScrolledWindow(vexpand=True, child=box))

        def heading(txt: str) -> None:
            lab = Gtk.Label(label=txt, xalign=0, css_classes=["title-4"])
            lab.set_margin_top(8)
            box.append(lab)

        def dropdown(items: list[str], current: str, labels: dict | None = None) -> Gtk.DropDown:
            shown = [labels.get(i, i) if labels else i for i in items]
            dd = Gtk.DropDown.new_from_strings(shown)
            if current in items:
                dd.set_selected(items.index(current))
            return dd

        def labeled(title: str, widget: Gtk.Widget) -> Gtk.Widget:
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            r.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            r.append(widget)
            box.append(r)
            return widget

        def scale(lo: float, hi: float, step: float, val: float) -> Gtk.Scale:
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            sc.set_value(val)
            sc.set_hexpand(True)
            sc.set_draw_value(True)
            sc.set_size_request(180, -1)
            return sc

        heading("🔊 Голос")
        dd_engine = dropdown(engines, v.get("engine", "silero"))
        labeled("Движок", dd_engine)
        dd_speaker = dropdown(speakers, v.get("silero_speaker", "baya"))
        labeled("Диктор Silero", dd_speaker)
        sw_rvc = Gtk.Switch(active=bool(v.get("use_rvc")), valign=Gtk.Align.CENTER)
        labeled("Аниме RVC", sw_rvc)
        dd_model = dropdown(models, v.get("rvc_model", "yuna"))
        labeled("RVC модель", dd_model)
        sc_pitch = scale(-6, 12, 1, float(v.get("rvc_pitch", 4)))
        labeled("Тон", sc_pitch)
        sc_index = scale(0, 1, 0.02, float(v.get("rvc_index", 0.6)))
        labeled("Характер", sc_index)
        sw_cleanup = Gtk.Switch(active=bool(v.get("cleanup_on", True)), valign=Gtk.Align.CENTER)
        labeled("Чистка эха", sw_cleanup)
        sw_autospeak = Gtk.Switch(active=bool(s.get("voice_autospeak", True)), valign=Gtk.Align.CENTER)
        labeled("Озвучивать ответы", sw_autospeak)

        heading("⚙ Поведение")
        dd_style = dropdown(styles, s.get("reply_style", "balanced"), style_ru)
        labeled("Стиль ответов", dd_style)
        sw_screen = Gtk.Switch(active=bool(s.get("screen_watch", True)), valign=Gtk.Align.CENTER)
        labeled("Следить за экраном", sw_screen)
        sc_interval = scale(5, 60, 1, float(s.get("screen_interval", 18)))
        labeled("Интервал, сек", sc_interval)

        heading("🎵 Действия")
        en_music = Gtk.Entry(text=str(s.get("favorite_music", "lofi hip hop radio")), hexpand=True)
        labeled("Любимая музыка", en_music)
        en_wall = Gtk.Entry(text=str(s.get("favorite_wallpaper", "sword art online")), hexpand=True)
        labeled("Любимые обои (тема)", en_wall)

        status = Gtk.Label(label="", css_classes=["dim-label"], xalign=0)
        status.set_margin_top(6)
        box.append(status)

        def collect_voice() -> dict:
            return {
                "engine": engines[dd_engine.get_selected()],
                "silero_speaker": speakers[dd_speaker.get_selected()],
                "use_rvc": sw_rvc.get_active(),
                "rvc_model": models[dd_model.get_selected()],
                "rvc_pitch": int(sc_pitch.get_value()),
                "rvc_index": round(sc_index.get_value(), 2),
                "cleanup_on": sw_cleanup.get_active(),
            }

        def collect_all() -> dict:
            return {
                "voice": collect_voice(),
                "voice_autospeak": sw_autospeak.get_active(),
                "reply_style": styles[dd_style.get_selected()],
                "screen_watch": sw_screen.get_active(),
                "screen_interval": int(sc_interval.get_value()),
                "favorite_music": en_music.get_text().strip(),
                "favorite_wallpaper": en_wall.get_text().strip(),
            }

        def do_test(*_a: object) -> None:
            status.set_text("Говорю…")
            payload = {"voice": collect_voice(), "text": "Привет, хозяин! Это мой голос. Как тебе?"}

            def work() -> None:
                try:
                    _api("POST", "/api/voice/test", payload, timeout=60)
                    GLib.idle_add(status.set_text, "")
                except Exception as e:
                    GLib.idle_add(status.set_text, f"Ошибка: {e}")

            threading.Thread(target=work, daemon=True).start()

        def do_save(*_a: object) -> None:
            status.set_text("Сохраняю…")
            payload = collect_all()

            def work() -> None:
                try:
                    _api("POST", "/api/settings", payload, timeout=10)
                    GLib.idle_add(status.set_text, "Сохранено ✓")
                except Exception as e:
                    GLib.idle_add(status.set_text, f"Ошибка: {e}")

            threading.Thread(target=work, daemon=True).start()

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=8)
        test_btn = Gtk.Button(label="▶ Прослушать")
        test_btn.connect("clicked", do_test)
        save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"], hexpand=True)
        save_btn.connect("clicked", do_save)
        close_btn = Gtk.Button(label="Закрыть")
        close_btn.connect("clicked", lambda *_: win.close())
        btns.append(test_btn)
        btns.append(save_btn)
        btns.append(close_btn)
        box.append(btns)
        win.present()

    def _scroll_bottom(self) -> None:
        buf = self.chat.get_buffer()
        end = buf.get_end_iter()
        mark = buf.get_mark("yuna_end")
        if not mark:
            mark = buf.create_mark("yuna_end", end, False)
        else:
            buf.move_mark(mark, end)
        self.chat.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def _scroll_bottom_later(self) -> None:
        self._scroll_bottom()
        GLib.timeout_add(80, lambda: (self._scroll_bottom(), False)[1])

    def _mark_seen(self, role: str, text: str) -> bool:
        """True если уже видели — не рисовать снова."""
        key = _content_key(role, text)
        if key in self._seen:
            return True
        self._seen.add(key)
        return False

    def _append_line(self, who: str, text: str, *, role: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        r = role or ("assistant" if who == "Юна" else "user")
        if self._mark_seen(r, text):
            return
        buf = self.chat.get_buffer()
        tag = "yuna" if who == "Юна" else "user"
        start_off = buf.get_end_iter().get_offset()
        buf.insert(buf.get_end_iter(), f"{who}: {text}\n\n")
        table = buf.get_tag_table()
        t = table.lookup(tag)
        if not t:
            t = buf.create_tag(tag, weight=Pango.Weight.BOLD if who == "Юна" else Pango.Weight.NORMAL)
        buf.apply_tag(t, buf.get_iter_at_offset(start_off), buf.get_end_iter())
        self._scroll_bottom_later()

    def _format_msg(self, m: dict) -> tuple[str, str]:
        who = "Ты" if m.get("role") == "user" else "Юна"
        return who, (m.get("text") or "").strip()

    def _append_message(self, m: dict) -> None:
        who, text = self._format_msg(m)
        if not text:
            return
        role = str(m.get("role") or ("user" if who == "Ты" else "assistant"))
        self._append_line(who, text, role=role)

    def _poll_status(self) -> bool:
        def work() -> None:
            try:
                st = _api("GET", "/api/status", timeout=2)
                mode = st.get("mode", "idle")
                detail = st.get("detail", "Готова")
                labels = {
                    "listening": "Слушаю… скажи фразу",
                    "thinking": "Думаю…",
                    "speaking": "Говорю…",
                    "watching": "Слежу за экраном",
                    "playing": "Играю…",
                    "idle": "Готова · скажи «Юна …»",
                }

                def apply() -> None:
                    playing = st.get("pilot") == "playing" or mode == "playing"
                    if playing and mode not in ("listening", "speaking", "thinking"):
                        tip = detail if detail and detail not in ("Готова", "стоп") else ""
                        self.status.set_text(f"Играю… {tip}".strip() if tip else "Играю…")
                    else:
                        self.status.set_text(labels.get(mode, detail))
                    self._set_indicator("listening", st.get("voice") == "listening")
                    self._set_indicator("speaking", st.get("voice") == "speaking")
                    self._set_indicator("thinking", st.get("agent") == "thinking")
                    self._set_indicator("watching", st.get("screen") == "watching")
                    self._set_indicator("playing", playing)

                GLib.idle_add(apply)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
        return True

    def _sync_messages(self, msgs: list[dict], initial: bool = False) -> None:
        if initial and not self._loaded:
            self.chat.get_buffer().set_text("")
            self._seen.clear()
            self._loaded = True
        for m in msgs:
            self._append_message(m)
        if initial:
            self._scroll_bottom_later()

    def _load_initial(self) -> None:
        def work() -> None:
            try:
                data = _api("GET", "/api/session/desktop", timeout=5)
                msgs = data.get("messages") or []
                GLib.idle_add(self._sync_messages, msgs, True)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _poll_chat(self) -> bool:
        if self._busy:
            return True

        def work() -> None:
            try:
                data = _api("GET", "/api/session/desktop", timeout=3)
                msgs = data.get("messages") or []
                GLib.idle_add(self._sync_messages, msgs, False)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
        return True

    def _send_text(self, *_a: object) -> None:
        text = self.entry.get_text().strip()
        if not text or self._busy:
            return
        self.entry.set_text("")
        self._append_line("Ты", text, role="user")
        self.status.set_text("Думаю…")
        self._busy = True

        def work() -> None:
            try:
                r = _api("POST", "/api/chat/wait", {"text": text}, timeout=320)
                reply = (r.get("reply") or "").strip()
                if reply:
                    GLib.idle_add(self._append_line, "Юна", reply)
                GLib.idle_add(self.status.set_text, "Готова · скажи «Юна …»")
            except urllib.error.HTTPError as e:
                err = e.read().decode() if e.fp else str(e)
                try:
                    err = json.loads(err).get("error", err)
                except Exception:
                    pass
                GLib.idle_add(self.status.set_text, f"Ошибка: {err}")
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def _send_voice(self, *_a: object) -> None:
        if self._busy:
            return
        self._busy = True
        self.mic.set_sensitive(False)
        self.status.set_text("Слушаю…")

        def work() -> None:
            try:
                r = _voice_talk(self._persona)
                if r.get("heard"):
                    GLib.idle_add(self._append_line, "Ты", r["heard"])
                if r.get("reply"):
                    GLib.idle_add(self._append_line, "Юна", r["reply"])
                GLib.idle_add(self.status.set_text, "Готова · скажи «Юна …»" if r.get("ok") else r.get("error", "ошибка"))
            except Exception as e:
                GLib.idle_add(self.status.set_text, str(e))
            finally:
                GLib.idle_add(self.mic.set_sensitive, True)
                self._busy = False

        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    app = Adw.Application(application_id="by.hoshi.yuna")
    win: YunaWindow | None = None

    def on_activate(application: Adw.Application) -> None:
        nonlocal win
        if win is None:
            win = YunaWindow(application)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
