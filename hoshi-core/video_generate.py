#!/usr/bin/env python3
"""Генерация коротких видео — слайдшоу и процедурная анимация через ffmpeg."""
from __future__ import annotations

import logging
import math
import random
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from config import MEDIA_DIR

log = logging.getLogger("hoshi.video_generate")

_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
_PROGRESS_CB = Callable[[str, int, int], None]

_DEFAULT_IDEAS = (
    "Юна в цифровом небе ALO — звёзды и световые частицы",
    "Ночной лес в VR — светлячки и туман",
    "Кристаллы в нулевой гравитации — вращение и блики",
)


def _run_ffmpeg(args: list[str], *, timeout: int = 300) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace")[:500]
            log.warning("ffmpeg failed (%s): %s", proc.returncode, err)
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("ffmpeg error: %s", e)
        return False


def _collect_images(source: Path | list[Path]) -> list[Path]:
    if isinstance(source, list):
        return [p for p in source if p.exists() and p.suffix.lower() in _IMAGE_EXTS]
    if source.is_file() and source.suffix.lower() in _IMAGE_EXTS:
        return [source]
    if source.is_dir():
        imgs = sorted(
            p for p in source.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
        )
        return imgs
    return []


def images_to_video(
    images: list[Path],
    out: Path | None = None,
    *,
    fps: int = 24,
    duration: float = 2.5,
) -> Path | None:
    """Собирает mp4 из списка изображений (ken-burns-lite через zoompan)."""
    if not images:
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dst = out or (MEDIA_DIR / f"gen_{uuid.uuid4().hex[:8]}.mp4")
    dst.parent.mkdir(parents=True, exist_ok=True)

    if len(images) == 1:
        frames = int(fps * max(duration, 1.0))
        ok = _run_ffmpeg([
            "-loop", "1",
            "-i", str(images[0]),
            "-vf", (
                f"scale=1280:-2,zoompan=z='min(zoom+0.001,1.08)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s=1280x720:fps={fps}"
            ),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-t", str(max(duration, 1.0)),
            str(dst),
        ])
        return dst if ok and dst.exists() else None

    list_file = MEDIA_DIR / f"gen_list_{uuid.uuid4().hex[:8]}.txt"
    try:
        lines = []
        for img in images:
            safe = str(img.resolve()).replace("'", "'\\''")
            lines.append(f"file '{safe}'")
            lines.append(f"duration {duration}")
        safe_last = str(images[-1].resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe_last}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok = _run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-vf", "scale=1280:-2:flags=lanczos,fps=24",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(dst),
        ])
        return dst if ok and dst.exists() else None
    finally:
        list_file.unlink(missing_ok=True)


def generate_from_images(source: str | Path | list[Path]) -> Path | None:
    """Генерирует mp4 из одного файла, папки или списка путей."""
    if isinstance(source, list):
        paths = _collect_images(source)
    else:
        paths = _collect_images(Path(source))
    if not paths:
        log.warning("generate_from_images: no images in %s", source)
        return None
    return images_to_video(paths)


def generate_video(
    *,
    images: list[str | Path] | None = None,
    image: str | Path | None = None,
) -> Path | None:
    """Точка входа для агента: собрать короткое видео из картинок."""
    if images:
        return generate_from_images([Path(p) for p in images])
    if image:
        return generate_from_images(Path(image))
    return None


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(fp).exists():
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


def _pick_idea(idea: str) -> str:
    clean = (idea or "").strip()
    return clean or random.choice(_DEFAULT_IDEAS)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _draw_alo_sky(draw: ImageDraw.ImageDraw, w: int, h: int, t: float) -> None:
    for y in range(h):
        ratio = y / max(h - 1, 1)
        r = int(_lerp(8, 24, ratio))
        g = int(_lerp(18, 72, ratio))
        b = int(_lerp(48, 140, ratio))
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def _draw_hex_grid(draw: ImageDraw.ImageDraw, w: int, h: int, t: float) -> None:
    horizon = int(h * 0.62)
    for row in range(8):
        z = row / 7
        y = int(horizon + (h - horizon) * z * z)
        alpha = int(40 + 80 * (1 - z))
        color = (80, 200, 255, alpha)
        width = max(1, int(2 + 4 * (1 - z)))
        draw.line([(0, y), (w, y)], fill=color[:3], width=width)
    offset = int((t * 120) % 80)
    for x in range(-80, w + 80, 80):
        x0 = x + offset
        draw.line([(x0, horizon), (x0 + 40, h)], fill=(60, 180, 240), width=1)


def _draw_orb(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int, pulse: float) -> None:
    for i in range(5, 0, -1):
        r = int(radius * (1 + i * 0.12 * pulse))
        alpha = int(30 + 20 * i)
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(120, 220, 255, alpha),
        )
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=(180, 240, 255),
    )
    draw.ellipse(
        [cx - radius // 3, cy - radius // 2, cx + radius // 4, cy + radius // 6],
        fill=(255, 255, 255, 180),
    )


def _is_spicy_idea(idea: str) -> bool:
    low = (idea or "").lower()
    keys = (
        "hent", "хент", "nsfw", "ecchi", "эрот", "ero", "lewd",
        "18+", "порно", "ню", "nude", "sexy", "интим",
    )
    return any(k in low for k in keys)


def _draw_spicy_room(draw: ImageDraw.ImageDraw, w: int, h: int, t: float) -> None:
    pulse = 0.5 + 0.5 * math.sin(t * math.pi * 2)
    for y in range(h):
        ratio = y / max(h - 1, 1)
        r = int(_lerp(28, 12, ratio) + 18 * pulse)
        g = int(_lerp(8, 18, ratio))
        b = int(_lerp(42, 72, ratio) + 10 * pulse)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    bed_y = int(h * 0.72)
    draw.rectangle([0, bed_y, w, h], fill=(180, 120, 150))
    for fold in range(6):
        fy = bed_y + fold * int((h - bed_y) / 6)
        shade = 150 + fold * 8
        draw.line([(0, fy), (w, fy)], fill=(shade, 90 + fold * 5, 120), width=2)


def _draw_anime_girl(
    draw: ImageDraw.ImageDraw,
    w: int,
    h: int,
    t: float,
    seed: str,
    *,
    revealed: bool = False,
) -> None:
    random.seed(seed or "yuna")
    breath = math.sin(t * math.pi * 2) * 6
    cx = int(w * 0.52)
    base_y = int(h * 0.38)
    skin = (255, 218, 198)
    hair = (140, 200, 255)
    blush = (255, 140, 160, 90)

    # волосы
    for i in range(14):
        ang = -2.4 + i * 0.35
        lx = cx + int(math.cos(ang) * (90 + i * 4))
        ly = base_y + int(math.sin(ang) * 30) + 20
        draw.ellipse([lx - 18, ly - 40, lx + 18, ly + 55], fill=hair)
    draw.ellipse([cx - 72, base_y - 30, cx + 72, base_y + 95], fill=hair)

    # торс
    body_top = base_y + 55 + int(breath * 0.3)
    slip = int(8 * math.sin(t * math.pi * 2))
    if not revealed:
        draw.ellipse([cx - 95, body_top, cx + 95, int(h * 0.88)], fill=(230, 200, 215))
    for sx in (cx - 38, cx + 38):
        draw.ellipse([sx - 28, body_top + 18 + slip, sx + 28, body_top + 58 + slip], fill=skin)
        draw.ellipse(
            [sx - 10, body_top + 22 + slip, sx + 6, body_top + 36 + slip],
            fill=(255, 235, 225),
        )
    if revealed:
        torso_bottom = int(h * 0.78)
        draw.ellipse([cx - 55, body_top + 45, cx + 55, torso_bottom], fill=skin)
        leg_y = torso_bottom - 20
        for lx in (cx - 35, cx + 35):
            draw.ellipse([lx - 22, leg_y, lx + 22, int(h * 0.92)], fill=skin)
        nav_y = body_top + 52
        draw.ellipse([cx - 4, nav_y, cx + 4, nav_y + 8], fill=(240, 190, 170))
    else:
        draw.ellipse([cx - 70, body_top + 10, cx + 70, int(h * 0.82)], fill=(245, 225, 235))

    # голова
    head_y = base_y + int(breath * 0.5)
    draw.ellipse([cx - 58, head_y - 10, cx + 58, head_y + 100], fill=skin)
    draw.ellipse([cx - 62, head_y - 30, cx + 62, head_y + 55], fill=hair)

    # лицо
    eye_y = head_y + 38
    for ex in (cx - 22, cx + 22):
        draw.ellipse([ex - 14, eye_y - 6, ex + 14, eye_y + 10], fill=(255, 255, 255))
        pupil = ex + int(3 * math.sin(t * 4))
        draw.ellipse([pupil - 8, eye_y, pupil + 8, eye_y + 14], fill=(200, 60, 100))
        draw.ellipse([pupil - 3, eye_y + 3, pupil + 1, eye_y + 7], fill=(255, 255, 255))
    draw.arc([cx - 12, head_y + 50, cx + 12, head_y + 70], 10, 170, fill=(220, 80, 110), width=3)
    # капли пота
    for sx, sy in ((cx - 50, head_y + 20), (cx + 55, head_y + 30)):
        draw.ellipse([sx - 3, sy, sx + 3, sy + 8], fill=(180, 220, 255))

    # румянец
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for bx in (cx - 38, cx + 38):
        od.ellipse([bx - 16, head_y + 48, bx + 16, head_y + 68], fill=blush)
    return overlay


def _post_process_still(img: Image.Image) -> Image.Image:
    """CPU-доводка: сглаживание, контраст, резкость."""
    out = img.filter(ImageFilter.SMOOTH_MORE)
    out = ImageEnhance.Contrast(out).enhance(1.08)
    out = ImageEnhance.Color(out).enhance(1.12)
    return ImageEnhance.Sharpness(out).enhance(1.35)


def _render_spicy_frame(
    *,
    idea: str,
    frame_idx: int,
    total_frames: int,
    size: tuple[int, int] = (1280, 720),
    revealed: bool = False,
) -> Image.Image:
    w, h = size
    t = frame_idx / max(total_frames - 1, 1)
    img = Image.new("RGBA", size, (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    _draw_spicy_room(draw, w, h, t)

    random.seed((idea or "yuna")[:32])
    for i in range(24):
        hx = random.randint(int(w * 0.1), int(w * 0.9))
        hy = random.randint(int(h * 0.05), int(h * 0.55))
        drift = int(12 * math.sin(t * 6 + i))
        sz = 6 + int(4 * math.sin(t * 3 + i))
        draw.ellipse(
            [hx + drift - sz, hy - sz, hx + drift + sz, hy + sz],
            fill=(255, 100, 140, 80),
        )

    blush_layer = _draw_anime_girl(draw, w, h, t, idea, revealed=revealed)
    if blush_layer:
        img = Image.alpha_composite(img, blush_layer)

    # мягкая виньетка
    vignette = Image.new("RGBA", size, (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)
    vd.rectangle([0, 0, w, h], fill=(40, 0, 30, 35))
    img = Image.alpha_composite(img, vignette)

    font = _load_font(28)
    title = "Юна"
    bbox = draw.textbbox((0, 0), title, font=font)
    tw = bbox[2] - bbox[0]
    draw = ImageDraw.Draw(img)
    draw.text(((w - tw) // 2, int(h * 0.04)), title, font=font, fill=(255, 200, 220))
    return img.convert("RGB")


def _render_creative_frame(
    *,
    idea: str,
    frame_idx: int,
    total_frames: int,
    size: tuple[int, int] = (1280, 720),
) -> Image.Image:
    w, h = size
    t = frame_idx / max(total_frames - 1, 1)
    img = Image.new("RGBA", size, (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    _draw_alo_sky(draw, w, h, t)
    _draw_hex_grid(draw, w, h, t)

    random.seed(idea[:32] or "hoshi")
    stars = [(random.randint(0, w - 1), random.randint(0, int(h * 0.55))) for _ in range(90)]
    for i, (sx, sy) in enumerate(stars):
        twinkle = 0.5 + 0.5 * math.sin(t * math.pi * 4 + i * 0.7)
        sz = 1 + int(twinkle * 2)
        draw.ellipse([sx, sy, sx + sz, sy + sz], fill=(255, 255, 255))

    cx = int(w * (0.35 + 0.3 * math.sin(t * math.pi * 2)))
    cy = int(h * (0.28 + 0.06 * math.cos(t * math.pi * 2)))
    _draw_orb(draw, cx, cy, int(42 + 8 * math.sin(t * math.pi * 2)), 0.5 + 0.5 * math.sin(t * 8))

    for i in range(18):
        ang = t * math.pi * 2 + i * 0.55
        dist = 90 + 40 * math.sin(t * 3 + i)
        px = int(cx + math.cos(ang) * dist)
        py = int(cy + math.sin(ang) * dist * 0.6)
        draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=(100, 220, 255))

    font = _load_font(34)
    title = "Hoshi"
    bbox = draw.textbbox((0, 0), title, font=font)
    tw = bbox[2] - bbox[0]
    tx = (w - tw) // 2
    ty = int(h * 0.08)
    draw.text((tx + 2, ty + 2), title, font=font, fill=(0, 0, 0, 160))
    draw.text((tx, ty), title, font=font, fill=(220, 245, 255))

    sub_font = _load_font(22)
    sub = idea[:64] + ("…" if len(idea) > 64 else "")
    sb = draw.textbbox((0, 0), sub, font=sub_font)
    sw = sb[2] - sb[0]
    draw.text(
        ((w - sw) // 2, int(h * 0.14)),
        sub,
        font=sub_font,
        fill=(160, 220, 255),
    )
    return img.convert("RGB")


def frames_to_gif(
    frames: list[Path],
    out: Path | None = None,
    *,
    fps: int = 10,
    max_width: int = 480,
) -> Path | None:
    """Собирает анимированный gif из PNG-кадров."""
    if not frames:
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dst = out or (MEDIA_DIR / f"gen_{uuid.uuid4().hex[:8]}.gif")
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration_ms = max(50, int(1000 / max(fps, 1)))
    try:
        images: list[Image.Image] = []
        for fp in frames:
            img = Image.open(fp).convert("RGB")
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize(
                    (max_width, max(1, int(img.height * ratio))),
                    Image.Resampling.LANCZOS,
                )
            images.append(img)
        images[0].save(
            dst,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )
        return dst if dst.exists() and dst.stat().st_size > 0 else None
    except OSError as e:
        log.warning("frames_to_gif failed: %s", e)
        return None


def generate_creative_video(
    idea: str = "",
    *,
    fps: int = 12,
    duration: float = 4.0,
    as_gif: bool = False,
    on_progress: _PROGRESS_CB | None = None,
) -> Path | None:
    """Собирает короткий ролик с нуля — идея + процедурные кадры, без чужих фото."""
    concept = _pick_idea(idea)
    spicy = _is_spicy_idea(concept) or _is_spicy_idea(idea)
    render = _render_spicy_frame if spicy else _render_creative_frame
    total_frames = max(12, int(fps * duration))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = MEDIA_DIR / f"gen_frames_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    try:
        if on_progress:
            on_progress("Рисую кадры", 0, total_frames)
        for i in range(total_frames):
            frame = render(idea=concept, frame_idx=i, total_frames=total_frames)
            path = tmp_dir / f"frame_{i:04d}.png"
            frame.save(path, optimize=True)
            frames.append(path)
            if on_progress and (i % 2 == 0 or i == total_frames - 1):
                on_progress("Рисую кадры", i + 1, total_frames)
        if on_progress:
            on_progress("Собираю gif" if as_gif else "Собираю mp4", total_frames, total_frames)
        if as_gif:
            return frames_to_gif(frames, fps=fps)
        return images_to_video(frames, fps=fps, duration=1.0 / fps)
    except OSError as e:
        log.warning("generate_creative_video failed: %s", e)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def generate_spicy_still(
    idea: str = "yuna ecchi",
    *,
    chat_id: int | str | None = None,
    size: tuple[int, int] = (1920, 1080),
    revealed: bool = True,
    refined: bool = True,
    suffix: str = "undress_refined",
) -> Path | None:
    """Один кадр ecchi с нуля + опциональная CPU-доводка."""
    cid = int(chat_id or 0)
    dest = MEDIA_DIR / f"gen_{cid}_{suffix}.jpg"
    try:
        frame = _render_spicy_frame(
            idea=idea,
            frame_idx=6,
            total_frames=12,
            size=size,
            revealed=revealed,
        )
        if refined:
            frame = _post_process_still(frame)
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        frame.save(dest, quality=95, optimize=True)
        log.info("spicy_still ok %s %dx%d", dest.name, size[0], size[1])
        return dest
    except OSError as e:
        log.warning("generate_spicy_still failed: %s", e)
        return None
