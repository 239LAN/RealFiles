"""认证路由：注册/登录/登出/改密码"""
import threading

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session, current_app,
)
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db
from app.models import User
from app.utils.helpers import safe_filename

auth_bp = Blueprint("auth", __name__)

# ---- 登录失败限流（进程内，按 IP） ----
LOGIN_MAX_FAIL = 10
LOGIN_LOCK_SECONDS = 10 * 60
_login_guard = {}  # ip -> [fail_count, first_fail_ts]


def _login_lock_remaining(ip: str) -> int:
    """返回剩余锁定秒数；0 表示未锁定"""
    from app.utils.helpers import utcnow
    rec = _login_guard.get(ip)
    if not rec:
        return 0
    cnt, first = rec
    if cnt < LOGIN_MAX_FAIL:
        return 0
    remaining = LOGIN_LOCK_SECONDS - (utcnow().timestamp() - first)
    if remaining <= 0:
        _login_guard.pop(ip, None)
        return 0
    return int(remaining)


def _record_login_fail(ip: str):
    from app.utils.helpers import utcnow
    # 表容量上限，防止大量不同 IP 造成内存膨胀
    if len(_login_guard) >= 10000:
        now0 = utcnow().timestamp()
        for k in [k for k, v in _login_guard.items()
                  if now0 - v[1] > LOGIN_LOCK_SECONDS]:
            _login_guard.pop(k, None)
        if len(_login_guard) >= 10000:
            _login_guard.clear()
    now = utcnow().timestamp()
    rec = _login_guard.get(ip) or [0, now]
    rec[0] += 1
    rec[1] = rec[1] if rec[0] > 1 else now
    _login_guard[ip] = rec


def _check_update_on_login(user):
    """管理员登录时后台检查一次更新（仅检查不安装，不阻塞登录、失败静默）"""
    if not user.is_admin or not current_app.config.get("UPDATE_CHECK_ON_LOGIN"):
        return
    from app.services import update_service

    def run():
        try:
            update_service.check(apply_update=False)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True, name="admin-login-update").start()


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("files.index"))
    if not current_app.config["ALLOW_REGISTER"]:
        flash("本站已关闭注册，请联系管理员开通账号", "warning")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip() or None
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        agree = request.form.get("agree")

        error = None
        if not agree:
            error = "请先阅读并同意《用户协议》《隐私政策》及《开源软件授权许可证》"
        elif not username or len(username) < 3:
            error = "用户名至少 3 个字符"
        elif len(password) < 6:
            error = "密码至少 6 个字符"
        elif password != confirm:
            error = "两次密码不一致"
        elif User.query.filter_by(username=username).first():
            error = "用户名已存在"
        elif email and User.query.filter_by(email=email).first():
            error = "邮箱已被注册"

        if error:
            flash(error, "danger")
            return render_template("auth/register.html", username=username, email=email)

        # 新用户默认免费版
        from app.models import MembershipPlan
        free = MembershipPlan.query.filter_by(name="free").first()
        user = User(username=username, email=email, plan_id=free.id if free else None)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("注册成功，请登录", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("files.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        agree = request.form.get("agree")
        ip = request.remote_addr or ""
        locked = _login_lock_remaining(ip)
        if locked:
            flash(f"登录失败次数过多，请 {locked // 60 + 1} 分钟后再试", "danger")
            return render_template("auth/login.html")
        user = User.query.filter_by(username=username).first()
        if not agree:
            flash("请先阅读并同意《用户协议》《隐私政策》及《开源软件授权许可证》", "danger")
        elif user is None or not user.check_password(password):
            _record_login_fail(ip)
            flash("用户名或密码错误", "danger")
        elif not user.is_active:
            flash("账号已被禁用", "danger")
        else:
            _login_guard.pop(ip, None)
            login_user(user, remember=bool(request.form.get("remember")))
            session.pop("admin_view_uid", None)
            _check_update_on_login(user)
            flash("登录成功", "success")
            # 仅允许跳转站内相对路径
            next_url = request.args.get("next") or ""
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("files.index"))
    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("admin_view_uid", None)
    flash("已退出登录", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm") or ""
        if not current_user.check_password(old):
            flash("原密码错误", "danger")
        elif len(new) < 6:
            flash("新密码至少 6 个字符", "danger")
        elif new != confirm:
            flash("两次密码不一致", "danger")
        else:
            current_user.set_password(new)
            db.session.commit()
            flash("密码修改成功", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/change_password.html")
