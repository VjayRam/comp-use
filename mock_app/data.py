import uuid


def new_review_token() -> str:
    return uuid.uuid4().hex[:8]


MEMBERS: dict[str, dict] = {
    "12345": {
        "name": "Jane Doe",
        "accounts": [
            {"id": "ACC-001", "type": "Checking", "balance": 1500.00},
            {"id": "ACC-002", "type": "Savings", "balance": 8200.50},
        ],
    },
    "67890": {
        "name": "John Smith",
        "accounts": [
            {"id": "ACC-101", "type": "Checking", "balance": 320.75},
        ],
    },
}

_sub_account_counter = {"n": 0}
_confirmation_counter = {"n": 0}


def next_sub_account_id() -> str:
    _sub_account_counter["n"] += 1
    return f"SUB-{_sub_account_counter['n']:04d}"


def next_confirmation_number() -> str:
    _confirmation_counter["n"] += 1
    return f"CONF-{_confirmation_counter['n']:06d}"


_txn_counter = {"n": 0}


def next_txn_id() -> str:
    _txn_counter["n"] += 1
    return f"TXN-{_txn_counter['n']:06d}"
