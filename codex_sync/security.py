from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def make_blob(content: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(content)
    blob = DataBlob(
        len(content),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


class WindowsDpapiProtector:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is only available on Windows")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def protect(self, plaintext: bytes) -> bytes:
        input_blob, input_buffer = make_blob(plaintext)
        output_blob = DataBlob()
        succeeded = self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Codex History Sync",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        if not succeeded:
            raise ctypes.WinError()
        return self._copy_and_free(output_blob)

    def unprotect(self, protected: bytes) -> bytes:
        input_blob, input_buffer = make_blob(protected)
        output_blob = DataBlob()
        description = wintypes.LPWSTR()
        succeeded = self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        if not succeeded:
            raise ctypes.WinError()
        if description:
            self._kernel32.LocalFree(description)
        return self._copy_and_free(output_blob)

    def _copy_and_free(self, output_blob: DataBlob) -> bytes:
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self._kernel32.LocalFree(output_blob.pbData)


def default_secret_protector() -> SecretProtector:
    return WindowsDpapiProtector()
