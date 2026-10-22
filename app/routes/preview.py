"""在线预览路由（仅 VIP/SVIP）"""
import os

from flask import (
    Blueprint, render_template, request, send_file, abort, Response, current_app,
)
from flask_login import login_required

from app.routes.transfer import current_target, waiting_redirect
from app.services import file_service
from app.services.quota_service import QuotaError, check_preview
from app.utils.helpers import current_file_owner

preview_bp = Blueprint("preview", __name__)


@preview_bp.before_request
def _check_enabled():
    """功能开关（config.yml: features.enable_preview）"""
    if not current_app.config["ENABLE_PREVIEW"]:
        abort(403, "在线预览功能已关闭")


# 支持的预览类型
TEXT_EXT = {".txt", ".md", ".py", ".js", ".html", ".css", ".json", ".xml", ".log", ".csv", ".yml", ".yaml", ".ini", ".conf", ".sh"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXT = {".mp4", ".webm", ".ogg", ".mov"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}


@preview_bp.route("/preview/<int:file_id>")
@login_required
def view(file_id):
    owner = current_file_owner()
    f = file_service.get_owned_file(owner, file_id)
    if f.is_dir:
        abort(400, "无法预览文件夹")
    if f.is_lost:
        abort(404, "文件已丢失")

    try:
        check_preview(owner)
    except QuotaError as e:
        abort(403, str(e))

    ext = os.path.splitext(f.name)[1].lower()
    path, task = file_service.local_path_or_pending(f)
    if task is not None:
        # 远端文件先取回，完成后自动回到本页；页面里的 stream 请求即能命中缓存
        return waiting_redirect(task.token, current_target(), f.name)
    if not path or not os.path.exists(path):
        abort(404, "文件实体丢失")

    if ext in IMAGE_EXT:
        kind = "image"
    elif ext in VIDEO_EXT:
        kind = "video"
    elif ext in AUDIO_EXT:
        kind = "audio"
    elif ext == ".pdf":
        kind = "pdf"
    elif ext in TEXT_EXT:
        kind = "text"
    else:
        kind = "unsupported"

    content = None
    if kind == "text":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()[:200000]
        except Exception:
            content = "（无法读取文件内容）"

    return render_template("preview/view.html", f=f, kind=kind, content=content,
                           ext=ext.lstrip("."))


@preview_bp.route("/preview/<int:file_id>/stream")
@login_required
def stream(file_id):
    """图片/视频/音频/PDF 流式输出（视频支持 Range）"""
    owner = current_file_owner()
    f = file_service.get_owned_file(owner, file_id)
    if f.is_dir:
        abort(400)
    if f.is_lost:
        abort(404, "文件已丢失")
    try:
        check_preview(owner)
    except QuotaError as e:
        abort(403, str(e))

    path, task = file_service.local_path_or_pending(f)
    if task is not None:
        # 正常路径下 view 已先取回；直接访问本地址时同样跳进度页等待
        return waiting_redirect(task.token, current_target(), f.name)
    if not path or not os.path.exists(path):
        abort(404)

    ext = os.path.splitext(f.name)[1].lower()
    mimetype = _guess_mime(ext)
    if ext in VIDEO_EXT or ext in AUDIO_EXT:
        return _range_response(path, mimetype)
    resp = send_file(path, mimetype=mimetype)
    if ext == ".svg":
        # SVG 可内嵌脚本，内联展示时禁脚本执行，防存储型 XSS
        resp.headers["Content-Security-Policy"] = "script-src 'none'"
    return resp


def _guess_mime(ext):
    m = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
        ".mp4": "video/mp4", ".webm": "video/webm", ".ogg": "video/ogg", ".mov": "video/quicktime",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".m4a": "audio/mp4",
        ".pdf": "application/pdf",
    }
    return m.get(ext, "application/octet-stream")


def _range_response(path, mimetype):
    """支持 HTTP Range 的响应，用于视频拖动播放"""
    size = os.path.getsize(path)
    range_header = request.headers.get("Range")

    if not range_header:
        resp = send_file(path, mimetype=mimetype)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    try:
        byte_range = range_header.replace("bytes=", "").split("-")
        if byte_range[0] == "":
            # 后缀请求 bytes=-N：取文件末尾 N 字节
            suffix = int(byte_range[1])
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(byte_range[0])
            end = int(byte_range[1]) if len(byte_range) > 1 and byte_range[1] else size - 1
            end = min(end, size - 1)
    except (ValueError, IndexError):
        abort(400, "无效的 Range 请求")

    if start < 0 or start >= size or start > end:
        # 416 Range Not Satisfiable
        resp = Response(status=416)
        resp.headers["Content-Range"] = f"bytes */{size}"
        return resp

    length = end - start + 1

    def generate():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            chunk = 256 * 1024
            while remaining > 0:
                data = fh.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(generate(), status=206, mimetype=mimetype,
                    direct_passthrough=True)
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    return resp
