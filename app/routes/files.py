"""文件路由：浏览/上传/下载/增删改/分片/秒传/批量操作"""
import os
import time

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    send_file, jsonify, current_app, abort, Response, after_this_request,
)
from flask_login import login_required

from app.extensions import db
from app.models import File, UploadSession
from app.routes.transfer import current_target, waiting_redirect
from app.services import file_service, transfer_service
from app.services.quota_service import (
    QuotaError, check_upload, check_download, get_speed_limit,
    consume_traffic, effective_quota,
)
from app.utils.helpers import (
    utcnow, current_file_owner, safe_filename, check_extension,
    content_disposition, guess_mimetype,
)

files_bp = Blueprint("files", __name__)

_SORT_CHOICES = ("name", "size", "time")
_ORDER_CHOICES = ("asc", "desc")


def _parse_ids(raw) -> list:
    out = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _pending_files(items) -> dict:
    """列表中的在途远端传输任务：{文件 id: {"kind": ..., "token": ...}}

    用于在文件列表上标记「取回中 / 上传中」，并轮询到全部结束为止。
    """
    pending = transfer_service.pending_map()
    if not pending:
        return {}
    out = {}
    for f in items:
        if f.is_dir or not f.storage_key or f.storage_id is None:
            continue
        task = pending.get((int(f.storage_id), f.storage_key))
        if task is not None:
            out[f.id] = {"kind": task.kind, "token": task.token}
    return out


@files_bp.route("/files")
@files_bp.route("/files/<int:parent_id>")
@login_required
def index(parent_id=None):
    owner = current_file_owner()
    sort = request.args.get("sort") if request.args.get("sort") in _SORT_CHOICES else "name"
    order = request.args.get("order") if request.args.get("order") in _ORDER_CHOICES else "asc"
    keyword = (request.args.get("q") or "").strip()
    q = effective_quota(owner)
    lost_count = File.query.filter(
        File.user_id == owner.id,
        File.is_lost == True,  # noqa: E712
        File.deleted_at.is_(None),
    ).count()

    if keyword:
        items = file_service.search(owner, keyword, sort, order)
        return render_template("files/index.html", parent=None, items=items, crumbs=[],
                               q=q, search=keyword, sort=sort, order=order, owner=owner,
                               lost_count=lost_count, pending=_pending_files(items))

    parent, items = file_service.list_dir(owner, parent_id, sort, order)
    crumbs = file_service.get_breadcrumb(parent)
    return render_template("files/index.html", parent=parent, items=items, crumbs=crumbs,
                           q=q, search="", sort=sort, order=order, owner=owner,
                           lost_count=lost_count, pending=_pending_files(items))


@files_bp.route("/files/folder/new", methods=["POST"])
@login_required
def create_folder():
    owner = current_file_owner()
    parent_id = request.form.get("parent_id") or None
    name = request.form.get("name") or ""
    try:
        file_service.create_folder(owner, parent_id, name)
        flash("文件夹创建成功", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/upload", methods=["POST"])
@login_required
def upload():
    """普通（小文件）上传"""
    owner = current_file_owner()
    parent_id = request.form.get("parent_id") or None
    f = request.files.get("file")
    # 页面用 fetch 调用本接口，flash + 重定向的响应体会被丢弃导致用户看不到提示，
    # 故对 AJAX 请求直接返回 JSON 错误
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def fail(message: str):
        if ajax:
            return jsonify(error=message), 400
        flash(message, "danger")
        return redirect(request.referrer or url_for("files.index"))

    if f is None:
        return fail("未选择文件")

    # 读取文件大小用于容量校验
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)

    try:
        check_upload(owner, size)
    except QuotaError as e:
        return fail(str(e))

    try:
        file_service.save_uploaded_file(
            owner, parent_id, f.filename, size, f, mime=f.mimetype
        )
        consume_traffic(owner, size, "upload")
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return fail(f"上传失败：{e}")
    if ajax:
        return jsonify(ok=True)
    flash("上传成功", "success")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/download")
@login_required
def download(file_id):
    owner = current_file_owner()
    f = file_service.get_owned_file(owner, file_id)
    if f.is_dir:
        return _download_folder(owner, f)
    return _download_file(owner, f)


def _download_file(owner, f: File):
    if f.is_lost:
        abort(404, "文件已丢失")
    path, task = file_service.local_path_or_pending(f)
    if task is not None:
        # 远端文件已入队后台取回：跳进度页，取回完成后自动回到本地址继续下载
        return waiting_redirect(task.token, current_target(), f.name)
    if not path or not os.path.exists(path):
        abort(404, "文件实体丢失")

    # 一律以真实文件大小为准：库中记录可能过期，浏览器会按 Content-Length 校验
    size = os.path.getsize(path)

    try:
        check_download(owner, size)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    consume_traffic(owner, size, "download")
    db.session.commit()

    limit = get_speed_limit(owner)  # None = 不限
    if limit:
        resp = _stream_file(path, limit)
        resp.headers["Content-Type"] = guess_mimetype(f.name)
    else:
        # 不限速时交给 send_file：自带正确的 Content-Length/ETag/Last-Modified/Range
        resp = send_file(path, mimetype=guess_mimetype(f.name), as_attachment=True,
                         download_name=f.name)
    resp.headers["Content-Disposition"] = content_disposition(f.name)
    return resp


def _stream_file(path, speed_limit, chunk=256 * 1024):
    """带限速的文件流式响应（限速时无法交给 send_file 处理）"""
    def generate():
        with open(path, "rb") as fh:
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                yield data
                time.sleep(len(data) / speed_limit)

    resp = Response(generate(), direct_passthrough=True)
    resp.headers["Content-Length"] = str(os.path.getsize(path))
    return resp


def _download_folder(owner, f: File):
    """文件夹打包 zip 下载：先写临时文件再回传"""
    import zipfile
    import tempfile

    total = file_service.tree_size(f)
    try:
        check_download(owner, total)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    # 远端文件先入队后台取回，全部到位后再打包，避免请求线程被拖到超时
    tokens = file_service.prefetch_files([f])
    if tokens:
        return waiting_redirect(tokens, current_target(), f.name)

    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            file_service.add_dir_to_zip(zf, f, "")
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise

    consume_traffic(owner, total, "download")
    db.session.commit()

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return resp

    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name=f"{f.name}.zip")


@files_bp.route("/files/<int:file_id>/rename", methods=["POST"])
@login_required
def rename(file_id):
    name = request.form.get("name") or ""
    try:
        file_service.rename(current_file_owner(), file_id, name)
        flash("重命名成功", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/move", methods=["POST"])
@login_required
def move(file_id):
    target = request.form.get("target_parent_id") or None
    try:
        file_service.move(current_file_owner(), file_id, target)
        flash("移动成功", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/copy", methods=["POST"])
@login_required
def copy(file_id):
    target = request.form.get("target_parent_id") or None
    try:
        file_service.copy(current_file_owner(), file_id, target)
        flash("复制成功", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(file_id):
    try:
        file_service.soft_delete(current_file_owner(), file_id)
        db.session.commit()
        flash("已移入回收站", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


# ---------- 批量操作 ----------

def _batch_run(func, ids, action_label):
    ok = fail = 0
    for fid in ids:
        try:
            func(fid)
            ok += 1
        except (ValueError, PermissionError, QuotaError):
            fail += 1
    db.session.commit()
    if ok:
        flash(f"已{action_label} {ok} 项" + (f"，{fail} 项失败" if fail else ""),
              "success" if fail == 0 else "warning")
    elif fail:
        flash(f"操作失败：{fail} 项不符合条件", "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/batch/delete", methods=["POST"])
@login_required
def batch_delete():
    ids = _parse_ids(request.form.getlist("ids"))
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    return _batch_run(
        lambda fid: file_service.soft_delete(current_file_owner(), fid), ids, "移入回收站")


@files_bp.route("/files/batch/move", methods=["POST"])
@login_required
def batch_move():
    ids = _parse_ids(request.form.getlist("ids"))
    target = request.form.get("target_parent_id") or None
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    return _batch_run(
        lambda fid: file_service.move(current_file_owner(), fid, target), ids, "移动")


@files_bp.route("/files/batch/copy", methods=["POST"])
@login_required
def batch_copy():
    ids = _parse_ids(request.form.getlist("ids"))
    target = request.form.get("target_parent_id") or None
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    return _batch_run(
        lambda fid: file_service.copy(current_file_owner(), fid, target), ids, "复制")


@files_bp.route("/files/api/paste", methods=["POST"])
@login_required
def paste():
    """剪贴板粘贴：mode=copy 复制 / mode=cut 剪切并移动"""
    payload = request.get_json(silent=True) or {}
    ids = _parse_ids(payload.get("ids") or [])
    mode = payload.get("mode")
    target = payload.get("target")
    if mode not in ("copy", "cut"):
        return jsonify(ok=False, error="无效的操作类型"), 400
    if not ids:
        return jsonify(ok=False, error="剪贴板为空"), 400

    target = None if target in (None, "", 0, "0") else target
    if target is not None:
        try:
            target = int(target)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="目标文件夹无效"), 400

    fn = file_service.copy if mode == "copy" else file_service.move
    owner = current_file_owner()
    done, failed = 0, []
    for fid in ids:
        try:
            fn(owner, fid, target)
            done += 1
        except (ValueError, PermissionError, QuotaError) as e:
            failed.append(str(e))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error="操作失败，请重试"), 500
    return jsonify(ok=True, done=done, failed=failed[:5])


@files_bp.route("/files/batch/download")
@login_required
def batch_download():
    """将选中的文件/文件夹打包为一个 zip 下载"""
    import zipfile
    import tempfile

    owner = current_file_owner()
    ids = _parse_ids(request.args.getlist("ids"))
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))

    nodes = []
    for fid in ids:
        try:
            nodes.append(file_service.get_owned_file(owner, fid))
        except PermissionError:
            continue
    if not nodes:
        abort(404, "所选文件不存在")

    total = sum(file_service.tree_size(n) for n in nodes)
    try:
        check_download(owner, total)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    # 远端文件先入队后台取回，全部到位后再打包
    tokens = file_service.prefetch_files(nodes)
    if tokens:
        return waiting_redirect(tokens, current_target(), "所选文件")

    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for n in nodes:
                if n.is_dir:
                    file_service.add_dir_to_zip(zf, n, n.name + "/")
                elif not n.is_lost:
                    p = file_service.get_physical_path(n.storage_key, n.storage_id)
                    if os.path.exists(p):
                        zf.write(p, n.name)
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise

    consume_traffic(owner, total, "download")
    db.session.commit()

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return resp

    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name="files.zip")


# ---------- 在线压缩 / 解压 ----------

@files_bp.route("/files/compress", methods=["POST"])
@login_required
def compress():
    """把选中的文件/文件夹压缩为 zip 存入网盘"""
    ids = _parse_ids(request.form.getlist("ids"))
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    parent_id = request.form.get("parent_id") or None
    try:
        nodes = file_service.owned_files(current_file_owner(), ids)
        # 远端文件先入队后台取回，全部到位后再压缩
        tokens = file_service.prefetch_files(nodes)
        if tokens:
            return waiting_redirect(tokens, current_target(), "所选文件",
                                    method="post",
                                    fields={"ids": ids, "parent_id": parent_id})
        f = file_service.compress(current_file_owner(), ids, parent_id)
        flash(f"已生成压缩包「{f.name}」", "success")
    except (ValueError, PermissionError, QuotaError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/extract", methods=["POST"])
@login_required
def extract(file_id):
    """解压 zip 压缩包到其所在目录"""
    owner = current_file_owner()
    parent_id = request.form.get("parent_id")
    if parent_id in (None, ""):
        try:
            parent_id = file_service.get_owned_file(owner, file_id).parent_id
        except (PermissionError, ValueError) as e:
            flash(str(e), "danger")
            return redirect(request.referrer or url_for("files.index"))
    try:
        node = file_service.get_owned_file(owner, file_id)
    except (PermissionError, ValueError) as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))
    # 远端压缩包先入队后台取回，到位后再解压
    tokens = file_service.prefetch_files([node])
    if tokens:
        return waiting_redirect(tokens, current_target(), node.name,
                                method="post", fields={"parent_id": parent_id})
    try:
        count, skipped = file_service.extract_zip(owner, file_id, parent_id)
        if skipped:
            shown = "、".join(skipped[:5])
            more = f" 等 {len(skipped)} 个" if len(skipped) > 5 else ""
            flash(f"解压完成，共 {count} 个文件；已跳过不支持的 {shown}{more}", "warning")
        else:
            flash(f"解压完成，共 {count} 个文件", "success")
    except (ValueError, PermissionError, QuotaError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


# ---------- 分片上传 ----------

@files_bp.route("/api/upload/init", methods=["POST"])
@login_required
def upload_init():
    owner = current_file_owner()
    data = request.get_json() or {}
    raw_name = data.get("filename") or ""
    try:
        total_size = int(data.get("total_size") or 0)
        chunk_size = int(data.get("chunk_size") or current_app.config["CHUNK_SIZE"])
    except (TypeError, ValueError):
        return jsonify(error="参数错误"), 400
    # 分片大小夹取在 64KB ~ 64MB，防止过小刷爆会话表或过大撑爆内存/磁盘
    chunk_size = max(64 * 1024, min(chunk_size, 64 * 1024 * 1024))
    md5 = data.get("md5")
    parent_id = data.get("parent_id") or None

    if not raw_name or total_size <= 0:
        return jsonify(error="参数错误"), 400

    filename = safe_filename(raw_name)
    try:
        check_extension(filename)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    try:
        check_upload(owner, total_size)
    except QuotaError as e:
        return jsonify(error=str(e)), 400

    # 秒传：md5 命中且文件实体存在
    if md5:
        existing = File.query.filter_by(
            user_id=owner.id, md5=md5, is_dir=False, is_lost=False).first()
        if existing and existing.storage_key and file_service.cached_local_path(existing):
            try:
                f = file_service.create_reference(
                    owner, parent_id, filename, total_size,
                    mime=existing.mime, md5=md5, storage_key=existing.storage_key,
                    storage_id=existing.storage_id,
                )
            except ValueError as e:
                return jsonify(error=str(e)), 400
            consume_traffic(owner, total_size, "upload")
            db.session.commit()
            return jsonify(uploaded=True, fast=True, file_id=f.id)

    sess = UploadSession(
        user_id=owner.id, parent_id=parent_id, filename=filename,
        total_size=total_size, chunk_size=chunk_size, md5=md5,
    )
    db.session.add(sess)
    db.session.commit()
    return jsonify(uploaded=False, upload_id=sess.upload_id, chunk_size=chunk_size)


@files_bp.route("/api/upload/chunk", methods=["POST"])
@login_required
def upload_chunk():
    owner = current_file_owner()
    upload_id = request.form.get("upload_id")
    try:
        index = int(request.form.get("index"))
    except (TypeError, ValueError):
        return jsonify(error="分片索引不正确"), 400
    chunk = request.files.get("chunk")
    sess = UploadSession.query.filter_by(upload_id=upload_id, user_id=owner.id).first()
    if sess is None or sess.status != "uploading":
        return jsonify(error="上传会话无效"), 400
    if chunk is None:
        return jsonify(error="缺少分片"), 400
    expected = -(-sess.total_size // sess.chunk_size)  # 向上取整的总片数
    if not 0 <= index < expected:
        return jsonify(error="分片索引超出范围"), 400

    tmp_dir = os.path.join(current_app.config["UPLOAD_TMP_ROOT"], upload_id)
    os.makedirs(tmp_dir, exist_ok=True)
    chunk_path = os.path.join(tmp_dir, f"{index}.part")
    chunk.save(chunk_path)
    # 单个分片不允许超过声明的 chunk_size，防止绕过配额/流量校验
    if os.path.getsize(chunk_path) > sess.chunk_size:
        try:
            os.remove(chunk_path)
        except OSError:
            pass
        return jsonify(error="分片大小超过限制"), 400

    received = set(sess.received_chunks.split(",")) if sess.received_chunks else set()
    received.add(str(index))
    sess.received_chunks = ",".join(sorted(received, key=int))
    db.session.commit()
    return jsonify(ok=True, received=len(received))


@files_bp.route("/api/upload/complete", methods=["POST"])
@login_required
def upload_complete():
    owner = current_file_owner()
    data = request.get_json() or {}
    upload_id = data.get("upload_id")
    sess = UploadSession.query.filter_by(upload_id=upload_id, user_id=owner.id).first()
    if sess is None:
        return jsonify(error="上传会话无效"), 400

    tmp_dir = os.path.join(current_app.config["UPLOAD_TMP_ROOT"], upload_id)
    # 校验分片完整性：缺片直接合并会得到损坏文件
    received = sorted([int(i) for i in sess.received_chunks.split(",") if i])
    expected = -(-sess.total_size // sess.chunk_size)
    if received != list(range(expected)):
        return jsonify(error=f"分片不完整（已收 {len(received)}/{expected}），请补传后再完成"), 400

    merged = os.path.join(tmp_dir, "merged")
    with open(merged, "wb") as out:
        for i in received:
            p = os.path.join(tmp_dir, f"{i}.part")
            if not os.path.exists(p):
                out.close()
                try:
                    os.remove(merged)
                except OSError:
                    pass
                return jsonify(error=f"分片 {i} 文件缺失，请重新上传该分片"), 400
            with open(p, "rb") as fh:
                shutil_copyfileobj(fh, out)
    if os.path.getsize(merged) != sess.total_size:
        shutil_rmtree(tmp_dir)
        db.session.rollback()
        return jsonify(error="合并后文件大小与声明不符，请重新上传"), 400

    try:
        f = file_service.save_chunked_file(
            owner, sess.parent_id, sess.filename, sess.total_size,
            merged, md5=sess.md5, mime=None
        )
        consume_traffic(owner, sess.total_size, "upload")
        sess.status = "complete"
        sess.completed_at = utcnow()
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify(error=str(e)), 400
    except Exception as e:
        db.session.rollback()
        return jsonify(error=str(e)), 500
    finally:
        shutil_rmtree(tmp_dir)

    return jsonify(ok=True, file_id=f.id)


def shutil_copyfileobj(src, dst):
    import shutil
    shutil.copyfileobj(src, dst)


def shutil_rmtree(p):
    import shutil
    if os.path.exists(p):
        shutil.rmtree(p, ignore_errors=True)


# ---------- 回收站 ----------

@files_bp.route("/trash")
@login_required
def trash():
    owner = current_file_owner()
    items = file_service.list_trash(owner)
    return render_template("files/trash.html", items=items)


@files_bp.route("/files/<int:file_id>/restore", methods=["POST"])
@login_required
def restore(file_id):
    try:
        file_service.restore(current_file_owner(), file_id)
        db.session.commit()
        flash("已恢复", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(url_for("files.trash"))


@files_bp.route("/files/<int:file_id>/purge", methods=["POST"])
@login_required
def purge(file_id):
    try:
        file_service.hard_delete(current_file_owner(), file_id)
        flash("已彻底删除", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(url_for("files.trash"))


@files_bp.route("/trash/empty", methods=["POST"])
@login_required
def empty_trash():
    n = file_service.empty_trash(current_file_owner())
    flash(f"回收站已清空（{n} 项）", "success")
    return redirect(url_for("files.trash"))
