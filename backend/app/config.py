"""运行配置：端口、跨域、运行环境。

配置来源为环境变量（容器/流水线注入），未设置时使用本地默认值，保证克隆即可启动。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_name: str = "数仓分层治理平台"
    env: str = os.getenv("APP_ENV", "local")
    port: int = int(os.getenv("APP_PORT", "8000"))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 血缘链路版本表迁移开关：
    # LINEAGE_DUAL_WRITE 开启后血缘写入同时落旧视图与版本表（默认开启，兼容旧查询）；
    # 关闭时回退为仅写旧视图，可由后续迁移批次补版本表。
    # LINEAGE_MIGRATE_POLICY 决定流水线迁移的默认冲突策略。
    lineage_dual_write: bool = _bool_env("LINEAGE_DUAL_WRITE", True)
    lineage_migrate_policy: str = os.getenv("LINEAGE_MIGRATE_POLICY", "resume")


settings = Settings()
