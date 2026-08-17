from flask import Flask, redirect, render_template, request, url_for

from mock_app.data import MEMBERS


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

    return app
