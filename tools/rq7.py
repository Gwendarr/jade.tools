# -*- coding: utf-8 -*-

from flask import Blueprint, render_template, redirect, url_for

import core

bp = Blueprint("rq7", __name__)


@bp.route("/rq7/")
def page():
    if core.get_settings().get("seen"):
        return redirect(url_for("index"))
    return render_template("rq7.html")
