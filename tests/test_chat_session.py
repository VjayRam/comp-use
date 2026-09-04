import threading
import time

from comp_use.chat.session import ChatSessionManager, PendingAction


def test_create_returns_a_session_with_a_unique_id_and_empty_state():
    manager = ChatSessionManager()
    session = manager.create()
    assert session.session_id
    assert session.messages == []
    assert session.target_site is None
    assert session.pending_action is None
    assert session.last_run_id is None


def test_get_returns_the_same_session_object_created_earlier():
    manager = ChatSessionManager()
    created = manager.create()
    fetched = manager.get(created.session_id)
    assert fetched is created


def test_get_returns_none_for_an_unknown_session_id():
    manager = ChatSessionManager()
    assert manager.get("does_not_exist") is None


def test_two_created_sessions_have_distinct_ids():
    manager = ChatSessionManager()
    a = manager.create()
    b = manager.create()
    assert a.session_id != b.session_id


def test_pending_action_holds_invoke_fields():
    action = PendingAction(kind="invoke", capability_name="lookup_member", params={"member_id": "123"})
    assert action.kind == "invoke"
    assert action.params == {"member_id": "123"}
    assert action.goal is None


def test_chat_session_has_a_real_threading_lock():
    session = ChatSessionManager().create()
    assert isinstance(session.lock, type(threading.Lock()))


def test_chat_session_lock_provides_mutual_exclusion_across_threads():
    # Final-review finding: nothing serialized access to a single ChatSession's
    # own mutable state once retrieved - two overlapping requests for the SAME
    # session could both read pending_action, both get "confirm", and both
    # start a run. This proves the lock actually blocks a second thread out of
    # a critical section while the first thread holds it (same style as
    # tests/test_transport.py's real-thread behavior tests).
    session = ChatSessionManager().create()
    events: list[str] = []

    def first():
        with session.lock:
            events.append("first_acquired")
            time.sleep(0.1)
            events.append("first_released")

    def second():
        time.sleep(0.02)  # let `first` acquire the lock first
        events.append("second_waiting")
        with session.lock:
            events.append("second_acquired")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # second must have started waiting before first released the lock, and
    # must only acquire it after first released - proving mutual exclusion.
    assert events.index("second_waiting") < events.index("first_released")
    assert events.index("second_acquired") > events.index("first_released")
