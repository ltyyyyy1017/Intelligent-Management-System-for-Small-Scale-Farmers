"""国密算法（纯标准库）：SM3 摘要、SM4 分组密码、SM2 椭圆曲线签名。

对应 docs/技术方案V2.md §4.4、§8.4：
- 摘要用 SM3（GB/T 32905）；
- 对称加密用 SM4-GCM（AEAD，同时保证机密性与完整性）；
- 签名用 SM2（GB/T 32918），配合设备标识形成双要素。

本包只依赖 Python 标准库，便于在无网络、无管理员权限的村委电脑上随包运行。
"""
from __future__ import annotations

from .sm3 import SM3, sm3, sm3_hex

__all__ = ["SM3", "sm3", "sm3_hex"]
