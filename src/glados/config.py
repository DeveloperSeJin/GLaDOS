"""설정 읽기.

사용자 데이터 폴더(설정·기록)는 GLADOS_HOME 환경 변수, 없으면 ~/.glados.
처음 실행할 때 없는 설정 파일만 기본값으로 만든다 (있는 파일은 덮어쓰지 않음).
모든 텍스트 파일은 utf-8로 읽고 쓴다.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

import yaml

# 사용자 폴더 파일 이름 -> 패키지 안의 기본 파일 이름
DEFAULT_FILES: dict[str, str] = {
    "glados.toml": "glados.toml",
    "folders.toml": "folders.toml",
    "servers.toml": "servers.toml",
    "policy.yaml": "policy.user.yaml",
}
SUBDIRS = ("logs", "checkpoints")


class ConfigError(Exception):
    """설정 파일을 읽을 수 없거나 값이 잘못됨."""


def glados_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("GLADOS_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".glados"


def default_text(name: str) -> str:
    return (resources.files("glados") / "defaults" / name).read_text(encoding="utf-8")


def ensure_home(home: Path) -> list[str]:
    """폴더와 없는 기본 설정 파일을 만든다. 새로 만든 파일 이름 목록을 돌려준다."""
    home.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (home / sub).mkdir(exist_ok=True)
    created: list[str] = []
    for target_name, default_name in DEFAULT_FILES.items():
        target = home / target_name
        if not target.exists():
            target.write_text(default_text(default_name), encoding="utf-8")
            created.append(target_name)
    return created


@dataclass
class ModelConfig:
    host: str = "http://localhost:11434"
    name: str = "qwen3.5:9b"
    num_ctx: int = 32768
    think: bool = False
    keep_alive: str = "30m"
    mode: str = "normal"

    @property
    def is_low(self) -> bool:
        return self.mode == "low"


@dataclass
class LoopLimits:
    max_steps: int = 12
    format_retries: int = 2
    same_call_limit: int = 3
    tool_error_limit: int = 2
    local_timeout_s: int = 60
    remote_timeout_s: int = 300


@dataclass
class Server:
    alias: str
    gpus: int = 0
    workdir: str = ""
    tmux_prefix: str = ""


@dataclass
class Config:
    home: Path
    model: ModelConfig = field(default_factory=ModelConfig)
    limits: LoopLimits = field(default_factory=LoopLimits)
    user_name: str = ""
    search_default: list[str] = field(default_factory=list)
    extra_protected: list[str] = field(default_factory=list)
    servers: list[Server] = field(default_factory=list)
    # (층 이름, 규칙) 목록. 기본 → 연구원님 순서. 마지막에 맞는 규칙이 이긴다
    policy_layers: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}을(를) 읽을 수 없습니다: {e}") from e


def parse_policy_text(text: str, source: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{source}을(를) 읽을 수 없습니다: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}의 최상위는 '도구: {{패턴: 판정}}' 형태여야 합니다")
    return data


def load_config(home: Path) -> Config:
    main = _read_toml(home / "glados.toml")
    folders = _read_toml(home / "folders.toml")
    servers = _read_toml(home / "servers.toml")

    model_sec = main.get("model", {})
    mode = str(model_sec.get("mode", "normal"))
    if mode not in ("normal", "low"):
        raise ConfigError(f'glados.toml의 model.mode는 "normal" 또는 "low"여야 합니다 (지금: {mode})')
    picked = model_sec.get(mode, {})
    model = ModelConfig(
        host=str(model_sec.get("host", ModelConfig.host)).rstrip("/"),
        name=str(picked.get("name", "qwen3.5:4b" if mode == "low" else ModelConfig.name)),
        num_ctx=int(picked.get("num_ctx", 16384 if mode == "low" else ModelConfig.num_ctx)),
        think=bool(model_sec.get("think", False)),
        keep_alive=str(model_sec.get("keep_alive", "30m")),
        mode=mode,
    )

    loop_sec = main.get("loop", {})
    limits = LoopLimits(**{k: int(v) for k, v in loop_sec.items() if k in LoopLimits.__dataclass_fields__})

    server_list = [
        Server(
            alias=str(s.get("alias", "")),
            gpus=int(s.get("gpus", 0)),
            workdir=str(s.get("workdir", "")),
            tmux_prefix=str(s.get("tmux_prefix", "")),
        )
        for s in servers.get("server", [])
        if str(s.get("alias", "")).strip()
    ]

    layers: list[tuple[str, dict[str, Any]]] = [
        ("기본", parse_policy_text(default_text("policy.yaml"), "기본 policy.yaml")),
    ]
    user_policy = home / "policy.yaml"
    if user_policy.exists():
        layers.append(("연구원님", parse_policy_text(user_policy.read_text(encoding="utf-8"), "policy.yaml")))

    return Config(
        home=home,
        model=model,
        limits=limits,
        user_name=str(main.get("user", {}).get("name", "")),
        search_default=[str(p) for p in folders.get("search_default", [])],
        extra_protected=[str(p) for p in folders.get("extra_protected", [])],
        servers=server_list,
        policy_layers=layers,
    )
