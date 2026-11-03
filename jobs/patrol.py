# -*- coding: utf-8 -*-
"""巡检：按 `check_interval` 查一遍水电，低于阈值推送到配置的群 / 私聊。

定时器本身是固定 60 秒醒一次（`plugin.PATROL_TICK`），是否真巡检由配置决定 ——
这样 `/水电 设置 间隔 30` 改完立刻生效，不用重挂定时器。
"""
from __future__ import annotations

import time

from ..commands.query import room_label


async def tick(plugin) -> None:
    """定时器回调：到点才巡检，其余时间直接返回。"""
    if not plugin.config.get("enabled", True):
        return
    interval = max(60, plugin._int_cfg("check_interval", 1800, minimum=60))
    now = time.time()
    last = float(plugin.state.get("last_patrol", 0) or 0)
    if now - last < interval:
        return
    # 先记时间再查：查挂了也不会每分钟疯狂重试
    plugin.state.set("last_patrol", now)
    plugin.state.save()
    try:
        plugin.logger.info("[leyun_power] %s", await check_once(plugin))
    except Exception as e:  # noqa: BLE001 - 定时任务里不能抛
        plugin.logger.error("[leyun_power] 巡检异常：%s", e)


async def check_once(plugin) -> str:
    """执行一次巡检：推送余额不足 / 查询异常，返回一句摘要。"""
    results = await plugin.query_all()
    now = time.time()
    alerts, errors = [], []
    for room, bill, err in results:
        if bill is None:
            errors.append("%s：%s" % (room_label(room), err))
            continue
        threshold = plugin.threshold(int(room.get("pay_proj_type") or 0))
        if threshold > 0 and bill["surplus_amt"] <= threshold:
            alerts.append((_alert_key(room), room, bill))

    # 免打扰时段照常查（余额是新的），但一条都不推 —— 也**不记冷却**，
    # 于是白天第一轮巡检会立刻把夜里攒下的提醒发出去。
    quiet = plugin.in_quiet_hours(now)

    pushed = 0
    if alerts and plugin.config.get("enabled", True) and not quiet:
        for key, room, bill in alerts:
            if not _cooled_down(plugin, key, now):
                continue
            sent = await plugin.push(plugin.remind_text(room, bill))
            pushed += sent
            # ⚠️ 只有真发出去才记冷却。以前不管发没发都记，于是「推送群还没配」
            # 那一轮把时间戳写进去，之后 6 小时一律跳过 —— 用户看到的就是
            # 「余额明明低于阈值，却一直不提醒」。
            if sent:
                plugin.state.set("last_remind", {**(_remind_map(plugin)), key: now})
            else:
                plugin.logger.warning(
                    "[leyun_power] %s 低于阈值但没发出去：没有可用的推送目标"
                    "（/水电 绑定 或 /水电 设置 私聊 <QQ>）",
                    room.get("label") or room.get("input4", {}).get("name") or "房间")

    if errors:
        plugin.state.set("last_err", time.strftime("%m-%d %H:%M") + " " + "；".join(errors))
        if not quiet and _cooled_down(plugin, "__error__", now):
            # 失败原因决定提示：需要短信校验就直说去哪做，别让用户猜密码是不是错了
            sent = await plugin.push(
                plugin.error_text(errors, plugin.verify_hint(errors)))
            pushed += sent
            if sent:
                plugin.state.set("last_remind", {**(_remind_map(plugin)),
                                                 "__error__": now})
    else:
        plugin.state.set("last_err", "")

    plugin.state.save()

    summary = "巡检完成：%d 项成功" % (len(results) - len(errors))
    if alerts:
        summary += "，%d 项低于阈值，推送 %d 条" % (len(alerts), pushed)
        if quiet:
            summary += "（免打扰 %s 中，攒到 %s 之后发）" % (
                plugin.quiet_text(), plugin.quiet_until())
        elif not pushed and not any(plugin.push_targets()):
            summary += "（⚠️ 没有推送目标，提醒发不出去）"
    if errors:
        summary += "，%d 项失败" % len(errors)
    return summary


def _alert_key(room: dict) -> str:
    """同房间同项目的提醒去重键。"""
    ptype = int(room.get("pay_proj_type") or 0)
    room_id = str((room.get("input4") or {}).get("id") or room.get("label") or "")
    return "%s@%s" % (ptype, room_id)


def _remind_map(plugin) -> dict:
    value = plugin.state.get("last_remind", {}) or {}
    return dict(value) if isinstance(value, dict) else {}


def _cooled_down(plugin, key: str, now: float) -> bool:
    """距上次提醒是否已过冷却时间。"""
    try:
        hours = float(plugin.config.get("remind_cooldown_hours", 6) or 0)
    except (TypeError, ValueError):
        hours = 6.0
    last = float(_remind_map(plugin).get(key, 0) or 0)
    return now - last >= hours * 3600
