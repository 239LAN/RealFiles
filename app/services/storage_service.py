"""存储点管理：容量统计、均衡分配、本地磁盘 / FTP / SFTP / S3 后端读写

存储点（StoragePoint）是文件实体的物理落点：
- local：本机磁盘目录（可多挂载点）
- ftp：远端 FTP 服务器目录
- sftp：远端 SFTP（SSH）服务器目录
- s3：S3 兼容对象存储（MinIO / OSS 等，支持自定义 Endpoint）

远端存储点（ftp/sftp/s3）读取时先拉取到本地缓存再响应，缓存由后台任务按 TTL 清理。
传输本身通过 ProgressCallback 上报进度，供后台任务写入数据库、前端轮询展示。

容量规则：每个存储点必填容量上限，可用空间按容量的 90% 计（占用达 90% 即视为已满）。
写入时在「空间足够的可用存储点」中选择占用率最低者，使各点占用率趋于平均。
读写失败的存储点会被标记为不健康并暂停被选为写入点，恢复后由定时任务自动回归。
"""
import ftplib
import os
import shutil
import time
from datetime import timedelta
from typing import Callable

from flask import current_app
from sqlalchemy import update as sa_update

from app.extensions import db
from app.models import File, StoragePoint, TransferTask
from app.utils.helpers import read_file_head, utcnow

# 占用率达该比例即视为已满，同时作为可用空间计算基准
FULL_RATIO = 0.9
# FTP 操作超时（秒）
FTP_TIMEOUT = 30
# FTP 相关异常合集：ftplib.all_errors 本身已是元组，不能再嵌进 except 的元组里
FTP_ERRORS = tuple(ftplib.all_errors) + (OSError,)
# SFTP 连接超时（秒）
SFTP_TIMEOUT = 30
# 支持的存储点类型（管理页表单与校验共用）
STORAGE_KINDS = ("local", "ftp", "sftp", "s3")
# 本地容量探测失败时的兜底值
FALLBACK_CAPACITY = 10 * 1024 ** 3
# 最小容量限制（1MB），避免误填导致无法写入
MIN_CAPACITY = 1024 ** 2
# 传输分块大小（1MB）：读一块上报一次进度，避免回调过于频繁
TRANSFER_CHUNK = 1024 * 1024

# 进度回调：on_progress(已传输字节数, 总字节数)，总字节数未知时为 0
ProgressCallback = Callable[[int, int], None]


class StorageError(Exception):
    """存储点操作失败"""


class StorageFullError(StorageError):
    """没有可用的存储点空间"""


# ---------------- 容量统计 ----------------

def detect_local_capacity(path: str) -> int:
    """探测本地目录所在磁盘总容量，用于新建存储点的默认容量"""
    target = path
    while target and not os.path.exists(target):
        parent = os.path.dirname(target.rstrip("/\\"))
        if parent == target:
            break
        target = parent
    try:
        total = int(shutil.disk_usage(target or "/").total)
    except OSError:
        return FALLBACK_CAPACITY
    return total or FALLBACK_CAPACITY


def used_bytes(point) -> int:
    """存储点已用字节数（按文件记录聚合，容量不随物理扫描变化）"""
    if point is None:
        return 0
    return int(db.session.query(
        db.func.coalesce(db.func.sum(File.size), 0)
    ).filter(
        File.storage_id == point.id,
        File.is_dir == False,  # noqa: E712
        File.is_lost == False,  # noqa: E712
    ).scalar() or 0)


def usable_capacity(point) -> int:
    """可用容量上限（容量的 90%）"""
    return int(int(point.capacity_bytes or 0) * FULL_RATIO)


def remaining_bytes(point) -> int:
    """存储点剩余可用空间（0 表示已满）"""
    return max(usable_capacity(point) - used_bytes(point), 0)


def is_full(point) -> bool:
    return remaining_bytes(point) <= 0


def stats(point) -> dict:
    """存储点容量概况（管理页与判满共用）"""
    capacity = int(point.capacity_bytes or 0)
    used = used_bytes(point)
    usable = usable_capacity(point)
    return {
        "used": used,
        "capacity": capacity,
        "usable": usable,
        "remaining": max(usable - used, 0),
        "ratio": (used / capacity) if capacity > 0 else 1.0,
        "is_full": used >= usable,
        "files": File.query.filter(
            File.storage_id == point.id, File.is_dir == False,  # noqa: E712
            File.is_lost == False,  # noqa: E712
        ).count(),
    }


# ---------------- 写入点选择 ----------------

def enabled_points():
    return StoragePoint.query.filter_by(enabled=True).order_by(
        StoragePoint.sort, StoragePoint.id).all()


def default_point():
    """历史数据兜底：STORAGE_ROOT 对应的本地存储点"""
    root = current_app.config["STORAGE_ROOT"]
    point = StoragePoint.query.filter_by(kind="local", path=root).first()
    if point is None:
        point = StoragePoint.query.filter_by(kind="local").order_by(
            StoragePoint.sort, StoragePoint.id).first()
    return point


def get_point(storage_id):
    """按 id 取存储点；缺失时回退到默认本地存储点（兼容历史记录）"""
    if storage_id:
        point = db.session.get(StoragePoint, int(storage_id))
        if point is not None:
            return point
    return default_point()


class Allocator:
    """按占用率均衡分配写入点

    预留的字节会累加到占用率中，因此批量规划（如存储点转移）能自然摊平到各点。
    """

    def __init__(self, exclude_ids=()):
        self.exclude = {int(i) for i in exclude_ids}
        self.reserved = {}

    def candidates(self):
        """可写存储点：启用 + 健康 + 未被排除"""
        return [p for p in enabled_points()
                if p.id not in self.exclude and p.healthy]

    def pick(self, need_bytes: int = 0):
        """选出「能装下且占用率最低」的可用存储点"""
        need = max(int(need_bytes or 0), 0)
        best = None
        best_ratio = None
        for point in self.candidates():
            used = used_bytes(point) + self.reserved.get(point.id, 0)
            if used + need > usable_capacity(point):
                continue
            ratio = used / max(int(point.capacity_bytes or 0), 1)
            if best is None or ratio < best_ratio:
                best, best_ratio = point, ratio
        if best is None:
            if not self.candidates():
                raise StorageFullError("没有可用的存储点（已停用或异常摘除）")
            raise StorageFullError("没有可用的存储点空间")
        self.reserved[best.id] = self.reserved.get(best.id, 0) + need
        return best


def pick_point(need_bytes: int = 0):
    """单次写入的存储点选择"""
    return Allocator().pick(need_bytes)


# ---------------- 本地后端 ----------------

def _local_path(point, storage_key: str) -> str:
    root = point.path
    sub = storage_key[:2]
    d = os.path.join(root, sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, storage_key)


# ---------------- FTP 后端 ----------------

def _cache_path(point, storage_key: str, create: bool = True) -> str:
    root = current_app.config["STORAGE_CACHE_ROOT"]
    d = os.path.join(root, str(point.id), storage_key[:2])
    if create:
        os.makedirs(d, exist_ok=True)
    return os.path.join(d, storage_key)


def _part_path(cached: str) -> str:
    """中转文件路径：先写 .part，完整拿到后才 os.replace 成缓存文件"""
    return cached + ".part"


def _touch(cached: str) -> str:
    """命中缓存时续期 mtime，让热文件不被 TTL 清掉（缓存天然按 LRU 淘汰）"""
    try:
        os.utime(cached, None)
    except OSError:
        pass
    return cached


def _remove_quietly(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _copy_stream(src, dst, total: int = 0,
                 on_progress: ProgressCallback = None) -> int:
    """分块拷贝并上报进度，返回拷贝字节数"""
    done = 0
    while True:
        chunk = src.read(TRANSFER_CHUNK)
        if not chunk:
            break
        dst.write(chunk)
        done += len(chunk)
        if on_progress:
            on_progress(done, int(total or 0))
    return done


class _ProgressWriter:
    """把「写文件」包装成可回调的函数（FTP retrbinary 按块调用）"""

    def __init__(self, fh, total: int = 0, on_progress: ProgressCallback = None):
        self._fh = fh
        self._total = int(total or 0)
        self._on_progress = on_progress
        self.done = 0

    def __call__(self, chunk: bytes):
        self._fh.write(chunk)
        self.done += len(chunk)
        if self._on_progress:
            self._on_progress(self.done, self._total)


class _CountingReader:
    """统计被读走字节数的流包装（S3 上传无法拿到回调时用）"""

    def __init__(self, fh, total: int = 0, on_progress: ProgressCallback = None):
        self._fh = fh
        self._total = int(total or 0)
        self._on_progress = on_progress
        self.done = 0

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def read(self, size=-1):
        data = self._fh.read(size)
        if data:
            self.done += len(data)
            if self._on_progress:
                self._on_progress(self.done, self._total)
        return data


def _ftp_connect(point) -> ftplib.FTP:
    conn = ftplib.FTP()
    try:
        conn.connect(point.host, int(point.port or 21), timeout=FTP_TIMEOUT)
        conn.login(point.username or "anonymous", point.password or "")
        try:
            conn.encoding = "utf-8"
        except (AttributeError, LookupError):
            pass
        conn.set_pasv(True)
        return conn
    except FTP_ERRORS as e:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise StorageError(f"FTP 连接失败：{e}")


def _ftp_close(conn):
    try:
        conn.quit()
    except Exception:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _ftp_dir(point, storage_key: str) -> str:
    base = (point.remote_dir or "/").rstrip("/")
    return f"{base}/{storage_key[:2]}"


def _ftp_path(point, storage_key: str) -> str:
    return f"{_ftp_dir(point, storage_key)}/{storage_key}"


def _ftp_makedirs(conn, dirpath: str):
    if not dirpath:
        return
    absolute = dirpath.startswith("/")
    cur = "/" if absolute else ""
    for part in [p for p in dirpath.split("/") if p]:
        cur = f"{cur.rstrip('/')}/{part}" if cur else part
        if absolute and not cur.startswith("/"):
            cur = "/" + cur
        try:
            conn.mkd(cur)
        except ftplib.all_errors:
            # 目录已存在（550）时忽略；其他错误会在后续读写时暴露
            pass


def _ftp_fetch(point, storage_key: str,
               on_progress: ProgressCallback = None) -> str:
    """把 FTP 上的文件拉取到本地缓存并返回缓存路径（可按块上报进度）"""
    cached = _cache_path(point, storage_key)
    if os.path.exists(cached):
        return _touch(cached)
    tmp = _part_path(cached)
    conn = _ftp_connect(point)
    try:
        remote = _ftp_path(point, storage_key)
        total = 0
        try:
            total = int(conn.size(remote) or 0)
        except FTP_ERRORS:
            total = 0  # 部分服务器不支持 SIZE，进度按总量未知处理
        with open(tmp, "wb") as out:
            conn.retrbinary("RETR " + remote, _ProgressWriter(out, total, on_progress))
        os.replace(tmp, cached)
        return cached
    except FTP_ERRORS as e:
        _remove_quietly(tmp)
        raise StorageError(f"FTP 读取失败：{e}")
    finally:
        _ftp_close(conn)


def _ftp_store(point, storage_key: str, stream, size: int = 0,
               on_progress: ProgressCallback = None):
    conn = _ftp_connect(point)
    try:
        _ftp_makedirs(conn, _ftp_dir(point, storage_key))
        if hasattr(stream, "seek"):
            stream.seek(0)
        total = int(size or 0)
        done = 0

        def _tick(block):
            nonlocal done
            done += len(block)
            if on_progress:
                on_progress(done, total)

        conn.storbinary("STOR " + _ftp_path(point, storage_key), stream,
                        callback=_tick if on_progress else None)
    except FTP_ERRORS as e:
        raise StorageError(f"FTP 写入失败：{e}")
    finally:
        _ftp_close(conn)


def _ftp_delete(point, storage_key: str):
    conn = _ftp_connect(point)
    try:
        conn.delete(_ftp_path(point, storage_key))
    except ftplib.error_perm as e:
        # 550 = 文件不存在，视为删除成功
        if "550" not in str(e):
            raise StorageError(f"FTP 删除失败：{e}")
    except FTP_ERRORS as e:
        raise StorageError(f"FTP 删除失败：{e}")
    finally:
        _ftp_close(conn)


# ---------------- SFTP 后端 ----------------

def _sftp_connect(point):
    """建立 SFTP 连接（paramiko，默认 22 端口，用户名 / 密码认证）"""
    try:
        import paramiko
    except ImportError as e:  # 依赖缺失时给出可读提示
        raise StorageError("未安装 paramiko，无法使用 SFTP 存储点") from e
    if not (point.host or "").strip():
        raise StorageError("请填写 SFTP 服务器地址")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=(point.host or "").strip(),
            port=int(point.port or 22),
            username=(point.username or "").strip() or None,
            password=point.password or None,
            timeout=SFTP_TIMEOUT,
            banner_timeout=SFTP_TIMEOUT,
            auth_timeout=SFTP_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        return client
    except Exception as e:  # noqa: BLE001 —— paramiko 异常类型繁多，统一包装
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        raise StorageError(f"SFTP 连接失败：{e}")


def _sftp_close(client):
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


def _sftp_dir(point, storage_key: str) -> str:
    base = (point.remote_dir or "/").rstrip("/")
    return f"{base}/{storage_key[:2]}"


def _sftp_path(point, storage_key: str) -> str:
    return f"{_sftp_dir(point, storage_key)}/{storage_key}"


def _sftp_makedirs(sftp, dirpath: str):
    """逐级创建远端目录（已存在则忽略）"""
    if not dirpath:
        return
    absolute = dirpath.startswith("/")
    cur = "/" if absolute else ""
    for part in [p for p in dirpath.split("/") if p]:
        cur = f"{cur.rstrip('/')}/{part}" if cur else part
        if absolute and not cur.startswith("/"):
            cur = "/" + cur
        try:
            sftp.stat(cur)
        except OSError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def _sftp_fetch(point, storage_key: str,
                on_progress: ProgressCallback = None) -> str:
    """把 SFTP 上的文件拉取到本地缓存并返回缓存路径（可按块上报进度）"""
    cached = _cache_path(point, storage_key)
    if os.path.exists(cached):
        return _touch(cached)
    tmp = _part_path(cached)
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            # paramiko 的 callback(已传输字节数, 总字节数) 与我们的签名一致
            sftp.get(_sftp_path(point, storage_key), tmp, callback=on_progress)
        finally:
            sftp.close()
        os.replace(tmp, cached)
        return cached
    except Exception as e:  # noqa: BLE001
        _remove_quietly(tmp)
        raise StorageError(f"SFTP 读取失败：{e}")
    finally:
        _sftp_close(client)


def _sftp_head(point, storage_key: str, length: int) -> bytes:
    """只取远端文件头部若干字节，避免整文件拉取"""
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(_sftp_path(point, storage_key), "rb") as remote:
                return remote.read(length)
        finally:
            sftp.close()
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"SFTP 读取失败：{e}")
    finally:
        _sftp_close(client)


def _sftp_store(point, storage_key: str, stream, size: int = 0,
                on_progress: ProgressCallback = None):
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            _sftp_makedirs(sftp, _sftp_dir(point, storage_key))
            if hasattr(stream, "seek"):
                stream.seek(0)
            with sftp.open(_sftp_path(point, storage_key), "wb") as remote:
                _copy_stream(stream, remote, size, on_progress)
        finally:
            sftp.close()
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"SFTP 写入失败：{e}")
    finally:
        _sftp_close(client)


def _sftp_delete(point, storage_key: str):
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            sftp.remove(_sftp_path(point, storage_key))
        except OSError as e:
            # 文件不存在视为删除成功
            if getattr(e, "errno", None) != 2 and "No such file" not in str(e):
                raise StorageError(f"SFTP 删除失败：{e}")
        finally:
            sftp.close()
    finally:
        _sftp_close(client)


def _sftp_exists(point, storage_key: str) -> bool:
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            sftp.stat(_sftp_path(point, storage_key))
            return True
        except OSError:
            return False
        finally:
            sftp.close()
    finally:
        _sftp_close(client)


# ---------------- S3 后端（兼容 MinIO / OSS 等） ----------------

def _s3_client(point):
    """构建 S3 客户端；endpoint 留空则使用 AWS 默认地址"""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as e:
        raise StorageError("未安装 boto3，无法使用 S3 存储点") from e
    cfg = BotoConfig(
        signature_version="s3v4",
        retries={"max_attempts": 3},
        s3={"addressing_style": "path" if point.path_style else "auto"},
    )
    try:
        return boto3.client(
            "s3",
            endpoint_url=(point.endpoint or "").strip() or None,
            aws_access_key_id=(point.access_key or "").strip() or None,
            aws_secret_access_key=(point.secret_key or "").strip() or None,
            region_name=(point.region or "").strip() or None,
            config=cfg,
        )
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 客户端初始化失败：{e}")


def _s3_bucket(point) -> str:
    bucket = (point.bucket or "").strip()
    if not bucket:
        raise StorageError("请填写 S3 Bucket")
    return bucket


def _s3_key(point, storage_key: str) -> str:
    """对象键：<remote_dir 作为前缀>/<分片目录>/<storage_key>"""
    prefix = (point.remote_dir or "").strip().strip("/")
    sub = storage_key[:2]
    return f"{prefix}/{sub}/{storage_key}" if prefix else f"{sub}/{storage_key}"


def _s3_fetch(point, storage_key: str,
              on_progress: ProgressCallback = None) -> str:
    """把 S3 对象下载到本地缓存并返回缓存路径（分块读取以便上报进度）"""
    cached = _cache_path(point, storage_key)
    if os.path.exists(cached):
        return _touch(cached)
    tmp = _part_path(cached)
    try:
        client = _s3_client(point)
        resp = client.get_object(
            Bucket=_s3_bucket(point), Key=_s3_key(point, storage_key))
        body = resp["Body"]
        total = int(resp.get("ContentLength") or 0)
        try:
            with open(tmp, "wb") as out:
                _copy_stream(body, out, total, on_progress)
        finally:
            body.close()
        os.replace(tmp, cached)
        return cached
    except StorageError:
        _remove_quietly(tmp)
        raise
    except Exception as e:  # noqa: BLE001
        _remove_quietly(tmp)
        raise StorageError(f"S3 读取失败：{e}")


def _s3_head(point, storage_key: str, length: int) -> bytes:
    """用 Range 请求只取对象头部若干字节"""
    try:
        client = _s3_client(point)
        resp = client.get_object(
            Bucket=_s3_bucket(point),
            Key=_s3_key(point, storage_key),
            Range=f"bytes=0-{max(int(length) - 1, 0)}",
        )
        body = resp["Body"]
        try:
            return body.read()
        finally:
            body.close()
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 读取失败：{e}")


def _s3_store(point, storage_key: str, stream, size: int = 0,
              on_progress: ProgressCallback = None):
    try:
        client = _s3_client(point)
        if hasattr(stream, "seek"):
            stream.seek(0)
        body = _CountingReader(stream, size, on_progress) if on_progress else stream
        client.upload_fileobj(body, _s3_bucket(point), _s3_key(point, storage_key))
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 写入失败：{e}")


def _s3_delete(point, storage_key: str):
    try:
        _s3_client(point).delete_object(
            Bucket=_s3_bucket(point), Key=_s3_key(point, storage_key))
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 删除失败：{e}")


def _s3_exists(point, storage_key: str) -> bool:
    try:
        client = _s3_client(point)
        client.head_object(Bucket=_s3_bucket(point), Key=_s3_key(point, storage_key))
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------- 存储点健康状态 ----------------

def _set_health(point, healthy: bool, error: str = None):
    """落库健康状态

    走独立连接更新，避免把调用方尚未提交的事务（如刚插入的文件记录）一起提交。
    """
    now = utcnow()
    try:
        with db.engine.begin() as conn:
            conn.execute(
                sa_update(StoragePoint).where(StoragePoint.id == point.id).values(
                    healthy=bool(healthy), health_error=error, last_health_at=now))
    except Exception:  # noqa: BLE001 —— 健康标记失败不影响主流程
        return
    point.healthy = bool(healthy)
    point.health_error = error
    point.last_health_at = now


def mark_healthy(point):
    """读写成功：恢复健康（本来就是健康状态时不写库）"""
    if point is None or point.kind == "local":
        return
    if point.healthy and not point.health_error:
        return
    _set_health(point, True, None)


def mark_unhealthy(point, error: str):
    """读写失败：摘除该点，不再被选为写入点，等定时任务巡检回归"""
    if point is None or point.kind == "local":
        return
    reason = (str(error or "").strip() or "未知错误")[:250]
    if not point.healthy and point.health_error == reason:
        return
    _set_health(point, False, reason)


def health_check_all() -> dict:
    """巡检所有远端存储点：掉线的摘除、恢复的回归（由定时任务调用）"""
    result = {"checked": 0, "failed": 0, "recovered": 0}
    for point in StoragePoint.query.filter(StoragePoint.kind != "local").all():
        err = test_connection(point)
        result["checked"] += 1
        if err:
            if point.healthy:
                result["failed"] += 1
            mark_unhealthy(point, err)
        else:
            if not point.healthy:
                result["recovered"] += 1
            mark_healthy(point)
    return result


# ---------------- 统一读写接口 ----------------

def read_path(point, storage_key: str,
              on_progress: ProgressCallback = None) -> str:
    """返回可直接读取的本地路径

    远端存储点会先拉取到本地缓存：命中缓存直接返回并续期，未命中的全过程
    按块回调 on_progress，供后台任务写库、前端轮询。
    """
    point = get_point(point.id if point is not None else None)
    if point is None:
        raise StorageError("尚未配置存储点")
    if point.kind == "local":
        return _local_path(point, storage_key)
    try:
        if point.kind == "sftp":
            path = _sftp_fetch(point, storage_key, on_progress)
        elif point.kind == "s3":
            path = _s3_fetch(point, storage_key, on_progress)
        else:
            path = _ftp_fetch(point, storage_key, on_progress)
    except StorageError as e:
        mark_unhealthy(point, str(e))
        raise
    mark_healthy(point)
    return path


def is_remote(point) -> bool:
    """是否远端存储点（读写需要网络传输，应放到后台任务执行）"""
    return bool(point is not None and point.kind != "local")


def cached_path(point, storage_key: str):
    """文件已在本地可直接读取时返回其路径，否则返回 None（不发起拉取）

    命中缓存时续期 mtime，避免热文件被 TTL 清掉。
    """
    point = get_point(point.id if point is not None else None)
    if point is None:
        return None
    if point.kind == "local":
        path = _local_path(point, storage_key)
        return path if os.path.exists(path) else None
    cached = _cache_path(point, storage_key, create=False)
    return _touch(cached) if os.path.exists(cached) else None


def read_head(point, storage_key: str, length: int) -> bytes:
    """读取文件头部若干字节（远端只取所需部分，避免整文件拉取）"""
    point = get_point(point.id if point is not None else None)
    if point is None:
        raise StorageError("尚未配置存储点")
    if point.kind == "local":
        return read_file_head(_local_path(point, storage_key), length)
    if point.kind == "sftp":
        return _sftp_head(point, storage_key, length)
    if point.kind == "s3":
        return _s3_head(point, storage_key, length)
    conn = _ftp_connect(point)
    try:
        sock = conn.transfercmd("RETR " + _ftp_path(point, storage_key))
        buf = b""
        try:
            while len(buf) < length:
                chunk = sock.recv(min(65536, length - len(buf)))
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        try:
            conn.voidresp()
        except ftplib.all_errors:
            pass
        return buf
    except FTP_ERRORS as e:
        raise StorageError(f"FTP 读取失败：{e}")
    finally:
        _ftp_close(conn)


def write_stream(point, storage_key: str, stream, size: int = 0,
                 on_progress: ProgressCallback = None):
    """把文件流写入存储点（stream 需位于起始位置）

    远端存储点的上传耗时较长，可传 on_progress 与 size 以按块上报进度。
    """
    if point.kind == "local":
        dest = _local_path(point, storage_key)
        with open(dest, "wb") as out:
            _copy_stream(stream, out, size, on_progress)
        return
    try:
        if point.kind == "sftp":
            _sftp_store(point, storage_key, stream, size, on_progress)
        elif point.kind == "s3":
            _s3_store(point, storage_key, stream, size, on_progress)
        else:
            _ftp_store(point, storage_key, stream, size, on_progress)
    except StorageError as e:
        mark_unhealthy(point, str(e))
        raise
    mark_healthy(point)


def write_from_path(point, storage_key: str, src_path: str, move: bool = True,
                    on_progress: ProgressCallback = None):
    """把本地文件写入存储点；move=True 时写入后删除源文件"""
    if point.kind == "local":
        dest = _local_path(point, storage_key)
        if os.path.abspath(src_path) == os.path.abspath(dest):
            return
        if move:
            shutil.move(src_path, dest)
        else:
            shutil.copy2(src_path, dest)
        return
    try:
        size = int(os.path.getsize(src_path))
    except OSError:
        size = 0
    with open(src_path, "rb") as fh:
        write_stream(point, storage_key, fh, size, on_progress)
    if move:
        _remove_quietly(src_path)


def delete_object(point, storage_key: str, drop_cache: bool = True):
    """删除存储点上的文件实体（含本地缓存）"""
    point = get_point(point.id if point is not None else None)
    if point is None:
        return
    if point.kind == "local":
        _remove_quietly(_local_path(point, storage_key))
    elif point.kind == "sftp":
        _sftp_delete(point, storage_key)
    elif point.kind == "s3":
        _s3_delete(point, storage_key)
    else:
        _ftp_delete(point, storage_key)
    if drop_cache:
        _remove_quietly(_cache_path(point, storage_key, create=False))


def delete_object_quietly(point, storage_key: str, drop_cache: bool = True):
    """删除失败不抛异常（用于彻底删除等不应中断的场景）"""
    try:
        delete_object(point, storage_key, drop_cache=drop_cache)
    except StorageError:
        pass


def object_exists(point, storage_key: str) -> bool:
    try:
        point = get_point(point.id if point is not None else None)
        if point is None:
            return False
        if point.kind == "local":
            return os.path.exists(_local_path(point, storage_key))
        if point.kind == "sftp":
            return _sftp_exists(point, storage_key)
        if point.kind == "s3":
            return _s3_exists(point, storage_key)
        conn = _ftp_connect(point)
        try:
            conn.size(_ftp_path(point, storage_key))
            return True
        except ftplib.error_perm as e:
            # 550 = 不存在；SIZE 不被支持时保守认为存在
            return "550" not in str(e)
        finally:
            _ftp_close(conn)
    except StorageError:
        return False


# ---------------- 存储点转移 / 校验 / 缓存清理 ----------------

def transfer_plan(point, files) -> dict:
    """统计把 files 从 point 转移出去所需与可用的空间（可用空间按 90% 计）"""
    need = sum(int(f.size or 0) for f in files)
    others = [p for p in enabled_points() if p.id != point.id]
    available = sum(remaining_bytes(p) for p in others)
    return {
        "need": need,
        "available": available,
        "feasible": bool(others) and available >= need,
        "points": len(others),
    }


def transfer_object(src_point, dst_point, storage_key: str):
    """把单个文件实体从 src_point 搬到 dst_point"""
    local = read_path(src_point, storage_key)
    if not os.path.exists(local):
        # 源实体缺失：抛 StorageError 让上层按失败计数处理，而非 500
        raise StorageError(f"源文件实体缺失：{storage_key}")
    write_from_path(dst_point, storage_key, local, move=True)


# ---------------- 删除存储点 ----------------

# 已丢失记录在库中的保留天数，超期由后台任务清理
LOST_RETENTION_DAYS = 7


def storage_files(point):
    """存储点上的实体文件记录（不含目录与已丢失记录）"""
    return File.query.filter(
        File.storage_id == point.id,
        File.is_dir == False,  # noqa: E712
        File.is_lost == False,  # noqa: E712
    ).all()


def delete_plan(point) -> dict:
    """删除存储点前的评估：待处理文件数与转移可行性"""
    files = storage_files(point)
    plan = transfer_plan(point, files)
    plan["count"] = len(files)
    return plan


def migrate_point_data(point, progress=None) -> int:
    """把 point 上的文件实体均衡转移到其他存储点，返回转移的文件数

    先整体规划落点（空间不足时在搬运任何数据前即失败），再逐个搬运。
    """
    files = storage_files(point)
    allocator = Allocator(exclude_ids=[point.id])
    assignments = [(f, allocator.pick(int(f.size or 0))) for f in files]

    moved = 0
    total = len(assignments)
    for f, dst in assignments:
        try:
            transfer_object(point, dst, f.storage_key)
        except StorageError as e:
            raise StorageError(f"转移「{f.name}」失败：{e}")
        f.storage_id = dst.id
        db.session.commit()
        moved += 1
        if progress:
            progress(moved, total, f)
    return moved


def drop_point_data(point) -> int:
    """删除 point 上的全部文件实体，并把对应记录标记为已丢失"""
    files = storage_files(point)
    now = utcnow()
    for f in files:
        delete_object_quietly(point, f.storage_key)
        f.is_lost = True
        f.lost_at = now
        f.storage_id = None
    db.session.commit()
    return len(files)


def purge_lost_records(days: int = LOST_RETENTION_DAYS) -> int:
    """清理超过保留期的已丢失文件记录，返回删除的记录数"""
    cutoff = utcnow() - timedelta(days=int(days))
    nodes = File.query.filter(
        File.is_lost == True,  # noqa: E712
        File.lost_at.isnot(None),
        File.lost_at < cutoff,
    ).all()
    for node in nodes:
        db.session.delete(node)
    if nodes:
        db.session.commit()
    return len(nodes)


def test_connection(point) -> str:
    """校验存储点配置可用，返回错误信息；可用时返回空串"""
    if point.kind == "local":
        path = (point.path or "").strip()
        if not path:
            return "请填写本地存储目录"
        if not os.path.isdir(path):
            return "目录不存在或不可访问"
        if not os.access(path, os.W_OK):
            return "目录不可写"
        return ""
    if point.kind == "sftp":
        if not (point.host or "").strip():
            return "请填写 SFTP 服务器地址"
        try:
            client = _sftp_connect(point)
        except StorageError as e:
            return str(e)
        try:
            sftp = client.open_sftp()
            try:
                _sftp_makedirs(sftp, (point.remote_dir or "/").rstrip("/") or "/")
            finally:
                sftp.close()
            return ""
        except Exception as e:  # noqa: BLE001
            return f"SFTP 目录不可用：{e}"
        finally:
            _sftp_close(client)
    if point.kind == "s3":
        try:
            client = _s3_client(point)
        except StorageError as e:
            return str(e)
        try:
            client.head_bucket(Bucket=_s3_bucket(point))
            return ""
        except StorageError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"S3 连接失败：{e}"
    if not (point.host or "").strip():
        return "请填写 FTP 服务器地址"
    try:
        conn = _ftp_connect(point)
    except StorageError as e:
        return str(e)
    try:
        _ftp_makedirs(conn, (point.remote_dir or "/").rstrip("/"))
        return ""
    except FTP_ERRORS as e:
        return f"FTP 目录不可用：{e}"
    finally:
        _ftp_close(conn)


def active_transfer_keys() -> set:
    """正在传输中的 (存储点 id, storage_key) 集合，缓存清理时必须跳过"""
    try:
        rows = db.session.query(
            TransferTask.point_id, TransferTask.storage_key
        ).filter(TransferTask.status.in_(("queued", "running"))).all()
    except Exception:  # noqa: BLE001
        return set()
    return {(int(row[0]), row[1]) for row in rows}


def _cache_key_of(root: str, path: str):
    """从缓存文件路径反推 (存储点 id, storage_key)；目录结构不符时返回 None"""
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    parts = rel.split("/")
    if len(parts) != 3:
        return None
    try:
        point_id = int(parts[0])
    except ValueError:
        return None
    name = parts[2]
    if name.endswith(".part"):
        name = name[:-5]
    return point_id, name


def clean_cache(ttl: int = None, max_bytes: int = None) -> int:
    """清理远端存储点本地缓存，返回删除文件数

    先按 TTL 删除过期文件（命中缓存的读取会续期 mtime，因此热文件不会过期），
    再按 LRU 淘汰把缓存总量压到 STORAGE_CACHE_MAX_BYTES 以内。
    正在取回 / 上传中的任务文件（含 .part 中转文件）不会被删除。
    """
    root = current_app.config["STORAGE_CACHE_ROOT"]
    if not root or not os.path.isdir(root):
        return 0
    ttl = int(ttl or current_app.config.get("STORAGE_CACHE_TTL") or 3600)
    if max_bytes is None:
        max_bytes = int(current_app.config.get("STORAGE_CACHE_MAX_BYTES") or 0)
    cutoff = time.time() - ttl
    protected = active_transfer_keys()
    kept = []  # (mtime, size, path)
    removed = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = _cache_key_of(root, path)
            if key is not None and key in protected:
                continue  # 交给传输任务自己收尾，避免删掉正在使用的文件
            if st.st_mtime < cutoff:
                _remove_quietly(path)
                removed += 1
                continue
            kept.append((st.st_mtime, st.st_size, path))
    if max_bytes > 0:
        total = sum(item[1] for item in kept)
        for _mtime, size, path in sorted(kept):  # 最久未访问的优先淘汰
            if total <= max_bytes:
                break
            _remove_quietly(path)
            if not os.path.exists(path):
                total -= size
                removed += 1
    return removed
