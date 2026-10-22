"""通用工具函数"""
import calendar
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

from flask import abort, session
from flask_login import current_user


def utcnow() -> datetime:
    """当前 UTC 时间（naive）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_admin_view_user():
    """管理员正在代管的目标用户；未代管返回 None"""
    if not current_user.is_authenticated or not current_user.is_admin:
        return None
    uid = session.get("admin_view_uid")
    if not uid:
        return None
    from app.extensions import db
    from app.models import User
    return db.session.get(User, int(uid))


def current_file_owner():
    """文件操作主体：管理员代管他人文件时为目标用户，否则为当前登录用户"""
    return get_admin_view_user() or current_user


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def gen_storage_key(ext: str = "") -> str:
    """生成物理存储文件名，避免路径穿越"""
    return uuid.uuid4().hex + ext


# 文件名长度上限，与 File.name 列宽保持一致
MAX_FILENAME_LEN = 512


def safe_filename(name: str) -> str:
    """清洗文件名，去除路径分隔符与危险字符；超长时截断但保留扩展名"""
    name = os.path.basename(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    if name in ("", ".", ".."):
        name = "untitled"
    if len(name) <= MAX_FILENAME_LEN:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext) >= MAX_FILENAME_LEN:
        return name[:MAX_FILENAME_LEN]
    return stem[:MAX_FILENAME_LEN - len(ext)] + ext


def guess_mimetype(filename: str) -> str:
    """按扩展名推断响应类型；未知类型回退为二进制流"""
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def content_disposition(filename: str) -> str:
    """构造带 ASCII 回退名与 RFC 5987 编码名的 Content-Disposition"""
    stem, ext = os.path.splitext(filename)
    ascii_stem = re.sub(r"[^\x20-\x7e]", "", stem).replace('"', "_").replace("\\", "_").strip()
    ascii_ext = re.sub(r"[^\x20-\x7e]", "", ext)
    fallback = (ascii_stem or "download") + ascii_ext
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


# 允许上传的扩展名白名单：只有明确列出的类型可以通过校验。
# 可执行文件、动态库、脚本与服务端脚本（exe/dll/so/sh/bat/ps1/py/pl/rb/jar/php/jsp/cgi
# 等）、安装包（msi/deb/rpm/dmg/pkg/apk/ipa 等）一律不在白名单内，因此不可上传。
ALLOWED_EXTS = {
    # 文档与电子书
    ".pdf", ".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".rtf", ".txt", ".text",
    ".md", ".markdown", ".mdx", ".tex", ".latex", ".bib", ".pages", ".wps", ".xps", ".oxps",
    ".hwp", ".hwpx", ".caj", ".epub", ".mobi", ".azw", ".azw3", ".fb2", ".djvu", ".cbz", ".cbr",
    # 字幕、日历、联系人
    ".srt", ".ass", ".ssa", ".vtt", ".lrc", ".sub", ".ics", ".vcf",
    # 表格与结构化数据
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".xlt", ".xltx", ".ods", ".fods", ".numbers", ".et",
    ".csv", ".tsv", ".psv", ".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".properties", ".sql", ".db", ".db3", ".sqlite", ".sqlite3",
    ".mdb", ".accdb", ".dbf", ".parquet", ".avro", ".orc", ".arrow", ".feather",
    ".h5", ".hdf5", ".nc", ".mat", ".npy", ".npz", ".sav", ".dta", ".rdata", ".rda", ".por",
    ".sas7bdat", ".xpt", ".dcm", ".fits",
    # 演示文稿
    ".ppt", ".pptx", ".pptm", ".pot", ".potx", ".pps", ".ppsx", ".odp", ".dps", ".key",
    # 图片与设计稿
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".dib", ".webp", ".tif", ".tiff",
    ".svg", ".svgz", ".ico", ".cur", ".heic", ".heif", ".avif", ".apng", ".jxl", ".tga",
    ".pcx", ".ppm", ".pgm", ".pbm", ".xbm", ".xpm", ".wbmp", ".emf", ".wmf",
    ".raw", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf", ".raf",
    ".rw2", ".pef", ".srw", ".x3f", ".psd", ".psb", ".ai", ".eps", ".indd",
    ".sketch", ".fig", ".xcf", ".afdesign", ".afphoto", ".cdr",
    # 音频
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".wma",
    ".aiff", ".aif", ".aifc", ".ape", ".amr", ".awb", ".mid", ".midi", ".ac3", ".dts",
    ".mka", ".mp2", ".mpga", ".au", ".wv", ".tak", ".tta", ".dsf", ".dff", ".caf", ".spx",
    # 视频
    ".mp4", ".m4v", ".mov", ".qt", ".avi", ".mkv", ".wmv", ".flv", ".f4v", ".webm",
    ".mpg", ".mpeg", ".mpe", ".m2v", ".3gp", ".3g2", ".ts", ".m2ts", ".mts", ".ogv",
    ".rm", ".rmvb", ".asf", ".vob", ".mxf", ".wtv", ".amv",
    # 压缩包（zip 可在网盘内在线解压）
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tbz2", ".xz", ".txz",
    ".lz", ".lzma", ".lzo", ".lz4", ".zst", ".z", ".cab", ".arj", ".ace", ".sit", ".sitx", ".cpio",
    # 光盘 / 磁盘镜像
    ".iso", ".img", ".ima", ".nrg", ".mdf", ".mds", ".ccd", ".cue",
    ".vhd", ".vhdx", ".vmdk", ".vdi", ".qcow2", ".wim",
    # 字体
    ".ttf", ".ttc", ".otf", ".woff", ".woff2", ".eot", ".fon", ".fnt",
    ".pfb", ".pfm", ".afm", ".bdf", ".pcf", ".sfnt",
    # 网页静态资源（服务端不解析，仅作为文件保存）
    ".html", ".htm", ".xhtml", ".css", ".js", ".mjs", ".cjs", ".map",
    ".scss", ".sass", ".less", ".styl",
    # 备份文件
    ".bak", ".backup", ".bkp", ".old", ".dump",
}

# 文件头特征：识别伪装成普通文件的程序或脚本
_BINARY_MAGIC = (
    (b"MZ", "Windows 可执行文件"),
    (b"\x7fELF", "Linux 可执行文件"),
    (b"\xca\xfe\xba\xbe", "Java 字节码"),
    (b"\xfe\xed\xfa\xce", "macOS 可执行文件"),
    (b"\xcf\xfa\xed\xfe", "macOS 可执行文件"),
    (b"\xed\xab\xee\xdb", "安装包"),
)
_TEXT_MAGIC = (("<?php", "PHP 脚本"), ("#!", "脚本文件"))

UPLOAD_SNIFF_LEN = 4096


def read_file_head(path: str, length: int = UPLOAD_SNIFF_LEN) -> bytes:
    """读取文件头部若干字节，供内容嗅探使用"""
    with open(path, "rb") as fh:
        return fh.read(length)


def sniff_upload_head(head: bytes) -> str:
    """按文件头判断是否疑似程序/脚本，返回类型描述；未命中返回空串"""
    if not head:
        return ""
    for magic, label in _BINARY_MAGIC:
        if head.startswith(magic):
            return label
    low = head.lower()
    for magic, label in _TEXT_MAGIC:
        if low.startswith(magic.encode()):
            return label
    return ""


def extension_allowed(filename: str) -> bool:
    """扩展名是否在白名单内；无扩展名时放行，交由文件头嗅探兜底"""
    ext = os.path.splitext(filename)[1].lower()
    return not ext or ext in ALLOWED_EXTS


def check_extension(filename: str):
    """白名单校验扩展名，不在白名单内抛 ValueError"""
    ext = os.path.splitext(filename)[1].lower()
    if ext and ext not in ALLOWED_EXTS:
        raise ValueError(f"不支持的文件类型 {ext}，仅允许上传文档、图片、音视频、压缩包等常见格式")


def check_upload_security(filename: str, head: bytes):
    """上传安全校验：扩展名白名单 + 文件头嗅探，命中抛 ValueError"""
    check_extension(filename)
    label = sniff_upload_head(head)
    if label:
        raise ValueError(f"文件内容疑似{label}，与扩展名不符，已拒绝上传")


def human_size(num: int) -> str:
    """字节数转为可读字符串"""
    if num is None:
        return "不限"
    if num < 0:
        num = 0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.2f}{unit}"
        num /= 1024
    return f"{num:.2f}PB"


def human_speed(bps) -> str:
    if bps is None:
        return "不限"
    return human_size(bps) + "/s"


# ---------------- 时长工具 ----------------
# 可选单位（月按 30 天折算，便于统一存秒）
DURATION_UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "month": 2592000,
}
DURATION_UNIT_LABELS = {
    "second": "秒",
    "minute": "分",
    "hour": "时",
    "day": "天",
    "month": "月",
}
# 展示时自动换算的单位（不含月，避免 30 天被写成 1 月）
_DISPLAY_UNITS = (("day", 86400), ("hour", 3600), ("minute", 60))


def parse_duration(value, unit="second") -> int:
    """「数值 + 单位」换算为整数秒"""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError("时长格式不正确")
    factor = DURATION_UNITS.get(str(unit or "").lower())
    if factor is None:
        raise ValueError("时长单位不支持")
    seconds = int(round(num * factor))
    if seconds <= 0:
        raise ValueError("时长必须大于 0")
    # 上限 100 年：防止超大数值在 timedelta 换算时溢出导致 500
    if seconds > 100 * 365 * 86400:
        raise ValueError("时长超出允许范围（最长 100 年）")
    return seconds


def split_duration(seconds):
    """秒数拆成「最合适的单位 + 数值」，个位尽量保留非零数"""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return 0, "second"
    for unit, factor in _DISPLAY_UNITS:
        if seconds >= factor:
            value = seconds / factor
            # 整除时返回整数，便于表单回填（30 而不是 30.0）
            return (int(value) if value.is_integer() else round(value, 2)), unit
    return seconds, "second"


def human_duration(seconds) -> str:
    """秒数转为可读时长，自动换算单位"""
    if seconds is None:
        return "不限"
    value, unit = split_duration(seconds)
    text = f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return f"{text}{DURATION_UNIT_LABELS[unit]}"


def add_months(dt: datetime, months: int) -> datetime:
    """按自然月顺延（精确月），当月天数不足时取月末"""
    months = int(months)
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))
