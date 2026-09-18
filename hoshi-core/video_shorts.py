#!/usr/bin/env python3
"""Нарезка вертикальных шортов (TikTok / YouTube Shorts) из длинного видео."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import MEDIA_DIR, ROOT

log = logging.getLogger("hoshi.shorts")

SHORTS_W = 1080
SHORTS_H = 1920
DEFAULT_CLIP_SEC = 42.0


@dataclass
class ShortClip:
    start: float
    end: float
    title: str
    label: str = "clip"

    @property
    def duration(self) -> float:
        return max(1.0, self.end - self.start)


def _run(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_vtt(path: Path) -> list[tuple[float, float, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\n+", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 2 or "-->" not in lines[0]:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+\.\d+)\s*-->\s*(\d+):(\d+):(\d+\.\d+)",
            lines[0],
        )
        if not m:
            continue
        h1, m1, s1, h2, m2, s2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + float(s1)
        end = int(h2) * 3600 + int(m2) * 60 + float(s2)
        content = " ".join(lines[1:]).strip()
        content = re.sub(r"<[^>]+>", "", content)
        content = re.sub(r"\[[^\]]*\]", "", content).strip()
        content = re.sub(r"\s+", " ", content)
        if content and not content.startswith("Kind:"):
            cues.append((start, end, content))
    return cues


def _score_cue(start: float, end: float, txt: str) -> float:
    score = len(txt) + (end - start) * 5
    if "!" in txt or "?" in txt:
        score += 20
    keys = (
        "шеф", "повар", "кухн", "блюд", "работ", "вика", "макс",
        "люб", "ноч", "докаж", "собесед", "шут", "идиот", "увол",
    )
    if any(k in txt.lower() for k in keys):
        score += 15
    if len(txt) > 40:
        score += 10
    if txt.lower() in ("[музыка]", "музыка"):
        score -= 100
    return score


def suggest_clips(
    vtt: Path,
    *,
    count: int = 3,
    clip_sec: float = DEFAULT_CLIP_SEC,
    video_duration: float | None = None,
) -> list[ShortClip]:
    cues = parse_vtt(vtt)
    if not cues:
        return []
    scored = sorted(
        ((_score_cue(s, e, t), s, e, t) for s, e, t in cues),
        reverse=True,
    )
    dur = video_duration or cues[-1][1]
    picked: list[tuple[float, float, float, str]] = []
    for score, start, end, txt in scored:
        if score < 80:
            continue
        mid = (start + end) / 2
        if any(abs(mid - (ps + pe) / 2) < 50 for ps, pe, _, _ in picked):
            continue
        picked.append((score, start, end, txt))
        if len(picked) >= count * 4:
            break
    if not picked:
        step = dur / (count + 1)
        return [
            ShortClip(
                start=max(0, step * (i + 1) - clip_sec / 2),
                end=min(dur, step * (i + 1) + clip_sec / 2),
                title=f"Момент {i + 1}",
                label=f"clip_{i + 1}",
            )
            for i in range(count)
        ]
    # spread: pick best from each third
    thirds = [(0, dur / 3), (dur / 3, 2 * dur / 3), (2 * dur / 3, dur)]
    clips: list[ShortClip] = []
    for i, (lo, hi) in enumerate(thirds[:count]):
        candidates = [
            (sc, s, e, t) for sc, s, e, t in picked if lo <= (s + e) / 2 < hi
        ]
        if not candidates:
            candidates = picked
        sc, s, e, txt = max(candidates, key=lambda x: x[0])
        mid = (s + e) / 2
        start = max(0, mid - clip_sec / 2)
        end = min(dur, start + clip_sec)
        start = max(0, end - clip_sec)
        title = txt[:60] + ("…" if len(txt) > 60 else "")
        clips.append(ShortClip(start=start, end=end, title=title, label=f"clip_{i + 1}"))
    return clips


def _sec_to_ass(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _build_ass_subs(vtt: Path, clip: ShortClip, out: Path) -> Path:
    cues = parse_vtt(vtt)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {SHORTS_W}",
        f"PlayResY: {SHORTS_H}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Montserrat,52,&H00FFFFFF,&H000000FF,&H00000000,"
        "&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,40,40,120,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, txt in cues:
        if end <= clip.start or start >= clip.end:
            continue
        rel_s = max(0, start - clip.start)
        rel_e = min(clip.duration, end - clip.start)
        if rel_e <= rel_s:
            continue
        safe = txt.replace("{", "").replace("}", "").replace("\n", " ")
        lines.append(
            f"Dialogue: 0,{_sec_to_ass(rel_s)},{_sec_to_ass(rel_e)},Default,,0,0,0,,{safe}"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _escape_drawtext(text: str) -> str:
    t = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    t = t.replace("%", "\\%")
    return t[:80]


def render_short(
    source: Path,
    clip: ShortClip,
    dest: Path,
    *,
    vtt: Path | None = None,
    brand: str = "Hoshi Kojima",
) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ass_path: Path | None = None
    if vtt and vtt.is_file():
        ass_path = dest.with_suffix(".ass")
        _build_ass_subs(vtt, clip, ass_path)

    title = _escape_drawtext(clip.title)
    brand_esc = _escape_drawtext(brand)
    # Вертикальный кроп 9:16 — split+boxblur на сервере убивает ffmpeg (OOM).
    vf = (
        f"scale={SHORTS_W}:{SHORTS_H}:force_original_aspect_ratio=increase,"
        f"crop={SHORTS_W}:{SHORTS_H},"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{title}':fontcolor=white:fontsize=42:borderw=3:bordercolor=black@0.7:"
        f"x=(w-text_w)/2:y=h*0.08,"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"text='{brand_esc}':fontcolor=white@0.75:fontsize=28:borderw=2:bordercolor=black@0.5:"
        f"x=(w-text_w)/2:y=h*0.94"
    )
    if ass_path:
        vf += f",ass='{ass_path}'"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{clip.start:.3f}",
        "-i", str(source),
        "-t", f"{clip.duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]
    log.info("render short %s %.1f-%.1f -> %s", clip.label, clip.start, clip.end, dest)
    proc = _run(cmd, timeout=900)
    if proc.returncode != 0:
        log.warning("render failed: %s", (proc.stderr or "")[-800:])
        return None
    if ass_path and ass_path.exists():
        ass_path.unlink(missing_ok=True)
    return dest if dest.is_file() and dest.stat().st_size > 0 else None


def make_shorts_from_url(
    url: str,
    *,
    out_dir: Path | None = None,
    clips: list[ShortClip] | None = None,
    count: int = 3,
) -> dict:
    from video_download import download_video

    work = out_dir or (MEDIA_DIR / "shorts" / "job")
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.mp4"
    if not source.is_file():
        hit = download_video(url)
        if not hit:
            return {"ok": False, "error": "download_failed", "dir": str(work)}
        if hit.resolve() != source.resolve():
            source.write_bytes(hit.read_bytes())
    vtt = work / "source.ru.vtt"
    if not vtt.is_file():
        _run([
            str(ROOT / ".venv" / "bin" / "yt-dlp"),
            "--write-auto-sub", "--sub-lang", "ru", "--skip-download",
            "-o", str(work / "source"), url,
        ])
    dur_proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source),
    ])
    try:
        video_dur = float((dur_proc.stdout or "0").strip())
    except ValueError:
        video_dur = 0.0
    plan = clips or suggest_clips(vtt, count=count, video_duration=video_dur or None)
    outputs: list[dict] = []
    for clip in plan:
        out = work / f"{clip.label}.mp4"
        hit = render_short(source, clip, out, vtt=vtt if vtt.is_file() else None)
        outputs.append({
            "label": clip.label,
            "title": clip.title,
            "start": clip.start,
            "end": clip.end,
            "path": str(hit) if hit else None,
        })
    manifest = work / "manifest.json"
    manifest.write_text(
        json.dumps({"url": url, "clips": outputs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ok = sum(1 for o in outputs if o.get("path"))
    return {"ok": ok > 0, "dir": str(work), "clips": outputs, "made": ok}


def try_run_shorts_task(text: str) -> dict | None:
    if not re.search(
        r"(?:нарез|шорт|shorts?|tik\s*tok|тик\s*ток|reels?|рилс)",
        text or "",
        re.I,
    ):
        return None
    m = re.search(r"https?://[^\s<>\"')\]]+", text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,)")
    return {"task": "shorts", "url": url, "text": text}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("usage: video_shorts.py <url>")
        raise SystemExit(1)
    res = make_shorts_from_url(url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
