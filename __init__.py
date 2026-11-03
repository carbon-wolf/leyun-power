# -*- coding: utf-8 -*-
"""leyun_power —— 乐云一卡通水电费巡检与余额提醒。

框架按 `PLUGIN` 显式取入口类（不写会按字母序扫第一个 PluginBase 子类，
拆分模块后容易误取基类）。包内一律相对导入，模块名由框架映射，不能 `import main`。
"""
from .plugin import LeyunPowerPlugin

PLUGIN = LeyunPowerPlugin
__all__ = ["LeyunPowerPlugin"]
