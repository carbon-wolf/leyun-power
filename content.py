# -*- coding: utf-8 -*-
"""提醒文案模板：默认模板 + 占位符渲染。

文案不写死在代码里 —— `config.remind_template`（余额不足）与
`config.error_template`（查询失败）可改，`{房间}`、`{余额}` 这类占位符在推送时
替换成实时值。改法和查看都在 `/leyun config 模板 ...` 一个入口里。

设计上的两个宽容：
- 全角花括号 `｛ ｝` 自动按半角处理（手机输入法容易打全角）
- 支持 `\\n` 转义成换行，免得在 QQ 里没法输入多行
"""
from __future__ import annotations

import re
import time

from .api import PTYPE_POWER, PTYPE_WATER

#: 余额不足提醒的默认文案（与 v2.0 的推送内容一致）
DEFAULT_REMIND = ("🔔 水电余额不足\n\n"
                  "{房间} — {项目}\n"
                  "剩余 {剩余} {单位} · 余额 {余额} 元 · 单价 {单价} 元/{单位}\n"
                  "⚠️ 已低于提醒阈值 {阈值} 元，记得充值")

#: 查询失败提醒的默认文案
DEFAULT_ERROR = ("⚠️ 水电查询失败\n"
                 "{提示}\n"
                 "{错误}")

#: 余额提醒可用占位符 → 说明（顺序即帮助里的展示顺序）
REMIND_VARS = {
    "房间": "房间名（config.rooms[].label）",
    "类型": "电费 / 水费",
    "项目": "平台返回的缴费项目名",
    "剩余": "剩余量，两位小数",
    "单位": "度 / 吨",
    "余额": "剩余金额（元）",
    "单价": "单价（元/度 或 元/吨）",
    "阈值": "当前提醒阈值（元）",
    "时间": "提醒时间（月-日 时:分）",
}

#: 失败提醒可用占位符
ERROR_VARS = {
    "错误": "失败原因，多项用「；」连接",
    "提示": "下一步该怎么做（自动生成）",
    "时间": "提醒时间（月-日 时:分）",
}

# 占位符最长 20 字，避免把整段 JSON 当变量名吞掉
_PLACEHOLDER = re.compile(r"\{([^{}]{0,20})\}")

#: @ 占位符：`{@全体成员}` / `{@全体}` / `{@all}` → @全体；`{@123456}` / `{at:123456}` → @某人。
#: 只认 5-12 位纯数字，避免把 `{@某人}` 这种写成错误 QQ 号的写法当成真人。
_AT = re.compile(
    r"\{\s*(?:@\s*(?:全体成员|全体|all)|(?:at|@)\s*[:：]\s*(\d{5,12})"
    r"|@\s*(\d{5,12}))\s*\}",
    re.IGNORECASE)

#: @ 用法说明（模板帮助里跟着变量表一起回显）
AT_HELP = ("@ 用法（只对群消息生效，私聊推送会自动去掉；@全体需要机器人是群管理员）：\n"
           "· {@全体成员} —— @全体成员（也可写 {@全体} / {@all}）\n"
           "· {@123456789} —— @某个 QQ（也可写 {at:123456789}）")

MAX_TEMPLATE_LEN = 500


def normalize(template: str) -> str:
    """统一全角花括号、把字面量 `\\n` 还原成换行。"""
    text = str(template or "")
    text = text.replace("｛", "{").replace("｝", "}")
    return text.replace("\\n", "\n")


def render(template: str, values: dict, fallback: str = "") -> str:
    """渲染模板；空模板 / 渲染后为空则回落到 `fallback`。

    未知占位符原样保留（不丢信息，也方便用户看出哪儿打错了）。
    """
    text = normalize(template).strip()
    if not text:
        text = normalize(fallback).strip()
    if not text:
        return ""

    def _sub(match: re.Match) -> str:
        key = match.group(1).strip()
        return str(values[key]) if key in values else match.group(0)

    return _PLACEHOLDER.sub(_sub, text).strip()


def unknown_vars(template: str, allowed: dict) -> list:
    """找出模板里不认识的占位符（用于保存时校验，防拼错变量名）。

    `{@全体成员}` / `{@123456}` 这类 @ 占位符是合法的，先剔掉再校验。
    """
    text = _AT.sub("", normalize(template))
    found = {m.group(1).strip() for m in _PLACEHOLDER.finditer(text)}
    return sorted(v for v in found if v and v not in allowed)


def var_help(allowed: dict) -> str:
    """把可用占位符排成一行一行，供设置命令回显。"""
    return "\n".join("· {%s} — %s" % (k, v) for k, v in allowed.items())


# ---------------------------------------------------------------------- @

def has_at(text: str) -> bool:
    """模板里有没有 @ 占位符（决定私聊要不要剥、群发失败要不要降级重发）。"""
    return bool(_AT.search(str(text or "")))


def segments(text: str) -> list:
    """把渲染后的文本切成 `[("text", 文本) | ("at", qq)]`。

    `qq` 为 `"all"` 表示 @全体成员。给 `MessageChain` 拼 at 段用 —— @ 是消息段，
    塞在纯文本里只会原样显示成「{@全体成员}」。
    """
    text = str(text or "")
    parts, pos = [], 0
    for match in _AT.finditer(text):
        if match.start() > pos:
            parts.append(("text", text[pos:match.start()]))
        parts.append(("at", match.group(1) or match.group(2) or "all"))
        pos = match.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return [p for p in parts if p[1]] or [("text", "")]


def plain_text(text: str) -> str:
    """把 @ 占位符换成可读文本（预览 / 命令回执用，这些地方发不出真 at 段）。"""
    def _sub(match: re.Match) -> str:
        qq = match.group(1) or match.group(2)
        return "@全体成员" if not qq else "@%s" % qq

    return _AT.sub(_sub, str(text or ""))


def strip_at(text: str) -> str:
    """去掉 @ 占位符（私聊推送用：私聊里 at 段没有意义，部分实现还会报错）。"""
    # 先标记成占位符：单独占一行的 @ 连同后面的换行一起删，免得留下一行空白
    cleaned = _AT.sub("\x00", str(text or ""))
    cleaned = re.sub(r"[ \t]*\x00[ \t]*\n", "", cleaned)
    cleaned = cleaned.replace("\x00", "")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------- 取值

def remind_values(room: dict, bill: dict, label: str, threshold: float,
                  now: float = None) -> dict:
    """余额提醒的占位符取值。"""
    ptype = int(room.get("pay_proj_type") or 0)
    kind = {PTYPE_POWER: "电费", PTYPE_WATER: "水费"}.get(ptype, "水电")
    unit = {PTYPE_POWER: "度", PTYPE_WATER: "吨"}.get(ptype, "")
    return {
        "房间": label,
        "类型": kind,
        "项目": str(bill.get("name") or ""),
        "剩余": "%.2f" % float(bill.get("surplus_qty") or 0),
        "单位": unit,
        "余额": "%.2f" % float(bill.get("surplus_amt") or 0),
        "单价": "%.2f" % float(bill.get("unit_price") or 0),
        "阈值": "%.2f" % float(threshold or 0),
        "时间": time.strftime("%m-%d %H:%M",
                             time.localtime(now if now is not None else time.time())),
    }


def error_values(errors: list, tip: str = "", now: float = None) -> dict:
    """查询失败提醒的占位符取值。"""
    return {
        "错误": "；".join(str(e) for e in errors) or "未知错误",
        "提示": str(tip or ""),
        "时间": time.strftime("%m-%d %H:%M",
                             time.localtime(now if now is not None else time.time())),
    }


def sample_bill(ptype: int = PTYPE_POWER) -> dict:
    """预览用的示例账单（没绑账号时也能看模板效果）。"""
    return {"name": "宿舍电费" if ptype == PTYPE_POWER else "宿舍水费",
            "surplus_qty": 3.5 if ptype == PTYPE_POWER else 1.2,
            "surplus_amt": 1.86 if ptype == PTYPE_POWER else 3.24,
            "unit_price": 0.53 if ptype == PTYPE_POWER else 2.70}
