"""分享外链路由：创建/管理分享、密码校验与会话授权、落地页浏览与下载（匿名下载限速并按 IP 计周配额）"""
import time
from datetime import timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    send_file, abort, session, Response, stream_with_context, current_app,
)
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db
from app.models import ShareLink, File, AnonDlWeek, DirectLink, DirectLinkQuota
from app.routes.transfer import current_target, waiting_redirect
from app.utils.helpers import (
    utcnow, current_file_owner, guess_mimetype, content_disposition,
)

share_bp = Blueprint("share", __name__)


@share_bp.before_request
def _check_enabled():
    """功能开关（config.yml: features.enable_share）"""
    if not current_app.config["ENABLE_SHARE"]:
        abort(403, "分享功能已关闭")


# ---- 密码暴力破解防护（进程内，按 IP+分享） ----
MAX_PWD_FAIL = 5
PWD_LOCK_SECONDS = 15 * 60
_pwd_guard = {}  # (token, ip) -> [fail_count, first_fail_ts]


def _lock_state(token: str):
    key = (token, request.remote_addr or "")
    now = utcnow().timestamp()
    rec = _pwd_guard.get(key)
    if not rec:
        return None, 0
    cnt, first = rec
    if cnt >= MAX_PWD_FAIL:
        remaining = PWD_LOCK_SECONDS - (now - first)
        if remaining > 0:
            return rec, int(remaining)
        _pwd_guard.pop(key, None)
    return rec, 0


def _record_fail(token: str):
    # 表容量上限：防止大量 (token, ip) 组合造成内存膨胀
    if len(_pwd_guard) >= 10000:
        now0 = utcnow().timestamp()
        for k in [k for k, v in _pwd_guard.items()
                  if now0 - v[1] > PWD_LOCK_SECONDS]:
            _pwd_guard.pop(k, None)
        if len(_pwd_guard) >= 10000:
            _pwd_guard.clear()
    key = (token, request.remote_addr or "")
    now = utcnow().timestamp()
    rec = _pwd_guard.get(key) or [0, now]
    rec[0] += 1
    rec[1] = rec[1] if rec[0] > 1 else now
    _pwd_guard[key] = rec


def _clear_fail(token: str):
    _pwd_guard.pop((token, request.remote_addr or ""), None)


# ---- 会话授权：密码验证通过后，同一浏览器免重复输入 ----
def _granted(token: str) -> bool:
    return bool(session.get("share_auth", {}).get(token))


def _grant(token: str):
    auth = dict(session.get("share_auth") or {})
    auth[token] = True
    session["share_auth"] = auth


def _load_share(token: str, check_downloads: bool = True):
    """加载并校验分享状态，返回 (link, file)；无效则 abort

    check_downloads=False 用于转存：转存不消耗下载次数
    """
    link = ShareLink.query.filter_by(token=token).first()
    if link is None:
        abort(404, "分享不存在")
    if link.expire_at and link.expire_at < utcnow():
        abort(410, "分享已过期")
    if check_downloads and link.max_downloads is not None and link.download_count >= link.max_downloads:
        abort(410, "下载次数已用完")

    f = db.session.get(File, link.file_id)
    if f is None:
        abort(404, "文件不存在")
    if f.deleted_at is not None:
        abort(410, "文件已被删除")
    return link, f


@share_bp.route("/shares")
@login_required
def my_shares():
    owner = current_file_owner()
    links = ShareLink.query.filter_by(user_id=owner.id).order_by(
        ShareLink.created_at.desc()).all()
    dlinks = DirectLink.query.filter_by(user_id=owner.id).order_by(
        DirectLink.created_at.desc()).all()
    return render_template(
        "share/list.html", links=links,
        direct_rows=[{"link": l, "week_used": _direct_week_used(l)} for l in dlinks],
        is_svip=_is_svip(owner),
        direct_created=_direct_created_this_week(owner.id),
        direct_create_limit=DIRECT_WEEKLY_CREATE,
        direct_week_limit=DIRECT_WEEKLY_BYTES,
    )


@share_bp.route("/share/create", methods=["POST"])
@login_required
def create():
    owner = current_file_owner()
    file_id = request.form.get("file_id")
    try:
        f = db.session.get(File, int(file_id))
    except (TypeError, ValueError):
        abort(404, "文件不存在或无权访问")
    if f is None or f.user_id != owner.id:
        abort(403)

    password = request.form.get("password") or None
    expire_days = request.form.get("expire_days") or None
    max_downloads = request.form.get("max_downloads") or None

    link = ShareLink(file_id=f.id, user_id=owner.id)
    if password:
        link.password_hash = generate_password_hash(password)
    if expire_days:
        try:
            days = int(expire_days)
        except (TypeError, ValueError):
            abort(400, "有效期必须是整数天")
        if days <= 0:
            abort(400, "有效期必须大于 0 天")
        link.expire_at = utcnow() + timedelta(days=days)
    if max_downloads:
        try:
            link.max_downloads = int(max_downloads)
        except (TypeError, ValueError):
            abort(400, "下载次数必须是整数")
        if link.max_downloads <= 0:
            abort(400, "下载次数必须大于 0")

    db.session.add(link)
    db.session.commit()
    flash("分享链接已生成", "success")
    return redirect(url_for("share.my_shares"))


@share_bp.route("/share/<token>", methods=["GET", "POST"])
def access(token):
    """分享入口页：密码校验 + 展示落地页/目录浏览页（GET 永不直接返回文件流）"""
    link, f = _load_share(token)

    # ---- 密码（可选）校验 ----
    if link.password_hash and not _granted(token):
        lock, remain = _lock_state(token)
        if lock and remain:
            flash(f"密码错误次数过多，请 {remain // 60 + 1} 分钟后再试", "danger")
            return render_template("share/password.html", token=token)
        if request.method == "POST":
            pw = request.form.get("password") or ""
            if check_password_hash(link.password_hash, pw):
                _clear_fail(token)
                _grant(token)
                return redirect(url_for("share.access", token=token))
            _record_fail(token)
            flash("密码错误", "danger")
        return render_template("share/password.html", token=token)

    return _show_page(link, f)


def _show_page(link: ShareLink, f: File):
    """落地页：文件 → 下载页；文件夹 → 目录浏览页"""
    from app.services import file_service

    if f.is_dir:
        folder = f
        folder_id = request.args.get("folder_id")
        if folder_id:
            try:
                folder = db.session.get(File, int(folder_id))
            except (TypeError, ValueError):
                abort(404, "目录不存在")
            if folder is None or not folder.is_dir or not _node_in_tree(folder, f):
                abort(404, "目录不存在")
        items = file_service.active_children(folder)
        crumbs = _build_crumbs(folder, f)
        return render_template("share/browse.html", link=link, root=f,
                               folder=folder, items=items, crumbs=crumbs)
    return render_template("share/landing.html", link=link, f=f)


@share_bp.route("/share/<token>/download", methods=["POST"])
def download(token):
    """下载端点（仅 POST）：网页内点下载按钮触发，不能靠 GET 直链拉取"""
    link, f = _load_share(token)

    # 密码分享必须已通过本浏览器验证
    if link.password_hash and not _granted(token):
        return redirect(url_for("share.access", token=token))

    # 文件夹分享内可下载指定子文件（file=<id>）
    target = f
    file_id = request.form.get("file") or request.args.get("file")
    if file_id:
        try:
            node = db.session.get(File, int(file_id))
        except (TypeError, ValueError):
            abort(404, "文件不存在或无权访问")
        if node is None or node.is_dir or not _node_in_tree(node, f):
            abort(404, "文件不存在或无权访问")
        target = node
    elif f.is_dir:
        abort(400, "请进入目录后选择要下载的文件")

    return _send_file(link, target)


@share_bp.route("/share/<token>/save", methods=["POST"])
@login_required
def save(token):
    """转存：把分享的文件/文件夹复制一份到自己网盘（不消耗下载次数）"""
    from app.services import file_service
    from app.services.quota_service import QuotaError

    link, f = _load_share(token, check_downloads=False)
    if link.password_hash and not _granted(token):
        flash("请先输入访问密码", "warning")
        return redirect(url_for("share.access", token=token))

    # 文件夹分享内可转存指定子节点（file=<id>）
    target = f
    file_id = request.form.get("file")
    if file_id:
        try:
            node = db.session.get(File, int(file_id))
        except (TypeError, ValueError):
            abort(404, "文件不存在或无权访问")
        if node is None or not _node_in_tree(node, f) or node.deleted_at is not None:
            abort(404, "文件不存在或无权访问")
        target = node

    try:
        new_f = file_service.save_shared(current_file_owner(), target)
        flash(f"已转存「{new_f.name}」到我的网盘", "success")
    except (ValueError, PermissionError, QuotaError) as e:
        flash(str(e), "danger")

    # 下载次数已用尽的分享无法再打开分享页，此时回自己的网盘展示提示
    if link.max_downloads is not None and link.download_count >= link.max_downloads:
        return redirect(url_for("files.index"))
    return redirect(url_for("share.access", token=token, **(
        {"folder_id": target.id} if f.is_dir and target.id != f.id else {})))


def _node_in_tree(node: File, root: File) -> bool:
    """node 是否位于 root 的子树内（含自身），沿途节点均未删除"""
    cur = node
    while cur is not None:
        if cur.id == root.id:
            return True
        if cur.deleted_at is not None or cur.user_id != root.user_id:
            return False
        cur = cur.parent
    return False


def _build_crumbs(folder: File, root: File):
    """分享目录内的面包屑（root → folder）"""
    crumbs = []
    cur = folder
    seen = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        crumbs.append(cur)
        if cur.id == root.id:
            break
        cur = cur.parent
    crumbs.reverse()
    return crumbs


ANON_SPEED_LIMIT = 10 * 1024 * 1024  # 未登录访客下载限速 10 MiB/s
ANON_WEEKLY_QUOTA = 1 * 1024 ** 3    # 未登录访客单 IP 每周最多 1GB


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _week_key() -> str:
    """ISO 自然周键，如 2026-W36（每周一重置）"""
    year, week, _ = utcnow().isocalendar()
    return f"{year}-W{week:02d}"


def _anon_used(ip: str) -> int:
    row = AnonDlWeek.query.filter_by(ip=ip, week=_week_key()).first()
    return int(row.bytes_used) if row else 0


def _anon_consume(ip: str, delta: int):
    """原子累加该 IP 当周已用匿名下载流量"""
    if delta <= 0:
        return
    row = AnonDlWeek.query.filter_by(ip=ip, week=_week_key()).first()
    if row is None:
        db.session.add(AnonDlWeek(ip=ip, week=_week_key(), bytes_used=delta))
    else:
        row.bytes_used += delta
    db.session.commit()


def _send_file(link: ShareLink, f: File):
    import os
    from app.services import file_service
    if f.is_lost:
        abort(404, "文件已丢失")
    path, task = file_service.local_path_or_pending(f)
    if task is not None:
        # 远端文件先取回；完成后按原 POST 表单重放回本入口继续下载
        return waiting_redirect([task.token], current_target(), f.name,
                                method="post", fields={"file": [str(f.id)]})
    if not path or not os.path.exists(path):
        abort(404, "文件实体丢失")
    link.download_count += 1
    db.session.commit()
    # 以真实文件大小为准：库中记录可能过期，浏览器会按 Content-Length 校验
    size = os.path.getsize(path)
    if current_user.is_authenticated:
        resp = send_file(path, mimetype=guess_mimetype(f.name), as_attachment=True,
                         download_name=f.name)
        resp.headers["Content-Disposition"] = content_disposition(f.name)
        return resp
    ip = _client_ip()
    used = _anon_used(ip)
    if used + size > ANON_WEEKLY_QUOTA:
        abort(429, "该 IP 本周匿名下载流量已用尽（1GB），请登录后继续下载")
    return _stream_limited(path, f.name, size, ip, used)


def _stream_limited(path: str, download_name: str, size: int, ip: str, used0: int):
    """未登录访客下载：按 10 MiB/s 流式限速，并实时扣减该 IP 本周匿名配额"""
    # 声明长度不得超出剩余匿名配额，保证 Content-Length 与实际发送字节一致
    length = min(size, max(ANON_WEEKLY_QUOTA - used0, 0))

    def generate():
        chunk = 256 * 1024
        sent = 0
        with open(path, "rb") as fh:
            while sent < length:
                data = fh.read(min(chunk, length - sent))
                if not data:
                    break
                sent += len(data)
                _anon_consume(ip, len(data))
                yield data
                if sent < length:
                    time.sleep(len(data) / ANON_SPEED_LIMIT)

    resp = Response(stream_with_context(generate()), direct_passthrough=True)
    resp.headers["Content-Type"] = guess_mimetype(download_name)
    resp.headers["Content-Length"] = str(length)
    resp.headers["Content-Disposition"] = content_disposition(download_name)
    return resp


@share_bp.route("/share/<int:link_id>/cancel", methods=["POST"])
@login_required
def cancel(link_id):
    link = ShareLink.query.filter_by(id=link_id, user_id=current_file_owner().id).first()
    if link:
        db.session.delete(link)
        db.session.commit()
        flash("已取消分享", "success")
    return redirect(url_for("share.my_shares"))


# ---------------- SVIP 直链 ----------------

DIRECT_WEEKLY_BYTES = 5 * 1024 ** 3  # 单条直链每周最多下载 5GB
DIRECT_WEEKLY_CREATE = 10            # 每个 SVIP 用户每周最多生成 10 条


def _is_svip(user) -> bool:
    """当前生效套餐是否为 SVIP（会员到期自动回落为免费版）"""
    from app.services.quota_service import get_plan
    plan = get_plan(user)
    return plan is not None and plan.name == "svip"


def _direct_created_this_week(user_id: int) -> int:
    row = DirectLinkQuota.query.filter_by(user_id=user_id, week=_week_key()).first()
    return int(row.created_count) if row else 0


def _direct_consume_create(user_id: int):
    """累加该用户本周直链生成计数（撤销也不返还）"""
    row = DirectLinkQuota.query.filter_by(user_id=user_id, week=_week_key()).first()
    if row is None:
        db.session.add(DirectLinkQuota(user_id=user_id, week=_week_key(), created_count=1))
    else:
        row.created_count += 1
    db.session.commit()


def _direct_week_used(link: DirectLink) -> int:
    """该直链当周已用下载字节；跨周自动归零"""
    return int(link.week_bytes or 0) if link.week_key == _week_key() else 0


def _direct_add(link: DirectLink, delta: int):
    """增减该直链当周已用流量（delta 可为负，用于退回未发送部分）"""
    if delta == 0:
        return
    week = _week_key()
    if link.week_key != week:
        link.week_key = week
        link.week_bytes = 0
    link.week_bytes = max(int(link.week_bytes or 0) + delta, 0)
    db.session.commit()


@share_bp.route("/direct/create", methods=["POST"])
@login_required
def direct_create():
    """生成直链（仅 SVIP，每用户每周限 10 条）"""
    owner = current_file_owner()
    if not _is_svip(owner):
        flash("获取直链为 SVIP 专享功能，请升级会员", "danger")
        return redirect(request.referrer or url_for("files.index"))

    try:
        f = db.session.get(File, int(request.form.get("file_id") or 0))
    except (TypeError, ValueError):
        abort(404, "文件不存在或无权访问")
    if f is None or f.user_id != owner.id or f.is_dir or f.deleted_at is not None:
        abort(404, "文件不存在或无权访问")

    if _direct_created_this_week(owner.id) >= DIRECT_WEEKLY_CREATE:
        flash(f"本周直链生成数量已达上限（{DIRECT_WEEKLY_CREATE} 条），请下周再试", "danger")
        return redirect(request.referrer or url_for("files.index"))

    link = DirectLink(file_id=f.id, user_id=owner.id, week_key=_week_key(), week_bytes=0)
    expire_days = request.form.get("expire_days") or None
    if expire_days:
        try:
            days = int(expire_days)
        except (TypeError, ValueError):
            abort(400, "有效期必须是整数天")
        if days <= 0:
            abort(400, "有效期必须大于 0 天")
        link.expire_at = utcnow() + timedelta(days=days)
    db.session.add(link)
    _direct_consume_create(owner.id)  # 与上面的 add 同一次提交落库
    flash("直链已生成，可在「我的分享」页查看", "success")
    return redirect(request.referrer or url_for("share.my_shares"))


@share_bp.route("/d/<token>")
def direct_download(token):
    """直链下载端点（GET，无需登录）：单条直链每周最多下载 5GB"""
    import os
    from app.services import file_service

    link = DirectLink.query.filter_by(token=token).first()
    if link is None:
        abort(404, "直链不存在或已撤销")
    if link.expire_at is not None and link.expire_at <= utcnow():
        abort(410, "直链已过期")

    f = db.session.get(File, link.file_id)
    if f is None or f.deleted_at is not None:
        abort(410, "文件已被删除")
    if f.is_lost:
        abort(404, "文件已丢失")
    path, task = file_service.local_path_or_pending(f)
    if task is not None:
        # 远端文件先取回，进度页完成后自动回到直链地址继续下载
        return waiting_redirect([task.token], current_target(), f.name)
    if not path or not os.path.exists(path):
        abort(404, "文件实体丢失")

    remaining = DIRECT_WEEKLY_BYTES - _direct_week_used(link)
    if remaining <= 0:
        abort(429, "该直链本周下载流量已用尽（5GB），请下周再试")

    # 以真实文件大小为准，且本次响应长度不超过该直链本周剩余额度
    length = min(os.path.getsize(path), remaining)
    link.download_count += 1
    _direct_add(link, length)  # 先预占，流结束时退回未发送部分
    return _direct_stream(path, f.name, length, link)


def _direct_stream(path: str, download_name: str, length: int, link: DirectLink):
    chunk = 256 * 1024

    def generate():
        sent = 0
        try:
            with open(path, "rb") as fh:
                while sent < length:
                    data = fh.read(min(chunk, length - sent))
                    if not data:
                        break
                    sent += len(data)
                    yield data
        finally:
            # 客户端提前断开时退回未发送的额度，避免多扣周流量
            if sent < length:
                try:
                    _direct_add(link, sent - length)
                except Exception:  # noqa: BLE001
                    db.session.rollback()

    resp = Response(stream_with_context(generate()), direct_passthrough=True)
    resp.headers["Content-Type"] = guess_mimetype(download_name)
    resp.headers["Content-Length"] = str(length)
    resp.headers["Content-Disposition"] = content_disposition(download_name)
    return resp


@share_bp.route("/direct/<int:link_id>/revoke", methods=["POST"])
@login_required
def direct_revoke(link_id):
    link = DirectLink.query.filter_by(id=link_id, user_id=current_file_owner().id).first()
    if link:
        db.session.delete(link)
        db.session.commit()
        flash("已撤销直链", "success")
    return redirect(url_for("share.my_shares"))
