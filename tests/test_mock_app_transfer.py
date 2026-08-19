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


def test_transfer_to_nonexistent_account_is_validation_error_not_silent_success():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-999", "amount": "100"},
    )
    assert resp.status_code == 200
    assert b"Amount must be greater than zero" in resp.data
    # the source account's balance must be untouched - no silent debit
    detail_resp = c.get("/member/12345")
    assert b"1500.00" in detail_resp.data  # ACC-001's original balance, unchanged


def test_valid_transfer_redirects_to_review():
    c = client()
    resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    assert resp.status_code == 302
    assert "/transfer/review/" in resp.headers["Location"]


def test_review_page_shows_pending_transfer_details():
    c = client()
    submit_resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    review_resp = c.get(submit_resp.headers["Location"])
    assert review_resp.status_code == 200
    assert b"Confirm Transfer" in review_resp.data
    assert b"100.00" in review_resp.data


def test_confirming_review_commits_transfer_and_shows_txn_id():
    c = client()
    submit_resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    confirm_url = submit_resp.headers["Location"] + "/confirm"
    confirm_resp = c.post(confirm_url, follow_redirects=True)
    assert b"Transaction ID" in confirm_resp.data


def _acc_001_balance(client) -> str:
    import re
    detail_data = client.get("/member/12345").data.decode()
    match = re.search(r"ACC-001</td><td>Checking</td><td>([\d.]+)</td>", detail_data)
    return match.group(1)


def test_confirming_an_already_used_transfer_token_shows_session_expired_not_a_crash():
    c = client()
    submit_resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    confirm_url = submit_resp.headers["Location"] + "/confirm"
    c.post(confirm_url, follow_redirects=True)  # consumes the token
    balance_after_first_confirm = _acc_001_balance(c)
    second_resp = c.post(confirm_url, follow_redirects=True)  # double-submit
    assert second_resp.status_code == 200
    assert b"Session Expired" in second_resp.data
    # the source account must not be double-debited by the second, stale submit
    assert _acc_001_balance(c) == balance_after_first_confirm


def test_reviewing_an_already_used_transfer_token_shows_session_expired_not_a_crash():
    c = client()
    submit_resp = c.post(
        "/member/12345/transfer",
        data={"from_account": "ACC-001", "to_account": "ACC-002", "amount": "100"},
    )
    review_url = submit_resp.headers["Location"]
    confirm_url = review_url + "/confirm"
    c.post(confirm_url, follow_redirects=True)  # consumes the token
    second_resp = c.get(review_url)  # GET the now-stale review page again
    assert second_resp.status_code == 200
    assert b"Session Expired" in second_resp.data
