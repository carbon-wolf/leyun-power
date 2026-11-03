# -*- coding: utf-8 -*-
"""乐云一卡通微信端协议实现（wx.leyunykt.com）。

报文格式（抓包 + 前端 JS 逆向得出，已用真实抓包 26 条响应全量验证）：

    请求: 8位长度前缀 + JSON + "SIGN_VAL" + base64(SHA256withRSA(JSON))
    响应: 8位长度前缀 + 4位返回码(0000=成功) + JSON 或 gzip(压缩JSON) + "SIGN_VAL" + 签名

两个容易踩的点：
  * 长度前缀 = **整个报文去掉前缀自身**的字节数（JSON + "SIGN_VAL" + 签名），
    不是 JSON 长度；写错会得到「非法接入[签名数据为空]」。
  * 大响应走 **HTTP chunked 传输 + 整包 gzip**，`Content-Encoding` 恒为空，
    所以顺序必须是「解 chunk → 解 gzip → 剥长度头 + 返回码」。

签名私钥内嵌在乐云一卡通前端 app.31d08a60f83bc27eed59.js 里（RSA 1024），
属于应用级常量，不随用户/会话变化。
"""
from __future__ import annotations

import base64
import json
import re
import zlib

DEFAULT_BASE = "http://wx.leyunykt.com"

# 应用级凭据（全校共用、长期不变；前端 JS 里可见）
DEFAULT_API_ID = "636972536092526519"
DEFAULT_ACCESS_TOKEN = "6c20d911214a4a28bb4cadf9876379b9"

# pay_proj_type：322 = 缴水费，323 = 缴电费（对接新开普水电控）
PTYPE_WATER = 322
PTYPE_POWER = 323

# 查询失败信息里带上这个前缀，巡检据此提示「去做短信校验」而不是干瞪眼。
VERIFY_MARK = "需短信校验"

# 前端内嵌的报文签名私钥（SHA256withRSA）
RSA_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIICdwIBADANBgkqhkiG9w0BAQEFAASCAmEwggJdAgEAAoGBAKamtwArkzoZVK9w
ji5ewx4beB1xbI98vkOyMWWqeebw3dI2E7vT3RJ/ALkxJpN+KWT6zB/7+zu+mk4B
IQdIyDCc1oGpnait8Q2+ROO31tKP1zthH4g7JHZMdqlFvmTK8m8o0IbKP8xdcmUF
f5qpyRb3qq5cH8kUioPNXo+wMFwDAgMBAAECgYBlqdaBuBN8oe9Th2p1R0HooAFK
lOMRNZwsWAxiZPkwtUfc8ThgoQKAQhIC58UMvS4reAQGjwXYkJI5e97WPsRARycc
Ol3prUGLkkK2aGkAiKGmKN3hLeRO2AfYpY+fso17kkwNcFiDHEtmdnZ2NXcWxwOo
9FTV0dTQgfTds7daYQJBANwusNXKJVIFCva5wmNkF8Wxu8QjTT2TtB1O6HkgZfUi
VP4uhzQB2idOU/ws3Fh/MvIr+TbujCSzmj7LbD82Cd0CQQDBwsOtuez3S0+RYiU6
iCLK872UgdbfGjEwHIE4I1Pho3ILpMq4MSs4bhHlNAeE94tCdqPHpJWXYbFyd/21
vc9fAkEAizN1luPC7nTDtrJABAguw0yXcewDlC+CSpktfg6OhwjahQwzbBzTp8/s
ZtyRvd1E/zfvydvMfBOUK6j92YS3fQJAcHKpwV7PqUzppoQA+xEasBH4igE4RE4d
EcDZNU07Vw3WF+/RPkg6vwsSE6S4YHCRP0c/EZPwxOQLaVU9xoWbnQJBANtXYwiv
o7GkNyz/zK2M7j6o24X3TKt85vqvNTLicZoaWhj9lQcVlDeMMj86fraFbrRZQ+T9
+asbqyQY1RsB4W4=
-----END PRIVATE KEY-----"""

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
       "NetType/WIFI MicroMessenger/7.0.20.1781")

# 登录密码加密参数（前端 chunk 0 的 MGjB 模块，与报文签名无关）
LOGIN_AES_KEY = b"uU4A6IIYKKpsywLi"
LOGIN_AES_IV = b"czaO0594RdKzYwlM"


class LeyunProtocolError(Exception):
    """报文/签名/网络异常。"""


class LeyunAuthError(Exception):
    """鉴权失败（返回码非 0000、登录失败、需要短信校验）。"""


class LeyunVerifyRequired(LeyunAuthError):
    """9006：新设备首次登录需要一次短信校验。

    Attributes:
        tel: 需要校验的手机号。
        valids_s: 校验码有效秒数（服务端给多少用多少，默认 120）。
    """

    def __init__(self, message: str = "", tel: str = "", valids_s: int = 120):
        super().__init__(message or "登录需要短信校验")
        self.tel = tel
        self.valids_s = valids_s


def _text(obj, limit: int = 120) -> str:
    """把响应体压成一行可读文本（dict / list / str 都能吃）。"""
    if isinstance(obj, str):
        return obj[:limit]
    try:
        return json.dumps(obj, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return str(obj)[:limit]


def encrypt_login_pwd(plain: str) -> str:
    """账号登录密码：AES-128-CBC/PKCS7，输出 base64（与前端 aesEncrypt 一致）。"""
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise LeyunProtocolError(
            "缺少依赖 cryptography，请先安装：pip install cryptography")
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(LOGIN_AES_KEY), modes.CBC(LOGIN_AES_IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


_signer_cache = None


def _get_signer():
    """懒加载 cryptography，返回 sign(data:bytes) -> str(b64)。"""
    global _signer_cache
    if _signer_cache is not None:
        return _signer_cache
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise LeyunProtocolError(
            "缺少依赖 cryptography，请先安装：pip install cryptography")
    key = serialization.load_pem_private_key(
        RSA_PRIVATE_KEY_PEM.encode(), password=None)

    def sign(data: bytes) -> str:
        sig = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    _signer_cache = sign
    return sign


def encode_packet(obj: dict) -> bytes:
    """构造请求报文：长度前缀 + JSON + SIGN_VAL + 签名。

    Args:
        obj: 业务请求体（user_id / api_id / ... + 方法参数）。

    Returns:
        可直接作为 POST body 的字节串。
    """
    payload = json.dumps(obj, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    tail = b"SIGN_VAL" + _get_signer()(payload).encode()
    return ("%08d" % (len(payload) + len(tail))).encode() + payload + tail


def _gunzip(buf: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    return d.decompress(buf) + d.flush()


def _strip_framing(b: bytes):
    """剥一层报文头（8 位长度 + 4 位返回码），返回 (retcode, 剩余载荷)。"""
    i = 0
    while i < len(b) and not (0x30 <= b[i] <= 0x39):
        i += 1
    if i + 12 > len(b):
        raise LeyunProtocolError("报文头不完整: %r" % b[:64])
    retcode = b[i + 8:i + 12].decode("ascii", "ignore")
    return retcode, b[i + 12:]


def _dechunk(b: bytes) -> bytes:
    """HTTP chunked 重组；非 chunked 原样返回。"""
    if not re.match(rb'^[0-9a-fA-F]{1,8}\r\n', b):
        return b
    out, i = b"", 0
    while True:
        j = b.find(b"\r\n", i)
        if j < 0:
            return out or b
        try:
            size = int(b[i:j], 16)
        except ValueError:
            return out or b
        if size == 0:
            return out
        out += b[j + 2:j + 2 + size]
        i = j + 2 + size + 2
        if i > len(b):
            return out


def decode_packet(body: bytes):
    """解析响应报文，返回 (retcode: str, obj)。

    小响应是明文 JSON，大响应是 chunked + gzip 包着的同一套报文。
    """
    body = _dechunk(body.lstrip(b"\r\n \t"))
    if body[:2] == b"\x1f\x8b":                 # 整包 gzip：先解压再剥报文头
        body = _gunzip(body)
    retcode, payload = _strip_framing(body)
    end = payload.find(b"SIGN_VAL")
    if end >= 0:
        payload = payload[:end]
    try:
        obj = json.loads(payload.decode("utf-8", "ignore"))
    except Exception as e:
        # 非 JSON 的业务提示（如 9006「检测到更换手机，需校验本人操作」）是纯文本，
        # 直接报“JSON 解析失败”对用户毫无帮助，这里把服务端原话带出来。
        hint = payload.decode("utf-8", "ignore").strip()
        readable = bool(hint) and not any(
            ord(ch) < 32 and ch not in "\r\n\t" for ch in hint)
        if readable:
            raise LeyunProtocolError(
                "服务端返回（%s）：%s" % (retcode, hint[:120])) from e
        raise LeyunProtocolError("响应JSON解析失败: %s / %r" % (e, payload[:120]))
    return retcode, obj


class LeyunClient:
    """乐云一卡通微信端 API 客户端（aiohttp，按需建会话）。

    鉴权只需 `api_id` + `access_token`（应用级），登录一次拿到 `user_id` 即可长期
    复用；`api_state` 与 `ltc_api_token` cookie 实测非必需，留空即可。
    """

    def __init__(self, session: dict, base: str = DEFAULT_BASE, timeout: int = 15):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.api_id = str(session.get("api_id") or DEFAULT_API_ID)
        self.open_id = str(session.get("open_id") or "123")   # 非微信环境用占位值
        self.access_token = str(session.get("access_token") or DEFAULT_ACCESS_TOKEN)
        self.api_state = str(session.get("api_state") or "")
        self.user_id = str(session.get("user_id") or "")
        self.ltc_api_token = str(session.get("ltc_api_token") or "")

    def _check(self, need_user: bool = True) -> None:
        missing = []
        if not self.api_id:
            missing.append("api_id")
        if not self.access_token:
            missing.append("access_token")
        if need_user and not self.user_id:
            missing.append("user_id(或配置账号密码以便自动登录)")
        if missing:
            raise LeyunAuthError("缺少必要参数: %s" % ", ".join(missing))

    async def _post(self, method: str, body: dict):
        import aiohttp
        url = ("%s/TP_API/DispApiServer/%s?DirectProcessData=true"
               "&DisablePacketHeader=false&PacketHeaderMode=1&PacketVer=2"
               % (self.base, method))
        headers = {
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base,
            "Referer": self.base + "/MobileWeb/YKT/index.html",
        }
        if self.ltc_api_token:
            headers["Cookie"] = "ltc_api_token=" + self.ltc_api_token
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout)) as sess:
                async with sess.post(url, data=encode_packet(body),
                                     headers=headers) as resp:
                    raw = await resp.read()
        except aiohttp.ClientError as e:
            raise LeyunProtocolError("网络请求失败: %s" % e)
        return decode_packet(raw)

    def _base_body(self) -> dict:
        """所有接口共用的报文骨架（不含业务参数）。"""
        return {
            "user_id": "",
            "api_id": self.api_id,
            "open_id": self.open_id,
            "access_token": self.access_token,
            "api_state": self.api_state,
            "menu_tag": "0",
        }

    async def send_login_code(self, tel: str) -> int:
        """发「换设备登录校验」短信（YKTUserLoginSVC），返回有效秒数。

        Raises:
            LeyunAuthError: 手机号为空 / 服务端拒绝（如频繁发送、号码未绑定）。
        """
        self._check(need_user=False)
        tel = str(tel or "").strip()
        if not tel:
            raise LeyunAuthError("未配置登录手机号")
        body = self._base_body()
        body["tel"] = tel
        retcode, obj = await self._post("YKTUserLoginSVC", body)
        if retcode != "0000":
            raise LeyunAuthError("发送校验码失败(%s)：%s" % (retcode, _text(obj)))
        seconds = 120
        if isinstance(obj, dict):
            for key in ("valids_s", "valids_sec", "valid_s", "valid_time"):
                value = obj.get(key)
                if value:
                    try:
                        seconds = int(value)
                    except (TypeError, ValueError):
                        pass
                    break
        return max(1, seconds)

    async def login(self, tel: str, password: str, valid_code: str = "") -> str:
        """手机号 + 登录密码登录，成功后写入并返回 `user_id`。

        Args:
            tel: 账号绑定的手机号。
            password: 登录密码明文（内部按前端算法 AES 加密）。
            valid_code: 短信校验码。首次在新设备登录会拿到 9006，
                此时先用 `send_login_code()` 发码，再带上校验码调本方法。

        Raises:
            LeyunVerifyRequired: 9006，需要（或校验码未通过）短信校验。
            LeyunAuthError: 密码错误、账号不存在等。
        """
        self._check(need_user=False)
        if not tel or not password:
            raise LeyunAuthError("未配置登录手机号/密码")
        code = str(valid_code or "").strip()
        body = self._base_body()
        body.update({
            "tel": tel,
            "login_pwd": encrypt_login_pwd(password),
            "valid_code": code,
        })
        retcode, obj = await self._post("YKTUserLogin", body)
        if retcode == "9006":
            if code:
                raise LeyunVerifyRequired(
                    "校验码不正确或已过期，请重新 /水电 验证", tel=tel)
            raise LeyunVerifyRequired(
                "新设备登录需要短信校验：发 /水电 验证 获取校验码", tel=tel)
        if retcode != "0000":
            raise LeyunAuthError("登录失败(%s): %s" % (retcode, _text(obj)))
        if not isinstance(obj, dict) or not obj.get("user_id"):
            raise LeyunAuthError("登录响应异常，未返回 user_id")
        self.user_id = str(obj["user_id"])
        return self.user_id

    async def call(self, method: str, extra: dict = None) -> dict:
        """调用业务接口，返回响应对象；返回码非 0000 抛 `LeyunAuthError`。"""
        self._check()
        body = {
            "user_id": self.user_id,
            "api_id": self.api_id,
            "open_id": self.open_id,
            "access_token": self.access_token,
            "api_state": self.api_state,
            "menu_tag": "0",
        }
        if extra:
            body.update(extra)
        retcode, obj = await self._post(method, body)
        if retcode != "0000":
            raise LeyunAuthError("接口返回非0000(%s): %s" % (retcode, str(obj)[:120]))
        return obj

    async def query_room(self, room: dict):
        """查一个房间的水电单据（YKTGetPayProj），返回原始响应对象。"""
        extra = {"pay_proj_type": int(room["pay_proj_type"])}
        for k in ("input1", "input2", "input3", "input4", "input5"):
            if room.get(k):
                extra[k] = room[k]
        return await self.call("YKTGetPayProj", extra)


def parse_room_bill(obj) -> dict:
    """把 YKTGetPayProj 的响应整理成易读字典（金额内部单位是「分」）。"""
    if not isinstance(obj, list) or not obj:
        raise LeyunProtocolError("缴费单据为空（房间可能未安装水电表）")
    item = obj[0]
    s_items = item.get("s_items") or []
    return {
        "proj_id": item.get("pay_proj_id", ""),
        "name": item.get("pay_proj_name", ""),
        "surplus_qty": (item.get("surplus_qty") or 0) / 100.0,   # 度 / 吨
        "surplus_amt": (item.get("surplus_amt") or 0) / 100.0,   # 元
        "unit_price": (item.get("unit_price") or 0) / 100.0,     # 元 / 度（吨）
        "details": ["%s%s" % (s.get("name", "").strip(), s.get("val", ""))
                    for s in s_items if s.get("name") or s.get("val")],
    }
