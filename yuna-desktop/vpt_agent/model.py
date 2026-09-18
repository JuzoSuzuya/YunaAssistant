"""
Обёртка над OpenAI VPT (Video-Pre-Training) foundation-model-1x.

Настоящая нейросеть (IMPALA CNN + transformer, ~0.5B параметров), обученная
на миллионах часов записей игры людей в Minecraft. Берёт кадр (RGB, HWC) —
выдаёт действия (кнопки + поворот камеры в градусах). Никаких хардкод-макросов.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vpt_lib"))
sys.path.insert(0, str(ROOT / "minerl_stub"))

import numpy as np  # noqa: E402
import torch as th  # noqa: E402

from agent import MineRLAgent, TARGET_ACTION_SPACE  # noqa: E402
from fake_env import FakeMineRLEnv  # noqa: E402

MODEL_PATH = ROOT / "models" / "foundation-model-1x.model"
WEIGHTS_PATH = ROOT / "models" / "foundation-model-1x.weights"


class VPTMotor:
    """Живая нейросеть-«моторика»: кадр → действия. Держит скрытое состояние
    (LSTM/transformer-память) между вызовами — важно не пересоздавать объект
    каждый тик, иначе агент «забывает» контекст последних секунд."""

    def __init__(self, *, device: str | None = None) -> None:
        if device is None:
            device = "cuda" if th.cuda.is_available() else "cpu"
        self.device = device
        env = FakeMineRLEnv(TARGET_ACTION_SPACE)

        agent_parameters = pickle.load(open(MODEL_PATH, "rb"))
        policy_kwargs = agent_parameters["model"]["args"]["net"]["args"]
        pi_head_kwargs = agent_parameters["model"]["args"]["pi_head_opts"]
        pi_head_kwargs["temperature"] = float(pi_head_kwargs["temperature"])

        self.agent = MineRLAgent(
            env, device=device, policy_kwargs=policy_kwargs, pi_head_kwargs=pi_head_kwargs
        )
        self.agent.load_weights(str(WEIGHTS_PATH))

    def reset(self) -> None:
        """Сбросить память агента (например, после смерти/респавна/загрузки мира)."""
        self.agent.reset()

    def act(self, frame_rgb: np.ndarray) -> dict[str, Any]:
        """frame_rgb: HWC uint8, RGB порядок каналов (не BGR!).
        Возвращает dict как в agent.TARGET_ACTION_SPACE: кнопки 0/1 + camera=[pitch,yaw] в градусах.
        """
        obs = {"pov": frame_rgb}
        with th.no_grad():
            action = self.agent.get_action(obs)
        out: dict[str, Any] = {}
        for k, v in action.items():
            if k == "camera":
                arr = np.asarray(v).reshape(-1)
                out["camera"] = [float(arr[0]), float(arr[1])]
            else:
                out[k] = int(np.asarray(v).reshape(-1)[0])
        return out


_singleton: VPTMotor | None = None


def get_motor() -> VPTMotor:
    global _singleton
    if _singleton is None:
        _singleton = VPTMotor()
    return _singleton


if __name__ == "__main__":
    import time

    print("Загружаю VPT foundation-model-1x ...")
    t0 = time.time()
    m = get_motor()
    print(f"Загрузилась за {time.time() - t0:.1f}s, device={m.device}")
    frame = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)
    t0 = time.time()
    for _ in range(20):
        act = m.act(frame)
    dt = time.time() - t0
    print(f"20 тиков за {dt:.2f}s -> {20 / dt:.1f} FPS")
    print("пример действия:", act)
