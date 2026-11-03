# -*- coding: utf-8 -*-
"""离线测试：真实 napcat + 轻量 PluginBase 桩，按框架的注入规则驱动插件。

跑法（在仓库根目录）：
    python tests/test_logic.py                    # 只跑逻辑，不联网
    NCQQPY_HOME=/path/to/ncqqpy python tests/test_logic.py
    LEYUN_ROOMS='[{...}]' python tests/test_logic.py --live 手机号 密码
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import types

# --------------------------------------------------- 真实 napcat + 轻量桩
#
# 消息段 / Subscription / 命令 Schema 一律用框架真实实现（避免桩和框架走偏），
# 只把 PluginBase 换成轻量桩，好让测试能直接拿到注册表与发送记录。

sys.path.insert(0, r"D:\model\ncqqpy")

import napcat                      # noqa: E402
import napcat.command as _cmd      # noqa: E402
import napcat.message as _msg      # noqa: E402
import napcat.scope as _scope      # noqa: E402

# 框架的 napcat/plugin.py 会拉进 aiofiles 等运行期依赖；测试只用到 PluginBase
# 的注册/调度接口，这里直接给一个同名模块顶替，避免为了跑测试装一堆包。
_plugin = types.ModuleType("napcat.plugin")

MessageChain = _msg.MessageChain
StrField = _cmd.StrField
VariadicField = _cmd.VariadicField
CommandSchema = _cmd.CommandSchema


class FakeConfig:
    """带点号路径的 config 代理（模拟 ConfigManager）。"""

    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        cur = self.data
        for part in str(key).split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, key, value):
        parts = str(key).split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value


class FakePerm:
    """测试里一律当超管：`check_access` 的服务范围校验由 scope 专项测试覆盖。"""

    def is_superuser(self, uid) -> bool:
        return True


class FakeCmds:
    """不实现 add_visibility_filter（插件会自己跳过）。"""


class PluginBase:
    name = ""
    version = ""
    description = ""
    author = ""

    def __init__(self, config=None):
        self.config = FakeConfig(config or {})
        self.logger = logging.getLogger("leyun.test")
        self.perm = FakePerm()
        self.cmds = FakeCmds()
        self.registered = []          # [(name, kwargs, func)]
        self.schedules = []
        self.sent = []

    # 框架接口
    def register_command(self, name, **kw):
        def deco(func):
            self.registered.append((name, kw, func))
            return func
        return deco

    def schedule(self, seconds, name=None):
        def deco(func):
            self.schedules.append((seconds, name or func.__name__, func))
            return func
        return deco


class FakeApi:
    def __init__(self, plugin):
        self.plugin = plugin

    async def send_group_msg(self, group_id=None, message=None, **kw):
        self.plugin.sent.append(("group", str(group_id), message.to_cq()))

    async def send_private_msg(self, user_id=None, message=None, **kw):
        self.plugin.sent.append(("private", str(user_id), message.to_cq()))


def install_stubs():
    """把框架的 PluginBase 换成轻量桩（其余 napcat API 保持真实）。"""
    _plugin.PluginBase = PluginBase
    sys.modules["napcat.plugin"] = _plugin
    napcat.plugin = _plugin


install_stubs()
sys.path.insert(0, r"D:\model\leyunpower\plugins")

from leyun_power import LeyunPowerPlugin          # noqa: E402
from leyun_power.schemas import BindSchema, ConfigSchema, LoginSchema  # noqa: E402


# ----------------------------------------------------------------- 事件 / 参数

class Event:
    def __init__(self, user_id="10001", group_id=None, is_temp_session=False):
        self.user_id = user_id
        self.group_id = group_id
        self.message_type = "group" if group_id else "private"
        self.is_temp_session = is_temp_session


class Params:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Packet:
    def __init__(self, args=None, event=None):
        self.args = args or []
        self.event = event or Event()


def make_plugin(account=None, push_groups=None, owner=None, rooms=None) -> LeyunPowerPlugin:
    plugin = LeyunPowerPlugin()
    cfg = plugin.config_template()
    if account:
        cfg["account"] = {"tel": account[0], "password": account[1]}
    if push_groups:
        cfg["push_groups"] = list(push_groups)
    if owner:
        cfg["owner_users"] = list(owner)
    if rooms:
        cfg["rooms"] = list(rooms)
    # 免打扰默认是 23:00-08:00；测试要跑在任意时刻都一致，这里先关掉，
    # 免打扰的判断另有专项（quiet_tests）
    cfg["quiet_hours"] = {"start": "", "end": ""}
    plugin.config = FakeConfig(cfg)
    plugin.api = FakeApi(plugin)
    return plugin


async def boot(plugin) -> dict:
    """跑一遍生命周期，返回 {命令名: 处理函数}。"""
    await plugin.on_load()
    await plugin.on_enable()
    return {name: func for name, kw, func in plugin.registered}


# ----------------------------------------------------------------- 逻辑测试

def check(cond, label, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + label + ((" → " + detail) if detail else ""))
    return bool(cond)


async def logic_tests() -> bool:
    ok = True
    print("\n== 1. 注册的命令（group + bind/query/config/scope）==")
    plugin = make_plugin()
    cmds = await boot(plugin)
    ok &= check(list(cmds) == ["leyun", "leyun.login", "leyun.bind", "leyun.query",
                               "leyun.config", "leyun.verify", "leyun.scope"],
                "命令清单 = %s" % list(cmds))
    ok &= check([s[0] for s in plugin.schedules] == [60],
                "定时器周期 %s 秒" % [s[0] for s in plugin.schedules])
    ok &= check(cmds["leyun.config"].__code__.co_varnames[:1] == ("params",),
                "config 处理器按参数名拿 params")

    print("\n== 2. 未登录时 /水电 查 ==")
    print("  " + (await cmds["leyun.query"](Packet())).replace("\n", "\n  "))

    print("\n== 3. /水电 设置（面板）==")
    text = await cmds["leyun.config"](Params(spec=[]), Event())
    ok &= check("未登录" in text and "自动巡检：开" in text, "面板含账号状态与开关")
    print("  " + text.replace("\n", "\n  "))

    print("\n== 4. 设置阈值 / 推送群 / 间隔 ==")
    print("  " + await cmds["leyun.config"](Params(spec=["电", "10"]), Event()))
    print("  " + await cmds["leyun.config"](Params(spec=["水", "5"]), Event()))
    print("  " + await cmds["leyun.config"](Params(spec=["本群"]), Event(group_id="100200300")))
    print("  " + await cmds["leyun.config"](Params(spec=["间隔", "15"]), Event()))
    print("  " + await cmds["leyun.config"](Params(spec=["冷却", "3"]), Event()))
    conf = plugin.config.data
    ok &= check(conf["threshold"]["323"] == 10.0 and conf["threshold"]["322"] == 5.0,
                "阈值写回 config：%s" % conf["threshold"])
    ok &= check(conf["push_groups"] == ["100200300"], "推送群写回：%s" % conf["push_groups"])
    ok &= check(conf["check_interval"] == 900, "巡检间隔写回：%s 秒" % conf["check_interval"])
    ok &= check(conf["remind_cooldown_hours"] == 3, "冷却写回：%s 小时" % conf["remind_cooldown_hours"])

    print("\n== 5. 非法输入（走 _schema_error，由框架回「用法不正确 + 帮助页」）==")
    for spec, want in ((["电", "abc"], "非负数字"), (["看不懂"], "看不懂"),
                       (["间隔", "0"], "1-1440"), (["电"], "阈值")):
        got = await cmds["leyun.config"](Params(spec=spec), Event())
        good = isinstance(got, tuple) and got[0] == "_schema_error" and want in got[1]
        ok &= check(good, "/水电 设置 %s → %s" % (" ".join(spec), got[1] if isinstance(got, tuple) else got))

    print("\n== 6. 开关与私聊目标 ==")
    print("  " + (await cmds["leyun.config"](Params(spec=["关"]), Event())).splitlines()[0])
    ok &= check(plugin.config.data["enabled"] is False, "enabled 已关闭")
    print("  " + (await cmds["leyun.config"](Params(spec=["开"]), Event())).splitlines()[0])
    print("  " + await cmds["leyun.config"](Params(spec=["私聊", "10001"]), Event()))
    ok &= check(plugin.config.data["push_users"] == ["10001"], "私聊目标写回")
    print("  " + await cmds["leyun.config"](Params(spec=["移群", "100200300"]), Event()))
    ok &= check(plugin.config.data["push_groups"] == [], "移群生效")

    print("\n== 7. 非 admin 改设置被拒 ==")
    plugin2 = make_plugin(owner=["99999"])
    cmds2 = await boot(plugin2)
    msg = await cmds2["leyun.config"](Params(spec=["电", "1"]), Event(user_id="10001"))
    ok &= check("管理员" in msg, "被 owner 名单拦住")

    print("\n== 8. login / bind 的 Schema 声明 ==")
    ok &= check(LoginSchema.__dict__.get("tel") is not None
                and LoginSchema.__dict__.get("password") is not None,
                "LoginSchema 声明了 tel + password")
    ok &= check(BindSchema.__dict__.get("group") is not None,
                "BindSchema 只声明 group（不碰密码）")
    ok &= check(ConfigSchema.__dict__.get("spec") is not None, "ConfigSchema 声明了 spec")
    return ok


# ----------------------------------------------------------------- 巡检测试

def _install_fake_bills(plugin) -> None:
    """把 `query_all` 换成固定账单，巡检逻辑离线可测（不碰网络、不带账号）。"""
    from leyun_power import content

    room_id = "100-10--20-203"

    async def fake_query_all():
        return [
            ({"label": "1号楼203", "pay_proj_type": 323,
              "input4": {"id": room_id, "name": "1号楼203"}},
             content.sample_bill(323), None),
            ({"label": "1号楼203", "pay_proj_type": 322,
              "input4": {"id": room_id, "name": "1号楼203"}},
             content.sample_bill(322), None),
        ]

    plugin.query_all = fake_query_all


async def patrol_tests() -> bool:
    print("\n== 9. 巡检：阈值调高后应推送并按冷却去重 ==")
    plugin = make_plugin(account=("10000000000", "000000"),
                         push_groups=["100200300"], owner=["10001"])
    await boot(plugin)
    from leyun_power.jobs import patrol

    # 巡检只关心「拿到账单后怎么判断/怎么推」，这里用假账单顶掉网络查询，
    # 测试保持离线（真实链路见 --live）。
    _install_fake_bills(plugin)
    plugin.config.data["threshold"] = {"322": 9999.0, "323": 9999.0}
    plugin.config.data["remind_cooldown_hours"] = 6
    plugin.state.set("last_remind", {})       # 清掉上一轮跑测试留下的冷却记录
    summary = await patrol.check_once(plugin)
    print("  " + summary)
    first = len(plugin.sent)
    summary2 = await patrol.check_once(plugin)
    print("  " + summary2)
    ok = check(first == 2 and len(plugin.sent) == first,
               "首轮推 2 条、第二轮被冷却拦住（共 %d 条）" % len(plugin.sent))
    for kind, target, text in plugin.sent:
        print("    [%s %s] %s" % (kind, target, text.splitlines()[0]))

    print("\n== 10. 关闭后不推送 ==")
    plugin.config.data["enabled"] = False
    plugin.state.set("last_remind", {})
    before = len(plugin.sent)
    await patrol.check_once(plugin)
    ok &= check(len(plugin.sent) == before, "enabled=false 时不推送")
    ok &= check(await patrol.tick(plugin) is None, "tick 在关闭时直接返回")

    print("\n== 11. 自定义文案在巡检推送里生效 ==")
    plugin.config.data["enabled"] = True
    plugin.config.data["threshold"] = {"322": 9999.0, "323": 9999.0}
    plugin.update_config("remind_template",
                         "【自定义】{房间}{类型}只剩 {余额} 元\\n阈值 {阈值} 元")
    plugin.state.set("last_remind", {})
    before = len(plugin.sent)
    await patrol.check_once(plugin)
    fresh = plugin.sent[before:]
    ok &= check(len(fresh) == 2 and all(t.startswith("【自定义】") for _, _, t in fresh),
                "推送用自定义文案（%d 条）" % len(fresh))
    if fresh:
        print("    " + fresh[0][2].replace("\n", "\n    "))

    print("\n== 11b. 没有推送目标时：不吞提醒、不写冷却、补上目标立刻发 ==")
    bare = make_plugin(account=("10000000000", "000000"), owner=["10001"])
    await boot(bare)
    _install_fake_bills(bare)
    bare.config.data["threshold"] = {"322": 9999.0, "323": 9999.0}
    bare.state.set("last_remind", {})
    summary = await patrol.check_once(bare)
    print("  " + summary)
    ok &= check(len(bare.sent) == 0 and "没有推送目标" in summary,
                "无目标时摘要里明说「提醒发不出去」")
    ok &= check(not bare.state.get("last_remind"),
                "没发出去就不记冷却：%s" % bare.state.get("last_remind"))
    bare.config.data["push_groups"] = ["100200300"]
    await patrol.check_once(bare)
    ok &= check(len(bare.sent) == 2, "补上推送群后下一轮立刻发出（%d 条）" % len(bare.sent))
    return ok


async def template_tests() -> bool:
    """`/水电 设置 模板 ...` 的查看 / 修改 / 校验 / 恢复默认 / 预览。"""
    print("\n== 12. 提醒文案自定义 ==")
    plugin = make_plugin(owner=["10001"])
    cmds = await boot(plugin)
    from leyun_power import content

    room = {"label": "1号楼203", "pay_proj_type": 323}
    default_text = plugin.remind_text(room, content.sample_bill(323))
    ok = check("🔔" in default_text and "1号楼203" in default_text
               and "0.53" in default_text, "默认模板渲染出房间/单价")
    print("  " + default_text.replace("\n", "\n  "))

    msg = await cmds["leyun.config"](Params(spec=["模板"]), Event())
    ok &= check("{余额}" in msg and "可用变量" in msg, "模板 不带参数 → 展示当前文案 + 变量表")
    print("  " + msg.replace("\n", "\n  "))

    msg = await cmds["leyun.config"](
        Params(spec=["模板", "⚡", "{房间}{类型}只剩", "{余额}元\\n快去充"]), Event())
    ok &= check("已保存" in msg, "模板 <文案> 保存成功")
    ok &= check(plugin.config.data["remind_template"].count("\n") == 1,
                "`\\n` 已还原成换行")
    ok &= check("1.86" in msg and "快去充" in msg, "保存后立刻回显渲染效果")
    print("  " + msg.replace("\n", "\n  "))

    bad = await cmds["leyun.config"](Params(spec=["模板", "{不认识的变量}"]), Event())
    ok &= check(isinstance(bad, tuple) and bad[0] == "_schema_error"
                and "不认识" in bad[1], "拼错变量名被拒：%s" % (bad[1] if isinstance(bad, tuple) else bad))

    too_long = await cmds["leyun.config"](Params(spec=["模板", "长" * 600]), Event())
    ok &= check(isinstance(too_long, tuple) and too_long[0] == "_schema_error",
                "超长文案被拒")

    msg = await cmds["leyun.config"](Params(spec=["失败模板", "查询炸了：{错误}"]), Event())
    ok &= check("已保存" in msg, "失败模板可单独设置")
    ok &= check(plugin.error_text(["查询超时"]).startswith("查询炸了：查询超时"),
                "失败文案用自定义模板渲染")

    msg = await cmds["leyun.config"](Params(spec=["模板", "默认"]), Event())
    ok &= check(plugin.config.data["remind_template"] == content.DEFAULT_REMIND,
                "模板 默认 → 恢复内置文案")
    ok &= check("恢复默认" in msg, "回显「已恢复默认」")

    msg = await cmds["leyun.config"](Params(spec=["预览"]), Event())
    ok &= check("提醒预览" in msg and "示例数据" in msg, "预览在未绑定时用示例数据")
    print("  " + msg.replace("\n", "\n  "))

    # 全角花括号 / 空模板回落
    ok &= check(content.render("｛余额｝元", {"余额": "9.90"}) == "9.90元",
                "全角花括号能识别")
    ok &= check(content.render("", {"余额": "1"}, content.DEFAULT_REMIND).startswith("🔔"),
                "空模板回落到默认文案")
    return ok


class FakeVerifyClient:
    """假客户端：只实现校验码流程用到的两个方法。"""

    def __init__(self):
        self.codes_sent = 0

    async def send_login_code(self, tel):
        self.codes_sent += 1
        return 120

    async def login(self, tel, pwd, valid_code=""):
        from leyun_power.api import LeyunVerifyRequired
        if valid_code != "123456":
            raise LeyunVerifyRequired("校验码不正确或已过期", tel=tel)
        return "uid-test"


async def verify_tests() -> bool:
    """90 秒校验流程：发码 / 错码 / 正确码 / 未绑定。"""
    print("\n== 13. 短信校验（9006）==")

    plugin = make_plugin()
    cmds = await boot(plugin)
    msg = await cmds["leyun.verify"](Params(code=""), Event())
    ok = check("还没登录账号" in msg, "未登录时提示先登录")

    plugin = make_plugin(account=("13800138000", "123456"), owner=["10001"])
    cmds = await boot(plugin)
    fake = FakeVerifyClient()
    plugin.new_client = lambda **kw: fake           # 不打真网络

    msg = await cmds["leyun.verify"](Params(code=""), Event())
    print("  " + msg.replace("\n", "\n  "))
    ok &= check("138****8000" in msg and "120" in msg,
                "发码回执带打码手机号与有效期")
    ok &= check(fake.codes_sent == 1, "确实调了一次发码接口")
    ok &= check("/水电 验证" in msg, "回执教下一步怎么提交")

    msg = await cmds["leyun.verify"](Params(code="000000"), Event())
    ok &= check("校验码不正确" in msg and "/水电 验证" in msg,
                "错码时提示重新发码")

    async def fake_query_all():
        return [({"label": "1号楼203", "pay_proj_type": 323},
                 {"name": "宿舍电费", "surplus_qty": 10.0,
                  "surplus_amt": 5.30, "unit_price": 0.53}, None)]

    plugin.query_all = fake_query_all
    msg = await cmds["leyun.verify"](Params(code="123456"), Event())
    print("  " + msg.replace("\n", "\n  "))
    ok &= check("校验通过" in msg, "正确校验码 → 登录成功")
    ok &= check(plugin.state.get("user_id") == "uid-test", "user_id 已缓存到 state")
    ok &= check("1号楼203" in msg, "校验后顺手查一次余额")
    return ok


async def bind_tests() -> bool:
    """`/水电 绑定`：绑群（群里留空=本群 / 私聊带号 / 未登录提示 / 非管理员拒绝）。"""
    print("\n== 15. 绑群 ==")
    plugin = make_plugin(owner=["10001"])
    cmds = await boot(plugin)

    msg = await cmds["leyun.bind"](Params(group=""), Event(user_id="10001", group_id="100200300"))
    print("  " + msg.replace("\n", "\n  "))
    ok = check(plugin.config.data["push_groups"] == ["100200300"], "群里裸发 → 绑当前群")
    ok &= check(100200300 in (plugin.config.data["binding"]["groups"] or []),
                "绑群顺带写进服务范围：%s" % plugin.config.data["binding"]["groups"])
    ok &= check("登录" in msg, "还没登录时回执指向 /水电 登录")

    msg = await cmds["leyun.bind"](Params(group=""), Event(user_id="10001"))
    ok &= check(isinstance(msg, tuple) and msg[0] == "_schema_error"
                and "群号" in msg[1], "私聊裸发 → 提示带群号")

    msg = await cmds["leyun.bind"](Params(group="100200400"), Event(user_id="10001"))
    ok &= check("100200400" in plugin.config.data["push_groups"]
                and len(plugin.config.data["push_groups"]) == 2,
                "私聊带号 → 再加一个群：%s" % plugin.config.data["push_groups"])

    await cmds["leyun.bind"](Params(group=""), Event(user_id="10001", group_id="100200300"))
    ok &= check(plugin.config.data["push_groups"].count("100200300") == 1, "重复绑同群不重复")

    stranger = await cmds["leyun.bind"](Params(group=""), Event(user_id="99999", group_id="100200500"))
    ok &= check("管理员" in stranger, "非 owner 绑群被拒")
    ok &= check("100200500" not in plugin.config.data["push_groups"], "被拒时配置没被改")
    return ok


async def quiet_tests() -> bool:
    """夜间免打扰：时段判断 / 命令设置 / 时段内攒着不推（也不写冷却）。"""
    import time as _time

    def at(hour, minute):
        return _time.mktime((2026, 11, 3, hour, minute, 0, 0, 0, -1))

    print("\n== 16. 夜间免打扰 ==")
    plugin = make_plugin(owner=["10001"])
    cmds = await boot(plugin)

    msg = await cmds["leyun.config"](Params(spec=["免打扰"]), Event())
    ok = check("未开启" in msg, "默认先关掉测试插件，面板说「未开启」")

    msg = await cmds["leyun.config"](Params(spec=["免打扰", "23:00-08:00"]), Event())
    print("  " + msg.replace("\n", "\n  "))
    ok &= check(plugin.config.data["quiet_hours"] == {"start": "23:00", "end": "08:00"},
                "跨零点时段已写入：%s" % plugin.config.data["quiet_hours"])

    cases = [(23, 30, True), (1, 0, True), (7, 59, True),
             (8, 0, False), (12, 0, False), (22, 59, False)]
    for hour, minute, want in cases:
        got = plugin.in_quiet_hours(at(hour, minute))
        ok &= check(got is want, "%02d:%02d → 免打扰=%s" % (hour, minute, got))

    msg = await cmds["leyun.config"](Params(spec=["免打扰", "12点到14点"]), Event())
    ok &= check(plugin.config.data["quiet_hours"] == {"start": "12:00", "end": "14:00"},
                "中文「到」与省略分钟也能解析")
    ok &= check(plugin.in_quiet_hours(at(13, 0)) and not plugin.in_quiet_hours(at(11, 59)),
                "同日时段判断正确")

    for bad in ("25:00-08:00", "23:00-23:00", "2300-0800"):
        got = await cmds["leyun.config"](Params(spec=["免打扰", bad]), Event())
        ok &= check(isinstance(got, tuple) and got[0] == "_schema_error",
                    "非法时段 %s 被拒" % bad)

    print("\n== 17. 免打扰期间：攒着不发，白天立刻补 ==")
    plugin = make_plugin(account=("10000000000", "000000"),
                         push_groups=["100200300"], owner=["10001"])
    cmds = await boot(plugin)
    _install_fake_bills(plugin)
    plugin.config.data["threshold"] = {"322": 9999.0, "323": 9999.0}
    plugin.config.data["quiet_hours"] = {"start": "23:00", "end": "08:00"}
    plugin.state.set("last_remind", {})
    from leyun_power.jobs import patrol

    plugin.in_quiet_hours = lambda now=None: True
    summary = await patrol.check_once(plugin)
    print("  " + summary)
    ok &= check(len(plugin.sent) == 0, "时段内一条都不发")
    ok &= check("免打扰" in summary, "摘要说明是免打扰而不是发失败")
    ok &= check(not plugin.state.get("last_remind"),
                "时段内不写冷却：%s" % plugin.state.get("last_remind"))

    plugin.in_quiet_hours = lambda now=None: False
    await patrol.check_once(plugin)
    ok &= check(len(plugin.sent) == 2, "时段结束后第一轮就补发（%d 条）" % len(plugin.sent))

    msg = await cmds["leyun.config"](Params(spec=["免打扰", "关"]), Event())
    ok &= check(plugin.quiet_text() == "" and "已关闭" in msg, "可以关掉：%s" % msg)

    panel = await cmds["leyun.config"](Params(spec=[]), Event())
    ok &= check("夜间免打扰" in panel, "设置面板显示免打扰状态")
    return ok


async def live_tests(tel: str, pwd: str) -> bool:
    print("\n== 14. 真实链路：login → bind → query → 巡检 ==")
    rooms = json.loads(os.environ.get("LEYUN_ROOMS") or "[]")
    if not rooms:
        print("  跳过：设环境变量 LEYUN_ROOMS='[{...}]' 再跑（房间 ID 因人而异）")
        return True
    plugin = make_plugin(account=(tel, pwd), owner=["10001"], rooms=rooms)
    cmds = await boot(plugin)
    text = await cmds["leyun.query"](Packet())
    print("  " + text.replace("\n", "\n  "))
    ok = check("查询成功" in text and "❌" not in text, "真实查到余额")

    text = await cmds["leyun.login"](Params(tel=tel, password=pwd), Event())
    print("  " + text.replace("\n", "\n  "))
    ok &= check("登录成功" in text, "login 走通并写回账号")
    ok &= check(plugin.config.data["account"]["tel"] == tel, "账号已落配置")
    ok &= check(plugin.config.data["owner_users"] == ["10001"], "首次 login 认领 owner")

    text = await cmds["leyun.bind"](Params(group=""), Event(group_id="100200300"))
    print("  " + text.replace("\n", "\n  "))
    ok &= check(plugin.config.data["push_groups"] == ["100200300"], "bind 绑群写回")
    return ok


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = await logic_tests()
    ok &= await patrol_tests()
    ok &= await template_tests()
    ok &= await verify_tests()
    ok &= await bind_tests()
    ok &= await quiet_tests()
    if "--live" in sys.argv:
        idx = sys.argv.index("--live")
        ok &= await live_tests(sys.argv[idx + 1], sys.argv[idx + 2])
    print("\n" + ("🎉 全部通过" if ok else "❌ 有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
