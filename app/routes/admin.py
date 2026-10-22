"""管理后台路由：用户管理、套餐/叠加包维护、兑换码、设置"""
import subprocess
from datetime import timedelta
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, session,
    current_app, jsonify,
)
from flask_login import login_required, current_user

from config import FEATURE_KEYS, HSTS_MAX_AGE, load_site_config_raw, save_site_config
from app.extensions import db
from app.models import (
    User, MembershipPlan, AddonPackage, File, ShareLink, BalanceLog,
    UserMembership, UserAddon, PurchaseOrder, RechargeRecord,
    UploadSession, AuditLog, RemoteDownload, RedeemCode, RedeemRecord,
    StoragePoint,
)
from app.services import billing_service, file_service, storage_service, update_service
from app.services.billing_service import BillingError
from app.services.quota_service import effective_quota
from app.utils.helpers import admin_required, parse_duration, utcnow

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

GB = 1024 ** 3
MB = 1024 ** 2
CODE_PAGE_SIZE = 200


def _guard_redirect():
    """管理页统一回跳目标（仅允许站内地址，防开放重定向）"""
    from urllib.parse import urlsplit
    ref = request.referrer or ""
    if ref:
        try:
            parts = urlsplit(ref)
            if not parts.netloc or parts.netloc == request.host:
                return ref
        except ValueError:
            pass
    return url_for("admin.users_page")


@admin_bp.before_request
@login_required
@admin_required
def _guard():
    pass


# ---------- 概览 ----------

def _dashboard_stats():
    now = utcnow()
    plan_ids = {p.id for p in MembershipPlan.query.all()}
    dist = {}
    for plan_id, cnt in db.session.query(User.plan_id, db.func.count()).group_by(User.plan_id).all():
        dist[plan_id if plan_id in plan_ids else None] = cnt

    storage_used, file_count = db.session.query(
        db.func.coalesce(db.func.sum(File.size), 0), db.func.count(File.id)
    ).filter(File.is_dir == False).first()  # noqa: E712

    return {
        "total_users": User.query.count(),
        "active_users": User.query.filter_by(is_active=True).count(),
        "admin_users": User.query.filter_by(is_admin=True).count(),
        "disabled_users": User.query.filter_by(is_active=False).count(),
        "locked_users": User.query.filter_by(storage_locked=True).count(),
        "expiring_soon": User.query.filter(
            User.vip_expire_at > now,
            User.vip_expire_at <= now + timedelta(days=7)).count(),
        "storage_used": int(storage_used),
        "file_count": file_count,
        "dir_count": File.query.filter_by(is_dir=True).count(),
        "share_count": ShareLink.query.count(),
        "plan_dist": dist,
        "recharge_today": db.session.query(db.func.coalesce(db.func.sum(BalanceLog.change), 0))
            .filter(BalanceLog.reason == "recharge",
                    BalanceLog.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0)).scalar(),
    }


@admin_bp.route("/")
def index():
    return redirect(url_for("admin.users_page"))


# ---------- 用户管理 ----------

@admin_bp.route("/users")
def users_page():
    stats = _dashboard_stats()
    keyword = (request.args.get("q") or "").strip()
    query = User.query
    if keyword:
        query = query.filter(User.username.contains(keyword))
    users = query.order_by(User.id.desc()).all()
    return render_template("admin/users.html", users=users, stats=stats,
                           keyword=keyword,
                           plans=MembershipPlan.query.order_by(MembershipPlan.sort).all(),
                           addon_pkgs=AddonPackage.query.order_by(AddonPackage.sort).all())


@admin_bp.route("/user/<int:user_id>")
def user_detail(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    q = effective_quota(user)
    addons = db.session.query(UserAddon, AddonPackage.name, AddonPackage.type) \
        .join(AddonPackage, UserAddon.package_id == AddonPackage.id) \
        .filter(UserAddon.user_id == user.id) \
        .order_by(UserAddon.id.desc()).limit(10).all()
    memberships = db.session.query(UserMembership, MembershipPlan.display_name) \
        .join(MembershipPlan, UserMembership.plan_id == MembershipPlan.id) \
        .filter(UserMembership.user_id == user.id) \
        .order_by(UserMembership.id.desc()).limit(10).all()
    logs = BalanceLog.query.filter_by(user_id=user.id).order_by(BalanceLog.id.desc()).limit(30).all()
    orders = PurchaseOrder.query.filter_by(user_id=user.id).order_by(PurchaseOrder.id.desc()).limit(10).all()
    shares = ShareLink.query.filter_by(user_id=user.id).order_by(ShareLink.id.desc()).limit(10).all()
    files_total = File.query.filter_by(user_id=user.id, is_dir=False).count()
    files_deleted = File.query.filter_by(user_id=user.id, is_dir=False).filter(
        File.deleted_at.is_not(None)).count()
    return render_template("admin/user_detail.html", u=user, q=q, addons=addons,
                           memberships=memberships, logs=logs, orders=orders,
                           shares=shares, files_total=files_total, files_deleted=files_deleted,
                           plans=MembershipPlan.query.order_by(MembershipPlan.sort).all(),
                           addon_pkgs=AddonPackage.query.order_by(AddonPackage.sort).all())


# ---------- 余额/会员/叠加包 赠送 ----------

@admin_bp.route("/user/<int:user_id>/balance", methods=["POST"])
def adjust_balance(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    delta = request.form.get("delta")
    note = request.form.get("note") or ""
    try:
        billing_service.admin_adjust_balance(user, Decimal(delta), note)
        flash(f"已调整 {user.username} 余额", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"调整失败：{e}", "danger")
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/gift_plan", methods=["POST"])
def gift_plan(user_id):
    """赠送会员：精确到秒，可自定义单位"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    plan_id = request.form.get("plan_id")
    try:
        seconds = parse_duration(request.form.get("duration_value"),
                                 request.form.get("duration_unit") or "day")
        billing_service.admin_gift_plan(user, int(plan_id), seconds)
        flash(f"已为 {user.username} 赠送会员", "success")
    except (BillingError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/revoke_plan", methods=["POST"])
def revoke_plan(user_id):
    """撤销会员：立即回落免费版并锁定容量（与自然到期一致）"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    try:
        billing_service.admin_revoke_plan(user)
        flash(f"已撤销 {user.username} 的会员", "success")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        flash(f"撤销失败：{e}", "danger")
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/gift_addon", methods=["POST"])
def gift_addon(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    package_id = request.form.get("package_id")
    try:
        billing_service.admin_gift_addon(user, int(package_id))
        flash(f"已为 {user.username} 赠送叠加包", "success")
    except BillingError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/toggle", methods=["POST"])
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id != current_user.id:
        user.is_active = not user.is_active
        db.session.commit()
        flash(f"已{'启用' if user.is_active else '禁用'} {user.username}", "success")
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/toggle_admin", methods=["POST"])
def toggle_admin(user_id):
    """提升/撤销管理员（不能撤自己或最后一名管理员）"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.is_admin:
        if user.id == current_user.id:
            flash("不能取消自己的管理员身份", "danger")
        elif User.query.filter_by(is_admin=True).count() <= 1:
            flash("系统至少需要保留一名管理员", "danger")
        else:
            user.is_admin = False
            db.session.commit()
            flash(f"已撤销 {user.username} 的管理员身份", "success")
    else:
        user.is_admin = True
        db.session.commit()
        flash(f"已将 {user.username} 提升为管理员", "success")
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/files", methods=["POST"])
def view_user_files(user_id):
    """进入代管模式：以目标用户为主体浏览/管理其文件"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    session["admin_view_uid"] = user.id
    return redirect(url_for("files.index"))


@admin_bp.route("/view_files/exit", methods=["POST"])
def exit_view_files():
    """退出代管模式"""
    session.pop("admin_view_uid", None)
    return redirect(_guard_redirect())


@admin_bp.route("/user/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    """彻底删除用户：清除其全部文件与关联数据"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == current_user.id:
        flash("不能删除自己的账号", "danger")
        return redirect(url_for("admin.users_page"))
    if user.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("系统至少需要保留一名管理员", "danger")
        return redirect(url_for("admin.users_page"))

    username = user.username
    file_service.purge_user_data(user)  # 删除全部文件记录与物理实体
    for model in (ShareLink, UserMembership, UserAddon, RechargeRecord,
                  BalanceLog, PurchaseOrder, UploadSession, AuditLog, RemoteDownload,
                  RedeemRecord):
        model.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    RedeemCode.query.filter_by(used_by=user.id).update(
        {"used_by": None}, synchronize_session=False)
    db.session.delete(user)
    db.session.commit()

    if session.get("admin_view_uid") == user_id:
        session.pop("admin_view_uid", None)
    flash(f"已删除用户 {username} 及其全部数据", "success")
    return redirect(url_for("admin.users_page"))


# ---------- 套餐管理与叠加包 ----------

@admin_bp.route("/plans")
def plans_page():
    return render_template("admin/plans.html",
                           plans=MembershipPlan.query.order_by(MembershipPlan.sort).all(),
                           addons=AddonPackage.query.order_by(AddonPackage.sort).all())


@admin_bp.route("/plans/save", methods=["POST"])
def plan_save():
    try:
        plan_id = request.form.get("plan_id")
        p = db.session.get(MembershipPlan, int(plan_id)) if plan_id else MembershipPlan()
        name = (request.form.get("name") or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        p.name = name.lower().replace(" ", "_")
        p.display_name = (request.form.get("display_name") or "").strip() or name
        p.monthly_price = Decimal(request.form.get("monthly_price") or 0)
        if p.monthly_price < 0:
            raise ValueError("价格不能为负数")
        p.storage_quota_bytes = int(float(request.form.get("storage_gb") or 0) * GB)
        if p.storage_quota_bytes <= 0:
            raise ValueError("容量必须大于 0")
        max_file_mb = int(float(request.form.get("max_file_mb") or 0) * MB)
        speed_mbps = int(float(request.form.get("speed_mbps") or 0) * 1024 ** 2)
        traffic_gb = int(float(request.form.get("traffic_gb") or 0) * GB)
        p.max_file_size_bytes = max_file_mb or None
        p.speed_limit_bps = speed_mbps or None
        p.monthly_traffic_bytes = traffic_gb or None
        p.allow_preview = bool(request.form.get("allow_preview"))
        p.sort = int(request.form.get("sort") or 0)
        if p.name == "free":
            p.monthly_price = Decimal("0.00")
        db.session.add(p)
        db.session.commit()
        flash("套餐已保存", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"保存失败：{e}", "danger")
    return redirect(url_for("admin.plans_page"))


@admin_bp.route("/plans/<int:plan_id>/delete", methods=["POST"])
def plan_delete(plan_id):
    p = db.session.get(MembershipPlan, plan_id)
    if p is None:
        abort(404)
    if p.name == "free":
        flash("免费套餐不可删除", "danger")
    elif db.session.query(User).filter_by(plan_id=p.id).first() or \
            db.session.query(UserMembership).filter_by(plan_id=p.id).first():
        flash("该套餐已被用户使用，无法删除", "danger")
    else:
        db.session.delete(p)
        db.session.commit()
        flash("套餐已删除", "success")
    return redirect(url_for("admin.plans_page"))


@admin_bp.route("/addons/save", methods=["POST"])
def addon_save():
    """保存叠加包：时长支持秒级精度与单位换算"""
    try:
        pkg_id = request.form.get("package_id")
        a = db.session.get(AddonPackage, int(pkg_id)) if pkg_id else AddonPackage()
        a.type = request.form.get("type") or "storage"
        if a.type not in ("traffic", "storage"):
            raise ValueError("类型无效")
        a.name = (request.form.get("name") or "").strip()
        if not a.name:
            raise ValueError("名称不能为空")
        a.amount_bytes = int(float(request.form.get("amount_gb") or 0) * GB)
        if a.amount_bytes <= 0:
            raise ValueError("数量必须大于 0")
        a.price = Decimal(request.form.get("price") or 0)
        if a.price < 0:
            raise ValueError("价格不能为负数")
        a.duration_seconds = parse_duration(request.form.get("duration_value"),
                                            request.form.get("duration_unit") or "day")
        a.sort = int(request.form.get("sort") or 0)
        db.session.add(a)
        db.session.commit()
        flash("叠加包已保存", "success")
    except (ValueError, ArithmeticError) as e:
        db.session.rollback()
        flash(f"保存失败：{e}", "danger")
    return redirect(url_for("admin.plans_page"))


@admin_bp.route("/addons/<int:pkg_id>/delete", methods=["POST"])
def addon_delete(pkg_id):
    a = db.session.get(AddonPackage, pkg_id)
    if a is None:
        abort(404)
    if db.session.query(UserAddon).filter_by(package_id=a.id).first():
        flash("该叠加包已有用户持有，无法删除", "danger")
    else:
        db.session.delete(a)
        db.session.commit()
        flash("叠加包已删除", "success")
    return redirect(url_for("admin.plans_page"))


# ---------- 兑换码 ----------

@admin_bp.route("/codes")
def codes_page():
    batch = (request.args.get("batch") or "").strip()
    status = (request.args.get("status") or "").strip()
    query = RedeemCode.query
    if batch:
        query = query.filter(RedeemCode.batch == batch)
    if status == "unused":
        query = query.filter(RedeemCode.used_count == 0)
    elif status == "used":
        query = query.filter(RedeemCode.used_count > 0)
    codes = query.order_by(RedeemCode.id.desc()).limit(CODE_PAGE_SIZE).all()

    now = utcnow()
    records = RedeemRecord.query.order_by(RedeemRecord.id.desc()).limit(100).all()
    return render_template(
        "admin/codes.html", codes=codes, records=records, now=now,
        batch=batch, status=status,
        total=RedeemCode.query.count(),
        used=RedeemCode.query.filter(RedeemCode.used_count > 0).count(),
        record_total=RedeemRecord.query.count(),
        plans=MembershipPlan.query.order_by(MembershipPlan.sort).all(),
        addon_pkgs=AddonPackage.query.order_by(AddonPackage.sort).all(),
    )


@admin_bp.route("/codes/generate", methods=["POST"])
def code_generate():
    form = request.form
    kind = form.get("kind") or "plan"
    seconds = None
    if kind in ("plan", "addon"):
        raw = (form.get("duration_value") or "").strip()
        if raw:
            try:
                seconds = parse_duration(raw, form.get("duration_unit") or "day")
            except ValueError as e:
                flash(str(e), "danger")
                return redirect(url_for("admin.codes_page"))
    try:
        codes = billing_service.generate_redeem_codes(
            kind=kind,
            count=form.get("count") or 1,
            prefix=form.get("prefix") or "",
            length=form.get("length") or 12,
            note=form.get("note") or "",
            expire_days=form.get("expire_days") or 0,
            plan_id=form.get("plan_id") or None,
            duration_seconds=seconds,
            amount=form.get("amount") or None,
            package_id=form.get("package_id") or None,
        )
    except (BillingError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("admin.codes_page"))
    flash(f"已生成 {len(codes)} 个兑换码", "success")
    return redirect(url_for("admin.codes_page", batch=codes[0].batch))


@admin_bp.route("/codes/<int:code_id>/delete", methods=["POST"])
def code_delete(code_id):
    code = db.session.get(RedeemCode, code_id)
    if code is None:
        abort(404)
    db.session.delete(code)
    db.session.commit()
    flash("兑换码已删除", "success")
    return redirect(_guard_redirect())


@admin_bp.route("/codes/clear_unused", methods=["POST"])
def codes_clear_unused():
    """批量删除未使用的兑换码（可按批次）"""
    batch = (request.form.get("batch") or "").strip()
    query = RedeemCode.query.filter(RedeemCode.used_count == 0)
    if batch:
        query = query.filter(RedeemCode.batch == batch)
        # 同批次中已使用的不删除，其余清空
    count = query.delete(synchronize_session=False)
    db.session.commit()
    flash(f"已删除 {count} 个未使用的兑换码", "success")
    return redirect(url_for("admin.codes_page", batch=batch))


@admin_bp.route("/codes/records/clear", methods=["POST"])
def codes_records_clear():
    """清空兑换记录日志（不影响兑换码本身的使用状态）"""
    count = RedeemRecord.query.delete(synchronize_session=False)
    db.session.commit()
    flash(f"已清空 {count} 条兑换记录", "success")
    return redirect(url_for("admin.codes_page"))


# ---------- 存储点 ----------

def _apply_point_form(point, is_new: bool):
    """把表单内容写入存储点对象并校验；失败抛 ValueError"""
    kind = (request.form.get("kind") or "local").strip()
    if kind not in storage_service.STORAGE_KINDS:
        raise ValueError("存储类型不支持")
    name = (request.form.get("name") or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    try:
        capacity_gb = float(request.form.get("capacity_gb") or 0)
    except ValueError:
        raise ValueError("容量格式不正确")
    capacity_bytes = int(capacity_gb * GB)
    if capacity_bytes <= 0:
        raise ValueError("容量必须大于 0")

    sort_raw = request.form.get("sort")
    try:
        sort = int(sort_raw) if sort_raw not in (None, "") else 0
    except ValueError:
        sort = 0

    point.name = name
    point.kind = kind
    point.capacity_bytes = capacity_bytes
    point.sort = sort
    # 停用的存储点能读不能写
    point.enabled = bool(request.form.get("enabled"))

    if kind == "local":
        path = (request.form.get("path") or "").strip()
        if not path:
            raise ValueError("请填写本地存储目录")
        point.path = path
        _clear_point_remote(point)
        return

    if kind == "s3":
        # 对象存储：自定义 Endpoint 以兼容 MinIO / OSS 等；remote_dir 作为对象键前缀
        bucket = (request.form.get("bucket") or "").strip()
        if not bucket:
            raise ValueError("请填写 S3 Bucket")
        secret = request.form.get("secret_key") or ""
        if not secret and not is_new:
            secret = point.secret_key or ""  # 留空表示沿用原密钥
        point.path = None
        point.host = None
        point.port = 21
        point.username = None
        point.password = None
        point.endpoint = (request.form.get("endpoint") or "").strip() or None
        point.bucket = bucket
        point.access_key = (request.form.get("access_key") or "").strip() or None
        point.secret_key = secret or None
        point.region = (request.form.get("region") or "").strip() or None
        point.path_style = bool(request.form.get("path_style"))
        point.remote_dir = (request.form.get("remote_dir") or "").strip().strip("/") or None
        return

    host = (request.form.get("host") or "").strip()
    if not host:
        raise ValueError("请填写 SFTP 服务器地址" if kind == "sftp" else "请填写 FTP 服务器地址")
    try:
        port = int(request.form.get("port") or (22 if kind == "sftp" else 21))
    except ValueError:
        raise ValueError("端口格式不正确")
    if not 1 <= port <= 65535:
        raise ValueError("端口超出范围")
    password = request.form.get("password") or ""
    if not password and not is_new:
        password = point.password or ""  # 留空表示沿用原密码
    point.path = None
    point.host = host
    point.port = port
    point.username = (request.form.get("username") or "").strip()
    point.password = password
    point.remote_dir = (request.form.get("remote_dir") or "").strip() or "/"
    point.endpoint = None
    point.bucket = None
    point.access_key = None
    point.secret_key = None
    point.region = None
    point.path_style = False


def _clear_point_remote(point):
    """清空非本地存储点的远端连接字段（本地存储点只需保留 path）"""
    point.port = 21
    point.host = None
    point.username = None
    point.password = None
    point.remote_dir = None
    point.endpoint = None
    point.bucket = None
    point.access_key = None
    point.secret_key = None
    point.region = None
    point.path_style = False


@admin_bp.route("/storage")
def storage_page():
    points = StoragePoint.query.order_by(StoragePoint.sort, StoragePoint.id).all()
    rows = [{"point": p, "stats": storage_service.stats(p),
             "plan": storage_service.delete_plan(p)} for p in points]
    return render_template("admin/storage.html", rows=rows,
                           default_root=current_app.config["STORAGE_ROOT"],
                           suggested_gb=round(storage_service.detect_local_capacity(
                               current_app.config["STORAGE_ROOT"]) / GB, 2))


@admin_bp.route("/storage/save", methods=["POST"])
def storage_save():
    point_id = request.form.get("point_id")
    try:
        if point_id:
            point = db.session.get(StoragePoint, int(point_id))
            if point is None:
                abort(404)
        else:
            point = StoragePoint()
        _apply_point_form(point, is_new=not point_id)
        err = storage_service.test_connection(point)
        if err:
            raise ValueError(err)
        db.session.add(point)
        db.session.commit()
        flash("存储点已保存", "success")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        flash(f"保存失败：{e}", "danger")
    return redirect(url_for("admin.storage_page"))


@admin_bp.route("/storage/test", methods=["POST"])
def storage_test():
    """测试表单当前填写的连接参数（不落库）"""
    point_id = request.form.get("point_id")
    point = StoragePoint()
    try:
        if point_id:
            saved = db.session.get(StoragePoint, int(point_id))
            if saved is None:
                abort(404)
            point.password = saved.password  # 供表单密码留空时沿用
            point.secret_key = saved.secret_key  # S3 密钥同理
        _apply_point_form(point, is_new=not point_id)
        err = storage_service.test_connection(point)
    except ValueError as e:
        err = str(e)
    if err:
        flash(f"连接测试失败：{err}", "danger")
    else:
        flash("连接测试通过", "success")
    return redirect(url_for("admin.storage_page"))


@admin_bp.route("/storage/<int:point_id>/toggle", methods=["POST"])
def storage_toggle(point_id):
    point = db.session.get(StoragePoint, point_id)
    if point is None:
        abort(404)
    if point.enabled and StoragePoint.query.filter_by(enabled=True).count() <= 1:
        flash("至少需要保留一个启用的存储点", "danger")
    else:
        point.enabled = not point.enabled
        db.session.commit()
        flash("存储点已启用" if point.enabled else "存储点已停用（仍可读取，不再写入）", "success")
    return redirect(url_for("admin.storage_page"))


@admin_bp.route("/storage/<int:point_id>/delete", methods=["POST"])
def storage_delete(point_id):
    """删除存储点：mode=migrate 转移数据；mode=drop 删除数据（仅转移不可行时）"""
    point = db.session.get(StoragePoint, point_id)
    if point is None:
        abort(404)
    if StoragePoint.query.count() <= 1:
        flash("至少需要保留一个存储点", "danger")
        return redirect(url_for("admin.storage_page"))

    mode = (request.form.get("mode") or "migrate").strip()
    plan = storage_service.delete_plan(point)
    # 源存储点不可访问时无法读取/转移数据，探测一次以决定可行路径
    src_err = storage_service.test_connection(point) if plan["count"] else ""
    try:
        if mode == "migrate":
            if src_err:
                raise ValueError(f"存储点无法连接（{src_err}），无法转移数据，请选择删除数据")
            if plan["count"] and not plan["feasible"]:
                raise ValueError(
                    f"其他存储点可用空间不足（需 {plan['need']} 字节，可用 {plan['available']} 字节），"
                    "无法转移，请选择删除数据")
            moved = storage_service.migrate_point_data(point)
            db.session.delete(point)
            db.session.commit()
            flash(f"已转移 {moved} 个文件到其他存储点，存储点「{point.name}」已删除", "success")
        else:
            # 源点可访问且其他点空间充足时必须先转移；源点不可访问时允许直接删除数据
            if plan["count"] and plan["feasible"] and not src_err:
                raise ValueError("其他存储点空间充足，请先转移数据")
            lost = storage_service.drop_point_data(point)
            name = point.name
            db.session.delete(point)
            db.session.commit()
            flash(f"已删除 {lost} 个文件实体并标记为「已丢失」，记录将在 7 天后自动清理"
                  f"（存储点「{name}」）", "warning")
    except (ValueError, storage_service.StorageError) as e:
        db.session.rollback()
        flash(f"删除失败：{e}", "danger")
    return redirect(url_for("admin.storage_page"))


# ---------- 设置 ----------

@admin_bp.route("/settings")
def settings_page():
    return render_template("admin/settings.html", cfg=load_site_config_raw(),
                           features=FEATURE_KEYS,
                           update_info=update_service.read_status())


@admin_bp.route("/settings/save", methods=["POST"])
def settings_save():
    form = request.form
    try:
        hsts_max_age = int(form.get("hsts_max_age") or 0)
    except ValueError:
        hsts_max_age = 0
    if hsts_max_age <= 0:
        hsts_max_age = HSTS_MAX_AGE
    try:
        hour = min(max(int(form.get("update_hour") or 0), 0), 23)
        minute = min(max(int(form.get("update_minute") or 0), 0), 59)
    except ValueError:
        flash("自动更新时间格式不正确", "danger")
        return redirect(url_for("admin.settings_page"))

    sections = {
        "project": {
            "name": (form.get("project_name") or "").strip(),
            "name_en": (form.get("project_name_en") or "").strip(),
        },
        "site": {"name": (form.get("site_name") or "").strip()},
        "features": {key: bool(form.get(key)) for key in FEATURE_KEYS},
        "https": {
            "enabled": bool(form.get("https_enabled")),
            "certfile": (form.get("https_certfile") or "").strip(),
            "keyfile": (form.get("https_keyfile") or "").strip(),
            "hsts": bool(form.get("https_hsts")),
            "hsts_max_age": hsts_max_age,
        },
        "update": {
            "enabled": bool(form.get("update_enabled")),
            "check_on_login": bool(form.get("update_check_on_login")),
            "hour": hour,
            "minute": minute,
            "repo": (form.get("update_repo") or "").strip(),
            "proxy": (form.get("update_proxy") or "").strip(),
        },
    }
    try:
        save_site_config(sections)
        flash("设置已保存，重启服务后生效", "success")
    except (OSError, ValueError) as e:
        flash(f"保存失败：{e}", "danger")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/update", methods=["POST"])
def settings_update():
    """手动检查更新；action=update 时立即下载并安装"""
    apply_now = (request.form.get("action") or "check") == "update"
    try:
        status = update_service.check(apply_update=apply_now)
    except Exception as e:
        flash(f"更新失败：{e}", "danger")
        return redirect(url_for("admin.settings_page"))

    if status.get("error"):
        flash(status["error"], "danger")
    elif status.get("has_update"):
        if apply_now:
            flash(f"已开始更新到 {status['latest']}，服务稍后会自动重启", "success")
        else:
            flash(f"发现新版本 {status['latest']}，可点击「立即更新」安装", "info")
    else:
        flash(f"当前已是最新版本 {status.get('current')}", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/update-info")
def settings_update_info():
    """更新状态与更新日志（JSON，供页脚/后台的新版本弹窗展示）"""
    return jsonify(update_service.update_detail())


@admin_bp.route("/settings/restart", methods=["POST"])
def settings_restart():
    """重启后台服务（依赖安装脚本写入的 sudoers 放行命令）"""
    trigger = current_app.config["RESTART_TRIGGER"]
    try:
        subprocess.run(["sudo", "-n", trigger], check=True, timeout=30,
                       capture_output=True, text=True)
        flash("已提交重启指令，服务将在数秒内重启", "success")
    except (OSError, subprocess.SubprocessError) as e:
        flash(f"重启失败：{e}", "danger")
    return redirect(url_for("admin.settings_page"))
