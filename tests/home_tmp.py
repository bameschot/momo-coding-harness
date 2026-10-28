"""Module fixtures for a test module that builds a Harness without patching HOME:
HOME points at a temp folder for the module, so its sessions, logs and prefs stay
out of the real ~/.momo-harness (session.py resolves them under HOME per call).

    from home_tmp import setUpModule, tearDownModule  # noqa: F401
"""
from __future__ import annotations

import os
import tempfile
from unittest import mock

_active: list[tuple[tempfile.TemporaryDirectory, mock._patch_dict]] = []


def setUpModule():
    tmp = tempfile.TemporaryDirectory()
    patch = mock.patch.dict(os.environ, {"HOME": tmp.name})
    patch.start()
    _active.append((tmp, patch))


def tearDownModule():
    tmp, patch = _active.pop()
    patch.stop()
    tmp.cleanup()
