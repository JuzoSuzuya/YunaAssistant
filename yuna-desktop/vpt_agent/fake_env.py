"""
Лёгкая подделка под MineRL env — только чтобы agent.MineRLAgent.__init__()
прошёл validate_env() и получил правильный action_space. Настоящий MineRL
(с Malmo/Java) НЕ нужен: inference идёт на кадрах с реального окна Minecraft,
которое хозяин запускает сам, а не через MineRL-обёртку.
"""
from __future__ import annotations

from gym import spaces

# Должно совпадать 1:1 с agent.ENV_KWARGS (кроме frameskip, его validate_env не проверяет)
_TASK_ATTRS = dict(
    fov_range=[70, 70],
    gamma_range=[2, 2],
    guiscale_range=[1, 1],
    resolution=[640, 360],
    cursor_size_range=[16.0, 16.0],
)


class _FakeTask:
    def __init__(self) -> None:
        for k, v in _TASK_ATTRS.items():
            setattr(self, k, v)


class FakeMineRLEnv:
    """Достаточно для agent.validate_env(): .task с нужными полями + .action_space."""

    def __init__(self, target_action_space: dict) -> None:
        self.task = _FakeTask()
        self.action_space = spaces.Dict(target_action_space)
