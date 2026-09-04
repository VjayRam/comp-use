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
