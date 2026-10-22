"""应用工厂"""
import os

from flask import Flask

from app.extensions import db, login_manager, bcrypt
from config import Config, env_get, load_site_config


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    # 站点名与功能开关以 config.yml 为准
    app.config.update(load_site_config())

    # 确保存储目录存在
    os.makedirs(app.config["STORAGE_ROOT"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_TMP_ROOT"], exist_ok=True)
    os.makedirs(app.config["REMOTE_TMP_ROOT"], exist_ok=True)
    os.makedirs(app.config["STORAGE_CACHE_ROOT"], exist_ok=True)

    # 初始化扩展
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # 注册蓝图
    from app.routes.auth import auth_bp
    from app.routes.files import files_bp
    from app.routes.share import share_bp
    from app.routes.preview import preview_bp
    from app.routes.billing import billing_bp
    from app.routes.admin import admin_bp
    from app.routes.remote import remote_bp
    from app.routes.transfer import transfer_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(share_bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(remote_bp)
    app.register_blueprint(transfer_bp)

    # 首页
    from app.routes.main import main_bp
    app.register_blueprint(main_bp)

    # 注册 Jinja 全局过滤器/上下文
    from app.utils.template_filters import register_filters
    register_filters(app)

    @app.context_processor
    def inject_globals():
        from app.utils.helpers import utcnow, get_admin_view_user
        from app.services import update_service
        return {
            "now": utcnow(),
            "admin_view_user": get_admin_view_user(),
            "site_name": app.config["SITE_NAME"],
            "project_name": app.config["PROJECT_NAME"],
            "version": app.config.get("VERSION") or "0.0.0",
            # 页脚展示版本号与「是否有新版本」（每日 0 点检查更新，见 update_service）
            "update_info": update_service.read_status(),
            "update_repo": app.config["UPDATE_REPO"],
            "allow_register": app.config["ALLOW_REGISTER"],
            "enable_preview": app.config["ENABLE_PREVIEW"],
            "enable_share": app.config["ENABLE_SHARE"],
            "enable_remote": app.config["ENABLE_REMOTE"],
        }

    @app.errorhandler(PermissionError)
    def handle_permission_error(e):
        """文件不存在/无权访问统一返回 404，避免裸抛 500"""
        return "文件不存在或无权访问", 404

    @app.after_request
    def add_hsts_header(resp):
        """HSTS 仅在 HTTPS 真正生效时下发（见 config.yml 的 https 段）"""
        if app.config["HSTS_ENABLED"]:
            resp.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={app.config['HSTS_MAX_AGE']}",
            )
        return resp

    # 初始化数据库与默认套餐
    with app.app_context():
        db.create_all()
        _migrate_addon_duration()
        _migrate_storage_points()
        _migrate_user_membership_queue()
        _seed_plans()
        _seed_storage_points()
        _drop_legacy_speed_limit()

    # 后台远程下载 Worker（每个进程一个轮询线程，任务由数据库乐观认领防重复）
    if env_get("DISABLE_BG") != "1" and app.config["ENABLE_REMOTE"]:
        from app.services import remote_service
        remote_service.start_worker(app)

    # 后台远端传输 Worker：取回/上传与是否启用远程下载无关，只要有远端存储点就需要
    if env_get("DISABLE_BG") != "1":
        from app.services import transfer_service
        transfer_service.start_worker(app)

    return app


def _drop_legacy_speed_limit():
    """升级兼容：历史版本默认对登录用户限速，将仍为旧默认值的套餐改为不限速"""
    from app.models import MembershipPlan

    legacy = {"free": 4 * 1024 ** 2, "vip": 8 * 1024 ** 2}
    changed = False
    for plan in MembershipPlan.query.all():
        if plan.name in legacy and plan.speed_limit_bps == legacy[plan.name]:
            plan.speed_limit_bps = None
            changed = True
    if changed:
        db.session.commit()


def _migrate_addon_duration():
    """升级兼容：叠加包有效期由「天」改为「秒」

    老库只有 duration_days 列，而新代码一律读 duration_seconds；
    create_all 不会修改已存在的表，这里补列并按天换算成秒。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "addon_packages" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("addon_packages")}
    if "duration_seconds" in columns or "duration_days" not in columns:
        return
    with db.engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE addon_packages ADD COLUMN duration_seconds BIGINT NOT NULL DEFAULT 2592000"))
        conn.execute(text(
            "UPDATE addon_packages SET duration_seconds = duration_days * 86400"))


def _migrate_storage_points():
    """升级兼容：为文件与远程下载记录补充存储点归属、丢失标记列

    create_all 不会修改已存在的表，老库需要手写 ALTER TABLE 补列。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    if "storage_points" in tables:
        # 1.3.1 起存储点支持 SFTP / S3，需补齐 S3 相关列
        columns = {c["name"] for c in inspector.get_columns("storage_points")}
        stmts = []
        for name, ddl in (
            ("endpoint", "VARCHAR(256)"),
            ("bucket", "VARCHAR(128)"),
            ("access_key", "VARCHAR(256)"),
            ("secret_key", "VARCHAR(256)"),
            ("region", "VARCHAR(64)"),
            ("path_style", "BOOLEAN NOT NULL DEFAULT 0"),
            # 1.3.3 起记录存储点健康状态，读写失败自动摘除
            ("healthy", "BOOLEAN NOT NULL DEFAULT 1"),
            ("health_error", "VARCHAR(256)"),
            ("last_health_at", "DATETIME"),
        ):
            if name not in columns:
                stmts.append(f"ALTER TABLE storage_points ADD COLUMN {name} {ddl}")
        if stmts:
            with db.engine.begin() as conn:
                for sql in stmts:
                    conn.execute(text(sql))

    if "files" in tables:
        columns = {c["name"] for c in inspector.get_columns("files")}
        stmts = []
        if "storage_id" not in columns:
            stmts.append("ALTER TABLE files ADD COLUMN storage_id INTEGER")
        if "is_lost" not in columns:
            stmts.append("ALTER TABLE files ADD COLUMN is_lost BOOLEAN NOT NULL DEFAULT 0")
        if "lost_at" not in columns:
            stmts.append("ALTER TABLE files ADD COLUMN lost_at DATETIME")
        if stmts:
            with db.engine.begin() as conn:
                for sql in stmts:
                    conn.execute(text(sql))

    if "remote_downloads" in tables:
        columns = {c["name"] for c in inspector.get_columns("remote_downloads")}
        if "storage_id" not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE remote_downloads ADD COLUMN storage_id INTEGER"))


def _migrate_user_membership_queue():
    """升级兼容：为用户补充排队会员列（VIP 顺延至 SVIP 到期后生效）"""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    stmts = []
    if "queued_plan_id" not in columns:
        stmts.append("ALTER TABLE users ADD COLUMN queued_plan_id INTEGER")
    if "queued_expire_at" not in columns:
        stmts.append("ALTER TABLE users ADD COLUMN queued_expire_at DATETIME")
    if stmts:
        with db.engine.begin() as conn:
            for sql in stmts:
                conn.execute(text(sql))


def _seed_storage_points():
    """初始化默认本地存储点，并把历史文件回填到该存储点"""
    from flask import current_app

    from app.models import StoragePoint, File, RemoteDownload
    from app.services import storage_service

    if StoragePoint.query.count() == 0:
        root = current_app.config["STORAGE_ROOT"]
        db.session.add(StoragePoint(
            name="本地存储",
            kind="local",
            path=root,
            capacity_bytes=storage_service.detect_local_capacity(root),
            enabled=True,
            sort=0,
        ))
        db.session.commit()

    root = current_app.config["STORAGE_ROOT"]
    default_point = (
        StoragePoint.query.filter_by(kind="local", path=root).first()
        or StoragePoint.query.filter_by(kind="local")
        .order_by(StoragePoint.sort, StoragePoint.id).first()
        or StoragePoint.query.order_by(StoragePoint.sort, StoragePoint.id).first()
    )
    if default_point is None:
        return

    changed = File.query.filter(
        File.storage_id.is_(None), File.storage_key.isnot(None)
    ).update({"storage_id": default_point.id}, synchronize_session=False)
    changed += RemoteDownload.query.filter(
        RemoteDownload.storage_id.is_(None), RemoteDownload.storage_key.isnot(None)
    ).update({"storage_id": default_point.id}, synchronize_session=False)
    if changed:
        db.session.commit()


def _seed_plans():
    """初始化默认套餐与叠加包"""
    from app.models import MembershipPlan, AddonPackage
    from decimal import Decimal

    if MembershipPlan.query.count() == 0:
        plans = [
            MembershipPlan(
                name="free", display_name="免费版",
                monthly_price=Decimal("0.00"),
                storage_quota_bytes=10 * 1024 ** 3,
                max_file_size_bytes=256 * 1024 ** 2,
                speed_limit_bps=None,
                monthly_traffic_bytes=10 * 1024 ** 3,
                allow_preview=False, sort=0,
            ),
            MembershipPlan(
                name="vip", display_name="VIP",
                monthly_price=Decimal("10.00"),
                storage_quota_bytes=50 * 1024 ** 3,
                max_file_size_bytes=1 * 1024 ** 3,
                speed_limit_bps=None,
                monthly_traffic_bytes=50 * 1024 ** 3,
                allow_preview=True, sort=1,
            ),
            MembershipPlan(
                name="svip", display_name="SVIP",
                monthly_price=Decimal("20.00"),
                storage_quota_bytes=100 * 1024 ** 3,
                max_file_size_bytes=None,
                speed_limit_bps=None,
                monthly_traffic_bytes=None,
                allow_preview=True, sort=2,
            ),
        ]
        db.session.add_all(plans)
        db.session.commit()

    if AddonPackage.query.count() == 0:
        addons = [
            # 流量包
            AddonPackage(type="traffic", name="1GB 流量", amount_bytes=1 * 1024 ** 3,
                         price=Decimal("1.00"), duration_seconds=30 * 86400, sort=1),
            AddonPackage(type="traffic", name="10GB 流量", amount_bytes=10 * 1024 ** 3,
                         price=Decimal("8.00"), duration_seconds=30 * 86400, sort=2),
            AddonPackage(type="traffic", name="100GB 流量", amount_bytes=100 * 1024 ** 3,
                         price=Decimal("48.00"), duration_seconds=30 * 86400, sort=3),
            # 容量包
            AddonPackage(type="storage", name="5GB 容量", amount_bytes=5 * 1024 ** 3,
                         price=Decimal("2.00"), duration_seconds=30 * 86400, sort=4),
            AddonPackage(type="storage", name="30GB 容量", amount_bytes=30 * 1024 ** 3,
                         price=Decimal("6.00"), duration_seconds=30 * 86400, sort=5),
            AddonPackage(type="storage", name="100GB 容量", amount_bytes=100 * 1024 ** 3,
                         price=Decimal("15.00"), duration_seconds=30 * 86400, sort=6),
            AddonPackage(type="storage", name="1TB 容量", amount_bytes=1 * 1024 ** 4,
                         price=Decimal("100.00"), duration_seconds=30 * 86400, sort=7),
        ]
        db.session.add_all(addons)
        db.session.commit()
