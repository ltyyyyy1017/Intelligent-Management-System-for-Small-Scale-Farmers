"""中小农户智能财务管理系统 · 应用层（交付产品）。

分层与依赖方向（docs/技术方案V2.md §4.3）：

    web  →  domain（ledger / privacy / report / snapshot / verify）  →  core（确定性核算）
                                                                   →  store（SQLite）

`core/` 只做纯函数核算，不认识数据库、HTTP、密钥；`app/` 才是有状态的产品。
模型层（OCR/ASR/LLM）不在本包内——识别能力以"可失败的附加项"接入，
未安装时全部功能仍可手工完成，且界面必须显式提示（原则 P2）。
"""

__version__ = "2.0.0"
