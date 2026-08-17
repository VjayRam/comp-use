from mock_app.app import create_app


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_new_sub_account_form_renders():
    c = client()
    resp = c.get("/member/12345/sub-account/new")
    assert resp.status_code == 200
    assert b"Deposit Amount" in resp.data


def test_invalid_deposit_amount_shows_validation_error():
    c = client()
    resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "-5"},
    )
    assert resp.status_code == 200
    assert b"Deposit amount must be greater than zero" in resp.data


def test_valid_submission_redirects_to_confirmation():
    c = client()
    resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    assert resp.status_code == 302
    assert "/confirm" in resp.headers["Location"]


def test_confirmation_page_shows_confirmation_number():
    c = client()
    post_resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
        follow_redirects=True,
    )
    assert b"Confirmation Number" in post_resp.data
    assert b"500.00" in post_resp.data
