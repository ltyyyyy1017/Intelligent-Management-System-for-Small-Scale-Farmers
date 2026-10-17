"""应用配置：数据目录、监听地址、路径约定。

一条原则：**所有可变数据都在 `data/` 下**，程序目录只读。这样村委电脑上
"复制整个目录 + 双击启动"即可用，卸载就是删目录（含加密库，需口令导出）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Config", "DEFAULT_PORT"]

DEFAULT_PORT = 8765


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT

    # 运行期派生属性用 property，保证 data_dir 变了路径也跟着变
    @property
    def db_path(self) -> Path:
        return self.data_dir / "village.db"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def key_dir(self) -> Path:
        return self.data_dir / "keys"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.evidence_dir,
            self.export_dir,
            self.key_dir,
            self.backup_dir,
            self.upload_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, root: Path | str | None = None) -> "Config":
        """按「显式参数 > 环境变量 > 当前目录/data」的顺序解析数据目录。"""
        if root is None:
            root = os.environ.get("NFM_DATA_DIR") or Path.cwd() / "data"
        path = Path(root).expanduser().resolve()
        host = os.environ.get("NFM_HOST", "127.0.0.1")
        port = int(os.environ.get("NFM_PORT", DEFAULT_PORT))
        return cls(data_dir=path, host=host, port=port)
