"""Tests for tradebot.telegram_bot.singleton — the outbox worker's
single-instance guarantee via flock."""
from __future__ import annotations

import pytest

from tradebot.telegram_bot.singleton import AlreadyRunningError, SingleInstanceLock


def test_acquire_succeeds_when_uncontended(tmp_path):
    lock = SingleInstanceLock(tmp_path / "worker.lock")
    lock.acquire()
    lock.release()  # must not raise


def test_a_second_instance_cannot_acquire_the_same_lock(tmp_path):
    path = tmp_path / "worker.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    try:
        second = SingleInstanceLock(path)
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()


def test_releasing_frees_the_lock_for_the_next_instance(tmp_path):
    path = tmp_path / "worker.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    first.release()

    second = SingleInstanceLock(path)
    second.acquire()  # must not raise — the lock was actually freed
    second.release()


def test_lock_file_records_the_holders_pid(tmp_path):
    import os

    path = tmp_path / "worker.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        assert path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_usable_as_a_context_manager(tmp_path):
    path = tmp_path / "worker.lock"
    with SingleInstanceLock(path):
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path).acquire()
    # released on exit
    with SingleInstanceLock(path):
        pass
