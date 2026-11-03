# -*- coding: utf-8 -*-
"""测试引导：定位 napcat（ncqqpy）与插件包，不把本机绝对路径写进仓库。

解析顺序：
1. 已经能 `import napcat`（pip 装过，或本来就在框架目录里跑）；
2. 环境变量 `NCQQPY_HOME` 指向 ncqqpy 仓库根目录；
3. 同级布局 `../../ncqqpy`（框架仓库与插件仓库放在同一个父目录时）。
"""
from __future__ import annotations

import os
import sys

# 本文件在 <repo>/tests/ 下
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PARENT = os.path.dirname(REPO_ROOT)      # `plugins/`，导入 leyun_power 用


def ensure_napcat() -> None:
    """把 ncqqpy 加进 sys.path（已经可导入则什么都不做）。"""
    try:
        import napcat  # noqa: F401
    except ImportError:
        pass
    else:
        return

    candidates = [
        os.environ.get("NCQQPY_HOME"),
        os.path.join(os.path.dirname(os.path.dirname(PLUGIN_PARENT)), "ncqqpy"),
    ]
    for path in candidates:
        if path and os.path.isdir(os.path.join(path, "napcat")):
            sys.path.insert(0, path)
            return
    raise SystemExit(
        "找不到 napcat：先装好 ncqqpy 框架，或设环境变量 "
        "NCQQPY_HOME=/path/to/ncqqpy")


def ensure_plugin() -> None:
    """把插件所在的目录（`plugins/`）加进 sys.path，好 `import leyun_power`。"""
    if PLUGIN_PARENT not in sys.path:
        sys.path.insert(0, PLUGIN_PARENT)


def prepare() -> None:
    ensure_napcat()
    ensure_plugin()
