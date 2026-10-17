"""确定性核算内核（参考实现）。

本包不依赖任何第三方库，只演示三件事：
1. 数字可复算：`costing.compute` 是纯函数，同输入必同输出（§6.6）
2. 账本可证明：`snapshot` 的事件集哈希、Merkle 根、重放校验（§5.4、§8.5）
3. 日志可验伪：`audit` 的哈希链（§8.4）

它不是产品：OCR、ASR、UI、数据库、签章都不在这里。
"""
from . import audit, costing, models, money, reports, snapshot

__all__ = ["money", "models", "costing", "reports", "snapshot", "audit"]
__version__ = "2.0.0"
