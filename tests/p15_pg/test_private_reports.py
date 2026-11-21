"""Real OS report access controls, independent of PostgreSQL or Django settings."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

from apps.history_reference.private_reports import create_private_report
from tests.p15_pg.private_report_checks import assert_private_report


class PrivateReportTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="p15-private-report-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "private-report.json"

    def test_new_report_is_owner_only_before_writing_and_handle_is_not_inherited(self):
        fd = create_private_report(self.path)
        try:
            self.assertFalse(os.get_inheritable(fd))
            # Inspect permissions while the original handle is still open and empty.
            assert_private_report(self, self.path)
            self.assertEqual(os.fstat(fd).st_size, 0)
            os.write(fd, b'{"synthetic": true}')
        finally:
            os.close(fd)
        self.assertEqual(self.path.read_bytes(), b'{"synthetic": true}')
        assert_private_report(self, self.path)

    def test_existing_report_is_never_overwritten(self):
        fd = create_private_report(self.path)
        os.write(fd, b"keep existing synthetic report")
        os.close(fd)
        with self.assertRaises(FileExistsError):
            create_private_report(self.path)
        self.assertEqual(self.path.read_bytes(), b"keep existing synthetic report")
        assert_private_report(self, self.path)

    def test_missing_parent_is_an_error_without_file_creation(self):
        path = self.path.parent / "missing-directory" / "report.json"
        with self.assertRaises(FileNotFoundError):
            create_private_report(path)
        self.assertFalse(path.exists())

    @skipUnless(os.name == "nt", "native Windows handle/error-path check")
    def test_acl_verification_failure_closes_handle_before_any_report_write(self):
        import ctypes
        from ctypes import wintypes as w
        from apps.history_reference import private_reports

        handles = []

        def reject(handle, *_):
            handles.append(handle)
            raise PermissionError("intentional synthetic ACL verification refusal")

        with patch.object(private_reports, "_check_windows_private_handle", side_effect=reject), \
                patch("msvcrt.open_osfhandle", side_effect=AssertionError("unverified handle exposed")) as expose:
            with self.assertRaisesRegex(PermissionError, "synthetic ACL"):
                create_private_report(self.path)
        self.assertEqual(len(handles), 1)
        expose.assert_not_called()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetHandleInformation.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.GetHandleInformation.restype = w.BOOL
        flags = w.DWORD()
        self.assertFalse(kernel.GetHandleInformation(handles[0], ctypes.byref(flags)))
        self.assertEqual(ctypes.get_last_error(), 6)  # ERROR_INVALID_HANDLE
        self.assertEqual(self.path.read_bytes(), b"")
        assert_private_report(self, self.path)
