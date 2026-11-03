# -*- coding: utf-8 -*-
"""leyun_power 插件主体：生命周期 + 命令注册 + 基础设施。

对外**只有一个入口**命令组 `leyun`（别名 `水电` / `sd`），下挂四个子命令：

===========================  ==========================================
`/leyun login <手机号> <密码>` 登录一卡通账号（仅私聊，密码不进群）
`/leyun bind [群号]`          把余额提醒绑到群（群里留空=本群）
`/leyun query`                手动查一次余额
`/leyun config [子项 ...]`     统一设置入口（阈值/开关/推送目标/间隔/冷却/文案）
`/leyun scope [子项 ...]`      服务范围白名单（仅私聊·仅超管）
===========================  ==========================================

不再为每个设置项注册一条子命令：`/leyun config` 的 `spec` 可变参数里分流，
既不占顶层命令空间（这个 bot 还挂着别的插件），也只有一个帮助页要维护。

服务范围（谁能用）走框架的 `napcat.scope`，与 ai_chat / bupt_news / byrdocs /
oj_bank 同一口径；本文件只负责「这道闸门装在哪」，判定与配置在 `commands/setup.py`。

这里只放「装配」和底层能力（配置读写、客户端、查询、推送）；面向用户的命令
逻辑在 `commands/`，定时巡检在 `jobs/patrol.py`。
"""
from __future__ import annotations

import os
import time

from napcat.message import MessageChain
from napcat.plugin import PluginBase
from napcat.scope import Scope

from . import content
from .api import (DEFAULT_ACCESS_TOKEN, DEFAULT_API_ID, VERIFY_MARK,
                  LeyunAuthError, LeyunClient, LeyunProtocolError,
                  LeyunVerifyRequired, parse_room_bill)
from .commands import query as query_cmd
from .commands import setup as setup_cmd
from .commands import verify as verify_cmd
from .jobs import patrol as patrol_job
from .schemas import BindSchema, ConfigSchema, LoginSchema, ScopeSchema, VerifySchema
from .store import StateStore

# 巡检定时器的唤醒周期（秒）。真正的巡检间隔由 config.check_interval 决定，
# 改配置不需要重挂定时器。
PATROL_TICK = 60


class LeyunPowerPlugin(PluginBase):
    """乐云一卡通水电费巡检插件。"""

    name = "leyun_power"
    version = "2.3.0"
    description = "乐云一卡通水电费：定时巡检，余额不足自动提醒到群（文案可自定义、可 @全体/个人、支持短信校验）"
    author = "carbon-wolf"

    # ------------------------------------------------------------------ 配置

    def config_template(self) -> dict:
        """出厂默认配置（框架构造期写入 `config.json → plugins.leyun_power`）。

        只补缺键、不覆盖已有值、嵌套段逐键补全，所以升级新增配置项时老部署会
        自动补齐。凭据类字段留空占位，真实值由 `/leyun bind` 或手工填写。
        """
        return {
            "enabled": True,
            "base_url": "http://wx.leyunykt.com",
            "http_timeout": 15,
            # 应用级凭据（全校共用、长期不变；留空则用 api.py 里的内置值）
            "session": {
                "api_id": DEFAULT_API_ID,
                "access_token": DEFAULT_ACCESS_TOKEN,
                "open_id": "123",
            },
            # 账号（`/leyun bind` 写入；也可手工预置）
            "account": {"tel": "", "password": ""},
            # 可管理本插件的 QQ（留空 = 谁先 bind 谁认领，之后只有这些 QQ 能改设置）
            "owner_users": [],
            # 提醒阈值（元）：322=水费，323=电费
            "threshold": {"322": 5.0, "323": 10.0},
            # 监控房间：每个房间查一次，电费/水费各算一项。
            # 出厂为空 —— 房间编号（input1~4）每个学校/每栋楼都不同，写死没用。
            # 填法见 README「房间怎么填」：从抓包的 YKTGetPayProj 请求里照抄。
            "rooms": [],
            "check_interval": 1800,       # 巡检间隔（秒）
            "remind_cooldown_hours": 6,   # 同一项重复提醒的冷却（小时）
            # 提醒文案（占位符见 /leyun config 模板；改这里或命令改都行）
            "remind_template": content.DEFAULT_REMIND,
            "error_template": content.DEFAULT_ERROR,
            "push_groups": [],            # 群号（`/leyun config 本群` 自动加入）
            "push_users": [],             # 私聊 QQ
            # 服务范围白名单（口径见 `napcat.scope`）：空 = 谁都不用，不回落
            # 框架的 `bound_groups`（那是「监听哪些群」，语义正好相反）。
            # ⚠️ 注意「推送目标」和「服务范围」是两层：加进 push_* 只影响推给谁，
            # 不让谁用；要让某个群/人能发命令，得加到这里。
            "binding": {"groups": [], "users": self._scope_seed_users()},
        }

    def _scope_seed_users(self) -> list:
        """`binding` **首次补齐**时沿用的私聊名单（一次性迁移，之后只认名单）。

        `binding` 是老部署没有的新键；直接补成空表的话，按「空 = 谁都不用」的
        口径，已经认领插件的管理员会在升级瞬间被一起挡在外面。补键那一刻把
        `owner_users` 带进名单，升级前后「谁能用」不跳变；用户之后删掉它也不会
        被重新加回来（键已存在时 `fill_defaults` 不再动它）。
        """
        cfg = self.config
        if cfg is None:                   # 手工 new 出来、没有配置宿主的实例
            return []
        try:
            owners = cfg.get("owner_users") or []
        except Exception:  # noqa: BLE001 - 迁移失败不该让插件起不来
            return []
        return [str(x) for x in owners]

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self):
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        self.state = StateStore(os.path.join(data_dir, "state.json"),
                                logger=self.logger)
        # `/help` 可见性过滤器（见 `_install_visibility_filter`）；禁用/重载时按它摘掉
        self._visibility_filter = None
        # 被服务范围挡下的调用者：{user_id: 上次记日志的时间}（只用于日志节流）
        self._scope_denied = {}

    async def on_enable(self):
        self._register_commands()
        self._install_visibility_filter()
        self._register_jobs()
        tel, _ = self.account()
        if not tel:
            self.logger.warning(
                "[leyun_power] 还没登录账号：私聊机器人发 /水电 登录 <手机号> <密码>")
        if not any(self.push_targets()):
            self.logger.warning(
                "[leyun_power] 还没有推送目标：把机器人拉进群后发 /水电 绑定")
        groups, users = self._scope_lists()
        if not groups and not users:
            self.logger.warning(
                "[leyun_power] 服务范围是空的（谁都用不了）："
                "私聊超管发 /水电 范围 加群 <群号> 或 加人 <QQ>")
        self.logger.info("[leyun_power] 已启用，巡检间隔 %d 秒，服务范围 %d 群 / %d 人",
                         self._int_cfg("check_interval", 1800, minimum=60),
                         len(groups), len(users))

    async def on_disable(self):
        self._remove_visibility_filter()
        self.state.save()

    async def on_unload(self):
        self._remove_visibility_filter()
        self.state.save()

    # ------------------------------------------------------------------ 装配

    def _register_commands(self) -> None:
        """只做登记：把命令名接到 `commands/` 里的逻辑上。"""

        # ---- 入口命令组（本体不会被执行：框架把裸调重定向到 /help leyun）
        # 服务范围闸门：被挡下就静默返回（不暴露「命令存在但你没资格」），
        # 与 `/help` 的可见性过滤器共用同一个 `check_access()`。
        def _denied(event) -> bool:
            if self.check_access(event):
                return False
            self._log_scope_denied(event)
            return True

        async def cmd_root(packet):
            return None

        async def cmd_login(params, event):
            # 登录**不过服务范围那道闸门**：它是「认领 / 开通」动作，新装机时
            # 白名单本来就是空的，先拦一道只会让人永远登不进来。权限改由
            # owner 名单把关（见 setup.login），框架层的 scope="private"
            # 仍然保证密码只可能在私聊里出现。
            return await setup_cmd.login(self, params, event)

        async def cmd_bind(params, event):
            # 绑群**故意不过服务范围那道闸门**：它的作用就是「开通这个群」，
            # 先被「名单里没这个群」挡下来会变成「绑完还是用不了」的死结。
            # 权限改由 owner 名单把关（见 setup.bind_group）。
            return await setup_cmd.bind_group(self, params, event)

        async def cmd_query(packet):
            if _denied(getattr(packet, "event", None)):
                return None
            return await query_cmd.run(self)

        async def cmd_config(params, event):
            if _denied(event):
                return None
            return await setup_cmd.config(self, params, event)

        async def cmd_verify(params, event):
            # 同 login：校验是「把账号登通」的一环，白名单为空时也得能用，
            # 否则新装机永远卡在 9006 上。scope="private" 保证校验码不进群。
            return await verify_cmd.run(self, params)

        async def cmd_scope(params, event):
            # 名单维护：声明式 `permission="superuser"` + `scope="private"`
            # 已在框架层挡掉非超管，而超管对 `check_access()` 恒豁免，这里不再判。
            return await setup_cmd.scope(self, params, event)

        self.register_command(
            "leyun", aliases=["水电", "sd"],
            description="乐云一卡通水电费：定时巡检 + 余额不足提醒")(cmd_root)

        # 中文别名不是锦上添花：插件自己的回执/日志/README 全部写的是
        # `/水电 登录 …`、`/水电 绑定`，不声明这些别名，用户照着做会落到
        # `/help leyun`（子命令名不认识 -> 帮助页），等于「按提示操作没反应」。
        self.register_command(
            "leyun.login", schema=LoginSchema, scope="private", aliases=["登录"],
            description="登录一卡通账号（仅私聊，密码不会进群）")(cmd_login)

        # 「绑群」和「登录」是两件事：前者定提醒发到哪，后者定用谁的身份查。
        # scope 放开到 all，因为绑群这个动作本身就要在群里发。
        self.register_command(
            "leyun.bind", schema=BindSchema, scope="all", aliases=["绑定"],
            description="把余额提醒绑到群：在群里发即绑本群，私聊需带群号")(
            cmd_bind)

        self.register_command(
            "leyun.query", scope="all", aliases=["查询"],
            description="查一次宿舍水电余额")(cmd_query)

        self.register_command(
            "leyun.config", schema=ConfigSchema, aliases=["设置"],
            description="统一设置入口：阈值 / 开关 / 推送群 / 巡检间隔 / 冷却")(
            cmd_config)

        # 短信校验只在校验那一下有用，校验码不进配置（留在 config.json 等于留痕）。
        self.register_command(
            "leyun.verify", schema=VerifySchema, scope="private", aliases=["验证"],
            description="新设备首次登录的短信校验：不带码=发送，带码=提交")(
            cmd_verify)

        self.register_command(
            "leyun.scope", schema=ScopeSchema, permission="superuser",
            scope="private", aliases=["范围"],
            description="服务范围白名单：加群 / 减群 / 加人 / 减人（仅私聊·仅超管）")(
            cmd_scope)

    def _register_jobs(self) -> None:
        """巡检定时器：固定周期醒一次，是否真巡检由间隔配置决定。"""

        async def tick():
            await patrol_job.tick(self)

        self.schedule(PATROL_TICK, name="leyun_power_patrol")(tick)

    # ------------------------------------------------------------------ 配置读写

    def _int_cfg(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    def account(self) -> tuple:
        """返回 `(手机号, 登录密码)`。"""
        acc = self.config.get("account") or {}
        return (str(acc.get("tel") or "").strip(),
                str(acc.get("password") or "").strip())

    def owner_users(self) -> list:
        return [str(x) for x in (self.config.get("owner_users") or [])]

    def is_owner(self, event) -> bool:
        """是否有权改本插件设置（未认领时放行，认领后仅限名单内）。"""
        owners = self.owner_users()
        if not owners:
            return True
        return str(getattr(event, "user_id", "") or "") in owners

    def claim_owner(self, event) -> None:
        """首次 login 时把操作者记为 owner。"""
        uid = str(getattr(event, "user_id", "") or "")
        if uid and not self.owner_users():
            self.update_config("owner_users", [uid])
            self.logger.info("[leyun_power] 已把 QQ %s 记为本插件 owner", uid)

    def threshold(self, ptype: int) -> float:
        th = self.config.get("threshold") or {}
        try:
            return float(th.get(str(ptype), 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def push_targets(self) -> tuple:
        """返回 `(群号列表, QQ 列表)`。"""
        groups = [str(g) for g in (self.config.get("push_groups") or [])]
        users = [str(u) for u in (self.config.get("push_users") or [])]
        return groups, users

    def update_config(self, key: str, value) -> bool:
        """写回 config.json（失败只记日志，不打断命令）。"""
        try:
            self.config.set(key, value)
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.error("[leyun_power] 写配置失败(%s)：%s", key, e)
            return False

    # ------------------------------------------------------------------ 服务范围

    def _scope(self) -> Scope:
        """服务范围（口径见 `napcat.scope` / docs/plugin-dev/20-scope.md）。

        只看本插件自己的 `binding`：空 = 谁都不用，不回落框架的 `bound_groups`
        （那是「机器人监听哪些群」，空表示监听所有群，与白名单语义相反）。
        """
        return Scope.of(self.config.get("binding"))

    def _scope_lists(self) -> tuple:
        """`(群号集合, QQ 集合)` —— 只给回执/日志展示用，判定一律走 `_scope()`。"""
        scope = self._scope()
        return scope.groups, scope.users

    def check_access(self, event) -> bool:
        """这个调用者能不能用本插件（**与权限等级无关**的那道闸门）。

        | 上下文 | 放行条件 |
        |--------|----------|
        | 群聊 | 来源群 ∈ `binding.groups` |
        | 群小窗 | 来源群 ∈ `binding.groups`（不看 QQ） |
        | 直接私聊 | QQ ∈ `binding.users` |

        超级用户豁免：改名单的 `/leyun scope` 只在私聊由超管执行，不豁免就会
        出现「名单配错 → 谁（包括超管）都改不了」的死锁（框架统一口径）。
        `event is None`（帮助渲染 / 脚本直调）一律放行。
        """
        if event is None:
            return True
        try:
            uid = int(getattr(event, "user_id", 0) or 0)
        except (TypeError, ValueError):
            return False
        if self.perm.is_superuser(uid):
            return True
        return self._scope().event_ok(event)

    def _log_scope_denied(self, event) -> None:
        """记一条「被服务范围挡下」，同一人 60 秒内只记一次（防刷日志）。"""
        if event is None:
            return
        try:
            uid = int(getattr(event, "user_id", 0) or 0)
        except (TypeError, ValueError):
            return
        now = time.time()
        denied = getattr(self, "_scope_denied", None)
        if denied is None:
            denied = self._scope_denied = {}
        last = denied.get(uid, 0.0)
        if now - last < 60.0:
            return
        if len(denied) >= 256:
            denied.clear()
        denied[uid] = now
        source = (f"，来源群 {getattr(event, 'group_id', '')}"
                  if getattr(event, "is_temp_session", False) else "")
        self.logger.info(
            "[leyun_power] 命令被服务范围拒绝：用户 %s%s"
            "（名单见 config.json → plugins.leyun_power.binding）", uid, source)

    def _install_visibility_filter(self) -> None:
        """让「用不了」等于「/help 里看不到」（与 `check_access()` 同一份判定）。

        没有它，范围外的人发 `/help` 仍会看到本命名空间与全部子命令 ——
        功能清单等于公开，实际执行却静默无响应，用户只会以为机器人坏了。
        """
        cmds = self.cmds
        if not hasattr(cmds, "add_visibility_filter"):
            return                            # 老框架没有这个钩子
        self._remove_visibility_filter()

        def _visible(handler, event) -> bool:
            # 只管本插件注册的 handler，别的插件的命令不归我们判
            if getattr(handler, "plugin", "") != self.name:
                return True
            return self.check_access(event)

        self._visibility_filter = _visible
        cmds.add_visibility_filter(_visible)

    def _remove_visibility_filter(self) -> None:
        """摘掉 `/help` 过滤器（禁用 / 卸载 / 重载时按引用精确移除）。"""
        flt = getattr(self, "_visibility_filter", None)
        if flt is None:
            return
        try:
            self.cmds.remove_visibility_filter(flt)
        except Exception:  # noqa: BLE001 - 摘不掉不该挡住卸载
            pass
        self._visibility_filter = None

    # ------------------------------------------------------------------ 文案

    def template(self, key: str) -> str:
        """读文案模板；配置为空（或只有空白）时回落到内置默认值。"""
        fallback = (content.DEFAULT_REMIND if key == "remind_template"
                    else content.DEFAULT_ERROR)
        return str(self.config.get(key) or "").strip() or fallback

    def remind_text(self, room: dict, bill: dict) -> str:
        """按 `remind_template` 渲染一条余额不足提醒。"""
        values = content.remind_values(
            room, bill, query_cmd.room_label(room),
            self.threshold(int(room.get("pay_proj_type") or 0)))
        return content.render(self.template("remind_template"), values,
                              content.DEFAULT_REMIND)

    def error_text(self, errors: list, tip: str = "") -> str:
        """按 `error_template` 渲染一条查询失败提醒。"""
        return content.render(self.template("error_template"),
                              content.error_values(errors, tip),
                              content.DEFAULT_ERROR)

    # ------------------------------------------------------------------ 查询

    def new_client(self, *, with_user: bool = True) -> LeyunClient:
        """构造一个客户端（**不登录**，验证码流程要用未登录态）。

        Args:
            with_user: 是否带上缓存的 `user_id`。发校验码 / 登录时要关掉，
                否则服务端会以为是已登录会话。
        """
        sess = dict(self.config.get("session") or {})
        sess["api_id"] = str(sess.get("api_id") or DEFAULT_API_ID)
        sess["access_token"] = str(sess.get("access_token") or DEFAULT_ACCESS_TOKEN)
        sess["open_id"] = str(sess.get("open_id") or "123")
        sess["user_id"] = (str(sess.get("user_id") or self.state.get("user_id") or "")
                           if with_user else "")
        return LeyunClient(sess, base=str(self.config.get("base_url") or ""),
                           timeout=self._int_cfg("http_timeout", 15, minimum=5))

    async def build_client(self) -> LeyunClient:
        """构造客户端；已配置账号但还缓存不到 user_id 时先登录一次。

        名字**不能**叫 `client`：`PluginBase.__init__` 把框架主客户端赋给
        `self.client`（文档化的框架属性，`ai_chat` 等插件都按属性在用），
        实例属性会遮蔽同名方法，`await self.client()` 就变成「调用一个对象」。
        """
        client = self.new_client()
        tel, pwd = self.account()
        if tel and pwd and not client.user_id:
            await self._login(client, tel, pwd)
        return client

    async def _login(self, client: LeyunClient, tel: str, pwd: str,
                     valid_code: str = "") -> str:
        uid = await client.login(tel, pwd, valid_code=valid_code)
        self.state.set("user_id", uid)
        self.state.save()
        return uid

    def verify_hint(self, errors: list) -> str:
        """查询失败时给用户的下一步提示（校验/密码/未绑定分别指路）。"""
        if any(VERIFY_MARK in str(e) for e in errors):
            return "在私聊里发 /水电 验证 发校验码，再 /水电 验证 <6位码> 提交"
        tel, _ = self.account()
        return ("账号密码已配置，正常会自动重登；若持续失败请重新 /水电 登录"
                if tel else "还没登录账号，私聊发 /水电 登录 <手机号> <密码>")

    async def query_all(self) -> list:
        """查全部房间，返回 `[(room, bill|None, err|None), ...]`。

        鉴权类失败（token 被轮换、缓存过期）会自动重登一次再查。
        """
        rooms = list(self.config.get("rooms") or [])
        if not rooms:
            return []
        try:
            client = await self.build_client()
        except LeyunVerifyRequired as e:
            # 9006：整批都没法查，逐房间标同一个原因，巡检据此提示去做校验
            return [(room, None, "%s：%s" % (VERIFY_MARK, e)) for room in rooms]
        except (LeyunAuthError, LeyunProtocolError) as e:
            return [(room, None, "登录失败：%s" % e) for room in rooms]
        tel, pwd = self.account()
        results, relogged = [], False
        for room in rooms:
            try:
                obj = await client.query_room(room)
            except LeyunAuthError as e:
                if not (tel and pwd) or relogged:
                    results.append((room, None, "鉴权失败：%s" % e))
                    continue
                relogged = True
                try:
                    await self._login(client, tel, pwd)
                    obj = await client.query_room(room)
                except LeyunVerifyRequired as e2:
                    results.append((room, None, "%s：%s" % (VERIFY_MARK, e2)))
                    continue
                except Exception as e2:  # noqa: BLE001
                    results.append((room, None, "重登后仍失败：%s" % e2))
                    continue
            except (LeyunProtocolError, Exception) as e:  # noqa: B014
                results.append((room, None, "查询失败：%s" % e))
                continue
            try:
                results.append((room, parse_room_bill(obj), None))
            except Exception as e:  # noqa: BLE001
                results.append((room, None, "解析失败：%s" % e))
        return results

    # ------------------------------------------------------------------ 推送

    def build_chain(self, text: str, *, with_at: bool = True) -> MessageChain:
        """把文案拼成消息链：`{@全体成员}` / `{@123456}` 变成真正的 at 段。

        at 是消息段而不是文本，直接塞在字符串里只会显示成「{@全体成员}」，
        所以模板渲染完还要再过一遍 `content.segments()`。
        """
        chain = MessageChain()
        source = text if with_at else content.strip_at(text)
        for kind, value in content.segments(source):
            if not value:
                continue
            if kind == "at":
                chain.at(value)
            else:
                chain.text(value)
        return chain

    async def push(self, text: str, event=None) -> int:
        """推送到所有配置目标，返回成功条数。

        Args:
            text: 消息文本（可含 `{@全体成员}` / `{@QQ}`，群里转成 at 段）。
            event: 传了就只回复消息来的地方（命令场景用），否则群发配置目标。
        """
        if event is not None:
            chain = self.build_chain(
                text, with_at=getattr(event, "message_type", "") == "group")
            return 1 if await self._send_to(event, chain) else 0

        groups, users = self.push_targets()
        if not groups and not users:
            # 静默返回 0 会让「为什么没提醒」变成一桩悬案，这里必须留一行日志
            self.logger.warning(
                "[leyun_power] 有提醒要发，但 push_groups / push_users 都是空的，"
                "什么都没发出去")
            return 0
        sent = 0
        for gid in groups:
            try:
                await self.api.send_group_msg(group_id=gid,
                                              message=self.build_chain(text))
                sent += 1
            except Exception as e:  # noqa: BLE001 - 单个目标失败不影响其他
                # @全体成员 需要机器人是群管理员；没权限时整条会发不出去，
                # 这里降级成纯文本再发一次，别让提醒彻底丢了。
                if content.has_at(text):
                    try:
                        await self.api.send_group_msg(
                            group_id=gid, message=self.build_chain(text, with_at=False))
                        sent += 1
                        self.logger.warning(
                            "[leyun_power] 群 %s 发送 @ 失败，已改发纯文本"
                            "（机器人可能不是管理员）：%s", gid, e)
                        continue
                    except Exception as e2:  # noqa: BLE001
                        e = e2
                self.logger.warning("[leyun_power] 推送到群 %s 失败：%s", gid, e)
        for uid in users:
            try:
                # 私聊里 at 段没有意义（部分实现还会直接报错），统一去掉
                await self.api.send_private_msg(
                    user_id=uid, message=self.build_chain(text, with_at=False))
                sent += 1
            except Exception as e:  # noqa: BLE001
                self.logger.warning("[leyun_power] 推送到 %s 失败：%s", uid, e)
        return sent

    async def _send_to(self, event, chain) -> bool:
        """回到「消息来的地方」（群里的小窗要带 source group_id）。"""
        try:
            if getattr(event, "message_type", "") == "group":
                await self.api.send_group_msg(group_id=str(event.group_id),
                                              message=chain)
            else:
                await self.api.send_private_msg(
                    user_id=str(event.user_id), message=chain,
                    group_id=(str(event.group_id)
                              if getattr(event, "is_temp_session", False) else None))
            return True
        except Exception as e:  # noqa: BLE001 - 回复失败不该把命令打挂
            self.logger.warning("[leyun_power] 回复发送失败：%s", e)
            return False
