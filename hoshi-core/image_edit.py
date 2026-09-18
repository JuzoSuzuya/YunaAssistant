#!/usr/bin/env python3
"""Догенерация и правка фото по просьбе владельца (без ad-hoc скриптов агента)."""
from __future__ import annotations

import logging
import math
import random
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from config import MEDIA_DIR

log = logging.getLogger("hoshi.image_edit")

TARGET_8K = (7680, 4320)
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})


@dataclass
class EditRequest:
    kind: str
    drop_count: int = 8
    label: str = "УЛЬТРА"
    target_size: tuple[int, int] = TARGET_8K
    cols: int = 4
    check_amount: int = 2000
    remove_watermark: bool = False


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(fp).exists():
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


_PHOTO_EDIT_RE = re.compile(
    r"(?:"
    r"догенер|"
    r"переделай|"
    r"(?:это|на)\s+фото|"
    r"измени(?:ть)?\s+(?:на\s+)?(?:это\s+)?фото|"
    r"стардроп|"
    r"мою\s+фотк|"
    r"8\s*к\b|"
    r"ультра|"
    r"апскейл|"
    r"outpaint|"
    r"чек|"
    r"вотермарк|"
    r"улучш(?:ить|и)\s+качеств"
    r")",
    re.I,
)

_PHOTO_DELIVERY_RE = re.compile(
    r"(?:"
    r"(?:скинь|отправь|кинь|дай).{0,40}(?:фото|картинк|изображ)|"
    r"(?:фото|картинк).{0,40}(?:скинь|отправь|кинь|сюда)|"
    r"отправь\s+(?:его|это)\s+сюда|"
    r"ничего\s+не\s+отправил"
    r")",
    re.I,
)


def is_photo_edit_request(text: str) -> bool:
    return bool(_PHOTO_EDIT_RE.search(text or ""))


def is_photo_delivery_request(text: str) -> bool:
    """Хозяин просит отправить/доработать фото — не учебная задача."""
    blob = text or ""
    return bool(_PHOTO_DELIVERY_RE.search(blob) or _PHOTO_EDIT_RE.search(blob))


def parse_edit_request(text: str) -> EditRequest:
    """Разбор просьбы: стардропы, 8K, чек, общее улучшение."""
    low = (text or "").lower()
    if re.search(r"чек", low):
        amount = 2000
        m = re.search(r"(\d[\d \u00a0.,]{0,12}\d|\d{1,7})\s*(?:\$|usd|долл)", low)
        if m:
            raw = re.sub(r"[^\d]", "", m.group(1))
            if raw:
                amount = int(raw)
        return EditRequest(kind="check_amount", check_amount=amount)
    ultra = bool(re.search(r"ультра|\bultra\b", low))
    stardrop = bool(re.search(r"стардроп|stardrop", low))
    eight_k = bool(re.search(r"8\s*к\b|\b8k\b|7680", low))

    drop_count = 8
    m = re.search(r"(\d+)\s*(?:ультра|ultra|стардроп|stardrop)", low)
    if m:
        drop_count = max(1, min(16, int(m.group(1))))
    elif re.search(r"4\s*/\s*2|4\s+поделил[аи]\s+на\s+2", low):
        drop_count = 8

    if stardrop or ultra:
        label = "УЛЬТРА" if ultra else "ЛЕГЕНДАРНЫЙ"
        cols = 4 if drop_count >= 4 else max(1, drop_count)
        return EditRequest(
            kind="ultra_stardrops",
            drop_count=drop_count,
            label=label,
            target_size=TARGET_8K,
            cols=cols,
        )
    watermark = bool(re.search(r"вотермарк|watermark|dewater", low))
    if eight_k:
        return EditRequest(
            kind="upscale_8k", target_size=TARGET_8K, remove_watermark=watermark
        )
    return EditRequest(
        kind="enhance", target_size=TARGET_8K, remove_watermark=watermark
    )


def wants_watermark_removal(text: str) -> bool:
    low = (text or "").lower()
    return bool(re.search(r"вотермарк|watermark|dewater", low))


def output_path(chat_id: int | str | None, kind: str) -> Path:
    """Стабильный путь результата — тот же, что уйдёт в [[photo:]]."""
    cid = int(chat_id or 0)
    suffix = {
        "ultra_stardrops": "ultra_8stardrops",
        "upscale_8k": "upscale_8k",
        "enhance": "enhanced",
        "watermark_enhance": "enhanced",
        "check_amount": "check_2000",
    }.get(kind, kind)
    return MEDIA_DIR / f"gen_{cid}_{suffix}.jpg"


def _atomic_save(img: Image.Image, dest: Path, *, quality: int = 95) -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp.jpg")
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    rgb.save(tmp, quality=quality, optimize=True)
    tmp.replace(dest)
    return dest


def _resolve_source(path: str | Path) -> Path | None:
    p = Path(path)
    if p.exists() and p.suffix.lower() in _IMAGE_EXTS:
        return p
    if p.is_dir():
        for hit in sorted(p.iterdir()):
            if hit.suffix.lower() in _IMAGE_EXTS:
                return hit
    return None


def _extract_stardrop_fixed(img: Image.Image) -> Image.Image | None:
    """Кроп под типичный Brawl Stars legendary (2400×1080, 4 в ряд)."""
    w, h = img.size
    x1, y1 = int(w * 0.258), int(h * 0.259)
    x2, y2 = int(w * 0.425), int(h * 0.722)
    drop = img.crop((x1, y1, x2, y2)).convert("RGBA")
    px = drop.load()
    dw, dh = drop.size
    for y in range(dh):
        for x in range(dw):
            r, g, b, _ = px[x, y]
            if r > 180 and g > 140 and b < 120:
                px[x, y] = (r, g, b, 0)
    bbox = drop.getbbox()
    if not bbox or (bbox[2] - bbox[0]) < 20:
        return None
    return drop.crop(bbox)


def _extract_stardrop_scan(img: Image.Image) -> Image.Image | None:
    """Ищет золотистый стардроп в верхней половине кадра."""
    w, h = img.size
    region = img.crop((0, 0, w, int(h * 0.72))).convert("RGBA")
    px = region.load()
    rw, rh = region.size
    mask = Image.new("L", (rw, rh), 0)
    mpx = mask.load()
    for y in range(rh):
        for x in range(rw):
            r, g, b, _ = px[x, y]
            if r > 165 and g > 120 and b < 140 and r > g > b:
                mpx[x, y] = 255
    bbox = mask.getbbox()
    if not bbox:
        return None
    bx1, by1, bx2, by2 = bbox
    pad = max(4, int(min(bx2 - bx1, by2 - by1) * 0.08))
    bx1 = max(0, bx1 - pad)
    by1 = max(0, by1 - pad)
    bx2 = min(rw, bx2 + pad)
    by2 = min(rh, by2 + pad)
    side = max(bx2 - bx1, by2 - by1)
    cx = (bx1 + bx2) // 2
    cy = (by1 + by2) // 2
    bx1 = max(0, cx - side // 2)
    by1 = max(0, cy - side // 2)
    bx2 = min(rw, bx1 + side)
    by2 = min(rh, by1 + side)
    crop = region.crop((bx1, by1, bx2, by2))
    alpha = Image.new("L", crop.size, 0)
    apx = alpha.load()
    cpx = crop.load()
    for y in range(crop.size[1]):
        for x in range(crop.size[0]):
            r, g, b, _ = cpx[x, y]
            if r > 165 and g > 120 and b < 140:
                apx[x, y] = 255
    crop.putalpha(alpha)
    return crop


def extract_stardrop(img: Image.Image) -> Image.Image:
    hit = _extract_stardrop_fixed(img)
    if hit is not None:
        return hit
    hit = _extract_stardrop_scan(img)
    if hit is not None:
        return hit
    w, h = img.size
    side = min(w, h) // 5
    cx, cy = w // 2, int(h * 0.45)
    return img.crop((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)).convert(
        "RGBA"
    )


def ultra_tint(drop: Image.Image, *, ultra: bool = True) -> Image.Image:
    tinted = ImageEnhance.Color(drop).enhance(1.08 if ultra else 1.0)
    tinted = ImageEnhance.Brightness(tinted).enhance(1.05 if ultra else 1.0)
    if ultra:
        glow = Image.new("RGBA", tinted.size, (120, 40, 200, 28))
        return Image.alpha_composite(tinted, glow)
    return tinted


def build_ultra_background(w: int, h: int) -> Image.Image:
    """Радиальный жёлтый burst как в Brawl Stars ultra/legendary."""
    bg = Image.new("RGBA", (w, h), (254, 177, 1, 255))
    draw = ImageDraw.Draw(bg)
    cx, cy = w // 2, int(h * 0.58)

    for i in range(120, 0, -1):
        t = i / 120
        rad = int(max(w, h) * 0.55 * t)
        r = int(255 - 20 * (1 - t))
        g = int(230 - 80 * (1 - t))
        b = int(40 + 30 * (1 - t))
        alpha = int(255 * (0.15 + 0.85 * t))
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(r, g, b, alpha))

    rays = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    for angle in range(0, 360, 10):
        rad = math.radians(angle)
        x2 = cx + int(math.cos(rad) * w * 0.7)
        y2 = cy + int(math.sin(rad) * h * 0.7)
        rd.line([(cx, cy), (x2, y2)], fill=(255, 245, 160, 45), width=max(3, w // 600))
    bg = Image.alpha_composite(bg, rays.filter(ImageFilter.GaussianBlur(12)))

    spark = ImageDraw.Draw(bg)
    random.seed(42)
    for _ in range(int(w * h / 18000)):
        sx = random.randint(0, w - 1)
        sy = random.randint(0, h - 1)
        sz = random.randint(2, 5)
        spark.ellipse([sx, sy, sx + sz, sy + sz], fill=(255, 255, 220, random.randint(80, 200)))
    return bg


def draw_title(canvas: Image.Image, text: str) -> None:
    w, h = canvas.size
    font_size = int(h * 0.09)
    font = _load_font(font_size)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    tx = (w - tw) // 2
    ty = int(h * 0.04) - bbox[1]

    outline = max(4, font_size // 28)
    for dx in range(-outline, outline + 1):
        for dy in range(-outline, outline + 1):
            if dx * dx + dy * dy <= outline * outline:
                draw.text((tx + dx, ty + dy), text, font=font, fill=(0, 0, 0, 255))
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
    canvas.alpha_composite(layer)


def place_stardrops(
    canvas: Image.Image,
    drop: Image.Image,
    count: int,
    *,
    cols: int = 4,
) -> None:
    w, h = canvas.size
    rows = max(1, (count + cols - 1) // cols)
    ref_w = 2400
    scale = (w / ref_w) * 0.58
    sd_w = max(1, int(drop.width * scale))
    sd_h = max(1, int(drop.height * scale))
    sd = drop.resize((sd_w, sd_h), Image.LANCZOS)

    gap_x = sd_w * 0.14
    gap_y = sd_h * 0.22
    row_w = cols * sd_w + (cols - 1) * gap_x
    block_h = rows * sd_h + (rows - 1) * gap_y
    start_x = (w - row_w) / 2
    start_y = int(h * 0.36) + (int(h * 0.22) - block_h) // 2

    for i in range(count):
        row = i // cols
        col = i % cols
        x = int(start_x + col * (sd_w + gap_x))
        arc = int(18 * math.sin(col / max(cols - 1, 1) * math.pi))
        y = int(start_y + row * (sd_h + gap_y) + arc)
        shadow_layer = Image.new("RGBA", sd.size, (0, 0, 0, 0))
        alpha = sd.split()[3]
        shadow_layer.putalpha(alpha.point(lambda a: int(a * 0.35)))
        canvas.paste(shadow_layer, (x + 8, y + 12), shadow_layer)
        canvas.paste(sd, (x, y), sd)


def remove_deviantart_watermark(img: Image.Image) -> Image.Image:
    """Снимает типичный DeviantArt-оверлей (лого + текст) и слегка подтягивает детали."""
    import cv2
    import numpy as np

    bgr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    wm_mask = np.zeros((h, w), dtype=np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    cv2.ellipse(
        wm_mask, (w // 2, int(h * 0.44)), (int(w * 0.13), int(h * 0.15)), 0, 0, 360, 255, -1
    )

    y1, y2 = int(h * 0.50), int(h * 0.68)
    x1, x2 = int(w * 0.08), int(w * 0.92)
    roi_gray = gray[y1:y2, x1:x2]
    roi_hsv = hsv[y1:y2, x1:x2]
    text_detect = (
        (roi_gray > 95) & (roi_hsv[:, :, 1] < 100) & (roi_hsv[:, :, 2] > 80)
    ).astype(np.uint8) * 255
    text_detect = cv2.morphologyEx(text_detect, cv2.MORPH_CLOSE, kernel, iterations=2)
    text_detect = cv2.dilate(text_detect, kernel, iterations=3)
    text_mask = np.zeros((h, w), dtype=np.uint8)
    text_mask[y1:y2, x1:x2] = text_detect
    text_band = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(text_band, (int(w * 0.10), int(h * 0.52)), (int(w * 0.90), int(h * 0.66)), 255, -1)
    text_mask = cv2.bitwise_or(text_mask, text_band)
    wm_mask = cv2.bitwise_or(wm_mask, text_mask)
    wm_mask = cv2.dilate(wm_mask, kernel, iterations=2)

    result = bgr.astype(np.float32)
    logo_mask = np.zeros((h, w), dtype=np.float32)
    cv2.ellipse(
        logo_mask, (w // 2, int(h * 0.44)), (int(w * 0.13), int(h * 0.15)), 0, 0, 360, 1, -1
    )
    for alpha in (0.35, 0.45, 0.55):
        m = logo_mask > 0
        for c in range(3):
            ch = result[:, :, c]
            ch[m] = np.clip((ch[m] - alpha * 255.0) / (1.0 - alpha), 0, 255)
            result[:, :, c] = ch
    ty1, ty2 = int(h * 0.52), int(h * 0.66)
    tx1, tx2 = int(w * 0.10), int(w * 0.90)
    text_gray = gray[ty1:ty2, tx1:tx2].astype(np.float32)
    local_med = cv2.medianBlur(text_gray.astype(np.uint8), 21).astype(np.float32)
    bright = text_gray > local_med + 4
    text_region = result[ty1:ty2, tx1:tx2].copy()
    for alpha in (0.4, 0.5, 0.6):
        for c in range(3):
            ch = text_region[:, :, c]
            ch[bright] = np.clip((ch[bright] - alpha * 255.0) / (1.0 - alpha), 0, 255)
            text_region[:, :, c] = ch
    result[ty1:ty2, tx1:tx2] = text_region
    result = result.astype(np.uint8)

    result = cv2.inpaint(result, wm_mask, inpaintRadius=10, flags=cv2.INPAINT_NS)
    text_only = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(text_only, (int(w * 0.10), int(h * 0.52)), (int(w * 0.90), int(h * 0.66)), 255, -1)
    result = cv2.inpaint(result, text_only, inpaintRadius=8, flags=cv2.INPAINT_TELEA)
    logo_only = np.zeros((h, w), dtype=np.uint8)
    cv2.ellipse(
        logo_only, (w // 2, int(h * 0.44)), (int(w * 0.12), int(h * 0.14)), 0, 0, 360, 255, -1
    )
    result = cv2.inpaint(result, logo_only, inpaintRadius=7, flags=cv2.INPAINT_NS)

    pil = Image.fromarray(cv2.cvtColor(result, cv2.COLOR_BGR2RGB))
    ow, oh = pil.size
    pil = pil.resize((ow * 2, oh * 2), Image.LANCZOS)
    pil = ImageEnhance.Contrast(pil).enhance(1.05)
    pil = ImageEnhance.Color(pil).enhance(1.03)
    pil = pil.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=2))
    return pil


def upscale_image(img: Image.Image, target: tuple[int, int]) -> Image.Image:
    tw, th = target
    if img.size == (tw, th):
        return img
    up = img.resize((tw, th), Image.LANCZOS)
    return ImageEnhance.Sharpness(up).enhance(1.15)


def edit_ultra_stardrops(
    source: Path,
    *,
    request: EditRequest,
    dest: Path,
) -> Path | None:
    try:
        src = Image.open(source).convert("RGBA")
    except OSError as e:
        log.warning("ultra_stardrops open failed %s: %s", source, e)
        return None

    drop = ultra_tint(extract_stardrop(src), ultra=request.label == "УЛЬТРА")
    tw, th = request.target_size
    canvas = build_ultra_background(tw, th)
    draw_title(canvas, request.label)
    place_stardrops(canvas, drop, request.drop_count, cols=request.cols)
    _atomic_save(canvas, dest)
    log.info("ultra_stardrops ok %s -> %s (%dx%d)", source.name, dest.name, tw, th)
    return dest


def _fmt_usdt(amount: int) -> str:
    return f"{amount:,}.00 USDT".replace(",", "\u00a0")


def _fmt_usd(amount: int) -> str:
    return f"${amount:,}".replace(",", "\u00a0")


def edit_check_amount(
    source: Path,
    *,
    request: EditRequest,
    dest: Path,
) -> Path | None:
    """Меняет сумму на скриншоте Crypto/@send чека (типичный 1080×2400 TG)."""
    try:
        img = Image.open(source).convert("RGB")
    except OSError as e:
        log.warning("check_amount open failed %s: %s", source, e)
        return None

    amount = max(1, int(request.check_amount))
    w, h = img.size
    sx, sy = w / 1080.0, h / 2400.0

    def sc(x: int, y: int) -> tuple[int, int]:
        return int(x * sx), int(y * sy)

    def box(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        a, b = sc(x1, y1)
        c, d = sc(x2, y2)
        return a, b, c, d

    def fsize(base: int) -> int:
        return max(12, int(base * sx))

    def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        return _load_font(fsize(size))

    def paste_patch(dst: tuple[int, int, int, int], src: tuple[int, int, int, int]) -> None:
        x1, y1, x2, y2 = dst
        patch = img.crop(src).resize((x2 - x1, y2 - y1), Image.LANCZOS)
        img.paste(patch, (x1, y1))

    card_bg = box(180, 560, 900, 680)
    usdt = f"{amount}.001112 USDT"
    usd = f"(${amount:,})".replace(",", "\u00a0")
    draw = ImageDraw.Draw(img)

    # Крупная сумма на карточке
    b = box(160, 712, 920, 812)
    paste_patch(b, card_bg)
    big = f"$ {amount}"
    f_big = font(96 if amount >= 1000 else 108)
    cx = (b[0] + b[2]) // 2
    cy = (b[1] + b[3]) // 2
    draw.text((cx, cy), big, fill=(255, 255, 255), font=f_big, anchor="mm")

    # USDT под суммой (справа от иконки)
    b = box(350, 862, 870, 932)
    paste_patch(b, card_bg)
    draw.text(sc(360, 898), usdt, fill=(255, 255, 255), font=font(48), anchor="lm")

    # Подпись — только цифры, «Чек на» и иконка остаются
    draw.rectangle(box(370, 1102, 765, 1146), fill=(32, 33, 35))
    draw.text(sc(375, 1124), f"{usdt} {usd}.", fill=(255, 255, 255), font=font(33), anchor="lm")

    # Кнопка «Получить …»
    paste_patch(box(170, 1284, 990, 1334), box(170, 1270, 350, 1295))
    draw.text(sc(175, 1309), f"Получить {usdt}", fill=(255, 255, 255), font=font(35), anchor="lm")

    # Превью в реплае (мелкий бейдж)
    paste_patch(box(135, 1440, 208, 1472), box(135, 1395, 208, 1427))
    badge = f"${amount // 1000}K" if amount >= 1000 else f"${amount}"
    draw.text(
        ((box(135, 1440, 208, 1472)[0] + box(135, 1440, 208, 1472)[2]) // 2, sc(0, 1456)[1]),
        badge,
        fill=(255, 255, 255),
        font=font(18),
        anchor="mm",
    )

    _atomic_save(img, dest)
    log.info("check_amount ok %s -> %s amount=%s", source.name, dest.name, amount)
    return dest


def edit_upscale(source: Path, *, request: EditRequest, dest: Path) -> Path | None:
    try:
        src = Image.open(source).convert("RGB")
    except OSError as e:
        log.warning("upscale open failed %s: %s", source, e)
        return None
    if request.remove_watermark:
        src = remove_deviantart_watermark(src)
        mid = upscale_image(src, (src.width * 2, src.height * 2))
        src = remove_deviantart_watermark(mid)
    out = upscale_image(src, request.target_size)
    _atomic_save(out, dest)
    log.info("upscale ok %s -> %s %s", source.name, dest.name, out.size)
    return dest


def run_photo_edit(
    source_path: str | Path,
    request_text: str,
    *,
    chat_id: int | str | None = None,
) -> Path | None:
    """Генерирует результат по исходнику и тексту просьбы."""
    src = _resolve_source(source_path)
    if not src:
        log.warning("photo_edit: source missing %s", source_path)
        return None

    req = parse_edit_request(request_text)
    if wants_watermark_removal(request_text):
        req.remove_watermark = True
    dest = output_path(chat_id, req.kind)

    if req.kind == "ultra_stardrops":
        return edit_ultra_stardrops(src, request=req, dest=dest)
    if req.kind == "check_amount":
        return edit_check_amount(src, request=req, dest=dest)
    return edit_upscale(src, request=req, dest=dest)


def describe_result(req: EditRequest) -> str:
    tw, th = req.target_size
    if req.kind == "ultra_stardrops":
        return (
            f"**{req.drop_count} {req.label}** стардропов, надпись **{req.label}**, "
            f"фон без чёрных углов, **8K** ({tw}×{th})"
        )
    if req.kind == "check_amount":
        return f"чек на **{_fmt_usd(req.check_amount)}**"
    return f"**8K** ({tw}×{th}), апскейл с исходника"


def build_reply(out_path: Path, request_text: str) -> str:
    req = parse_edit_request(request_text)
    summary = describe_result(req)
    return f"Готово — {summary}.\n\n[[photo:{out_path.resolve()}]]"
