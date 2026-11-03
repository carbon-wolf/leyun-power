# -*- coding: utf-8 -*-
"""`/leyun query` 的逻辑与排版（巡检复用同一套排版，消息长得一致）。"""
from __future__ import annotations


def room_label(room: dict) -> str:
    """房间显示名：优先 label，其次房间名。"""
    return str(room.get("label")
               or (room.get("input4") or {}).get("name")
               or "房间")


def format_room(plugin, room: dict, bill: dict, *, mark_low: bool = True) -> str:
    """把一条房间账单排成两三行文本。"""
    ptype = int(room.get("pay_proj_type") or 0)
    threshold = plugin.threshold(ptype)
    low = mark_low and threshold > 0 and bill["surplus_amt"] <= threshold
    lines = [
        "%s — %s" % (room_label(room), bill["name"]),
        "剩余 %.2f · 余额 %.2f 元 · 单价 %.2f 元"
        % (bill["surplus_qty"], bill["surplus_amt"], bill["unit_price"]),
    ]
    if low:
        lines.append("⚠️ 已低于提醒阈值 %.2f 元，记得充值" % threshold)
    return "\n".join(lines)


def format_all(plugin, results: list) -> str:
    """把 `query_all()` 的结果整体排版。"""
    if not results:
        return ("还没有配置监控房间。\n"
                "编辑 config.json → plugins.leyun_power.rooms，按下面格式加（电费/水费各一条）：\n"
                '{\n'
                '  "label": "1号楼203 电费",\n'
                '  "pay_proj_type": 323,\n'
                '  "input1": {"id": "校区ID", "name": "校区名"},\n'
                '  "input2": {"id": "楼栋ID", "name": "楼栋名"},\n'
                '  "input3": {"id": "楼层ID", "name": "楼层名"},\n'
                '  "input4": {"id": "房间ID", "name": "房间名"}\n'
                '}\n'
                "pay_proj_type：323=电费，322=水费。房间 ID 从抓包的 "
                "YKTGetPayProj 请求里抄（详见 README）。\n"
                "改完 /reload leyun_power 生效。")
    parts, ok = [], 0
    for room, bill, err in results:
        if bill is not None:
            parts.append(format_room(plugin, room, bill))
            ok += 1
        else:
            parts.append("%s — ❌ %s" % (room_label(room), err))
    text = "\n\n".join(parts) + "\n\n共 %d/%d 项查询成功" % (ok, len(results))
    hint = remind_hint(plugin, results)
    return text + ("\n\n" + hint if hint else "")


def remind_hint(plugin, results: list) -> str:
    """「低于阈值了但没收到提醒」的现场诊断。

    用户对这件事只有两个判断依据：余额数字、有没有收到消息。中间隔着四道闸门
    （绑定 / 开关 / 推送目标 / 冷却），任何一道关着都表现为「什么都不发」。
    与其让人去翻日志，不如在查询回执里直接告诉他卡在哪一道。
    """
    low = []
    for room, bill, err in results:
        if bill is None:
            continue
        threshold = plugin.threshold(int(room.get("pay_proj_type") or 0))
        if threshold > 0 and bill["surplus_amt"] <= threshold:
            low.append(room_label(room))
    if not low:
        return ""

    blocked = []
    tel, pwd = plugin.account()
    if not tel or not pwd:
        blocked.append("· 还没登录账号（私聊 /水电 登录 <手机号> <密码>）")
    if not plugin.config.get("enabled", True):
        blocked.append("· 自动巡检是关的（发 /水电 设置 开）")
    groups, users = plugin.push_targets()
    if not groups and not users:
        blocked.append("· 没有推送目标（在群里发 /水电 绑定，"
                       "或 /水电 设置 私聊 <你的QQ>）")
    if blocked:
        return ("⚠️ %s 已低于阈值，但现在不会提醒：\n%s"
                % ("、".join(low), "\n".join(blocked)))

    targets = []
    if groups:
        targets.append("群 " + "、".join(groups))
    if users:
        targets.append("QQ " + "、".join(users))
    interval = plugin._int_cfg("check_interval", 1800, minimum=60) // 60
    return ("· 提醒正常：每 %d 分钟巡检一次，余额仍低于阈值就推到 %s\n"
            "  想立刻试一次效果：/水电 设置 预览"
            % (interval, " / ".join(targets)))


async def run(plugin) -> str:
    """`/leyun query` 的入口。"""
    tel, _ = plugin.account()
    if not tel:
        return ("还没登录账号。\n"
                "私聊机器人发：/水电 登录 <手机号> <登录密码>\n"
                "（也可以直接编辑 config.json → plugins.leyun_power.account）")
    try:
        results = await plugin.query_all()
    except Exception as e:  # noqa: BLE001 - 命令里不抛异常给框架
        return "❌ 查询失败：%s" % e
    return format_all(plugin, results)
