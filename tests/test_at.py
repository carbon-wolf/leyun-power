# -*- coding: utf-8 -*-
"""leyun_power（D:\\model\\leyunpower 部署版）的离线测试：重点验证 @ 功能。

跑法：
    python test_leyunpower_at.py

用**真实的** `napcat.message` / `napcat.scope` / `napcat.command`（从 D:\\model\\ncqqpy 导入），
只把 `napcat.plugin.PluginBase` 换成轻量桩 —— 这样 `MessageChain` 生成的 at 段是真段，
能验证「{@全体成员}」确实变成了 `[CQ:at,qq=all]`，而不是留在文本里。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import types

NCQQPY = r"D:\model\ncqqpy"
PLUGIN_ROOT = r"D:\model\leyunpower\plugins"

# ----------------------------------------------------------------- 真实 napcat

sys.path.insert(0, NCQQPY)
import napcat  # noqa: E402
import napcat.command  # noqa: E402,F401
import napcat.message  # noqa: E402
import napcat.scope  # noqa: E402,F401
from napcat.message import MessageChain  # noqa: E402


# ----------------------------------------------------------------- 轻量基类桩

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
    def is_superuser(self, uid) -> bool:
        return str(uid) == "99999"


class FakeCmds:
    """不实现 add_visibility_filter（插件会自己跳过）。"""


class FakeApi:
    def __init__(self, plugin):
        self.plugin = plugin

    async def send_group_msg(self, group_id=None, message=None, **kw):
        text = message.to_cq() if hasattr(message, "to_cq") else str(message)
        if "[CQ:at,qq=all]" in text and self.plugin.at_forbidden:
            raise RuntimeError("no permission to at all")
        self.plugin.sent.append(("group", str(group_id), text))

    async def send_private_msg(self, user_id=None, message=None, **kw):
        text = message.to_cq() if hasattr(message, "to_cq") else str(message)
        self.plugin.sent.append(("private", str(user_id), text))


class PluginBase:
    name = ""
    version = ""
    description = ""
    author = ""

    def __init__(self):
        self.config = FakeConfig({})
        self.logger = logging.getLogger("leyun.test")
        self.perm = FakePerm()
        self.cmds = FakeCmds()
        self.registered = []
        self.schedules = []
        self.sent = []
        self.at_forbidden = False

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


_stub = types.ModuleType("napcat.plugin")
_stub.PluginBase = PluginBase
sys.modules["napcat.plugin"] = _stub
napcat.plugin = _stub

sys.path.insert(0, PLUGIN_ROOT)
pkg = importlib.import_module("leyun_power")
content = importlib.import_module("leyun_power.content")
patrol = importlib.import_module("leyun_power.jobs.patrol")


# ----------------------------------------------------------------- 测试夹具

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


class TempState:
    """内存里的 state，别碰部署目录的 data/state.json。"""

    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


def make_plugin(push_groups=None, push_users=None):
    plugin = pkg.LeyunPowerPlugin()
    cfg = plugin.config_template()
    cfg["account"] = {"tel": "13800138000", "password": "123456"}
    cfg["push_groups"] = list(push_groups or [])
    cfg["push_users"] = list(push_users or [])
    cfg["binding"] = {"groups": [100200300], "users": [10001]}
    plugin.config = FakeConfig(cfg)
    plugin.api = FakeApi(plugin)
    plugin.state = TempState()
    plugin._register_commands()
    return plugin


def cmds_of(plugin):
    return {name: func for name, kw, func in plugin.registered}


def check(cond, label, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + label + ((" → " + detail) if detail else ""))
    return bool(cond)


# ----------------------------------------------------------------- 用例

def test_content() -> bool:
    print("\n== 1. 模板层：@ 占位符解析 ==")
    ok = True
    ok &= check(content.segments("a{@全体成员}b") == [("text", "a"), ("at", "all"), ("text", "b")],
                "`{@全体成员}` 切成 at 段")
    ok &= check(content.segments("{@全体}{@all}") == [("at", "all"), ("at", "all")],
                "`{@全体}` / `{@all}` 等价")
    ok &= check(content.segments("{@10001}") == [("at", "10001")], "`{@10001}` → at 10001")
    ok &= check(content.segments("{at:123456}") == [("at", "123456")],
                "`{at:123456}` → at 123456")
    ok &= check(content.segments("没有 at") == [("text", "没有 at")], "无 @ 时原样返回")
    ok &= check(content.plain_text("{@全体成员} 余额 {余额}") == "@全体成员 余额 {余额}",
                "plain_text 换成可读文本（不渲染变量）")
    ok &= check(content.strip_at("头\n{@全体成员}\n尾") == "头\n尾",
                "strip_at 去掉 @ 占位符：%r" % content.strip_at("头\n{@全体成员}\n尾"))
    ok &= check(content.has_at("{@10001}") and not content.has_at("{余额}"), "has_at 判定")
    ok &= check(content.unknown_vars("{@全体成员} {余额}", content.REMIND_VARS) == [],
                "@ 占位符不算未知变量")
    ok &= check(content.unknown_vars("{at:123}", content.REMIND_VARS) == ["at:123"],
                "3 位数字不是合法 QQ，按未知变量拒掉")
    return ok


async def test_chain() -> bool:
    print("\n== 2. 推送链路：文本 → 消息段 ==")
    plugin = make_plugin(push_groups=["100200300"], push_users=["20002"])
    text = "{@全体成员}\n1号楼203电费只剩 1.86 元"
    group_chain = plugin.build_chain(text)
    cq = group_chain.to_cq()
    ok = check(cq.startswith("[CQ:at,qq=all]"), "群消息首段是 @全体成员：%s" % cq[:40])
    ok &= check("1号楼203" in cq and "{@" not in cq, "占位符没有残留在文本里")

    private_chain = plugin.build_chain(text, with_at=False)
    ok &= check("[CQ:at" not in private_chain.to_cq(), "私聊消息不带 at 段")

    sent = await plugin.push(text)
    ok &= check(sent == 2, "推到 1 群 + 1 私聊，成功 %d 条" % sent)
    kind, target, body = plugin.sent[0]
    ok &= check(kind == "group" and "[CQ:at,qq=all]" in body, "群里发了 at 段")
    kind2, target2, body2 = plugin.sent[1]
    ok &= check(kind2 == "private" and "@" not in body2, "私聊自动去掉 @：%r" % body2[:20])

    print("\n== 3. 机器人没 @全体 权限时降级为纯文本 ==")
    plugin2 = make_plugin(push_groups=["100200300"])
    plugin2.at_forbidden = True
    await plugin2.push("{@全体成员} 余额不足")
    ok &= check(len(plugin2.sent) == 1 and "[CQ:at" not in plugin2.sent[0][2],
                "先失败后补发纯文本，最终 1 条：%r" % (plugin2.sent[0][2] if plugin2.sent else "无"))
    return ok


async def test_commands() -> bool:
    print("\n== 4. `/水电 设置 模板` 里写 @ ==")
    plugin = make_plugin()
    cmds = cmds_of(plugin)
    ok = True

    msg = await cmds["leyun.config"](Params(spec=["模板"]), Event(user_id="99999"))
    ok &= check("{@全体成员}" in msg and "@ 用法" in msg, "查看帮助里带 @ 用法")

    msg = await cmds["leyun.config"](
        Params(spec=["模板", "{@全体成员}", "⚡{房间}只剩 {余额} 元"]),
        Event(user_id="99999"))
    ok &= check("已保存" in msg and "@ 已生效" in msg, "带 @ 的模板保存成功")
    saved = plugin.config.data["remind_template"]
    ok &= check(saved.startswith("{@全体成员}"), "config 里原样存占位符：%r" % saved)
    ok &= check("@全体成员" in msg, "回执里把 @ 显示成可读文本")

    msg = await cmds["leyun.config"](
        Params(spec=["模板", "{@全体成员}", "{余额}", "{不认识的}"]),
        Event(user_id="99999"))
    ok &= check(isinstance(msg, tuple) and msg[0] == "_schema_error",
                "真·未知变量仍被拒：%s" % (msg[1] if isinstance(msg, tuple) else msg))

    msg = await cmds["leyun.config"](Params(spec=["失败模板", "{@10001} 查询失败：{错误}"]),
                                     Event(user_id="99999"))
    ok &= check("已保存" in msg, "失败模板也能带 @")

    panel = await cmds["leyun.config"](Params(spec=[]), Event(user_id="99999"))
    ok &= check("@全体成员" in panel and "{@" not in panel,
                "设置面板里的文案行不残留 { }（否则回执会真的 @人）")

    plugin.config.data["enabled"] = True
    plugin.config.data["threshold"] = {"322": 9999.0, "323": 9999.0}
    plugin.config.data["rooms"] = [
        {"label": "1号楼203", "pay_proj_type": 323, "input4": {"id": "x", "name": "1号楼203"}}]
    return ok


async def test_patrol_push() -> bool:
    print("\n== 5. 巡检推送真的带 @（并且只 @ 一次，不重复刷屏）==")
    plugin = make_plugin(push_groups=["100200300"])
    plugin.config.data["threshold"] = {"322": 9999.0, "323": 9999.0}
    plugin.config.data["rooms"] = [
        {"label": "1号楼203", "pay_proj_type": 323,
         "input4": {"id": "100-10--20-203", "name": "1号楼203"}}]
    plugin.config.data["remind_template"] = "{@全体成员}\n{房间}{类型}只剩 {余额} 元"
    plugin.config.data["remind_cooldown_hours"] = 6

    async def fake_query_all():
        return [({"label": "1号楼203", "pay_proj_type": 323,
                  "input4": {"id": "100-10--20-203", "name": "1号楼203"}},
                 {"name": "宿舍电费", "surplus_qty": 62.36, "surplus_amt": 1.86,
                  "unit_price": 0.53}, None)]

    plugin.query_all = fake_query_all
    summary = await patrol.check_once(plugin)
    print("  " + summary)
    ok = check(len(plugin.sent) == 1, "推了 1 条")
    body = plugin.sent[0][2] if plugin.sent else ""
    ok &= check(body.startswith("[CQ:at,qq=all]"), "开头就是 @全体成员：%r" % body[:30])
    ok &= check("1号楼203电费只剩 1.86 元" in body, "变量已渲染：%r" % body)

    again = await patrol.check_once(plugin)
    ok &= check(len(plugin.sent) == 1, "冷却期内不重复推（%s）" % again)
    return ok


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ok = test_content()
    ok &= await test_chain()
    ok &= await test_commands()
    ok &= await test_patrol_push()
    print("\n" + ("🎉 全部通过" if ok else "❌ 有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
