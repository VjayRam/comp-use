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


def test_valid_submission_redirects_to_review():
    c = client()
    resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    assert resp.status_code == 302
    assert "/sub-account/review/" in resp.headers["Location"]


def test_review_page_shows_pending_details():
    c = client()
    submit_resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    review_resp = c.get(submit_resp.headers["Location"])
    assert review_resp.status_code == 200
    assert b"Confirm Sub-Account" in review_resp.data
    assert b"500.00" in review_resp.data


def test_confirming_review_creates_account_and_shows_confirmation_number():
    c = client()
    submit_resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    confirm_url = submit_resp.headers["Location"] + "/confirm"
    confirm_resp = c.post(confirm_url, follow_redirects=True)
    assert b"Confirmation Number" in confirm_resp.data
    assert b"500.00" in confirm_resp.data


def test_confirming_an_already_used_token_shows_session_expired_not_a_crash():
    c = client()
    submit_resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    confirm_url = submit_resp.headers["Location"] + "/confirm"
    c.post(confirm_url, follow_redirects=True)  # consumes the token
    second_resp = c.post(confirm_url, follow_redirects=True)  # double-submit
    assert second_resp.status_code == 200
    assert b"Session Expired" in second_resp.data
    assert b"start" in second_resp.data.lower()


def test_reviewing_an_already_used_token_shows_session_expired_not_a_crash():
    c = client()
    submit_resp = c.post(
        "/member/12345/sub-account/new",
        data={"account_type": "Savings", "deposit_amount": "500"},
    )
    review_url = submit_resp.headers["Location"]
    confirm_url = review_url + "/confirm"
    c.post(confirm_url, follow_redirects=True)  # consumes the token
    second_resp = c.get(review_url)  # GET the now-stale review page again
    assert second_resp.status_code == 200
    assert b"Session Expired" in second_resp.data
