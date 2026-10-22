"""权益计算与校验服务

统一处理容量、流量、单文件大小、速度、预览权限等校验。
所有受限操作都必须通过本服务判断，避免逻辑散落。
"""
from app.extensions import db
from app.models import User, File, UserAddon, MembershipPlan
from app.utils.helpers import utcnow
from config import Config


class QuotaError(Exception):
    """权益不足异常，message 为面向用户的提示"""


def _reset_month_if_needed(user: User):
    """跨月重置流量统计"""
    now = utcnow()
    if user.month_reset_at is None:
        user.month_reset_at = now
        return
    if (now.year, now.month) != (user.month_reset_at.year, user.month_reset_at.month):
        user.used_upload_month = 0
        user.used_download_month = 0
        user.month_reset_at = now


def get_plan(user: User) -> MembershipPlan:
    """获取用户当前套餐；会员到期时有排队套餐则自动转正，否则回落到免费版"""
    from app.models import MembershipPlan
    free = MembershipPlan.query.filter_by(name="free").first()
    if user.plan is None:
        return free
    if user.vip_expire_at is not None and user.vip_expire_at < utcnow():
        from app.services import billing_service
        if billing_service.sync_membership(user):
            return user.plan  # 排队套餐（如顺延的 VIP）即时生效
        return free
    return user.plan


def _active_addons(user: User, addon_type: str):
    """获取用户有效期内、有剩余量的叠加包"""
    now = utcnow()
    return UserAddon.query.filter(
        UserAddon.user_id == user.id,
        UserAddon.status == "active",
        UserAddon.expire_at > now,
        UserAddon.remaining_bytes > 0,
    ).all()


def effective_quota(user: User) -> dict:
    """计算用户当前生效的权益"""
    _reset_month_if_needed(user)
    plan = get_plan(user)

    storage_quota = plan.storage_quota_bytes
    traffic_quota = plan.monthly_traffic_bytes  # 可能为 None（不限）

    # 叠加包
    for addon in _active_addons(user, "storage"):
        storage_quota += addon.package.amount_bytes
    traffic_addon_bytes = 0
    for addon in _active_addons(user, "traffic"):
        traffic_addon_bytes += addon.remaining_bytes

    # 已用容量（含回收站，不含彻底删除）
    used_storage = db.session.query(db.func.coalesce(db.func.sum(File.size), 0)).filter(
        File.user_id == user.id,
        File.is_dir == False,  # noqa: E712
    ).scalar()

    return {
        "plan": plan,
        "storage_quota": storage_quota,
        "used_storage": int(used_storage),
        "free_storage": max(0, storage_quota - int(used_storage)),
        "max_file_size": plan.max_file_size_bytes,   # None = 不限
        "speed_limit": plan.speed_limit_bps,         # None = 不限
        "monthly_traffic": traffic_quota,            # None = 不限
        "traffic_addon": traffic_addon_bytes,
        "used_upload": int(user.used_upload_month),
        "used_download": int(user.used_download_month),
        "allow_preview": plan.allow_preview,
    }


def is_storage_locked(user: User) -> bool:
    """容量是否到期锁定（禁止上传/下载）"""
    return user.storage_locked


def check_upload(user: User, size: int):
    """校验上传是否允许，抛 QuotaError"""
    if user.storage_locked:
        raise QuotaError("容量已到期锁定，请续费后重试")

    q = effective_quota(user)

    # 单文件大小限制
    if q["max_file_size"] is not None and size > q["max_file_size"]:
        from app.utils.helpers import human_size
        raise QuotaError(f"文件大小超过限制（单文件最大 {human_size(q['max_file_size'])}）")

    # 剩余容量
    if size > q["free_storage"]:
        from app.utils.helpers import human_size
        raise QuotaError(
            f"剩余空间不足，需要 {human_size(size)}，剩余 {human_size(q['free_storage'])}"
        )

    # 月流量（上传）
    _check_traffic(q, size)


def check_download(user: User, size: int):
    """校验下载是否允许"""
    if user.storage_locked:
        raise QuotaError("容量已到期锁定，请续费后重试")
    q = effective_quota(user)
    _check_traffic(q, size)


def _check_traffic(q: dict, size: int):
    """校验月流量（上传+下载合计）是否足够"""
    total = q["monthly_traffic"]
    if total is None:
        return  # 不限
    total += q["traffic_addon"]
    used = q["used_upload"] + q["used_download"]
    if used + size > total:
        from app.utils.helpers import human_size
        raise QuotaError(f"本月流量不足，已用 {human_size(used)}，剩余 {human_size(max(0, total - used))}")


def check_preview(user: User):
    """校验预览权限（仅 VIP/SVIP）"""
    q = effective_quota(user)
    if not q["allow_preview"]:
        raise QuotaError("在线预览为 VIP/SVIP 专享功能，请升级会员")


def get_speed_limit(user: User):
    """获取下载速度峰值（字节/秒，None=不限）"""
    return effective_quota(user)["speed_limit"]


def consume_traffic(user: User, size: int, direction: str):
    """实际消耗流量（上传/下载完成后调用）"""
    _reset_month_if_needed(user)
    if direction == "upload":
        user.used_upload_month += size
    elif direction == "download":
        user.used_download_month += size
    db.session.add(user)


def get_effective_quota_dict(user: User) -> dict:
    """供模板/前端使用的可读权益信息"""
    q = effective_quota(user)
    return q
