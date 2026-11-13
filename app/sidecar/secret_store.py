# -*- coding: utf-8 -*-
"""本地密钥存储：Windows DPAPI 加密 + 脱敏显示。

用于通知通道密钥（SMTP 授权码、Webhook key、Bark device key）与模型 API Key：
* 明文只在进程内存中出现，落库的是 ``dpapi:<base64>``；
* DPAPI 绑定「当前 Windows 用户 + 本机」，换用户/换机后解不开 → 抛
  :class:`SecretError`，调用方提示用户重新填写（符合本地软件的安全语义）；
* 非 Windows（开发/CI 的 Linux）降级为 ``b64:<base64>``，仅用于测试，**不是加密**；
* 任何情况下都不得把明文写日志、写 outbox 或返回给渲染进程。
"""
from __future__ import annotations

import base64
import logging
import sys

log = logging.getLogger("sidecar.secret_store")

DPAPI_PREFIX = "dpapi:"
PLAIN_PREFIX = "b64:"  # 仅非 Windows 开发环境兜底（无加密）


class SecretError(RuntimeError):
    """密钥加解密失败（通常是换 Windows 用户/换机，或密文损坏）。"""


# ---------------------------------------------------------------------------
# Windows DPAPI（ctypes，标准库，无第三方依赖）
# ---------------------------------------------------------------------------
_DESCRIPTION = "OpinionLens local secret"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if _dpapi_available():
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob_from_bytes(data: bytes) -> "_DataBlob":
        buffer = ctypes.create_string_buffer(data, len(data))
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    def _dpapi_protect(data: bytes) -> bytes:
        blob_in = _blob_from_bytes(data)
        blob_out = _DataBlob()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        ok = _crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            _DESCRIPTION,
            None,
            None,
            None,
            flags,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise SecretError(f"DPAPI 加密失败（winerror={ctypes.get_last_error()}）")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

    def _dpapi_unprotect(data: bytes) -> bytes:
        blob_in = _blob_from_bytes(data)
        blob_out = _DataBlob()
        flags = 0x1
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            flags,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise SecretError(
                f"DPAPI 解密失败（winerror={ctypes.get_last_error()}）；"
                "换 Windows 用户/换机后需重新填写密钥"
            )
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def protect(plain: str) -> str:
    """明文 → 可落库密文；空字符串返回空字符串。"""
    if plain is None:
        return ""
    raw = str(plain)
    if not raw:
        return ""
    data = raw.encode("utf-8")
    if _dpapi_available():
        return DPAPI_PREFIX + base64.b64encode(_dpapi_protect(data)).decode("ascii")
    log.warning("当前平台非 Windows，密钥降级为 base64 存储（仅开发/测试环境）")
    return PLAIN_PREFIX + base64.b64encode(data).decode("ascii")


def unprotect(blob: str) -> str:
    """密文 → 明文；失败抛 :class:`SecretError`。"""
    if not blob:
        return ""
    text = str(blob)
    try:
        if text.startswith(DPAPI_PREFIX):
            data = base64.b64decode(text[len(DPAPI_PREFIX):].encode("ascii"))
            if not _dpapi_available():
                raise SecretError("密文由 Windows DPAPI 加密，当前平台无法解密")
            return _dpapi_unprotect(data).decode("utf-8")
        if text.startswith(PLAIN_PREFIX):
            return base64.b64decode(text[len(PLAIN_PREFIX):].encode("ascii")).decode("utf-8")
    except SecretError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转 SecretError
        raise SecretError(f"密钥解密失败：{exc}") from exc
    raise SecretError("无法识别的密钥格式（请重新填写）")


def mask_secret(value: str) -> str:
    """脱敏显示：只露末尾 4 位，例如 ``****a1b2``。"""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]
