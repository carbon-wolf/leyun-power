# -*- coding: utf-8 -*-
"""`/leyun bind` / `/leyun config` / `/leyun scope` 的逻辑。

`config` 是**唯一的设置入口**：`电 / 水 / 开 / 关 / 本群 / 群 / 私聊 / 移群 /
移私聊 / 间隔 / 冷却 / 模板 / 失败模板 / 预览` 全部在这里分流，不为它们各注册
一条子命令。提醒文案也不写死在代码里，见 `content.py`。

`scope` 管的是**另一层**：谁能用这个插件（服务范围白名单，口径见
`napcat.scope`）。它跟 `push_groups` / `push_users`（推给谁）不是一回事，
管理命令只对超管开放。
"""
from __future__ import annotations

from napcat.scope import parse_ids

from .. import content
from ..api import (PTYPE_POWER, PTYPE_WATER, LeyunAuthError, LeyunClient,
                   LeyunProtocolError, LeyunVerifyRequired)
from ..schemas import parse_scope_spec, parse_spec
from .query import format_all, room_label
from .verify import mask_tel

_LABEL = {PTYPE_POWER: "电费", PTYPE_WATER: "水费"}

# 动作 → (配置键, 默认文案, 可用变量, 显示名, 命令里怎么写)
_TEMPLATES = {
    "template": ("remind_template", content.DEFAULT_REMIND,
                 content.REMIND_VARS, "余额不足提醒", "模板"),
    "errtemplate": ("error_template", content.DEFAULT_ERROR,
                    content.ERROR_VARS, "查询失败提醒", "失败模板"),
}

# 写到「重置 / 查看」时要认的词
_RESET_WORDS = ("默认", "default", "重置", "reset")
_SHOW_WORDS = ("", "查看", "show", "状态")


def panel(plugin) -> str:
    """当前配置一览（`/水电 设置` 的默认输出）。"""
    tel, _ = plugin.account()
    groups, users = plugin.push_targets()
    enabled = bool(plugin.config.get("enabled", True))
    interval = plugin._int_cfg("check_interval", 1800, minimum=60) // 60
    cooldown = plugin.config.get("remind_cooldown_hours", 6)
    if tel and len(tel) >= 7:
        account = "%s****%s" % (tel[:3], tel[-4:])
    else:
        account = "未绑定（私聊发 /水电 绑定 <手机号> <密码>）"
    lines = [
        "⚙️ 水电插件设置",
        "账号：%s" % account,
        "自动巡检：%s（每 %d 分钟）" % ("开" if enabled else "关", interval),
        "提醒阈值：电费 %.2f 元 · 水费 %.2f 元"
        % (plugin.threshold(PTYPE_POWER), plugin.threshold(PTYPE_WATER)),
        "提醒冷却：%s 小时" % cooldown,
        "推送群：%s" % (", ".join(groups) or "未设置（在群里发 /水电 设置 本群）"),
        "推送私聊：%s" % (", ".join(users) or "未设置"),
    ]
    owners = plugin.owner_users()
    if owners:
        lines.append("管理员 QQ：%s" % ", ".join(owners))
    # 文案首行：@ 占位符先换成可读文本 —— 面板是「回执」，真 at 段发出去会变成 @全体
    template_head = content.plain_text(
        plugin.template("remind_template")).splitlines()
    lines.append("提醒文案：%s%s"
                 % (template_head[0] if template_head else "",
                    " …" if len(template_head) > 1 else ""))
    lines += [
        "",
        "改法示例：/水电 设置 电 10 · /水电 设置 水 5 · /水电 设置 开 · "
        "/水电 设置 本群 · /水电 设置 间隔 30",
        "自定义提醒：/水电 设置 模板 <文案>（发 /水电 设置 模板 看可用变量，"
        "/水电 设置 预览 看效果）",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- 绑定

async def bind(plugin, params, event) -> str:
    """校验账号 → 真登录一次 → 落盘 → 立刻返回当前余额。"""
    if not plugin.is_owner(event):
        return "这个插件已被其他人绑定（config.json → plugins.leyun_power.owner_users）"

    tel = str(getattr(params, "tel", "") or "").strip()
    pwd = str(getattr(params, "password", "") or "").strip()

    sess = dict(plugin.config.get("session") or {})
    client = LeyunClient(sess, base=str(plugin.config.get("base_url") or ""),
                         timeout=plugin._int_cfg("http_timeout", 15, minimum=5))
    try:
        user_id = await client.login(tel, pwd)
    except LeyunVerifyRequired:
        # 9006 说明密码本身是对的，只差一次设备校验：账号先落盘，别让用户重输
        plugin.update_config("account", {"tel": tel, "password": pwd})
        plugin.claim_owner(event)
        return ("⚠️ 账号已保存，但这是台「新设备」，登录需要一次短信校验。\n\n"
                "1) 发 /水电 验证 —— 校验码会发到 %s\n"
                "2) 收到后发 /水电 验证 <6位校验码>\n\n"
                "校验一次以后就免验证了。" % mask_tel(tel))
    except (LeyunAuthError, LeyunProtocolError) as e:
        return ("❌ 绑定失败：%s\n\n"
                "提示：密码是一卡通「手机号登录」那个 6-8 位密码，"
                "不是 6 位支付密码；没设过就在 App 里点「重置登录密码」。"
                % e)

    plugin.update_config("account", {"tel": tel, "password": pwd})
    plugin.claim_owner(event)
    plugin.state.set("user_id", user_id)
    plugin.state.save()

    try:
        text = format_all(plugin, await plugin.query_all())
    except Exception as e:  # noqa: BLE001 - 登录成功就算绑定成功
        text = "（登录成功，但查询失败：%s）" % e

    tips = []
    groups, users = plugin.push_targets()
    if not groups:
        tips.append("· 还没设提醒群：把机器人拉进宿舍群，在群里发 /水电 设置 本群")
    if not users:
        tips.append("· 想私聊也收提醒：/水电 设置 私聊 <你的QQ>")
    return ("✅ 绑定成功，账号已保存\n\n" + text
            + ("\n\n" + "\n".join(tips) if tips else ""))


# ---------------------------------------------------------------------- 设置

async def config(plugin, params, event) -> str:
    """统一设置入口。"""
    if not plugin.is_owner(event):
        return "只有管理员能改设置（在 config.json 的 owner_users 里加上你的 QQ）"
    try:
        action, value = parse_spec(getattr(params, "spec", None))
    except ValueError as e:
        return ("_schema_error", str(e))

    if action == "show":
        return panel(plugin)

    if action in ("on", "off"):
        plugin.update_config("enabled", action == "on")
        head = "✅ 自动巡检已开启" if action == "on" else "🔕 自动巡检已关闭"
        return "%s\n\n%s" % (head, panel(plugin))

    if action in ("power", "water"):
        ptype = PTYPE_POWER if action == "power" else PTYPE_WATER
        num = _number(value, minimum=0)
        if num is None:
            return ("_schema_error", "阈值要填非负数字，例如 /水电 设置 电 10")
        thresholds = dict(plugin.config.get("threshold") or {})
        thresholds[str(ptype)] = num
        plugin.update_config("threshold", thresholds)
        return ("✅ %s提醒阈值已设为 %.2f 元（低于此值会提醒）"
                % (_LABEL[ptype], num))

    if action == "here":
        group_id = str(getattr(event, "group_id", "") or "")
        if getattr(event, "message_type", "") != "group" or not group_id:
            return ("_schema_error", "「本群」要在群里发；私聊请用 群 <群号>")
        return _add_group(plugin, group_id)

    if action == "group":
        return _add_group(plugin, _target_id(value))

    if action == "user":
        target = _target_id(value)
        if target.startswith("-"):
            return ("_schema_error", "QQ 号填错了")
        users = _unique(plugin.push_targets()[1] + [target])
        plugin.update_config("push_users", users)
        return "✅ 已把 QQ %s 加入私聊提醒" % target

    if action == "ungroup":
        target = (str(getattr(event, "group_id", "") or "")
                  if value.strip() in ("本群", "here", "")
                  else _target_id(value))
        groups = [g for g in plugin.push_targets()[0] if g != target]
        plugin.update_config("push_groups", groups)
        return "✅ 已把群 %s 移出提醒列表" % target

    if action == "unuser":
        target = _target_id(value)
        users = [u for u in plugin.push_targets()[1] if u != target]
        plugin.update_config("push_users", users)
        return "✅ 已把 QQ %s 移出私聊提醒" % target

    if action == "interval":
        minutes = _number(value, minimum=1, maximum=1440, integer=True)
        if minutes is None:
            return ("_schema_error", "巡检间隔要填 1-1440 之间的整数分钟")
        plugin.update_config("check_interval", int(minutes) * 60)
        return "✅ 巡检间隔已设为 %d 分钟" % int(minutes)

    if action == "cooldown":
        hours = _number(value, minimum=0, maximum=720)
        if hours is None:
            return ("_schema_error", "提醒冷却要填 0-720 之间的小时数")
        plugin.update_config("remind_cooldown_hours", hours)
        return "✅ 同一项提醒的冷却已设为 %s 小时" % _trim(hours)

    if action in _TEMPLATES:
        return _set_template(plugin, action, value)

    if action == "preview":
        return await _preview(plugin)

    return ("_schema_error", "看不懂「%s」" % action)


# ---------------------------------------------------------------------- 文案

def _set_template(plugin, action: str, value: str) -> str:
    """查看 / 恢复默认 / 修改提醒文案（三类动作都在同一条命令里分流）。"""
    key, default, allowed, title, word = _TEMPLATES[action]
    raw = str(value or "").strip()
    head = "📝 %s文案" % title

    if raw in _SHOW_WORDS:
        return ("%s（当前）\n%s\n\n可用变量：\n%s\n\n%s\n\n"
                "改法：/水电 设置 %s <文案>（换行写 \\n）\n"
                "恢复默认：/水电 设置 %s 默认 · 看效果：/水电 设置 预览"
                % (head, content.plain_text(plugin.template(key)),
                   content.var_help(allowed), content.AT_HELP, word, word))

    if raw.lower() in _RESET_WORDS:
        plugin.update_config(key, default)
        return ("✅ %s已恢复默认\n\n%s\n\n—— 效果 ——\n%s"
                % (head, content.plain_text(default), _example(plugin, action)))

    text = content.normalize(raw)
    if len(text) > content.MAX_TEMPLATE_LEN:
        return ("_schema_error", "文案太长了（上限 %d 字，现在 %d 字）"
                % (content.MAX_TEMPLATE_LEN, len(text)))
    bad = content.unknown_vars(text, allowed)
    if bad:
        return ("_schema_error", "不认识的变量：%s\n可用的有：%s"
                % ("、".join("{%s}" % b for b in bad),
                   "、".join("{%s}" % k for k in allowed)))

    plugin.update_config(key, text)
    tip = ""
    if content.has_at(text):
        tip = ("\n\n@ 已生效：群提醒会真的 @ 到人，私聊提醒自动去掉 @。"
               "若机器人不是群管理员，@全体成员 会失败并自动改发纯文本。")
    return ("✅ %s已保存（下次推送生效）\n\n—— 效果 ——\n%s%s\n\n"
            "改法提示：/水电 设置 %s · 恢复默认加「默认」"
            % (head, _example(plugin, action), tip, word))


def _example(plugin, action: str) -> str:
    """用示例数据渲染一条，让用户立刻看到效果（不联网）。

    @ 占位符在这里只是展示，换成「@全体成员」这类可读文本（命令回执发不出真 at 段）。
    """
    if action == "errtemplate":
        return content.plain_text(
            plugin.error_text(["示例：查询超时"], "示例提示（真实内容自动生成）"))
    room = {"label": "1号楼203", "pay_proj_type": PTYPE_POWER}
    values = content.remind_values(room, content.sample_bill(PTYPE_POWER),
                                   room_label(room),
                                   plugin.threshold(PTYPE_POWER))
    return content.plain_text(
        content.render(plugin.template("remind_template"), values,
                       content.DEFAULT_REMIND))


async def _preview(plugin) -> str:
    """用真实余额（未绑定则用示例数据）渲染两条提醒，确认模板效果。"""
    room, bill, note = None, None, ""
    tel, _ = plugin.account()
    if tel:
        try:
            for r, b, _err in await plugin.query_all():
                if b is not None:
                    room, bill = r, b
                    break
        except Exception:  # noqa: BLE001 - 预览失败不该把命令打挂
            room = bill = None
    if bill is None:
        room = {"label": "1号楼203", "pay_proj_type": PTYPE_POWER}
        bill = content.sample_bill(PTYPE_POWER)
        note = "（未取到实时数据，用示例数据）"
    else:
        note = "（用当前实时余额）"
    return ("📝 提醒预览 %s\n\n%s\n\n—— 查询失败时 ——\n%s\n\n"
            "%s"
            % (note, content.plain_text(plugin.remind_text(room, bill)),
               content.plain_text(plugin.error_text(["示例：查询超时"], "示例提示")),
               content.AT_HELP))


# ---------------------------------------------------------------------- 服务范围

def _binding(plugin) -> dict:
    """把 `binding` 规范成 `{"groups": [int…], "users": [int…]}`（去重升序）。

    解析口径直接用框架的 `parse_ids`（只收正整数，`true` / 0 / 负数一律丢弃），
    不在这里另写一份。
    """
    raw = plugin.config.get("binding")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "groups": sorted(parse_ids(raw.get("groups"))),
        "users": sorted(parse_ids(raw.get("users"))),
    }


def _scope_panel(binding: dict) -> str:
    """名单一览（查看动作与每次增删后的回执共用）。"""
    groups = "、".join(str(g) for g in binding["groups"]) or "（空 = 群聊都用不了）"
    users = "、".join(str(u) for u in binding["users"]) or "（空 = 直接私聊都用不了）"
    return "\n".join([
        "🎛️ 服务范围（谁能用）",
        "群：%s" % groups,
        "人：%s" % users,
        "",
        "口径：群聊 / 群小窗按来源群判，直接私聊按 QQ 判；两者互不代替；"
        "超管恒可用（防「名单配错谁都改不了」）。",
        "注意：这是「谁能用」，与「推给谁」（push_groups / push_users）是两层。",
        "维护：/水电 范围 加群 <群号> · 减群 <群号> · 加人 <QQ> · 减人 <QQ>",
    ])


async def scope(plugin, params, event) -> str:
    """`/leyun scope`：服务范围白名单的查看与增删（仅私聊·仅超管）。"""
    try:
        action, value = parse_scope_spec(getattr(params, "spec", None))
    except ValueError as e:
        return ("_schema_error", str(e))

    binding = _binding(plugin)
    if action == "show":
        return _scope_panel(binding)

    kind = "groups" if action.endswith("group") else "users"
    number = _positive_id(value)
    if number is None:
        return ("_schema_error",
                "群号 / QQ 号要填正整数，例如 /水电 范围 加群 100200300")

    if action.startswith("add"):
        if number in binding[kind]:
            head = "👌 %s 已经在名单里了" % number
        else:
            binding[kind] = sorted(binding[kind] + [number])
            if not plugin.update_config("binding", binding):
                return ("_schema_error", "写配置失败，名单没改动（看日志）")
            head = ("✅ 已开通%s %s —— 现在可以%s用 /水电 了"
                    % ("群" if kind == "groups" else "QQ", number,
                       "在该群（含群成员小窗）" if kind == "groups" else "私聊"))
        return "%s\n\n%s" % (head, _scope_panel(binding))

    # 删除
    if number not in binding[kind]:
        head = "🤔 %s 本来就不在名单里" % number
    else:
        binding[kind] = [n for n in binding[kind] if n != number]
        if not plugin.update_config("binding", binding):
            return ("_schema_error", "写配置失败，名单没改动（看日志）")
        head = ("✅ 已关闭%s %s 的使用权限"
                % ("群" if kind == "groups" else "QQ", number))
    tip = ""
    if not binding["groups"] and not binding["users"]:
        tip = "\n\n⚠️ 名单已经空了 —— 除超管外谁都用不了，别把自己也关在外面。"
    return "%s%s\n\n%s" % (head, tip, _scope_panel(binding))


def _positive_id(value: str):
    """严格取正整数（管理命令不接受 `@123` / 带字母的写法，避免加错号）。"""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


# ---------------------------------------------------------------------- 工具

def _number(value: str, *, minimum: float = 0.0, maximum: float = None,
            integer: bool = False):
    """把字符串转成数字并做范围校验；不合法返回 None。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if integer and num != int(num):
        return None
    if num < minimum or (maximum is not None and num > maximum):
        return None
    return int(num) if integer else num


def _trim(num: float) -> str:
    return ("%d" % num) if float(num).is_integer() else ("%g" % num)


def _target_id(value: str) -> str:
    """取纯数字的群号 / QQ 号（容忍 @123、`group 123` 这类写法）。"""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return ("-" if str(value).strip().startswith("-") else "") + digits


def _unique(items) -> list:
    return list(dict.fromkeys(items))


def _add_group(plugin, group_id: str) -> str:
    if not (group_id.lstrip("-").isdigit() and 4 <= len(group_id.lstrip("-")) <= 12):
        return ("_schema_error", "群号要填数字，例如 /水电 设置 群 100200300")
    groups = _unique(plugin.push_targets()[0] + [group_id])
    plugin.update_config("push_groups", groups)
    plugin.update_config("enabled", True)
    interval = plugin._int_cfg("check_interval", 1800, minimum=60) // 60
    return ("✅ 已在群 %s 提醒，并开启自动巡检（每 %d 分钟）\n"
            "余额低于阈值时我会自动在这里提醒。" % (group_id, interval))
