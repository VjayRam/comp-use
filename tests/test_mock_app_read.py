from mock_app.app import create_app


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_search_form_renders():
    c = client()
    resp = c.get("/member/search")
    assert resp.status_code == 200
    assert b"Member ID" in resp.data


def test_search_known_member_redirects_to_detail():
    c = client()
    resp = c.get("/member/search", query_string={"member_id": "12345"})
    assert resp.status_code == 302
    assert "/member/12345" in resp.headers["Location"]


def test_search_unknown_member_shows_not_found():
    c = client()
    resp = c.get("/member/search", query_string={"member_id": "99999"})
    assert resp.status_code == 200
    assert b"No member found" in resp.data


def test_detail_page_shows_name_and_balance():
    c = client()
    resp = c.get("/member/12345")
    assert resp.status_code == 200
    assert b"Jane Doe" in resp.data
    assert b"1500.00" in resp.data
