"""The cross-process lock guarding worktree and approval state.

The lock is what keeps two concurrent publishes from both spending the same
approval, so the tests cover the failure modes that would break that: a live
holder must block, and a dead one must not block forever.
"""

import json
import os
import socket
import time

import pytest

from src.agent_worktree.locking import LockBusy, file_lock

pytestmark = pytest.mark.area_security


def test_lock_is_created_and_released(tmp_path):
    path = str(tmp_path / "l" / "state.lock")
    with file_lock(path):
        assert os.path.exists(path)
    assert not os.path.exists(path)


def test_a_live_holder_blocks_a_second_acquirer(tmp_path):
    path = str(tmp_path / "state.lock")
    with file_lock(path):
        with pytest.raises(LockBusy):
            with file_lock(path, timeout_s=0.2):
                pytest.fail("the lock must not be granted twice")


def test_a_dead_holder_is_reclaimed(tmp_path):
    path = str(tmp_path / "state.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # PID 0 is never a live process the app could be running as.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 0, "host": socket.gethostname(), "at": time.time()}, fh)
    with file_lock(path, timeout_s=0.5):
        pass
    assert not os.path.exists(path)


def test_an_ancient_lock_from_another_host_is_reclaimed(tmp_path):
    path = str(tmp_path / "state.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "host": "some-other-host", "at": time.time() - 10_000}, fh)
    with file_lock(path, timeout_s=0.5, stale_after_s=60):
        pass


def test_a_fresh_lock_from_another_host_is_respected(tmp_path):
    path = str(tmp_path / "state.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "host": "some-other-host", "at": time.time()}, fh)
    with pytest.raises(LockBusy):
        with file_lock(path, timeout_s=0.2, stale_after_s=3600):
            pytest.fail("a live remote holder must block")


def test_the_lock_is_released_when_the_body_raises(tmp_path):
    path = str(tmp_path / "state.lock")
    with pytest.raises(ValueError):
        with file_lock(path):
            raise ValueError("boom")
    assert not os.path.exists(path)
    with file_lock(path, timeout_s=0.2):
        pass
