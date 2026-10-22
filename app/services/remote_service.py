"""远程下载任务执行服务

任务以状态机（queued -> downloading -> done/failed/canceled）驱动：后台轮询线程按用户套餐
并发上限与“数据库乐观认领”分派下载；执行超过 1 小时、下载中连续 60 秒无新数据或长期
无心跳的 downloading 任务会被判定失败或回收。下载量受剩余容量、单文件上限与本月流量约束。
"""
import hashlib
import ipaddress
import os
import socket
import threading
import time
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import (
    HTTPRedirectHandler, Request, build_opener,
)

from app.extensions import db
from app.models import File, RemoteDownload, User
from app.services import file_service, storage_service
from app.services.quota_service import consume_traffic, effective_quota
from app.utils.helpers import (
    gen_storage_key, human_size, safe_filename, utcnow,
    check_upload_security, read_file_head,
)

# 套餐 -> 同时进行的远程下载任务数
CONCURRENCY_BY_PLAN = {"free": 1, "vip": 3, "svip": 5}
DEFAULT_CONCURRENCY = 1
# 单进程内最多并行执行的下载线程；跨进程并发由 DB 计数控制
MAX_PROCESS_ACTIVE = 6
POLL_SECONDS = 5          # 轮询间隔
CHUNK = 256 * 1024        # 流式读取块
STALL_SECONDS = 60        # 连续无新数据判定超时
TASK_MAX_SECONDS = 3600   # 单任务最长执行 1 小时
# 回收阈值：downloading 任务超过该时间无心跳即判定执行者失联
SWEEP_IDLE_SECONDS = 180

_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 RealFiles-Remote/1.0")

_started = False
_started_lock = threading.Lock()
_active_ids = set()
_active_lock = threading.Lock()


def concurrency_limit(user: User) -> int:
    """用户允许的并发远程下载任务数"""
    from app.services.quota_service import get_plan
    plan = get_plan(user)
    return CONCURRENCY_BY_PLAN.get(plan.name, DEFAULT_CONCURRENCY)


def _running_count(user_id: int) -> int:
    return RemoteDownload.query.filter_by(
        user_id=user_id, status="downloading").count()


def allowed_bytes(task: RemoteDownload) -> int:
    """当前该任务最多可下载的字节数（磁盘/单文件/流量三方约束）"""
    user = db.session.get(User, task.user_id)
    if user is None:
        return 0
    q = effective_quota(user)
    cap = q["free_storage"]
    if q["max_file_size"] is not None:
        cap = min(cap, q["max_file_size"])
    if q["monthly_traffic"] is not None:
        remain = q["monthly_traffic"] + q["traffic_addon"] - q["used_upload"] - q["used_download"]
        cap = min(cap, max(0, remain))
    return cap


def start_worker(app):
    """在进程内启动一次远程下载轮询线程（幂等）"""
    global _started
    with _started_lock:
        if _started:
            return None
        _started = True

    def _loop():
        while True:
            try:
                with app.app_context():
                    _sweep_stale_tasks()
                    _dispatch(app)
            except Exception:
                db.session.rollback()
            time.sleep(POLL_SECONDS)

    t = threading.Thread(target=_loop, name="remote-dl-worker", daemon=True)
    t.start()
    return t


# --------------------------- 轮询 ---------------------------

def _sweep_stale_tasks():
    """回收执行者失联（长时间无心跳）的任务"""
    now = utcnow()
    idle = now - timedelta(seconds=SWEEP_IDLE_SECONDS)
    overdue = now - timedelta(seconds=TASK_MAX_SECONDS)
    stale = RemoteDownload.query.filter_by(status="downloading").filter(
        (RemoteDownload.last_progress_at.is_(None)) | (RemoteDownload.last_progress_at < idle)
    ).all()
    for t in stale:
        _mark_failed(t, "下载执行进程中断，任务已重置")
    long = RemoteDownload.query.filter_by(status="downloading").filter(
        RemoteDownload.started_at < overdue).all()
    for t in long:
        _mark_failed(t, "下载超时（超过 1 小时）")
    db.session.commit()


def _mark_failed(task, message):
    task.status = "failed"
    task.error = message[:500]
    task.finished_at = utcnow()


def _dispatch(app):
    """扫描排队任务并按用户并发配额认领，随后分派到独立线程下载"""
    queued = RemoteDownload.query.filter_by(status="queued").order_by(
        RemoteDownload.created_at.asc()).all()

    def _capacity():
        with _active_lock:
            return MAX_PROCESS_ACTIVE - len(_active_ids)

    for task in queued:
        if _capacity() <= 0:
            return
        user = db.session.get(User, task.user_id)
        if user is None:
            _mark_failed(task, "用户不存在")
            db.session.commit()
            continue
        limit = concurrency_limit(user)
        if _running_count(task.user_id) >= limit:
            continue
        # 乐观认领：仅当仍是 queued 时才置为 downloading
        claimed = RemoteDownload.query.filter_by(id=task.id, status="queued").update({
            "status": "downloading",
            "started_at": utcnow(),
            "last_progress_at": utcnow(),
        })
        db.session.commit()
        if claimed != 1:
            continue
        # 并发进程可能同时认领成功，复核是否超限
        if _running_count(task.user_id) > limit:
            task2 = db.session.get(RemoteDownload, task.id)
            if task2 and task2.status == "downloading":
                task2.status = "queued"
                task2.started_at = None
                task2.last_progress_at = None
            db.session.commit()
            continue
        with _active_lock:
            _active_ids.add(task.id)
        t = threading.Thread(target=_download_runner, args=(app, task.id), daemon=True)
        t.start()


def _download_runner(app, task_id: int):
    try:
        with app.app_context():
            _run_task(task_id)
    finally:
        with _active_lock:
            _active_ids.discard(task_id)


# --------------------------- 单任务执行 ---------------------------

def _part_path(task_id: int) -> str:
    from flask import current_app
    return os.path.join(current_app.config["REMOTE_TMP_ROOT"], f"rt_{task_id}.part")


def _ip_blocked(ip) -> bool:
    """非公网 IP 一律视为受限（回环/内网/链路本地/保留/组播/未指定）"""
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _is_blocked_host(url: str) -> bool:
    """校验下载地址：拦截 localhost 与回环/内网/保留地址

    域名先做 DNS 解析，解析出的全部 IP 均校验，防止「域名指向内网 IP」绕过；
    无法解析的域名一律拒绝。
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return _ip_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return True  # 解析失败不允许访问
    ips = set()
    for info in infos:
        try:
            ips.add(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not ips:
        return True
    return any(_ip_blocked(ip) for ip in ips)


class _SafeRedirectHandler(HTTPRedirectHandler):
    """重定向时校验目标地址，防止 302 跳转到内网绕过 SSRF 防护"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _is_blocked_host(newurl):
            raise URLError("重定向目标为受限地址，已拦截")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = build_opener(_SafeRedirectHandler)


def _run_task(task_id: int):
    from flask import current_app
    task = db.session.get(RemoteDownload, task_id)
    if task is None or task.status != "downloading":
        return
    if _is_blocked_host(task.url):
        db.session.rollback()
        task = db.session.get(RemoteDownload, task_id)
        if task and task.status != "canceled":
            _mark_failed(task, "不允许下载内网/本机地址")
            db.session.commit()
        return
    part = _part_path(task_id)
    started = utcnow()
    idle = 0.0
    md5 = hashlib.md5()
    written = 0

    try:
        cap = allowed_bytes(task)
        if cap <= 0:
            raise ValueError("存储空间或本月流量不足，无法开始下载")

        req = Request(task.url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
        conn = _SAFE_OPENER.open(req, timeout=30)  # noqa: S310 远程下载需访问任意用户给定 URL
        try:
            ctype = conn.headers.get("Content-Type", "")
            length = conn.headers.get("Content-Length")
            if length and length.isdigit():
                declared = int(length)
                if declared > cap:
                    raise ValueError(
                        f"文件过大，超出当前可用额度（{human_size(cap)}）")
                if task.total_size is None:
                    task.total_size = declared
                    db.session.commit()

            os.makedirs(os.path.dirname(part), exist_ok=True)
            with open(part, "wb") as fh:
                last_tick = time.time()
                while True:
                    _ensure_not_canceled(task)
                    try:
                        data = conn.read(CHUNK)
                    except socket.timeout:
                        idle += 30
                        if idle >= STALL_SECONDS:
                            raise TimeoutError("连续 1 分钟未下载到新数据")
                        _heartbeat(task)
                        continue
                    if not data:
                        break
                    idle = 0.0
                    if written + len(data) > cap:
                        raise ValueError(
                            f"下载内容超过可用额度（{human_size(cap)}），已中止")
                    fh.write(data)
                    written += len(data)
                    md5.update(data)
                    now = time.time()
                    if now - last_tick >= 1.0:
                        last_tick = now
                        _progress(task, written)
            if written == 0:
                raise ValueError("远端未返回任何内容")
            _finalize(task, written, md5.hexdigest(), part)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        task = db.session.get(RemoteDownload, task_id)
        if task is None:
            _remove_part(part)
            return
        if task.status == "canceled":
            _remove_part(part)
            return  # 用户主动取消，不再改写状态
        if utcnow() - started > timedelta(seconds=TASK_MAX_SECONDS):
            _mark_failed(task, "下载超时（超过 1 小时）")
        else:
            _mark_failed(task, _err_text(e))
        db.session.commit()
        _remove_part(part)


def _ensure_not_canceled(task):
    fresh = db.session.get(RemoteDownload, task.id)
    if fresh is None or fresh.status == "canceled":
        raise _Canceled()


class _Canceled(Exception):
    pass


def _err_text(e: Exception) -> str:
    if isinstance(e, HTTPError):
        return f"服务器返回 HTTP {e.code}"
    if isinstance(e, URLError):
        return f"无法访问目标地址：{getattr(e, 'reason', e)}"
    if isinstance(e, TimeoutError):
        return "连续 1 分钟未下载到新数据，判定超时"
    return str(e)[:480]


def _heartbeat(task):
    RemoteDownload.query.filter_by(id=task.id, status="downloading").update({
        "last_progress_at": utcnow(),
    })
    db.session.commit()


def _progress(task, written):
    RemoteDownload.query.filter_by(id=task.id, status="downloading").update({
        "downloaded_bytes": written,
        "last_progress_at": utcnow(),
    })
    db.session.commit()


def _remove_part(part):
    try:
        if os.path.exists(part):
            os.remove(part)
    except OSError:
        pass


def _finalize(task, written: int, md5hex: str, part: str):
    """下载完成：写入正式存储、落 File 记录、扣除上传流量"""
    user = db.session.get(User, task.user_id)
    parent = file_service.get_dir_by_id(user, task.parent_id) if task.parent_id else None
    name = safe_filename(task.filename or "") or _guess_name(task.url, "download")
    name = _unique_name(user, parent, name)
    # 远端内容落地前同样做安全校验，避免绕过上传限制
    check_upload_security(name, read_file_head(part))
    ext = os.path.splitext(name)[1]
    storage_key = gen_storage_key(ext)
    point = storage_service.pick_point(written)
    storage_service.write_from_path(point, storage_key, part, move=True)

    f = File(
        user_id=user.id,
        parent_id=parent.id if parent else None,
        name=name,
        is_dir=False,
        size=written,
        storage_key=storage_key,
        storage_id=point.id,
        md5=md5hex,
    )
    db.session.add(f)
    db.session.flush()  # 先取得自增主键，再回填到任务记录
    consume_traffic(user, written, "upload")
    task.storage_key = storage_key
    task.storage_id = point.id
    task.file_id = f.id
    task.downloaded_bytes = written
    task.status = "done"
    task.finished_at = utcnow()
    db.session.commit()


def _unique_name(user, parent, name: str) -> str:
    stem, ext = os.path.splitext(name)
    pid = parent.id if parent else None
    candidate = name
    n = 1
    while File.query.filter(
            File.user_id == user.id,
            File.parent_id == pid,
            File.name == candidate,
            File.deleted_at.is_(None),
    ).first():
        candidate = f"{stem} 副本{'' if n == 1 else f' ({n})'}{ext}"
        n += 1
    return candidate


def _guess_name(url: str, fallback: str) -> str:
    path = urlsplit(url).path
    base = unquote(os.path.basename(path or "")).strip()
    return safe_filename(base) or fallback
