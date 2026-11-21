"""Real directory redirections for traversal tests, without Windows symlink privilege."""
from pathlib import Path
import os


def create_directory_link(link: Path, target: Path) -> None:
    link, target = Path(link).absolute(), Path(target).resolve(strict=True)
    if not target.is_dir():
        raise ValueError("Directory link target must be a real test directory")
    if os.name != "nt":
        os.symlink(target, link, target_is_directory=True)
        return
    import ctypes
    from ctypes import wintypes
    import struct

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    substitute = ("\\??\\" + str(target)).encode("utf-16le")
    printable = str(target).encode("utf-16le")
    data = (struct.pack("<IHHHHHH", 0xA0000003, 8 + len(substitute) + len(printable) + 4,
                        0, 0, len(substitute), len(substitute) + 2, len(printable))
            + substitute + b"\0\0" + printable + b"\0\0")
    link.mkdir()
    handle = kernel.CreateFileW(str(link), 0x40000000, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        link.rmdir()
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x900A4, data, len(data), None, 0,
                                      ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(handle)
        link.rmdir()
        raise
    else:
        kernel.CloseHandle(handle)
