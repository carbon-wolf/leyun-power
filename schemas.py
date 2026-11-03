# -*- coding: utf-8 -*-
"""乐云一卡通水电插件的命令参数 Schema。

用法行不手写 —— 由框架的 `build_usage()` 从 Schema 生成，`/help` 里的占位符与
参数表也据此渲染，手写一定会漂移。

`leyun.config` 是**唯一的设置入口**（不对每个设置项各注册一条子命令，避免把顶层
命令空间塞满）：它只声明一个可变参数 `spec`，具体动作（电/水/开/关/本群/…）
在这里解析成 `(action, value)`，非法时抛 `ValueError`，由命令层转成
`("_schema_error", 原因)` 交给框架回「用法不正确 + 帮助页」。
"""
from __future__ import annotations

import re

from napcat.command import CommandSchema, StrField, VariadicField

#: 时间点（`23:00` / `8:00`，全角冒号也认）
_QUIET_TIME = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")

#: 时段分隔符：半角/全角横线、波浪号，以及中文的「到/至」
_QUIET_SEP = re.compile(r"[-－—–~～]|到|至")


def quiet_minutes(value):
    """`"23:00"` / `"23"` / `"23点"` / `"23点30"` → 分钟数；格式不对返回 None。

    容忍手写习惯：全角冒号、只写小时、中文「点」「时」。
    """
    text = re.sub(r"点\s*(\d{1,2})", r":\1", str(value or "").strip())
    text = text.rstrip("点时").strip()
    match = _QUIET_TIME.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    elif re.match(r"^\d{1,2}$", text):
        hour, minute = int(text), 0      # 只写小时，分钟按 0 算
    else:
        return None
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def quiet_clock(minutes: int) -> str:
    """1380 → `"23:00"`。"""
    return "%02d:%02d" % divmod(int(minutes), 60)


def parse_quiet(text: str) -> tuple:
    """把 `"23:00-08:00"` 解析成 `("23:00", "08:00")`。

    Raises:
        ValueError: 没有分隔符、时间点不合法、或首尾相同（那等于什么都不屏蔽）。
    """
    raw = _QUIET_SEP.sub("-", str(text or "").strip())
    parts = [p.strip() for p in raw.split("-") if p.strip()]
    if len(parts) != 2:
        raise ValueError("时段要写成 起始-结束，例如 23:00-08:00")
    start, end = quiet_minutes(parts[0]), quiet_minutes(parts[1])
    if start is None or end is None:
        raise ValueError("时间点写成 HH:MM，例如 23:00-08:00")
    if start == end:
        raise ValueError("起始和结束一样，等于没屏蔽；要关掉请写 /水电 设置 免打扰 关")
    return quiet_clock(start), quiet_clock(end)


class LoginSchema(CommandSchema):
    """`/leyun login <手机号> <登录密码>` —— 仅私聊，密码不进群。"""

    tel = StrField(required=True, regex=r"^1\d{10}$",
                   description="一卡通绑定的手机号（11 位）")
    password = StrField(required=True, min_length=6, max_length=8,
                        description="一卡通登录密码，6-8 位（不是 6 位支付密码）")


class BindSchema(CommandSchema):
    """`/leyun bind [群号]` —— 把提醒绑到群：群里留空=本群，私聊要带群号。"""

    group = StrField(required=False, default="", regex=r"^-?\d{4,12}$",
                     description="群号（在群里留空即当前群；私聊必须带）")


class VerifySchema(CommandSchema):
    """`/leyun verify [校验码]` —— 新设备首次登录的一次性短信校验。"""

    code = StrField(required=False, default="", regex=r"^\d{4,8}$",
                    description="短信校验码（留空 = 先发一条到你绑定的手机）")


class ConfigSchema(CommandSchema):
    """`/leyun config [子项 ...]` —— 所有设置都从这里进。"""

    spec = VariadicField(
        description="留空=查看当前配置；也可写：电 <元> / 水 <元> / 开 / 关 / "
                    "本群 / 群 <群号> / 私聊 <QQ> / 移群 <群号> / 移私聊 <QQ> / "
                    "间隔 <分钟> / 冷却 <小时> / 免打扰 <23:00-08:00> / "
                    "模板 <文案> / 模板 默认 / 失败模板 <文案> / 预览"
                    "（文案里可写 {@全体成员} / {@QQ号}）")


class ScopeSchema(CommandSchema):
    """`/leyun scope [子项 ...]` —— 服务范围白名单（仅私聊·仅超管）。"""

    spec = VariadicField(
        description="留空=查看名单；也可写：加群 <群号> / 减群 <群号> / "
                    "加人 <QQ> / 减人 <QQ>")


# 服务范围动作别名 → 内部动作名
_SCOPE_ACTIONS = {
    "加群": "add_group", "开群": "add_group", "addgroup": "add_group",
    "减群": "del_group", "移群": "del_group", "delgroup": "del_group",
    "加人": "add_user", "开人": "add_user", "adduser": "add_user",
    "减人": "del_user", "移人": "del_user", "deluser": "del_user",
    "列表": "show", "查看": "show", "show": "show", "list": "show",
}


# 动作别名 → 内部动作名（中文优先，英文只做兼容）
_ACTIONS = {
    "电": "power", "电费": "power", "power": "power",
    "水": "water", "水费": "water", "water": "water",
    "开": "on", "开启": "on", "on": "on",
    "关": "off", "关闭": "off", "off": "off",
    "本群": "here", "here": "here",
    "群": "group", "群号": "group", "group": "group",
    "私聊": "user", "私聊号": "user", "user": "user",
    "移群": "ungroup", "移除群": "ungroup", "ungroup": "ungroup",
    "移私聊": "unuser", "移除私聊": "unuser", "unuser": "unuser",
    "间隔": "interval", "interval": "interval",
    "冷却": "cooldown", "cooldown": "cooldown",
    "免打扰": "quiet", "夜间": "quiet", "静音": "quiet", "quiet": "quiet",
    "模板": "template", "提醒内容": "template", "提醒文案": "template",
    "template": "template",
    "失败模板": "errtemplate", "失败提醒": "errtemplate",
    "errtemplate": "errtemplate", "error_template": "errtemplate",
    "预览": "preview", "模板预览": "preview", "preview": "preview",
    "查看": "show", "状态": "show", "show": "show", "status": "show",
}

# 动作 → (目标名, 是否必须带一个数值/目标参数)
_NEEDS_VALUE = {
    "power": ("电费阈值（元）", True),
    "water": ("水费阈值（元）", True),
    "group": ("群号", True),
    "user": ("QQ 号", True),
    "interval": ("巡检间隔（分钟）", True),
    "cooldown": ("提醒冷却（小时）", True),
    "quiet": ("时段（如 23:00-08:00；写 关 关闭）", False),
    "ungroup": ("群号（可写 本群）", False),
    "unuser": ("QQ 号", True),
    "template": ("文案（留空=查看；写 默认 可恢复）", False),
    "errtemplate": ("文案（留空=查看；写 默认 可恢复）", False),
    "preview": ("", False),
    "on": ("", False),
    "off": ("", False),
    "here": ("", False),
    "show": ("", False),
}


def parse_spec(spec) -> tuple:
    """把可变参数解析成 `(action, value)`。

    Args:
        spec: `VariadicField` 收到的列表（也接受空格分隔的字符串）。

    Returns:
        `(action, value)`；`value` 为字符串，可能为空串。

    Raises:
        ValueError: 动作不认识，或该动作缺少必需的参数。
    """
    words = ([str(x) for x in spec] if isinstance(spec, (list, tuple))
             else str(spec or "").split())
    if not words:
        return "show", ""
    action = _ACTIONS.get(words[0].strip().lower())
    if action is None:
        raise ValueError("看不懂「%s」" % words[0])
    value = " ".join(words[1:]).strip()
    label, required = _NEEDS_VALUE[action]
    if required and not value:
        raise ValueError("「%s」后面要跟%s" % (words[0], label))
    return action, value


def parse_scope_spec(spec) -> tuple:
    """把 `/leyun scope` 的可变参数解析成 `(action, value)`。

    与 `parse_spec` 分开：这里的动作只有「加/减 群或人」，而且只在超管私聊
    里用，不该混进面向普通用户的设置入口。

    Returns:
        `(action, value)`，`action ∈ {show, add_group, del_group, add_user, del_user}`。

    Raises:
        ValueError: 动作不认识，或「加/减」没带号码。
    """
    words = ([str(x) for x in spec] if isinstance(spec, (list, tuple))
             else str(spec or "").split())
    if not words:
        return "show", ""
    action = _SCOPE_ACTIONS.get(words[0].strip().lower())
    if action is None:
        raise ValueError("看不懂「%s」（可用：加群 / 减群 / 加人 / 减人）" % words[0])
    value = " ".join(words[1:]).strip()
    if action != "show" and not value:
        raise ValueError("「%s」后面要跟号码" % words[0])
    return action, value

