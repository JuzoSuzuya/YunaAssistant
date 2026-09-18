#!/usr/bin/env python3
"""RVC voice conversion: русский TTS → аниме-голос (Haruka)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# PyTorch 2.6+ defaults weights_only=True — hubert/rvc checkpoints need False.
import torch

_orig_torch_load = torch.load


def _torch_load_compat(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _orig_torch_load(*args, **kwargs)


torch.load = _torch_load_compat  # type: ignore[method-assign]

from rvc_python.infer import RVCInference  # noqa: E402

ROOT = Path(__file__).resolve().parent

_MODELS = {
    "yuna": (
        ROOT / "models" / "yuna" / "yuna.pth",
        ROOT / "models" / "yuna" / "yuna.index",
    ),
    "hikari": (
        ROOT / "models" / "hikari" / "VT-TTS_Hikari.pth",
        ROOT / "models" / "hikari" / "added_IVF1344_Flat_nprobe_1_VT-TTS_Hikari_v2.index",
    ),
    "haruka": (
        ROOT / "models" / "haruka" / "VT-TTS_Haruka.pth",
        ROOT / "models" / "haruka" / "added_IVF1327_Flat_nprobe_1_VT-TTS_Haruka_v2.index",
    ),
}


def _default_model_paths() -> tuple[Path, Path]:
    import os

    name = os.environ.get("YUNA_RVC_MODEL", "yuna").strip().lower()
    pth, idx = _MODELS.get(name, _MODELS["yuna"])
    if not pth.exists():
        for fb in ("yuna", "hikari", "haruka"):
            if _MODELS[fb][0].exists():
                pth, idx = _MODELS[fb]
                break
    return pth, idx


DEFAULT_MODEL, DEFAULT_INDEX = _default_model_paths()

_engine: RVCInference | None = None
_engine_key: tuple[str, str, str] | None = None


def _get_engine(
    *,
    model: Path,
    index: Path | None,
    device: str,
    version: str,
    f0_method: str,
    pitch: int,
    index_rate: float,
) -> RVCInference:
    global _engine, _engine_key
    idx = str(index) if index and index.exists() else ""
    key = (str(model), idx, device)
    if _engine is not None and _engine_key == key:
        _engine.f0method = f0_method
        _engine.f0up_key = pitch
        _engine.index_rate = index_rate
        _engine.resample_sr = 48000
        _engine.protect = 0.52
        _engine.rms_mix_rate = 0.70
        _engine.filter_radius = 5
        return _engine

    rvc = RVCInference(device=device, version=version)
    rvc.f0method = f0_method
    rvc.f0up_key = pitch
    rvc.index_rate = index_rate
    rvc.resample_sr = 48000
    rvc.protect = 0.52
    rvc.rms_mix_rate = 0.70
    rvc.filter_radius = 5
    rvc.load_model(str(model), version=version, index_path=idx)
    _engine = rvc
    _engine_key = key
    return rvc


def _to_wav(src: Path, dst: Path, *, sr: int = 40000) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ar", str(sr), "-ac", "1", str(dst)],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:500]}")


def convert_file(
    input_path: Path,
    output_path: Path,
    *,
    model: Path = DEFAULT_MODEL,
    index: Path | None = DEFAULT_INDEX,
    device: str = "cuda:0",
    pitch: int = 2,
    index_rate: float = 0.75,
    f0_method: str = "rmvpe",
    version: str = "v2",
) -> Path:
    if not model.exists():
        raise FileNotFoundError(f"RVC model not found: {model}")

    wav_in = input_path
    tmp_wav: Path | None = None
    if input_path.suffix.lower() != ".wav":
        tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
        _to_wav(input_path, tmp_wav)
        wav_in = tmp_wav

    try:
        rvc = _get_engine(
            model=model,
            index=index,
            device=device,
            version=version,
            f0_method=f0_method,
            pitch=pitch,
            index_rate=index_rate,
        )
        rvc.infer_file(str(wav_in), str(output_path))
    finally:
        if tmp_wav is not None:
            tmp_wav.unlink(missing_ok=True)

    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError("RVC produced empty output")
    return output_path


def main() -> int:
    p = argparse.ArgumentParser(description="Convert speech to anime voice via RVC")
    p.add_argument("-i", "--input", required=True, type=Path)
    p.add_argument("-o", "--output", required=True, type=Path)
    p.add_argument("-m", "--model", type=Path, default=DEFAULT_MODEL)
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    p.add_argument("-d", "--device", default=os.environ.get("YUNA_RVC_DEVICE", "cuda:0"))
    p.add_argument("-p", "--pitch", type=int, default=int(os.environ.get("YUNA_RVC_PITCH", "2")))
    p.add_argument("-r", "--index-rate", type=float, default=float(os.environ.get("YUNA_RVC_INDEX_RATE", "0.75")))
    p.add_argument("--f0", default=os.environ.get("YUNA_RVC_F0", "rmvpe"))
    args = p.parse_args()

    try:
        convert_file(
            args.input,
            args.output,
            model=args.model,
            index=args.index,
            device=args.device,
            pitch=args.pitch,
            index_rate=args.index_rate,
            f0_method=args.f0,
        )
        print(args.output)
        return 0
    except Exception as e:
        print(f"rvc_convert error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
