import os
import subprocess

import process_utils
import pytest


class FakeFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeKernel32:
    def __init__(self, handle=123, exit_code=process_utils.STILL_ACTIVE):
        self.handle = handle
        self.exit_code = exit_code
        self.closed = []
        self.OpenProcess = FakeFunction(lambda *_args: self.handle)

        def get_exit_code(_handle, code_pointer):
            code_pointer._obj.value = self.exit_code
            return 1

        self.GetExitCodeProcess = FakeFunction(get_exit_code)
        self.CloseHandle = FakeFunction(lambda handle: self.closed.append(handle) or 1)


def test_posix_pid_checks(monkeypatch):
    monkeypatch.setattr(process_utils.os, "name", "posix")
    monkeypatch.setattr(process_utils.os, "kill", lambda _pid, _signal: None)

    assert process_utils.pid_is_running(os.getpid()) is True
    assert process_utils.pid_is_running(0) is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [(PermissionError(), True), (ProcessLookupError(), False), (OSError(), False)],
)
def test_posix_pid_errors(monkeypatch, error, expected):
    monkeypatch.setattr(process_utils.os, "name", "posix")

    def fail(_pid, _signal):
        raise error

    monkeypatch.setattr(process_utils.os, "kill", fail)
    assert process_utils.pid_is_running(123) is expected


def test_windows_pid_check_closes_handle(monkeypatch):
    kernel32 = FakeKernel32()
    monkeypatch.setattr(process_utils.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)

    assert process_utils._windows_pid_is_running(123) is True
    assert kernel32.closed == [123]


def test_windows_access_denied_means_process_exists(monkeypatch):
    kernel32 = FakeKernel32(handle=0)
    monkeypatch.setattr(process_utils.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(
        process_utils.ctypes,
        "get_last_error",
        lambda: process_utils.ERROR_ACCESS_DENIED,
        raising=False,
    )

    assert process_utils._windows_pid_is_running(123) is True


def test_platform_process_flags(monkeypatch):
    monkeypatch.setattr(process_utils.os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)

    assert process_utils.managed_process_kwargs() == {"creationflags": 0x200}
    assert process_utils.daemon_process_kwargs() == {"creationflags": 0x208}

    monkeypatch.setattr(process_utils.os, "name", "posix")
    assert process_utils.managed_process_kwargs() == {}
    assert process_utils.daemon_process_kwargs() == {"start_new_session": True}
