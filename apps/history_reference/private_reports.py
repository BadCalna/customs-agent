"""Exclusive audit reports: POSIX 0600 or a protected Windows owner-only DACL.

Windows chmod/mode bits do not restrict readers. Install the DACL in CreateFileW
itself, before a report exists, and verify it before exposing a writable fd.
"""
import os


def create_private_report(path):
    """Return a new writable fd; refuse existing paths and insecure filesystems."""
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return _create_windows_report(os.fsdecode(path))


def _windows_api():
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    signatures = (
        (kernel.GetCurrentProcess, [], w.HANDLE),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
        (kernel.LocalFree, [w.LPVOID], w.LPVOID),
        (kernel.CreateFileW, [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                              w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        (security.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (security.GetTokenInformation, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD,
                                        ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.ConvertSidToStringSidW, [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        (security.ConvertStringSecurityDescriptorToSecurityDescriptorW,
         [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.GetSecurityInfo, [w.HANDLE, ctypes.c_int, w.DWORD,
                                   ctypes.POINTER(w.LPVOID), w.LPVOID,
                                   ctypes.POINTER(w.LPVOID), w.LPVOID,
                                   ctypes.POINTER(w.LPVOID)], w.DWORD),
        (security.GetSecurityDescriptorControl,
         [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.GetAclInformation, [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int], w.BOOL),
        (security.GetAce, [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)], w.BOOL),
        (security.EqualSid, [w.LPVOID, w.LPVOID], w.BOOL),
    )
    for function, arguments, result in signatures:
        function.argtypes, function.restype = arguments, result
    return kernel, security


def _check_windows_private_handle(handle, user_sid, security, kernel):
    """Fail closed if the filesystem ignored the requested security descriptor."""
    import ctypes
    from ctypes import wintypes as w

    class AclSize(ctypes.Structure):
        _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]

    class AllowedAce(ctypes.Structure):
        _fields_ = [("kind", w.BYTE), ("flags", w.BYTE), ("size", w.WORD), ("mask", w.DWORD)]

    owner, dacl, descriptor = w.LPVOID(), w.LPVOID(), w.LPVOID()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    result = security.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                      ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        control, revision = w.WORD(), w.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        # SE_DACL_PRESENT | SE_DACL_PROTECTED; no inherited or additional grants.
        if not dacl or control.value & 0x1004 != 0x1004 or not security.EqualSid(owner, user_sid):
            raise PermissionError("Private report requires a protected owner-only Windows DACL")
        info, ace = AclSize(), w.LPVOID()
        if not security.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.count != 1:
            raise PermissionError("Private report DACL contains unexpected grants")
        if not security.GetAce(dacl, 0, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        entry = ctypes.cast(ace, ctypes.POINTER(AllowedAce)).contents
        ace_sid = w.LPVOID(ace.value + ctypes.sizeof(AllowedAce))
        if (entry.kind != 0 or entry.flags != 0 or entry.mask != 0x1F01FF
                or not security.EqualSid(ace_sid, user_sid)):
            raise PermissionError("Private report DACL must grant only its owner full control")
    finally:
        kernel.LocalFree(descriptor)


def _create_windows_report(path):
    import ctypes
    from ctypes import wintypes as w
    import msvcrt

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", w.LPVOID), ("attributes", w.DWORD)]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]

    kernel, security = _windows_api()
    token, descriptor, sid_text = w.HANDLE(), w.LPVOID(), w.LPWSTR()
    handle = None
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        needed = w.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        if ctypes.get_last_error() != 122 or not needed.value:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 1, user, len(user), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = ctypes.cast(user, ctypes.POINTER(SidAndAttributes)).contents.sid
        if not security.ConvertSidToStringSidW(user_sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        # P protects this DACL from directory inheritance. Only TokenUser has an ACE.
        sddl = f"O:{sid_text.value}D:P(A;;FA;;;{sid_text.value})"
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # GENERIC_WRITE | READ_CONTROL, CREATE_NEW, no sharing or handle inheritance.
        handle = kernel.CreateFileW(path, 0x40020000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        _check_windows_private_handle(handle, user_sid, security, kernel)
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT)
        handle = None  # The fd now owns the Win32 handle.
        return fd
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, w.LPVOID))
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)
