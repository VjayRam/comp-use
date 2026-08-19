from flask import Flask, redirect, render_template, request, url_for

from mock_app.data import (
    MEMBERS,
    new_review_token,
    next_confirmation_number,
    next_sub_account_id,
    next_txn_id,
)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/widgets/ticker")
    def widgets_ticker():
        return "<html><body><p>Savings APY: 0.10% | 12mo CD: 1.25%</p></body></html>"

    @app.get("/member/search")
    def member_search():
        member_id = request.args.get("member_id")
        if member_id is None:
            return render_template("search.html")
        if member_id not in MEMBERS:
            return render_template("search.html", not_found=True)
        return redirect(url_for("member_detail", member_id=member_id))

    @app.get("/member/<member_id>")
    def member_detail(member_id: str):
        member = MEMBERS[member_id]
        return render_template("detail.html", member=member, member_id=member_id)

    _confirmations: dict[str, dict] = {}
    _pending_sub_accounts: dict[str, dict] = {}

    @app.get("/member/<member_id>/sub-account/new")
    def new_sub_account_form(member_id: str):
        return render_template("new_sub_account.html", member_id=member_id)

    @app.post("/member/<member_id>/sub-account/new")
    def new_sub_account_submit(member_id: str):
        account_type = request.form.get("account_type", "Savings")
        try:
            deposit_amount = float(request.form.get("deposit_amount", "0"))
        except ValueError:
            deposit_amount = -1
        if deposit_amount <= 0:
            return render_template(
                "new_sub_account.html",
                member_id=member_id,
                error="Deposit amount must be greater than zero.",
            )
        token = new_review_token()
        _pending_sub_accounts[token] = {
            "account_type": account_type,
            "deposit_amount": deposit_amount,
        }
        return redirect(
            url_for("sub_account_review", member_id=member_id, token=token)
        )

    @app.get("/member/<member_id>/sub-account/review/<token>")
    def sub_account_review(member_id: str, token: str):
        pending = _pending_sub_accounts[token]
        return render_template(
            "sub_account_review.html",
            member_id=member_id,
            token=token,
            account_type=pending["account_type"],
            deposit_amount=pending["deposit_amount"],
        )

    @app.post("/member/<member_id>/sub-account/review/<token>/confirm")
    def sub_account_review_confirm(member_id: str, token: str):
        pending = _pending_sub_accounts.pop(token)
        sub_account_id = next_sub_account_id()
        member = MEMBERS[member_id]
        member["accounts"].append(
            {
                "id": sub_account_id,
                "type": pending["account_type"],
                "balance": pending["deposit_amount"],
            }
        )
        confirmation_number = next_confirmation_number()
        _confirmations[sub_account_id] = {
            "confirmation_number": confirmation_number,
            "deposit_amount": pending["deposit_amount"],
        }
        return redirect(
            url_for(
                "sub_account_confirmation",
                member_id=member_id,
                sub_account_id=sub_account_id,
            )
        )

    @app.get("/member/<member_id>/sub-account/<sub_account_id>/confirm")
    def sub_account_confirmation(member_id: str, sub_account_id: str):
        info = _confirmations[sub_account_id]
        return render_template(
            "sub_account_confirmation.html",
            sub_account_id=sub_account_id,
            confirmation_number=info["confirmation_number"],
            deposit_amount=info["deposit_amount"],
        )

    _transfers: dict[str, dict] = {}
    _pending_transfers: dict[str, dict] = {}

    def _find_account(member_id: str, account_id: str) -> dict | None:
        for acc in MEMBERS[member_id]["accounts"]:
            if acc["id"] == account_id:
                return acc
        return None

    @app.get("/member/<member_id>/transfer")
    def transfer_form(member_id: str):
        return render_template("transfer.html", member_id=member_id)

    @app.post("/member/<member_id>/transfer")
    def transfer_submit(member_id: str):
        from_account_id = request.form.get("from_account", "")
        to_account_id = request.form.get("to_account", "")
        try:
            amount = float(request.form.get("amount", "0"))
        except ValueError:
            amount = -1

        from_account = _find_account(member_id, from_account_id)
        to_account = _find_account(member_id, to_account_id)
        if amount <= 0 or from_account is None or to_account is None:
            return render_template(
                "transfer.html",
                member_id=member_id,
                error="Amount must be greater than zero and accounts must be valid.",
            )
        if amount > from_account["balance"]:
            return render_template(
                "transfer.html", member_id=member_id, insufficient_funds=True
            )

        token = new_review_token()
        _pending_transfers[token] = {
            "from_account": from_account_id,
            "to_account": to_account_id,
            "amount": amount,
        }
        return redirect(url_for("transfer_review", member_id=member_id, token=token))

    @app.get("/member/<member_id>/transfer/review/<token>")
    def transfer_review(member_id: str, token: str):
        pending = _pending_transfers[token]
        return render_template(
            "transfer_review.html",
            member_id=member_id,
            token=token,
            from_account=pending["from_account"],
            to_account=pending["to_account"],
            amount=pending["amount"],
        )

    @app.post("/member/<member_id>/transfer/review/<token>/confirm")
    def transfer_review_confirm(member_id: str, token: str):
        pending = _pending_transfers.pop(token)
        from_account = _find_account(member_id, pending["from_account"])
        to_account = _find_account(member_id, pending["to_account"])
        from_account["balance"] -= pending["amount"]
        if to_account is not None:
            to_account["balance"] += pending["amount"]

        txn_id = next_txn_id()
        _transfers[txn_id] = {"amount": pending["amount"]}
        return redirect(
            url_for("transfer_confirmation", member_id=member_id, txn_id=txn_id)
        )

    @app.get("/member/<member_id>/transfer/<txn_id>/confirm")
    def transfer_confirmation(member_id: str, txn_id: str):
        info = _transfers[txn_id]
        return render_template(
            "transfer_confirmation.html", txn_id=txn_id, amount=info["amount"]
        )

    return app
