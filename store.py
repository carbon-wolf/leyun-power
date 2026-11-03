# -*- coding: utf-8 -*-
"""运行期状态存储（`data/state.json`）。

只存「重启后能重算 / 只是缓存」的东西：登录得到的 `user_id`、上次巡检与提醒的
时间戳、上次错误。用户可改的开关阈值一律走 `config.json`（`self.config`），
两边不重复，避免「设置写在一处、配置写在另一处」的优先级混乱。
"""
from __future__ import annotations

import json
import os


class StateStore:
    """极简 JSON 键值存储：内存里改，`save()` 落盘。"""

    def __init__(self, path: str, logger=None):
        self.path = path
        self.logger = logger
        self.data = self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001 - 坏文件不该让插件起不来
            if self.logger:
                self.logger.warning("[leyun_power] 读取状态失败（按空处理）：%s", e)
            return {}

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            if self.logger:
                self.logger.warning("[leyun_power] 保存状态失败：%s", e)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
