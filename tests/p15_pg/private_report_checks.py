"""Independent OS checks for private report permissions; no application/DB setup."""
import os


def assert_private_report(test, path):
    if os.name != "nt":
        test.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return
    observed = windows_report_security(path)
    test.assertEqual(observed["owner"], observed["process_user"])
    test.assertEqual(observed["control"] & 0x1004, 0x1004)
    test.assertEqual(observed["aces"], [
        {"type": 0, "flags": 0, "mask": 0x1F01FF, "sid": observed["owner"]},
    ])
    test.assertTrue(observed["owner_read_write"])
    test.assertFalse(observed["restricted_read"])
    test.assertFalse(observed["restricted_write"])
    test.assertTrue(observed["permissive_control_read_write"])


def windows_report_security(path):
    """Read the real ACL and ask AccessCheck about normal/restricted tokens.

    A restricted token must pass both the normal and restricting SID checks.
    Restricting it to Everyone excludes the owner's ACE from the second pass.
    A permissive descriptor is the positive control: the same token can access
    it. No account, system ACL, privilege or thread identity is changed.
    """
    import ctypes
    from ctypes import wintypes as w

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", w.LPVOID), ("attributes", w.DWORD)]

    class AclSize(ctypes.Structure):
        _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]

    class Ace(ctypes.Structure):
        _fields_ = [("type", w.BYTE), ("flags", w.BYTE), ("size", w.WORD), ("mask", w.DWORD)]

    class GenericMapping(ctypes.Structure):
        _fields_ = [("read", w.DWORD), ("write", w.DWORD), ("execute", w.DWORD), ("all", w.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    signatures = (
        (kernel.GetCurrentProcess, [], w.HANDLE),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
        (kernel.LocalFree, [w.LPVOID], w.LPVOID),
        (security.GetNamedSecurityInfoW, [w.LPWSTR, ctypes.c_int, w.DWORD,
             ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID),
             w.LPVOID, ctypes.POINTER(w.LPVOID)], w.DWORD),
        (security.GetSecurityDescriptorControl,
             [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.GetAclInformation, [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int], w.BOOL),
        (security.GetAce, [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)], w.BOOL),
        (security.ConvertSidToStringSidW, [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        (security.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (security.GetTokenInformation, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD,
             ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.DuplicateToken, [w.HANDLE, ctypes.c_int, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (security.CreateWellKnownSid, [ctypes.c_int, w.LPVOID, w.LPVOID, ctypes.POINTER(w.DWORD)], w.BOOL),
        (security.CreateRestrictedToken, [w.HANDLE, w.DWORD, w.DWORD, w.LPVOID,
             w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (security.AccessCheck, [w.LPVOID, w.HANDLE, w.DWORD, ctypes.POINTER(GenericMapping),
             w.LPVOID, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.BOOL)], w.BOOL),
        (security.ConvertStringSecurityDescriptorToSecurityDescriptorW,
             [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)], w.BOOL),
    )
    for function, arguments, result in signatures:
        function.argtypes, function.restype = arguments, result

    def require(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def sid_string(sid):
        text = w.LPWSTR()
        require(security.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, w.LPVOID))

    def access(descriptor, token, mask):
        privileges, size = ctypes.create_string_buffer(1024), w.DWORD(1024)
        granted, allowed = w.DWORD(), w.BOOL()
        mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        require(security.AccessCheck(descriptor, token, mask, ctypes.byref(mapping),
                                    privileges, ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed)))
        return bool(allowed.value)

    owner, group, dacl, descriptor, permissive = (w.LPVOID() for _ in range(5))
    process, normal, restricted_source, restricted = (w.HANDLE() for _ in range(4))
    try:
        code = security.GetNamedSecurityInfoW(str(path), 1, 7, ctypes.byref(owner), ctypes.byref(group),
                                             ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        control, revision = w.WORD(), w.DWORD()
        require(security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
        # TOKEN_QUERY | TOKEN_DUPLICATE, followed by a SecurityImpersonation token.
        require(security.OpenProcessToken(kernel.GetCurrentProcess(), 0x000A, ctypes.byref(process)))
        require(security.DuplicateToken(process, 2, ctypes.byref(normal)))
        size = w.DWORD()
        security.GetTokenInformation(process, 1, None, 0, ctypes.byref(size))
        user = ctypes.create_string_buffer(size.value)
        require(security.GetTokenInformation(process, 1, user, len(user), ctypes.byref(size)))
        process_sid = ctypes.cast(user, ctypes.POINTER(SidAndAttributes)).contents.sid
        world, world_size = ctypes.create_string_buffer(68), w.DWORD(68)
        require(security.CreateWellKnownSid(1, None, world, ctypes.byref(world_size)))
        restriction = SidAndAttributes(ctypes.cast(world, w.LPVOID), 0)
        # DuplicateToken's result lacks TOKEN_DUPLICATE. Restrict the original
        # query/duplicate handle first, then make an impersonation token for AccessCheck.
        require(security.CreateRestrictedToken(process, 1, 0, None, 0, None, 1,
                                               ctypes.byref(restriction), ctypes.byref(restricted_source)))
        require(security.DuplicateToken(restricted_source, 2, ctypes.byref(restricted)))
        owner_text, process_text = sid_string(owner), sid_string(process_sid)
        require(security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{owner_text}G:{sid_string(group)}D:P(A;;FA;;;WD)", 1, ctypes.byref(permissive), None))
        entries = []
        if dacl:
            info = AclSize()
            require(security.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2))
            for index in range(info.count):
                pointer = w.LPVOID()
                require(security.GetAce(dacl, index, ctypes.byref(pointer)))
                entry = ctypes.cast(pointer, ctypes.POINTER(Ace)).contents
                entries.append({"type": entry.type, "flags": entry.flags, "mask": entry.mask,
                                "sid": sid_string(w.LPVOID(pointer.value + ctypes.sizeof(Ace)))})
        return {
            "owner": owner_text, "process_user": process_text, "control": control.value, "aces": entries,
            "owner_read_write": access(descriptor, normal, 3),
            "restricted_read": access(descriptor, restricted, 1),
            "restricted_write": access(descriptor, restricted, 2),
            "permissive_control_read_write": access(permissive, restricted, 3),
        }
    finally:
        for token in (restricted, restricted_source, normal, process):
            if token:
                kernel.CloseHandle(token)
        for pointer in (permissive, descriptor):
            if pointer:
                kernel.LocalFree(pointer)
