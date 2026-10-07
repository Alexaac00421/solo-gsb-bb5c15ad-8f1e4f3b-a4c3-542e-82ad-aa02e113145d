"""服务配置：全部通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    # 数据根目录：SQLite 库、原图、发布成品都存放在此
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "./data"))
    # 上传大小上限（字节），由 MAX_UPLOAD_MB 换算
    max_upload_bytes: int = field(
        default_factory=lambda: _int_env("MAX_UPLOAD_MB", 25) * 1024 * 1024
    )
    # 归档核验请求体大小上限（字节），由 MAX_VERIFY_MB 换算。
    # 核验只读取上传的 ZIP（放在系统临时目录、核验后即删），绝不解压到
    # DATA_DIR，也不写入数据库；该上限用于约束内存/临时盘占用。
    max_verify_bytes: int = field(
        default_factory=lambda: _int_env("MAX_VERIFY_MB", 512) * 1024 * 1024
    )
    # 归档恢复请求体大小上限（字节），由 MAX_RESTORE_MB 换算。
    # 恢复同样先把上传 ZIP 落到系统临时目录（核验通过后才写入 DATA_DIR），
    # 该上限用于约束内存/临时盘占用。
    max_restore_bytes: int = field(
        default_factory=lambda: _int_env("MAX_RESTORE_MB", 512) * 1024 * 1024
    )
    # 定时公开前内审预览凭证的**本地管理员密钥**（环境变量 PREVIEW_ADMIN_KEY）。
    # 未设置或为纯空白时，凭证颁发端点一律拒绝（fail-closed）；领取端点只需
    # 持一次性凭证、不使用本密钥。密钥不写入数据库，也不进入服务数据归档。
    preview_admin_key: str | None = field(
        default_factory=lambda: (os.environ.get("PREVIEW_ADMIN_KEY") or "").strip()
        or None
    )

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "app.db")

    @property
    def originals_dir(self) -> str:
        return os.path.join(self.data_dir, "originals")

    @property
    def publications_dir(self) -> str:
        return os.path.join(self.data_dir, "publications")
