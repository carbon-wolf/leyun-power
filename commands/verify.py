# -*- coding: utf-8 -*-
"""`/leyun verify` —— 新设备首次登录的短信校验（9006）。

乐云一卡通对「没见过的设备」要求一次短信校验：`YKTUserLoginSVC` 发码，
`YKTUserLogin` 带上 `valid_code` 才给 `user_id`。校验通过后同一账号再登录
即免验证，所以正常只会用到一两次。

命令只有一个入口，两种形态：

    /水电 验证           → 发一条校验码到绑定手机
    /水电 验证 123456    → 提交校验码，成功后立刻查一次余额

设计上不把校验码当常驻配置：它只活一次，进 `config.json` 反而会留痕。
"""
from __future__ import annotations

from ..api import LeyunAuthError, LeyunProtocolError, LeyunVerifyRequired
from .query import format_all


def mask_tel(tel: str) -> str:
    """手机号打码：`147****6978`（回执会发到群里，别把整号带出去）。"""
    tel = str(tel or "")
    if len(tel) >= 7:
        return "%s****%s" % (tel[:3], tel[-4:])
    return tel or "未绑定"


async def run(plugin, params) -> str:
    """`/leyun verify [校验码]` 的入口。"""
    tel, pwd = plugin.account()
    if not tel or not pwd:
        return ("还没绑定账号，没法做校验。\n"
                "私聊机器人发：/水电 绑定 <手机号> <登录密码>")

    code = str(getattr(params, "code", "") or "").strip()
    client = plugin.new_client()

    # ---- 第一步：发校验码
    if not code:
        try:
            seconds = await client.send_login_code(tel)
        except (LeyunAuthError, LeyunProtocolError) as e:
            return ("❌ 校验码没发出去：%s\n\n"
                    "常见原因：1 分钟内重复发送、号码未绑定一卡通、网络超时。"
                    "稍等一下重发即可。" % e)
        return ("📩 校验码已发送到 %s（%d 秒内有效）\n\n"
                "收到后发：/水电 验证 <6位校验码>\n"
                "例：/水电 验证 123456\n\n"
                "校验只需做一次，通过后同账号再登录就免验证了。"
                % (mask_tel(tel), seconds))

    # ---- 第二步：带码登录
    try:
        user_id = await client.login(tel, pwd, valid_code=code)
    except LeyunVerifyRequired as e:
        return ("❌ %s\n\n"
                "重新发一条校验码：/水电 验证" % e)
    except (LeyunAuthError, LeyunProtocolError) as e:
        return ("❌ 校验失败：%s\n\n"
                "如果是密码问题，重新绑定一次：/水电 绑定 <手机号> <密码>" % e)

    plugin.state.set("user_id", user_id)
    plugin.state.save()

    try:
        text = format_all(plugin, await plugin.query_all())
    except Exception as e:  # noqa: BLE001 - 登录成功就算校验通过
        text = "（登录成功，但查询失败：%s）" % e
    return "✅ 校验通过，已登录（之后免验证）\n\n" + text
