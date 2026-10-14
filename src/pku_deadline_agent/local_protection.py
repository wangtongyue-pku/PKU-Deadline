"""Current-user Windows DPAPI. No machine-wide flag, pickle or plaintext fallback."""

import ctypes
import os
from ctypes import wintypes


class DataProtection:
    def _crypt(self, value: bytes, *, decrypt=False) -> bytes:
        if os.name != "nt":
            raise RuntimeError("邮件缓存加密需要 Windows DPAPI。")
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(value)
        source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN=1. No CRYPTPROTECT_LOCAL_MACHINE.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise RuntimeError("邮件缓存加解密失败；请使用保存它的 Windows 账户。")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel.LocalFree(output.data)

    def protect(self, value: bytes) -> bytes:
        return self._crypt(value)

    def unprotect(self, value: bytes) -> bytes:
        return self._crypt(value, decrypt=True)
