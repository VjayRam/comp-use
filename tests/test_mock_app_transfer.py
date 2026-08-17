from mock_app.app import create_app


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_transfer_form_renders():
    c = client()
    resp = c.get("/member/12345/transfer")
    assert resp.status_code == 200
    assert b"From Account" in resp.data


def test_transfer_missing_amount_is_validation_error():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "0"},
    )
    assert resp.status_code == 200
    assert b"Amount must be greater than zero" in resp.data


def test_transfer_exceeding_balance_is_business_outcome():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "999999"},
    )
    assert resp.status_code == 200
    assert b"Insufficient funds" in resp.data


def test_valid_transfer_redirects_to_confirmation():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    assert resp.status_code == 302
    assert "/confirm" in resp.headers["Location"]


def test_transfer_confirmation_shows_txn_id():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
        follow_redirects=True,
    )
    assert b"Transaction ID" in resp.data
