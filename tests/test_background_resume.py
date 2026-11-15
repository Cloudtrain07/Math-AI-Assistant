import threading
import time

import pytest
from fastapi import HTTPException

from api import BackgroundGenerations, _sse
from core.llm_client import TurnAbort


def test_replay_does_not_restart_and_is_account_scoped():
    manager = BackgroundGenerations()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def factory():
        calls.append(1)
        yield _sse("start", {"message": "q"})
        entered.set()
        release.wait(2)
        yield _sse("done", {"answer": "a"})
    manager.start("alice", "one", factory, TurnAbort(), 0)
    assert entered.wait(2)
    manager.operation("alice", "one", "attach")
    manager.start("alice", "one", factory, TurnAbort(), 0)
    with pytest.raises(HTTPException):
        manager.operation("bob", "one", "attach")
    release.set()
    deadline = time.monotonic() + 2
    while not manager.items["alice"]["finished"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert calls == [1]
    assert len(manager.items["alice"]["events"]) == 2
    manager.clear("alice")
    with pytest.raises(HTTPException):
        manager.start("alice", "one", factory, TurnAbort(), 0)


def test_stop_before_start_prevents_execution():
    manager = BackgroundGenerations()
    manager.operation("alice", "late", "stop")
    with pytest.raises(HTTPException):
        manager.start("alice", "late", lambda: iter(()), TurnAbort(), 0)


def test_clear_before_worker_start_cannot_begin_new_epoch():
    from api import chat_event_stream
    manager = BackgroundGenerations()
    entered = threading.Event()
    calls = []
    class Registry:
        def begin_turn(self, *args):
            calls.append("begin")
    abort = TurnAbort()
    def factory():
        entered.set()
        return chat_event_stream(None, Registry(), "alice", None, "ip", {},
                                 turn_abort=abort, start_lock=manager.lock)
    with manager.lock:
        manager.start("alice", "late", factory, abort, 0)
        assert entered.wait(2)
        manager.clear("alice")
    deadline = time.monotonic() + 2
    while not abort.event.is_set() and time.monotonic() < deadline:
        time.sleep(.01)
    assert abort.event.is_set()
    assert calls == []
