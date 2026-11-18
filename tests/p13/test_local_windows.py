from unittest import TestCase
from unittest.mock import patch

from scripts.queue import local_windows


class LocalWindowsRedisTests(TestCase):
    def test_stop_refuses_foreign_listener(self):
        with patch.object(local_windows, "listening", return_value=True), \
             patch.object(local_windows, "redis_owned", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "拒绝关闭"):
                local_windows.stop()

    def test_pid_file_alone_does_not_prove_port_ownership(self):
        with patch.object(local_windows, "_read_pid", return_value=42), \
             patch.object(local_windows, "_listener_pid", return_value=43), \
             patch.object(local_windows, "_process_command_line") as command:
            self.assertFalse(local_windows.redis_owned())
        command.assert_not_called()
