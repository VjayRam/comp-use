from flask import Flask, redirect, render_template, request, url_for

from mock_app.data import (
    MEMBERS,
    next_confirmation_number,
    next_sub_account_id,
    next_txn_id,
)


def create_app() -> Flask:
    app = Flask(__name__)

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
        sub_account_id = next_sub_account_id()
        member = MEMBERS[member_id]
        member["accounts"].append(
            {"id": sub_account_id, "type": account_type, "balance": deposit_amount}
        )
        confirmation_number = next_confirmation_number()
        _confirmations[sub_account_id] = {
            "confirmation_number": confirmation_number,
            "deposit_amount": deposit_amount,
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
        if amount <= 0 or from_account is None:
            return render_template(
                "transfer.html",
                member_id=member_id,
                error="Amount must be greater than zero and accounts must be valid.",
            )
        if amount > from_account["balance"]:
            return render_template(
                "transfer.html", member_id=member_id, insufficient_funds=True
            )

        from_account["balance"] -= amount
        to_account = _find_account(member_id, to_account_id)
        if to_account is not None:
            to_account["balance"] += amount

        txn_id = next_txn_id()
        _transfers[txn_id] = {"amount": amount}
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
