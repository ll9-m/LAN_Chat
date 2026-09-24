"""
在线匿名聊天室（多房间版 — 单端口 + URL 路由）
================================================

项目定位
--------
数据库原理与应用课程设计项目。局域网内多房间匿名即时通讯：
用户免密匿名加入，管理员凭密码管理；后台 MySQL 8.0 统一存储，
应用层 Flask 单进程 threaded 服务，实时推送用 SSE。

整体架构
--------
单 Flask 应用（默认端口 5000），支持多个聊天房间，分层如下：

    浏览器 (templates/*.html + EventSource)
        │  HTTP 请求 / SSE 长连接
        ▼
    路由层  Flask 视图 + 房间 Blueprint          （鉴权、参数解析）
        │
        ▼
    业务层  ChatRoom（每房间一个实例）            （校验、状态变更、广播）
        │
        ▼
    数据层  DatabaseManager（主库）/ Database      （参数化 SQL）
            （每实例持有独立 pymysql 连接，threading.Lock 保证线程安全）
        │
        ▼
    MySQL 8.0  库 lan_chat（7 表 + 2 视图 + 1 触发器 + 3 存储过程）

路由分两大类：
  1) 全局路由（app 本身）
       /                       → 登录入口页（未登录时展示 login.html；已登录跳 /rooms）
       /login  POST            → 角色登录（role=user 免密 / role=admin 校验 config.admin_password）
       /logout POST            → 清除 session，回登录页
       /rooms                  → 房间列表（按 session 角色分流：admin→rooms.html，user→rooms_user.html）
       /room/<room_id>/        → 聊天页面（?admin=1 时 session 必须是 admin，否则重定向 /）
       /admin                  → 旧地址兼容，渲染 admin.html（meta 跳转 /）
       /api/rooms              → 房间列表 JSON（公开只读）
       /api/rooms/create       → 创建房间（需 admin 会话）
       /api/rooms/delete       → 删除房间（需 admin 会话；删光则自动重建 room_default）
       /api/rooms/toggle       → 开关房间（需 admin 会话）
       /api/rooms/qr/<id>      → 房间二维码 PNG（不存在则现场生成）
       /api/admin/change_password → 修改管理员密码（需 admin 会话 + 正确旧密码）
       /api/admin/global_reset → 全局恢复出厂（需 admin 会话 + 当前密码确认）
       /api/entry-qr           → 入口首页二维码图片（登录页手机扫码用）
  2) 房间 Blueprint（url_prefix=/room/<room_id>，before_request 自动加载 g.room）
       /room/<id>/api/join | leave | send | upload_file | messages | ...
                              → 加入/退出/发消息/上传/查询/收藏等
       /room/<id>/api/admin/* → 房间内管理（改配置/禁言/踢出/删消息/统计/审计）
       /room/<id>/stream       → SSE 实时推送（history/room_name/user_list + 后续事件）
       /room/<id>/uploads/*    → 房间内文件下载/预览

权限模型（两层鉴权）
--------------------
第 1 层 —— 会话层（Flask session['role'] ∈ {'user', 'admin'}）：
  - 普通用户：登录页选「用户登录」直接写 session，可浏览开放房间、进入聊天。
  - 管理员  ：登录页选「管理员登录」+ 密码（config 表 admin_password，默认 ADMIN）。
  - _require_admin()：全局管理 API 的统一鉴权入口，session.role != 'admin' 返回 403。
  - 访问 /room/<id>/?admin=1 时，session 必须是 admin，否则重定向到 /。
第 2 层 —— 在线身份层（ChatRoom.is_admin(user_id)）：
  - 房间内 /api/admin/* 路由校验 user_id 在当前房间在线表中且 is_admin=True。
  - user_id 由 join 时生成：管理员为 admin_ 前缀随机串，普通用户为 user_ 前缀。
  - 这样即使伪造 user_id 字符串，不在在线表或 is_admin=False 也会 403，
    防止「会话是 admin 但未以管理员身份进入该房间」的越权，也防止普通用户冒充。

数据库（MySQL 8.0）
-------------------
- 库名 lan_chat，字符集 utf8mb4，排序规则 utf8mb4_unicode_ci（全库/全表/存储过程参数统一，
  否则 CALL 存储过程会报 1267 Illegal mix of collations）。
- 连接账号见 MYSQL_* 常量（支持环境变量覆盖），PyMySQL 驱动，autocommit + DictCursor。
- 全部房间共用一个库，业务表以 room_id 区分作用域；完整 DDL 见 sql/schema.sql，
  启动时 ensure_mysql_schema() 幂等执行同等 DDL。
- 表与访问类的对应：
    DatabaseManager（主库单例）
      rooms          房间注册表（单一事实来源：名称/开关/密码/限额/撤回时限）
      config         全局键值（admin_password）
    Database（每房间实例一个，查询均带 WHERE room_id=%s）
      profiles       用户档案（房间+设备复合主键；昵称/头像/颜色/禁言截止）
      messages       聊天消息（seq 自增代理主键；room_id+id 唯一）
      blacklist      踢出黑名单（房间+设备复合主键）
      favorites      收藏夹（room_id+user_id+msg_id 唯一，快照冗余）
      message_audit  消息删除审计（触发器写入，无外键，房间删除后仍可清查）
- 完整性设计：
    主键：单属性（rooms.room_id / messages.seq）或复合（profiles(room_id,device_id)）
    外键：profiles/messages/blacklist/favorites → rooms ON DELETE CASCADE
    候选键 UNIQUE：messages(room_id,id)、favorites(room_id,user_id,msg_id)
    CHECK：is_open∈{0,1}、file_limit_mb≥0、max_history≥10、recall_time_limit≥0、
           muted_until≥0、type∈{text,image,file,system}、is_admin∈{0,1}
    DEFAULT：房间默认开放、保留200条、撤回300秒；档案默认未禁言等
    索引：messages(room_id,timestamp)、messages(room_id,sender)、
          profiles(room_id,nickname)、favorites(room_id,user_id,created_at)、
          blacklist(created_at)、message_audit(room_id,deleted_at)
- 数据库对象：
    视图    v_message_stats（按用户统计消息数与活跃时间）
            v_message_type_stats（按消息类型统计数量）
    触发器  trg_message_delete_audit（AFTER DELETE ON messages → 写审计）
    存储过程 sp_get_message_stats（三结果集综合统计）
            sp_cleanup_old_messages（按保留条数裁剪旧消息）
            sp_get_audit_log（审计日志按时间倒序取N条）
- 时间统一存 Unix 时间戳（DOUBLE），前端 JS 直接 new Date(t*1000) 渲染。

线程模型
--------
- Flask app.run(threaded=True)：每个请求一个线程。
- 每个 Database / DatabaseManager 实例持有独立 pymysql 连接，
  内部 threading.Lock 序列化该连接上的游标操作（PyMySQL 连接非线程安全）。
- ChatRoom.lock（RLock）保护 online_users 字典与内存中的房间配置字段。
- 每房间一个 Broadcaster：维护 SSE 客户端 queue 列表；broadcast() 序列化 JSON
  后逐个投递；SSE 视图各自 q.get() 取数据推给浏览器。
- RoomManager._lock 保护 _rooms 字典的懒加载（get_or_create 双检）。

主要类与职责
------------
- DatabaseManager : 主库封装（rooms/config CRUD、delete_room 级联清理、config 键值读写）
- Database        : 单房间业务表读写（所有查询带 room_id 过滤；调用视图/触发器/存储过程）
- Broadcaster     : SSE 事件广播（register/unregister/broadcast）
- ChatRoom        : 房间业务逻辑中枢（join/leave/send/管理操作，均返回 (json_dict, http_status)）
- RoomManager     : room_id → ChatRoom 实例的线程安全懒加载缓存


"""
import os
import sys
import re
import time
import json
import random
import queue
import shutil
import socket
import glob
import threading
import qrcode
import pymysql
from flask import (Flask, request, Response, jsonify, render_template, g,
                   send_from_directory, stream_with_context, Blueprint, session, redirect)

# ============================================================================
# 常量与路径
# ============================================================================
# 所有运行时目录的根。打包为 exe（PyInstaller frozen）时取 exe 所在目录，
# 开发环境取本文件所在目录——保证 uploads/、static/qrcode/ 等相对路径
# 在两种运行方式下行为一致。
# ----------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# MySQL 连接配置
# ----------------------------------------------------------------------------
# 优先级：环境变量 > 默认值。
# 部署到其他机器时无需改代码，设置 MYSQL_* 环境变量即可。
# 这些常量被 _mysql_connect() 统一读取，不在业务代码里散落硬编码连接参数。
# ----------------------------------------------------------------------------
MYSQL_HOST = os.environ.get('MYSQL_HOST', '127.0.0.1')       # MySQL 主机地址
MYSQL_PORT = int(os.environ.get('MYSQL_PORT', '3306'))       # MySQL 端口（默认 3306）
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')            # 连接用户名
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'root')    # 连接密码
MYSQL_DB = os.environ.get('MYSQL_DB', 'lan_chat')            # 数据库名（库不存在时自动创建）

# ----------------------------------------------------------------------------
# 运行时目录
# ----------------------------------------------------------------------------
UPLOAD_ROOT = os.path.join(BASE_DIR, 'uploads')        # 全局上传目录（兼容早期单房间版本遗留的旧文件；新文件按房间存 uploads_<room_id>/）
QR_DIR = os.path.join(BASE_DIR, 'static', 'qrcode')    # 二维码图片输出目录：room_<id>.png（房间码）+ entry.png（入口码）

# ----------------------------------------------------------------------------
# 业务默认值
# ----------------------------------------------------------------------------
MAX_HISTORY = 200        # 消息默认保留条数。rooms 表 DEFAULT 200、CHECK ≥10；
                         # 此常量仅作 Database.__init__ 的兜底，实际以房间配置为准。
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}  # 图片扩展名白名单：
                         # 命中则 /uploads/<file> 直接内联展示，否则 as_attachment 下载。
BASE_PORT = 5000         # 服务监听端口。二维码 URL 也用它——改端口必须同步改这里，否则生成的二维码指向错误端口。
                         

# ----------------------------------------------------------------------------
# 随机头像池 / 昵称颜色池
# ----------------------------------------------------------------------------
# 新用户首次加入房间时从池中 random.choice 分配，结果存入 profiles 表，
# 同一设备下次进入复用同一头像/颜色（匿名但视觉身份稳定）。
# 头像用 emoji（utf8mb4 单字符即可存储，profiles.avatar VARCHAR(16) 足够）。
# 颜色为 6 位 HEX，前端直接作为昵称与气泡的 color 样式。
# ----------------------------------------------------------------------------
AVATARS = ['😀', '😎', '🤖', '👽', '🐱', '🐶', '🦊', '🐼', '🐸', '🐵', '🦁', '🐯']
COLORS = ['#FF5733', '#33FF57', '#3357FF', '#F333FF', '#FF33A8', '#33FFF5', '#F5FF33', '#FF8C33',
          '#8E44AD', '#2ECC71', '#E67E22', '#1ABC9C', '#E74C3C', '#3498DB', '#9B59B6', '#34495E']

# ----------------------------------------------------------------------------
# 敏感词过滤（课程演示用的极简实现）
# ----------------------------------------------------------------------------
# 发送 text 类型消息前调用 filter_sensitive()，把命中的词替换为等长 '*'。
# 逐词 str.replace，不做分词/变体/拼音绕过识别——仅作功能演示，
# 报告中如实说明其局限性。
# ----------------------------------------------------------------------------
SENSITIVE_WORDS = ['傻逼', '操你妈', '去死', 'fuck', 'shit', 'bitch']

# ----------------------------------------------------------------------------
# 文件类消息 content 字段的合法格式（正则白名单）
# ----------------------------------------------------------------------------
# image / file 类型消息的 content 不是自由文本，必须是本应用的上传 URL：
#   /room/<room_id>/uploads/<filename>
# send_message() 用 UPLOAD_URL_RE.fullmatch 校验，防止把任意字符串
# 当作文件 URL 入库（绕过上传接口直接 send 时的注入面）。
# 字符类 [A-Za-z0-9_.\-] 与上传接口生成的 文件名规则（时间戳_随机hex.扩展名）一致。
# ----------------------------------------------------------------------------
UPLOAD_URL_RE = re.compile(r'/room/[^/]+/uploads/[A-Za-z0-9_.\-]+')


# ============================================================================
# 通用辅助函数
# ============================================================================
def filter_sensitive(text):
    """
    敏感词过滤：把消息正文中每个命中词替换为等长的 '*'。

    参数：
        text (str) —— 原始消息正文（已 strip）。
    返回：
        str —— 过滤后的正文；未命中任何词时原样返回。

    实现说明：
        对 SENSITIVE_WORDS 逐个做 str.replace，替换长度与原词相同
        （中文按字符数、英文按字母数），保持消息长度感知一致。
        这是课程演示级实现，不处理大小写变体、谐音、分词边界，
        报告「系统测试/局限性」中如实说明。
    """
    for word in SENSITIVE_WORDS:
        text = text.replace(word, '*' * len(word))
    return text


def get_local_ip():
    """
    获取本机在局域网中的 IP 地址。

    用途：启动横幅打印的访问地址 + 生成二维码时拼 http://<ip>:<port>/ URL。
         局域网内其他设备（手机扫码）必须能路由到这个 IP。

    返回：
        str —— 点分十进制 IPv4 地址。

    三级降级策略（保证任何环境都能返回一个可用字符串）：
      1. UDP 技巧：向 8.8.8.8:80 建 UDP socket 并 connect（UDP 不真发包、
         不建连接），读 getsockname() 得到系统为该路由选择的本地地址——
         这是获取「对外网卡 IP」最可靠的标准做法。
      2. 解析主机名：socket.gethostbyname(gethostname())。
      3. 兜底：127.0.0.1（仅本机可访问，局域网扫码会失败，但程序不崩溃）。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # UDP connect 只是选定路由与本地地址，不发包
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def generate_qr(url, filepath):
    """
    把 url 编码为二维码图片并保存到 filepath。

    参数：
        url      (str) —— 要编码的完整 URL，如 http://192.168.1.5:5000/room/xxx/
        filepath (str) —— 输出 PNG 的绝对路径（自动创建父目录）。

    二维码参数：
        version=1  起始版本，fit=True 允许按内容自动增大版本号
        box_size=10 每个码点 10px，保证手机在一定距离可识别
        border=2    静区宽度（规范建议 ≥4，此处 2 足够室内近距离扫码）
        fill/back   黑底白码（标准对比度，识别率最高）

    调用场景：入口页 /api/entry-qr、创建房间、room_default 初始化、
              /api/rooms/qr/<id>（文件不存在时现场生成）。
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(filepath)


def room_uploads_dir(room_id):
    """
    返回并确保房间上传目录存在。

    目录命名：BASE_DIR/uploads_<room_id>/，一房间一目录，
    物理隔离不同房间的文件，删除房间时整目录 shutil.rmtree。

    参数：
        room_id (str) —— 房间标识。
    返回：
        str —— 该房间上传目录的绝对路径（目录已创建）。
    """
    d = os.path.join(BASE_DIR, f'uploads_{room_id}')
    os.makedirs(d, exist_ok=True)
    return d


# ============================================================================
# MySQL 连接与建库建表
# ============================================================================
def _mysql_connect(with_db=True):
    """
    建立一条到 MySQL 的独立连接（每次调用返回新连接）。

    参数：
        with_db (bool) —— True：连接时指定 MYSQL_DB 库（正常业务路径）；
                          False：只连服务器不选库（用于 CREATE DATABASE，
                          此时目标库可能还不存在）。

    连接参数说明：
        charset='utf8mb4'   客户端字符集，与库/表一致，保证 emoji 正确传输
        DictCursor          查询结果返回 dict 而非 tuple，业务层按列名取值
        autocommit=True     每条 SQL 自动提交，不显式开事务（本应用无跨表
                            事务需求；单语句原子性由 InnoDB 保证）
        connect_timeout=5   连接超时 5 秒，MySQL 未启动时快速失败而非长时间挂起

    注意：PyMySQL 连接不是线程安全的，一个连接只能被一个线程顺序使用。
          Database / DatabaseManager 各自持锁（threading.Lock）序列化访问。
    """
    kwargs = dict(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                  password=MYSQL_PASSWORD, charset='utf8mb4',
                  cursorclass=pymysql.cursors.DictCursor, autocommit=True,
                  connect_timeout=5)
    if with_db:
        kwargs['database'] = MYSQL_DB
    return pymysql.connect(**kwargs)


def ensure_mysql_schema():
    """
    幂等初始化数据库结构（应用启动时在模块级调用一次）。

    「幂等」= 可重复执行，效果与执行一次相同：
      - CREATE DATABASE IF NOT EXISTS / CREATE TABLE IF NOT EXISTS
      - CREATE OR REPLACE VIEW
      - DROP + CREATE（触发器与存储过程先删后建）

    执行步骤：
      1. with_db=False 连服务器 → CREATE DATABASE IF NOT EXISTS lan_chat
         （utf8mb4 + utf8mb4_unicode_ci，与所有表 collation 统一，
           避免存储过程参数与表列 collation 混用报 1267）。
      2. with_db=True 连上库 → 依次执行：
         a. 7 张 CREATE TABLE（含主键/外键/CHECK/DEFAULT/UNIQUE/索引）
         b. 2 个视图  v_message_stats / v_message_type_stats
         c. 1 个触发器 trg_message_delete_audit（DROP 后 CREATE）
         d. 3 个存储过程 sp_get_message_stats / sp_cleanup_old_messages /
            sp_get_audit_log（均 DROP 后 CREATE）
         e. 种子数据：config 表插入 admin_password='ADMIN'
            （ON DUPLICATE KEY UPDATE value=value，重复执行不覆盖已改密码）
      3. finally 关闭连接——本函数只负责建结构，不持有长连接。

    与 sql/schema.sql 的关系：
      两边 DDL 内容保持一致。schema.sql 是报告第四章的「可阅读版」
      （带详细注释），本函数是应用运行时实际执行的「等价版」。
      修改表结构时须同步两处。

    异常传播：
      MySQL 未启动或凭据错误时 pymysql 抛 OperationalError，
      模块导入失败、进程带着明确错误退出（fail-fast，避免带病运行）。
    """
    conn = _mysql_connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'CREATE DATABASE IF NOT EXISTS `{MYSQL_DB}` '
                f"DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()

    conn = _mysql_connect(with_db=True)
    try:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id            VARCHAR(64)  NOT NULL,
                    room_name          VARCHAR(100) NOT NULL DEFAULT '新房间',
                    is_open            TINYINT      NOT NULL DEFAULT 1,
                    password           VARCHAR(255)          DEFAULT NULL,
                    file_limit_mb      INT          NOT NULL DEFAULT 0,
                    max_history        INT          NOT NULL DEFAULT 200,
                    recall_time_limit  INT          NOT NULL DEFAULT 300,
                    created_at         DOUBLE                DEFAULT NULL,
                    PRIMARY KEY (room_id),
                    CONSTRAINT chk_rooms_open        CHECK (is_open IN (0, 1)),
                    CONSTRAINT chk_rooms_file_limit  CHECK (file_limit_mb >= 0),
                    CONSTRAINT chk_rooms_max_history CHECK (max_history >= 10),
                    CONSTRAINT chk_rooms_recall      CHECK (recall_time_limit >= 0)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    `key`   VARCHAR(64) NOT NULL,
                    `value` TEXT,
                    PRIMARY KEY (`key`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS profiles (
                    room_id      VARCHAR(64) NOT NULL,
                    device_id    VARCHAR(64) NOT NULL,
                    nickname     VARCHAR(50) NOT NULL,
                    avatar       VARCHAR(16),
                    color        VARCHAR(16),
                    muted_until  DOUBLE      NOT NULL DEFAULT 0,
                    PRIMARY KEY (room_id, device_id),
                    CONSTRAINT fk_profiles_room FOREIGN KEY (room_id)
                        REFERENCES rooms (room_id) ON DELETE CASCADE,
                    CONSTRAINT chk_profiles_mute CHECK (muted_until >= 0),
                    INDEX idx_profiles_nickname (room_id, nickname)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    seq         BIGINT       NOT NULL AUTO_INCREMENT,
                    room_id     VARCHAR(64)  NOT NULL,
                    id          VARCHAR(64)  NOT NULL,
                    type        VARCHAR(16),
                    content     TEXT,
                    sender      VARCHAR(50),
                    user_id     VARCHAR(64),
                    is_admin    TINYINT      NOT NULL DEFAULT 0,
                    avatar      VARCHAR(16),
                    color       VARCHAR(16),
                    timestamp   DOUBLE,
                    file_name   VARCHAR(100),
                    file_size   BIGINT,
                    PRIMARY KEY (seq),
                    UNIQUE KEY uk_messages_room_id (room_id, id),
                    CONSTRAINT fk_messages_room FOREIGN KEY (room_id)
                        REFERENCES rooms (room_id) ON DELETE CASCADE,
                    CONSTRAINT chk_messages_type CHECK (
                        type IS NULL OR type IN ('text', 'image', 'file', 'system')
                    ),
                    CONSTRAINT chk_messages_admin CHECK (is_admin IN (0, 1)),
                    INDEX idx_messages_room_time (room_id, timestamp),
                    INDEX idx_messages_room_sender (room_id, sender)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS blacklist (
                    room_id    VARCHAR(64) NOT NULL,
                    device_id  VARCHAR(64) NOT NULL,
                    nickname   VARCHAR(50),
                    ip         VARCHAR(45),
                    created_at DOUBLE,
                    PRIMARY KEY (room_id, device_id),
                    CONSTRAINT fk_blacklist_room FOREIGN KEY (room_id)
                        REFERENCES rooms (room_id) ON DELETE CASCADE,
                    INDEX idx_blacklist_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS favorites (
                    id         BIGINT       NOT NULL AUTO_INCREMENT,
                    room_id    VARCHAR(64)  NOT NULL,
                    user_id    VARCHAR(64)  NOT NULL,
                    msg_id     VARCHAR(64),
                    msg_type   VARCHAR(16),
                    content    TEXT,
                    sender     VARCHAR(50),
                    file_name  VARCHAR(100),
                    file_size  BIGINT,
                    created_at DOUBLE       NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_favorites_user_msg (room_id, user_id, msg_id),
                    CONSTRAINT fk_favorites_room FOREIGN KEY (room_id)
                        REFERENCES rooms (room_id) ON DELETE CASCADE,
                    INDEX idx_favorites_user (room_id, user_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            # 审计表故意不设外键：房间删除后仍保留删除痕迹（报告 4.2.3 可说明）
            cur.execute('''
                CREATE TABLE IF NOT EXISTS message_audit (
                    audit_id   BIGINT       NOT NULL AUTO_INCREMENT,
                    room_id    VARCHAR(64)  NOT NULL,
                    message_id VARCHAR(64),
                    type       VARCHAR(16),
                    content    TEXT,
                    sender     VARCHAR(50),
                    user_id    VARCHAR(64),
                    deleted_at DOUBLE,
                    action     VARCHAR(16),
                    PRIMARY KEY (audit_id),
                    INDEX idx_audit_room_time (room_id, deleted_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')

            cur.execute('''
                CREATE OR REPLACE VIEW v_message_stats AS
                SELECT room_id, user_id, sender,
                       COUNT(*) AS message_count,
                       MAX(`timestamp`) AS last_active,
                       MIN(`timestamp`) AS first_active
                FROM messages
                WHERE user_id IS NOT NULL
                GROUP BY room_id, user_id, sender''')
            cur.execute('''
                CREATE OR REPLACE VIEW v_message_type_stats AS
                SELECT room_id, type, COUNT(*) AS count
                FROM messages
                GROUP BY room_id, type''')

            cur.execute('DROP TRIGGER IF EXISTS trg_message_delete_audit')
            cur.execute('''
                CREATE TRIGGER trg_message_delete_audit
                AFTER DELETE ON messages
                FOR EACH ROW
                INSERT INTO message_audit
                    (room_id, message_id, type, content, sender, user_id, deleted_at, action)
                VALUES
                    (OLD.room_id, OLD.id, OLD.type, OLD.content, OLD.sender,
                     OLD.user_id, UNIX_TIMESTAMP(), 'DELETE')''')

            cur.execute('DROP PROCEDURE IF EXISTS sp_get_message_stats')
            cur.execute('''
                CREATE PROCEDURE sp_get_message_stats(IN p_room_id VARCHAR(64))
                BEGIN
                    SELECT COUNT(*) AS total_messages
                    FROM messages WHERE room_id = p_room_id;

                    SELECT * FROM v_message_stats
                    WHERE room_id = p_room_id
                    ORDER BY message_count DESC;

                    SELECT m.type, COUNT(*) AS count,
                           ROUND(COUNT(*) * 100.0 /
                                 NULLIF((SELECT COUNT(*) FROM messages
                                         WHERE room_id = p_room_id), 0), 1) AS percentage
                    FROM messages m
                    WHERE m.room_id = p_room_id
                    GROUP BY m.type
                    ORDER BY count DESC;
                END''')

            cur.execute('DROP PROCEDURE IF EXISTS sp_cleanup_old_messages')
            cur.execute('''
                CREATE PROCEDURE sp_cleanup_old_messages(
                    IN p_room_id VARCHAR(64), IN p_max_keep INT)
                BEGIN
                    DELETE FROM messages
                    WHERE room_id = p_room_id
                      AND seq NOT IN (
                          SELECT seq FROM (
                              SELECT seq FROM messages
                              WHERE room_id = p_room_id
                              ORDER BY seq DESC LIMIT p_max_keep
                          ) t
                      );
                END''')

            cur.execute('DROP PROCEDURE IF EXISTS sp_get_audit_log')
            cur.execute('''
                CREATE PROCEDURE sp_get_audit_log(
                    IN p_room_id VARCHAR(64), IN p_limit INT)
                BEGIN
                    SELECT * FROM message_audit
                    WHERE room_id = p_room_id
                    ORDER BY audit_id DESC
                    LIMIT p_limit;
                END''')

            cur.execute("INSERT INTO config (`key`, `value`) VALUES ('admin_password', 'ADMIN') "
                        "ON DUPLICATE KEY UPDATE `value` = `value`")
    finally:
        conn.close()


# ============================================================================
# 数据库：主库封装（rooms / config 表）
# ============================================================================
class DatabaseManager:
    """
    lan_chat 主库的线程安全封装——全局单例（模块底部 db_manager）。

    管辖的表：
      rooms   房间注册表（单一事实来源）：所有房间的名称、开关、密码、
              文件限额、保留条数、撤回时限都存这里；ChatRoom 启动时读入
              内存，setter 修改后经 update_room() 写回。
      config  全局键值配置：目前存 admin_password（管理员登录密码）。

    线程安全：
      self._lock 串行化同一连接上的所有游标操作（PyMySQL 连接非线程安全）。
      autocommit=True，每条语句独立提交，无需手动事务。

    与其他类的关系：
      - DatabaseManager 只管「房间存在与否 + 全局配置」，不管房间内业务数据。
      - 房间内业务数据（messages/profiles/...）由 Database（room_id 作用域）访问。
      - delete_room() 负责删房时的「跨层清理」：先显式清审计表（无外键），
        再删 rooms 行（外键 CASCADE 级联清 profiles/messages/blacklist/favorites），
        最后删磁盘上传目录。
    """

    def __init__(self):
        """创建线程锁 + 建立到 MySQL 的独立连接（autocommit + DictCursor）。"""
        self._lock = threading.Lock()
        self._conn = _mysql_connect()

    # ------------------------------------------------------------------
    # config 键值读写
    # ------------------------------------------------------------------
    def get_config(self, key, default=None):
        """
        读取一个全局配置值。

        参数：
            key     (str) —— 配置键，如 'admin_password'。
            default       —— 键不存在时的返回值（不抛异常）。
        返回：
            配置值（str/None）或 default。
        SQL：SELECT value FROM config WHERE key=%s（参数化，防注入）。
        """
        rows = self._query('SELECT `value` FROM config WHERE `key` = %s', (key,))
        return rows[0]['value'] if rows else default

    def set_config(self, key, value):
        """
        写入/更新一个全局配置值（幂等 upsert）。

        INSERT ... ON DUPLICATE KEY UPDATE：
            键已存在 → 更新 value；不存在 → 插入新行。
            依赖 config 表主键 key 触发「重复键」分支。
        典型调用：修改管理员密码 set_config('admin_password', new_pw)。
        """
        self._execute('INSERT INTO config (`key`, `value`) VALUES (%s, %s) '
                      'ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)', (key, value))

    # ------------------------------------------------------------------
    # 底层读写（所有 SQL 必须走这两个方法，保证持锁 + 参数化）
    # ------------------------------------------------------------------
    def _query(self, sql, params=()):
        """
        执行 SELECT，返回全部行（list[dict]）。
        持 self._lock → 取游标 → execute(sql, params) → fetchall。
        params 元组按 %s 占位符顺序填充，杜绝字符串拼接 SQL。
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def _execute(self, sql, params=()):
        """
        执行 INSERT/UPDATE/DELETE，返回受影响行数（int）。
        autocommit=True，执行完即提交，无显式 commit()。
        调用方可通过返回值判断是否命中（如 remove_* 返回 >0 表示成功）。
        """
        with self._lock:
            with self._conn.cursor() as cur:
                return cur.execute(sql, params)

    # ------------------------------------------------------------------
    # rooms 表 CRUD
    # ------------------------------------------------------------------
    def list_rooms(self):
        """
        列出全部房间，按创建时间倒序（最新房间排最前）。
        返回：list[dict]，每项为 rooms 表一行的全部列。
        调用方：/rooms 页面渲染、/api/rooms、删除房间后判断是否需要重建默认房。
        """
        return self._query('SELECT * FROM rooms ORDER BY created_at DESC')

    def get_room(self, room_id):
        """
        按 room_id 查单个房间。
        返回：dict（存在）或 None（不存在）——上层据此区分 404 与成功。
        """
        rows = self._query('SELECT * FROM rooms WHERE room_id = %s', (room_id,))
        return rows[0] if rows else None

    def create_room(self, room_id, room_name, password=None):
        """
        插入一行新房间（rooms 表）。

        参数：
            room_id   (str) —— 应用生成的唯一标识（room_毫秒时间戳_随机hex）。
            room_name (str) —— 显示名称。
            password         —— 进房密码，None 表示无密码。

        只插入 4 列，其余列（is_open/file_limit_mb/max_history/recall_time_limit）
        走表定义的 DEFAULT 值（1/0/200/300），保持「新房间默认配置」只在
        DDL 一处定义，避免应用层与表结构两处硬编码不一致。
        created_at 显式写入 time.time()（Unix 时间戳），供 list_rooms 排序。
        """
        self._execute('INSERT INTO rooms(room_id, room_name, password, created_at) VALUES (%s, %s, %s, %s)',
                      (room_id, room_name, password, time.time()))

    def delete_room(self, room_id):
        """
        删除房间——跨层清理，顺序很重要：

        步骤 1：显式 DELETE message_audit WHERE room_id=...。
            message_audit 无外键（设计权衡，见 schema.sql 表7注释），
            不会随 rooms 删除级联，必须先手动清，否则留下孤儿审计行。
        步骤 2：DELETE rooms WHERE room_id=...。
            profiles/messages/blacklist/favorites 对 rooms 有
            ON DELETE CASCADE 外键，此语句触发级联，业务数据全部连带删除。
            每删一行 messages 都会触发 trg_message_delete_audit——
            但审计行已在步骤1清掉、且新触发的审计行又会指向已删 room_id，
            因此调用方（api_delete_room）在事务语义上接受这些「删房审计」
            短暂存在；全局恢复出厂时会再显式清一次。
        步骤 3：删除磁盘上传目录 uploads_<room_id>/。
            Windows 下目录内若有打开的文件句柄会删失败，用
            shutil.rmtree(..., ignore_errors=True) 忽略残余错误，不阻塞主流程。
        """
        self._execute('DELETE FROM message_audit WHERE room_id = %s', (room_id,))
        self._execute('DELETE FROM rooms WHERE room_id = %s', (room_id,))
        up = os.path.join(BASE_DIR, f'uploads_{room_id}')
        if os.path.isdir(up):
            shutil.rmtree(up, ignore_errors=True)

    def update_room(self, room_id, **kwargs):
        """
        按 room_id 局部更新房间配置（只更新传入的字段）。

        参数：
            room_id (str) —— 目标房间。
            **kwargs       —— 要更新的列名=值，如 room_name='xx', is_open=0。
                              列名来自调用方（ChatRoom._save_state 等），
                              为受控白名单，不存在用户输入直接进列名的风险。

        实现：动态拼 'SET col1=%s, col2=%s'，值全部走 %s 参数化。
        kwargs 为空时直接返回，避免生成非法 'UPDATE rooms SET WHERE ...'。
        """
        if not kwargs:
            return
        sets = ', '.join(f'{k} = %s' for k in kwargs)
        vals = list(kwargs.values()) + [room_id]
        self._execute(f'UPDATE rooms SET {sets} WHERE room_id = %s', vals)


# ============================================================================
# 数据库：房间业务表访问层（同库、room_id 作用域）
# ============================================================================
class Database:
    """
    单个房间的业务数据读写——每房间实例持有一个独立 MySQL 连接。

    管辖的表（查询一律带 WHERE room_id=%s，保证房间隔离）：
      profiles       用户档案（房间+设备复合主键）
      messages       聊天消息（seq 自增代理主键）
      blacklist      踢出黑名单
      favorites      收藏夹（快照冗余）
      message_audit  消息删除审计（只读；写入由触发器完成）

    关联的数据库对象：
      视图   v_message_stats / v_message_type_stats —— 供 sp_get_message_stats 复用
      触发器 trg_message_delete_audit —— 本类任何 delete_message/clear/factory/
             cleanup 都会触发审计写入，业务代码不直接 INSERT audit
      存储过程 sp_get_message_stats / sp_cleanup_old_messages / sp_get_audit_log
             —— CALL 调用，多结果集用 cursor.nextset() 依次读取

    线程安全：
      self._lock 串行化本连接上的游标操作；autocommit=True。

    生命周期：
      由 RoomManager.get_or_create 创建，随 ChatRoom 缓存存活；
      删房/全局重置时先调 close() 释放连接（Windows 下尽早释放文件/句柄锁），
      再删数据库行与磁盘目录。
    """

    def __init__(self, room_id):
        """
        参数：
            room_id (str) —— 绑定的房间标识；本实例所有 SQL 都以它为作用域。
        side effect：建立一条新的 MySQL 连接；max_history 用全局默认值初始化，
                     随后 ChatRoom.__init__ 会用房间配置覆盖它。
        """
        self.room_id = room_id
        self._lock = threading.Lock()
        self._conn = _mysql_connect()
        self.max_history = MAX_HISTORY   # 兜底默认；ChatRoom 构造时用 rooms.max_history 覆盖

    def close(self):
        """关闭 MySQL 连接（幂等；重复 close 或已断开时静默忽略异常）。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _query(self, sql, params=()):
        """执行 SELECT，持锁返回 list[dict]。params 按 %s 顺序填充。"""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def _execute(self, sql, params=()):
        """执行写语句，持锁返回受影响行数；autocommit 自动提交。"""
        with self._lock:
            with self._conn.cursor() as cur:
                return cur.execute(sql, params)

    # ------------------------------------------------------------------
    # 档案 profiles —— 匿名用户的持久身份（昵称/头像/颜色/禁言截止）
    # ------------------------------------------------------------------
    def get_profile(self, device_id):
        """
        按 (当前房间, device_id) 查用户档案。
        返回：dict（存在）/ None（首次进入该房间，尚无档案）。
        调用方：join() 判断是「首次随机分配头像」还是「复用已有档案」；
                send_message() 读 muted_until 判断是否禁言中。
        """
        rows = self._query('SELECT * FROM profiles WHERE room_id=%s AND device_id=%s',
                           (self.room_id, device_id))
        return rows[0] if rows else None

    def save_profile(self, device_id, nickname, avatar, color):
        """
        插入/更新用户档案（幂等 upsert）。

        依赖 profiles 复合主键 (room_id, device_id)：
          不存在 → INSERT，muted_until 显式写 0（未禁言）；
          已存在 → ON DUPLICATE KEY UPDATE 刷新 nickname/avatar/color，
                   并把 muted_until 重置为 0。

        注意：muted_until=0 的重置语义——「重新保存档案即视为新身份，
        清除禁言」目前 join() 只在档案不存在时调 save_profile，
        已有档案走 update_profile_nickname，不会误清禁言；
        此处的 muted_until=0 仅在 INSERT 分支生效（新档案默认未禁言）。
        """
        self._execute(
            'INSERT INTO profiles(room_id,device_id,nickname,avatar,color,muted_until) VALUES(%s,%s,%s,%s,%s,0) '
            'ON DUPLICATE KEY UPDATE nickname=VALUES(nickname), avatar=VALUES(avatar), color=VALUES(color), muted_until=0',
            (self.room_id, device_id, nickname, avatar, color))

    def update_profile_nickname(self, device_id, nickname):
        """
        仅更新档案昵称（用户改名后 join 时调用）。
        不动 avatar/color/muted_until —— 保持视觉身份与禁言状态延续。
        """
        self._execute('UPDATE profiles SET nickname=%s WHERE room_id=%s AND device_id=%s',
                      (nickname, self.room_id, device_id))

    def set_muted_until(self, device_id, muted_until):
        """
        设置禁言截止时间戳。
        参数 muted_until：0 = 解除禁言；> now = 禁言到该时刻。
        由 ChatRoom.mute_user / unmute_user 调用。
        """
        self._execute('UPDATE profiles SET muted_until=%s WHERE room_id=%s AND device_id=%s',
                      (muted_until, self.room_id, device_id))

    def get_muted_users(self):
        """
        查询当前房间所有「禁言尚未到期」的档案（muted_until > now）。
        返回列：device_id/nickname/avatar/color/muted_until。
        调用方：ChatRoom.get_muted_users() 补充 online/user_id/remaining 后
                返回给管理端「禁言名单」面板。
        """
        return self._query(
            'SELECT device_id,nickname,avatar,color,muted_until FROM profiles '
            'WHERE room_id=%s AND muted_until>%s',
            (self.room_id, time.time()))

    # ------------------------------------------------------------------
    # 消息 messages —— 聊天核心数据
    # ------------------------------------------------------------------
    def query_messages(self, nickname=None):
        """
        按昵称模糊搜索文本消息，按发送者分组返回（管理端「消息查询」）。

        参数：
            nickname (str|None) —— 搜索关键词；None/空 = 搜全部文本消息。
        返回：
            list[dict]，每项 {'sender','avatar','color','messages':[msg,...]}，
            便于前端按发送者折叠展示。

        SQL：
            sender LIKE '%kw%' —— 模糊匹配，命中 idx_messages_room_sender
            (room_id, sender) 索引的前缀（room_id 等值 + sender 范围）。
            仅 type='text' —— 文件/图片走 query_files()。
            ORDER BY timestamp ASC —— 组内按时间正序。
        """
        if nickname:
            rows = self._query(
                'SELECT * FROM messages WHERE room_id=%s AND sender LIKE %s AND type=%s ORDER BY timestamp ASC',
                (self.room_id, f'%{nickname}%', 'text'))
        else:
            rows = self._query(
                'SELECT * FROM messages WHERE room_id=%s AND type=%s ORDER BY timestamp ASC',
                (self.room_id, 'text'))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            groups.setdefault(msg['sender'], {'sender': msg['sender'], 'avatar': msg['avatar'],
                                              'color': msg['color'], 'messages': []})['messages'].append(msg)
        return list(groups.values())

    def query_files(self, nickname=None, upload_dir=None):
        """
        按昵称模糊搜索图片/文件消息，按发送者分组返回。

        与 query_messages 的差异：
          1. type IN ('image','file') —— 只查文件类消息。
          2. 额外计算 file_exists —— 检查 content 中 URL 对应的磁盘文件
             是否仍存在（文件可能被手动删除/清理），前端据此置灰下载按钮。
        upload_dir：该房间的上传目录绝对路径；None 时退回全局 UPLOAD_ROOT
             （兼容早期单房间版本遗留文件）。
        返回结构与 query_messages 相同（按 sender 分组）。
        """
        if nickname:
            rows = self._query(
                'SELECT * FROM messages WHERE room_id=%s AND sender LIKE %s AND type IN (%s,%s) ORDER BY timestamp ASC',
                (self.room_id, f'%{nickname}%', 'image', 'file'))
        else:
            rows = self._query(
                'SELECT * FROM messages WHERE room_id=%s AND type IN (%s,%s) ORDER BY timestamp ASC',
                (self.room_id, 'image', 'file'))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            fn = os.path.basename(msg.get('content') or '')
            msg['file_exists'] = os.path.isfile(os.path.join(upload_dir or UPLOAD_ROOT, fn))
            groups.setdefault(msg['sender'], {'sender': msg['sender'], 'avatar': msg['avatar'],
                                              'color': msg['color'], 'messages': []})['messages'].append(msg)
        return list(groups.values())

    def _row_to_message(self, r):
        """
        MySQL 行 dict → 前端消息 dict（统一字段名与类型）。

        转换要点：
          is_admin：TINYINT(0/1) → bool，前端直接 if 判断；
          舍弃 seq 列 —— seq 是数据库内部代理主键，前端只认业务 id。
        该方法是「DB 行 → API JSON」的唯一出口，保证返回结构稳定。
        """
        return {'id': r['id'], 'type': r['type'], 'content': r['content'], 'sender': r['sender'],
                'user_id': r['user_id'], 'is_admin': bool(r['is_admin']), 'avatar': r['avatar'],
                'color': r['color'], 'timestamp': r['timestamp'],
                'file_name': r['file_name'], 'file_size': r['file_size']}

    def get_recent_messages(self, limit=None):
        """
        取房间最近 N 条消息（时间正序返回，供前端渲染历史记录）。

        参数：
            limit (int|None) —— 返回条数；None 用 self.max_history
                                （房间配置的保留条数，默认 200）。
        SQL 技巧：
            ORDER BY seq DESC LIMIT N —— 先倒序取「最新的 N 条」
            （命中主键 seq 的排序，代价最低）；
            Python 侧 reversed(rows) 翻转为时间正序，避免
            ORDER BY timestamp（可能有并列值不稳定）+ 大 OFFSET。
        调用方：SSE 首包 history 事件、/api/messages 轮询兜底。
        """
        rows = self._query('SELECT * FROM messages WHERE room_id=%s ORDER BY seq DESC LIMIT %s',
                           (self.room_id, limit or self.max_history))
        return [self._row_to_message(r) for r in reversed(rows)]

    def add_message(self, msg):
        """
        插入一条消息（幂等 upsert）。

        参数：
            msg (dict) —— ChatRoom 构造的消息对象，必含 id/type/content/sender/
                          user_id/is_admin/avatar/color/timestamp；
                          file_name/file_size 可为 None（非文件消息）。

        依赖 UNIQUE(room_id, id)：
          首次插入正常写入；
          若同 id 已存在（极端并发重复提交、或幂等重放），走 ON DUPLICATE KEY
          UPDATE 刷新为最新内容，而不是抛 1062 错误——保证上层 send_message
          拿到的总是成功语义。

        消息发送后，ChatRoom.send_message 还会 broadcast 给 SSE 客户端；
        本方法只负责落库，不涉及推送。
        """
        self._execute(
            'INSERT INTO messages(room_id,id,type,content,sender,user_id,is_admin,avatar,color,timestamp,file_name,file_size) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
            'ON DUPLICATE KEY UPDATE type=VALUES(type), content=VALUES(content), sender=VALUES(sender), '
            'user_id=VALUES(user_id), is_admin=VALUES(is_admin), avatar=VALUES(avatar), color=VALUES(color), '
            'timestamp=VALUES(timestamp), file_name=VALUES(file_name), file_size=VALUES(file_size)',
            (self.room_id, msg['id'], msg.get('type'), msg.get('content'), msg.get('sender'),
             msg.get('user_id'), int(msg.get('is_admin', False)), msg.get('avatar'),
             msg.get('color'), msg.get('timestamp'), msg.get('file_name'), msg.get('file_size')))

    # ------------------------------------------------------------------
    # 黑名单 blacklist —— 被踢设备禁止再入该房间
    # ------------------------------------------------------------------
    def is_blacklisted(self, device_id):
        """
        判断设备是否在当前房间黑名单中。
        SELECT 1 AS x —— 只关心存在性，不取多余列（轻微优化）。
        返回：bool。join() 在通过开放/密码校验后调用。
        """
        return bool(self._query('SELECT 1 AS x FROM blacklist WHERE room_id=%s AND device_id=%s',
                                (self.room_id, device_id)))

    def add_blacklist(self, device_id, nickname, ip):
        """
        拉黑设备（幂等 upsert）。
        依赖复合主键 (room_id, device_id)：已存在则刷新 nickname/ip/created_at
        （以最后一次拉黑为准），不报重复键错误。
        由 ChatRoom.kick_user 调用：踢出在线会话 + 写入本表。
        """
        self._execute(
            'INSERT INTO blacklist(room_id,device_id,nickname,ip,created_at) VALUES(%s,%s,%s,%s,%s) '
            'ON DUPLICATE KEY UPDATE nickname=VALUES(nickname), ip=VALUES(ip), created_at=VALUES(created_at)',
            (self.room_id, device_id, nickname, ip, time.time()))

    def remove_blacklist(self, device_id):
        """
        把设备移出黑名单。
        返回：bool（True=确实删了一行；False=本来就不在，供上层 404）。
        """
        return self._execute('DELETE FROM blacklist WHERE room_id=%s AND device_id=%s',
                             (self.room_id, device_id)) > 0

    def get_blacklist(self):
        """
        当前房间黑名单列表，按拉黑时间倒序（最新在前）。
        返回列：device_id/nickname/ip/created_at。
        """
        return self._query(
            'SELECT device_id,nickname,ip,created_at FROM blacklist WHERE room_id=%s ORDER BY created_at DESC',
            (self.room_id,))

    # ------------------------------------------------------------------
    # 收藏夹 favorites —— 消息快照收藏
    # ------------------------------------------------------------------
    def add_favorite(self, user_id, msg):
        """
        收藏一条消息（幂等，重复收藏静默忽略）。

        参数：
            user_id (str) —— 收藏者当前会话标识。
            msg     (dict) —— get_message_by_id() 返回的完整消息行。

        依赖 UNIQUE(room_id, user_id, msg_id) + INSERT IGNORE：
          首次收藏 → 正常插入；
          重复收藏 → IGNORE 静默跳过（affected rows=0），不抛 1062。

        快照冗余：msg_type/content/sender/file_name/file_size 从原消息复制，
          之后即使原消息被撤回/删除，收藏夹仍能独立展示——这是
          favorites 表不对 msg_id 建外键的原因（schema.sql 表6注释）。
        """
        self._execute(
            'INSERT IGNORE INTO favorites(room_id,user_id,msg_id,msg_type,content,sender,file_name,file_size,created_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (self.room_id, user_id, msg.get('id'), msg.get('type'), msg.get('content'),
             msg.get('sender'), msg.get('file_name'), msg.get('file_size'), time.time()))

    def get_favorites(self, user_id):
        """
        列出某用户在当前房间的全部收藏，按收藏时间倒序（最新在前）。
        走 idx_favorites_user(room_id, user_id, created_at) 索引。
        """
        return self._query(
            'SELECT id,msg_id,msg_type,content,sender,file_name,file_size,created_at FROM favorites '
            'WHERE room_id=%s AND user_id=%s ORDER BY created_at DESC',
            (self.room_id, user_id))

    def remove_favorite(self, user_id, fav_id):
        """
        删除一条收藏。
        WHERE 同时带 id + user_id + room_id —— 三重校验防止越权：
        即使前端伪造别人的 fav_id，user_id/room_id 不匹配也删不掉。
        返回：bool（True=删除成功；False=不存在或无权）。
        """
        return self._execute('DELETE FROM favorites WHERE id=%s AND user_id=%s AND room_id=%s',
                             (fav_id, user_id, self.room_id)) > 0

    # ------------------------------------------------------------------
    # 清理 / 维护
    # ------------------------------------------------------------------
    def cleanup_old_messages(self, max_history):
        """
        按保留条数裁剪旧消息——调用存储过程 sp_cleanup_old_messages。

        参数：
            max_history (int) —— 保留最新 N 条（应用层已校验 ≥10）。

        存储过程内部 DELETE 掉 seq 不在「最新 N 条」集合内的行；
        每删一行都触发 trg_message_delete_audit 写审计（裁剪也留痕）。

        PyMySQL 调用存储过程的固定写法：
          execute('CALL ...') → 读结果 → while nextset(): pass
          nextset() 消耗完所有剩余结果集，避免残留状态影响
          同连接上的下一条语句。
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute('CALL sp_cleanup_old_messages(%s, %s)', (self.room_id, int(max_history)))
                while cur.nextset():
                    pass

    def clear_all_messages(self):
        """
        清空当前房间全部消息（管理端「清空聊天记录」）。
        每行 DELETE 都触发审计触发器——清空操作在审计表留痕。
        不清 profiles/blacklist/favorites/audit，只清 messages。
        """
        self._execute('DELETE FROM messages WHERE room_id=%s', (self.room_id,))

    def factory_reset(self):
        """
        房间级恢复出厂——清空该房间全部业务数据（不含 rooms 配置行本身）。
        按依赖顺序删除：messages（最底层）→ profiles → blacklist → favorites。
        message_audit 不清——审计是「谁删了什么」的历史事实，恢复出厂
        不应抹除审计痕迹（与 clear_history 的设计取向一致）。
        rooms 行的配置回默认值由 ChatRoom.factory_reset 负责（调 _save_state）。
        """
        self._execute('DELETE FROM messages WHERE room_id=%s', (self.room_id,))
        self._execute('DELETE FROM profiles WHERE room_id=%s', (self.room_id,))
        self._execute('DELETE FROM blacklist WHERE room_id=%s', (self.room_id,))
        self._execute('DELETE FROM favorites WHERE room_id=%s', (self.room_id,))

    def delete_message(self, message_id):
        """
        按业务 id 删除一条消息（房间作用域限定）。
        返回：bool（True=删了一行；False=id 不存在或不属于本房间）。
        触发器 trg_message_delete_audit 会自动写审计行。
        调用方：admin_delete_message（管理员强制删）、recall_message（撤回）。
        """
        return self._execute('DELETE FROM messages WHERE room_id=%s AND id=%s',
                             (self.room_id, message_id)) > 0

    def get_message_by_id(self, message_id):
        """
        按业务 id 查单条消息（房间作用域）。
        返回：dict / None。
        用途：撤回前校验归属与时间戳；管理员删消息/收藏前校验存在性。
        """
        rows = self._query('SELECT * FROM messages WHERE room_id=%s AND id=%s',
                           (self.room_id, message_id))
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # 存储过程调用（课程设计「数据库对象」展示）
    # ------------------------------------------------------------------
    def sp_get_message_stats(self):
        """
        调用 sp_get_message_stats(room_id) —— 一次拿三组统计数据。

        返回：
            {
              'total_messages': int,          # 结果集1：消息总数
              'user_stats': [dict, ...],      # 结果集2：按用户统计（视图 v_message_stats）
              'type_stats': [dict, ...]       # 结果集3：按类型统计（count + percentage）
            }

        PyMySQL 多结果集读取协议：
          execute('CALL ...') 后游标停在第一个结果集；
          fetchone/fetchall 读完当前集 → nextset() 切到下一个 → 再 fetch...；
          nextset() 返回 None 表示没有更多结果集。
        调用方：/api/admin/message_stats（仅在线管理员可见）。
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute('CALL sp_get_message_stats(%s)', (self.room_id,))
                total_row = cur.fetchone()
                total = (total_row or {}).get('total_messages', 0)
                user_stats = []
                type_stats = []
                if cur.nextset():
                    user_stats = list(cur.fetchall() or [])
                if cur.nextset():
                    type_stats = list(cur.fetchall() or [])
                return {'total_messages': total, 'user_stats': user_stats, 'type_stats': type_stats}

    def sp_get_audit_log(self, limit=50):
        """
        调用 sp_get_audit_log(room_id, limit) —— 取最近 N 条删除审计。

        参数：
            limit (int) —— 返回条数（路由层默认 50，前端可传）。
        返回：
            list[dict] —— message_audit 行，按 audit_id 降序（最新删除在前）。
        单结果集：fetchall 后 while nextset() 清残留状态即可。
        调用方：/api/admin/audit_log（仅在线管理员可见）。
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute('CALL sp_get_audit_log(%s, %s)', (self.room_id, int(limit)))
                rows = cur.fetchall()
                while cur.nextset():
                    pass
                return list(rows or [])


# ============================================================================
# 广播层（SSE 事件分发）
# ============================================================================
class Broadcaster:
    """
    每房间一个的 SSE 事件广播器——发布/订阅模型的「发布端」。

    工作原理：
      1. 每个浏览器 SSE 连接（/room/<id>/stream）在 event_stream() 开头调用
         register()，得到一个属于自己的 queue.Queue，存入 _clients 列表。
      2. 业务代码（ChatRoom 的 send/join/管理操作）调用 broadcast(event_dict)
         时，事件被 json.dumps 序列化一次，然后逐个 put 进所有客户端的队列。
      3. 每个 SSE 连接在自己的生成器循环里 q.get(timeout=30) 取数据，
         yield 成 'data: {...}\n\n' 推给浏览器；30 秒取不到就 yield 心跳注释行。
      4. 连接断开时（生成器 finally）调用 unregister() 摘除队列，防止泄漏。

    为什么用「每客户端一个 Queue」而不是共享一个：
      SSE 连接是长驻生成器，各自阻塞在 q.get()；独立队列天然实现
      「每个消费者按自己节奏取」，无需在 broadcast 时关心消费速率，
      也不会出现一个慢客户端阻塞其他客户端。

    线程安全：
      _lock 保护 _clients 列表的并发读写（register/unregister/broadcast
      来自不同 Flask 请求线程）。broadcast 时先持锁拷贝 targets 再遍历 put，
      避免遍历过程中列表被修改，也缩短持锁时间。
    """

    def __init__(self):
        """初始化线程锁与客户端队列列表（每房间实例独立）。"""
        self._lock = threading.Lock()
        self._clients = []   # list[queue.Queue]：当前在线的 SSE 连接各一个

    def register(self):
        """
        新 SSE 连接注册。
        返回：queue.Queue —— 该连接专属的事件队列；调用方（event_stream）
              保存它并循环 q.get()。
        """
        q = queue.Queue()
        with self._lock:
            self._clients.append(q)
        return q

    def unregister(self, q):
        """
        SSE 连接断开时注销队列（幂等：不在列表里也不报错）。
        由 event_stream() 的 finally 块调用，保证浏览器断开/超时/异常
        任何路径下都能摘除，防止 _clients 无限增长泄漏。
        """
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def broadcast(self, event):
        """
        向当前房间所有在线 SSE 客户端广播一个事件。

        参数：
            event (dict) —— 必须含 'type' 字段（前端 switch 分发依据），
                            如 {'type':'message','message':{...}}。
                            事件在业务层构造，本方法只负责序列化与投递。

        实现细节：
          json.dumps(..., ensure_ascii=False) —— 中文/emoji 不转成 \\uXXXX 转义序列，
              减小传输体积、前端 JSON.parse 后原样还原。
          持锁拷贝 targets 再遍历 —— 拷贝后立即放锁，某个客户端队列
              put 阻塞（理论上无界队列不会阻塞）不会卡住 register/unregister。
        """
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            targets = list(self._clients)
        for q in targets:
            q.put(data)


# ============================================================================
# 业务层（单个房间的完整逻辑）
# ============================================================================
class ChatRoom:
    """
    单个房间的业务逻辑中枢——每房间一个实例，由 RoomManager 缓存。

    持有的资源：
      db          Database 实例（该房间的 MySQL 连接与业务表读写）
      broadcaster Broadcaster 实例（该房间的 SSE 事件广播器）
      uploads_dir 该房间的文件上传目录绝对路径
      room_id     房间标识
      _db_manager 主库 DatabaseManager 引用（_save_state 写回 rooms 表用）
      lock        RLock，保护 online_users 与内存配置字段
      online_users dict：user_id → {user_id, nickname, device_id, avatar,
                    color, is_admin, ip}，「当前在线会话」的唯一事实来源

    内存中的房间配置副本（构造时从 rooms 表加载，setter 修改后 _save_state 写回）：
      room_name / room_password / room_open / file_limit_mb / max_history /
      recall_time_limit
      —— 热路径（join/send 的开放与密码校验）直接读内存，避免每次请求查库；
         配置变更通过 setter 同步「内存 + 数据库 + SSE 广播」三处，
         保证在线客户端实时看到新配置。

    所有管理方法的统一模式：
      1. is_admin(admin_id) 鉴权（在线身份层，见模块 docstring 权限模型）
      2. 校验参数合法性
      3. 修改内存字段 → _save_state() 持久化 → broadcaster.broadcast() 通知客户端
      4. 返回 (json_dict, http_status)，路由层直接 jsonify(result), status

    生命周期：
      RoomManager.get_or_create 首次访问时构造；删房/全局重置时从缓存摘除
      并 close() 其 db 连接。
    """

    def __init__(self, db, broadcaster, uploads_dir, room_id, db_manager):
        """
        构造房间实例并从主库加载配置到内存。

        参数：
            db          (Database)          —— 该房间的数据库访问对象
            broadcaster (Broadcaster)       —— 该房间的 SSE 广播器
            uploads_dir (str)               —— 上传目录绝对路径
            room_id     (str)               —— 房间标识
            db_manager  (DatabaseManager)   —— 主库引用（读写 rooms 表）

        配置加载细节：
          info = db_manager.get_room(room_id) or {} —— 房间行不存在时用空 dict，
            后续 .get(key, default) 全部走默认值（构造「配置齐全」的内存视图）。
          recall_time_limit 特殊处理：0 是合法业务值（=禁止撤回），
            不能用 `or 300` 兜底（0 是 falsy 会被吞掉），必须显式判 None。
          self.db.max_history 同步给 Database —— get_recent_messages 的
            LIMIT 用它，保持两处一致。
        """
        self.db = db
        self.broadcaster = broadcaster
        self.uploads_dir = uploads_dir
        self.room_id = room_id
        self._db_manager = db_manager
        self.lock = threading.RLock()       # 可重入锁：join 等方法内部可能嵌套调用
        self.online_users = {}              # user_id → 会话信息（在线表）
        # 从主库加载房间配置到内存（热更新由各 setter + _save_state 维护）
        info = db_manager.get_room(room_id) or {}
        self.room_name = info.get('room_name', '在线匿名聊天室')
        self.room_password = info.get('password')
        self.room_open = bool(info.get('is_open', 1))
        self.file_limit_mb = info.get('file_limit_mb', 0) or 0
        self.max_history = info.get('max_history', 200) or 200
        # 注意：0 是合法值（=禁止撤回），不能用 or 兜底，否则重启后 0 会变回 300
        rt = info.get('recall_time_limit')
        self.recall_time_limit = 300 if rt is None else int(rt)
        self.db.max_history = self.max_history   # 同步给 DB 层做消息裁剪

    def _save_state(self):
        """
        把内存中的房间配置持久化到主库 rooms 表（热更新的写回路径）。

        由每个配置 setter（set_room_name/set_room_open/set_password/
        set_file_limit/set_max_history/set_recall_time_limit/factory_reset）
        在修改内存字段后调用，实现「内存 + 数据库」双写一致。
        kwargs 列名来自本类受控字段，非用户输入，无注入风险。
        """
        self._db_manager.update_room(self.room_id,
            room_name=self.room_name, is_open=int(self.room_open),
            password=self.room_password, file_limit_mb=self.file_limit_mb,
            max_history=self.max_history, recall_time_limit=self.recall_time_limit)

    def _system_message(self, content):
        """
        发送一条系统消息（入库 + 广播），type='system'、sender='系统'。
        用于「xxx 加入了房间」「xxx 被踢出」等提示。
        id 用 f'{time.time()}_sys' 后缀，与用户消息的随机 hex 区分，
        且同秒多条系统消息靠 time.time() 浮点精度+后续 add_message 的
        ON DUPLICATE KEY UPDATE 兜底（极端同值时更新而非报错）。
        """
        msg = {'id': f'{time.time()}_sys', 'type': 'system', 'content': content, 'sender': '系统', 'timestamp': time.time()}
        self.db.add_message(msg)
        self.broadcaster.broadcast({'type': 'message', 'message': msg})

    def user_list_event(self):
        """
        构造 user_list 事件体（SSE 推送给所有客户端刷新侧边栏）。

        返回：
            {
              'type': 'user_list',
              'users': [{nickname, avatar, color}, ...],        # 普通用户
              'admin_users': [{user_id, nickname, avatar, color}, ...]  # 管理员
            }
        管理员数组额外带 user_id —— 管理面板需要 user_id 调用踢出/禁言等 API；
        普通用户只展示昵称头像，不暴露 user_id（降低伪造面）。
        持 self.lock 读 online_users，保证与 join/leave 的写操作互斥。
        """
        with self.lock:
            normal = [{'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for u in self.online_users.values() if not u['is_admin']]
            admins = [{'user_id': uid, 'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for uid, u in self.online_users.items() if u['is_admin']]
        return {'type': 'user_list', 'users': normal, 'admin_users': admins}

    def is_admin(self, user_id):
        """
        判断 user_id 是否为「当前房间在线的管理员」——在线身份层鉴权核心。

        返回：bool。False 的情况：user_id 为空、不在 online_users、
              或在线但 is_admin=False。
        与会话层 session['role'] 的区别：
          session 只说明「浏览器登录了管理员」；本方法还要求
          「以管理员身份真实加入了这个房间」——防止会话是 admin
          但未进房/伪造 user_id 的越权调用房间内管理 API。
        所有 /api/admin/* 路由与 ChatRoom 管理方法的第一步都是它。
        """
        with self.lock:
            user = self.online_users.get(user_id)
            return user is not None and user.get('is_admin')

    # ------------------------------------------------------------------
    # 加入 / 离开
    # ------------------------------------------------------------------
    def join(self, nickname, device_id, ip, password=None, is_admin_user=False):
        """
        加入房间——创建一个匿名会话并写入/复用用户档案。

        参数：
            nickname      (str) —— 房间内显示的匿名昵称（用户输入）。
            device_id     (str) —— 浏览器设备标识（前端 localStorage 持久化，
                                   用于关联 profiles 档案与 blacklist 黑名单）。
            ip            (str) —— 来源 IP（request.remote_addr，存在线表供管理面板展示）。
            password      (str) —— 用户输入的房间密码（房间无密码时忽略）。
            is_admin_user (bool) —— 是否以管理员身份加入；路由层已验会话，
                                   这里据此跳过 开放/密码/黑名单 校验，
                                   并用 admin_ 前缀的 device_id/user_id
                                   隔离档案命名空间（避免与普通用户档案冲突）。

        校验顺序（每步失败返回 (error_json, http_status)）：
          1. nickname/device_id 非空               → 400
          2. 房间开放（管理员豁免）                  → 403
          3. 房间密码匹配（管理员豁免、无密码跳过）   → 403
          4. 设备不在黑名单（管理员豁免）            → 403
          5. 同房间昵称唯一（先清同 device 残留会话）→ 400

        成功路径：
          a. 清残留：同一 device_id 若有旧会话（刷新页面 leave 未送达），
             先从 online_users 摘除，否则旧会话占昵称导致新会话 400。
          b. 档案复用：profiles 已有该 device → 复用 avatar/color；
             昵称变了只 update nickname。首次 → 随机分配 avatar/color 并 INSERT。
          c. 生成会话 user_id（每次进入都新的随机串，匿名性）：
             管理员 admin_ + 8位hex；普通 user_ + 时间戳_4位hex。
          d. 写入 online_users → 广播 user_list → 读 muted_until 算禁言剩余。

        返回成功 dict：
          {success, user_id, nickname, avatar, color, muted_remaining,
           file_limit_mb} + 200
          —— 前端存 user_id 供后续 send/recall/管理 API 使用。
        """
        nickname = (nickname or '').strip()
        if not nickname or not device_id:
            return {'error': '昵称和设备ID不能为空'}, 400
        if not self.room_open and not is_admin_user:
            return {'error': '房间已关闭'}, 403
        if self.room_password and password != self.room_password and not is_admin_user:
            return {'error': '密码错误'}, 403
        if self.db.is_blacklisted(device_id) and not is_admin_user:
            return {'error': '您已被加入黑名单'}, 403
        # 管理员档案独立命名空间（admin_ 前缀），与普通用户同设备档案隔离
        target_device_id = f'admin_{device_id}' if is_admin_user else device_id
        # 同设备重复加入（刷新页面且 leave 未送达）：先清掉残留会话，
        # 否则旧会话占着昵称，新会话会报"昵称已被占用"
        with self.lock:
            for uid in [uid for uid, u in self.online_users.items()
                        if u.get('device_id') == target_device_id]:
                self.online_users.pop(uid, None)
        # 昵称唯一：同房间在线不允许重名（上面已清掉自己的残留会话）
        with self.lock:
            for uid, u in self.online_users.items():
                if u['nickname'] == nickname:
                    return {'error': '昵称已被占用'}, 400
        # 档案：首次随机分配头像/颜色，之后复用；昵称变了就更新
        existing = self.db.get_profile(target_device_id)
        if not existing:
            avatar, color = random.choice(AVATARS), random.choice(COLORS)
            self.db.save_profile(target_device_id, nickname, avatar, color)
        else:
            avatar, color = existing['avatar'], existing['color']
            if existing['nickname'] != nickname:
                self.db.update_profile_nickname(target_device_id, nickname)
        # 会话 user_id：每次进入随机生成（匿名会话，device_id 只关联档案）
        user_id = f'admin_{os.urandom(4).hex()}' if is_admin_user else f'user_{time.time()}_{os.urandom(2).hex()}'
        user = {'user_id': user_id, 'nickname': nickname, 'device_id': target_device_id,
                'avatar': avatar, 'color': color, 'is_admin': is_admin_user, 'ip': ip}
        with self.lock:
            self.online_users[user_id] = user
        self.broadcaster.broadcast(self.user_list_event())
        profile = self.db.get_profile(target_device_id)
        muted_remaining = max(0, int(profile['muted_until'] - time.time())) if profile else 0
        return {'success': True, 'user_id': user_id, 'nickname': nickname,
                'avatar': avatar, 'color': color, 'muted_remaining': muted_remaining,
                'file_limit_mb': self.file_limit_mb}, 200

    def leave(self, user_id):
        """
        离开房间：从 online_users 移除会话。
        user_id 不在（重复 leave/超时已清）时静默成功——
        leave 常由页面 unload 的 sendBeacon 触达，幂等返回避免无意义报错。
        有移除才广播 user_list 更新侧边栏。
        返回：({'success': True}, 无状态码，由路由层默认 200)。
        """
        with self.lock:
            user = self.online_users.pop(user_id, None)
        if user:
            self.broadcaster.broadcast(self.user_list_event())
        return {'success': True}

    # ------------------------------------------------------------------
    # 发消息
    # ------------------------------------------------------------------
    def send_message(self, user_id, content, msg_type, display_name=None):
        """
        发送一条聊天消息（text / image / file）——聊天核心写路径。

        参数：
            user_id     (str) —— 发送者会话标识（join 返回的 user_id）。
            content     (str) —— 文本正文，或 image/file 的上传 URL。
            msg_type    (str) —— 'text' | 'image' | 'file'。
            display_name(str) —— 文件消息的自定义显示名（可选，默认用磁盘文件名）。

        校验链（全部在业务层，路由层只做参数透传）：
          1. user_id 在 online_users        → 403（会话过期/伪造）
          2. room_open                       → 403（房间被管理员关闭）
          3. profiles.muted_until ≤ now      → 403 + 剩余秒数（禁言中）
          4. msg_type ∈ 白名单               → 400
          5. content 非空                    → 400
          6a. text  → filter_sensitive() 星号替换后入库
          6b. image/file → UPLOAD_URL_RE.fullmatch(content) 必须通过
               （防止把任意字符串当文件 URL 注入），file 还要求磁盘文件
               真实存在，并 stat 大小、截取显示名（≤100 字符）。

        写路径：
          构造 message dict（含发送者 avatar/color 快照，档案日后被改
          也不影响历史消息展示）→ db.add_message() 落库（幂等 upsert）→
          broadcaster.broadcast({'type':'message', ...}) 推给在线客户端。

        返回：({'success': True}, 200) 或 (error, 4xx)。
        """
        with self.lock:
            user = self.online_users.get(user_id)
            if user is None:
                return {'error': '您不在房间中'}, 403
            if not self.room_open:
                return {'error': '房间已关闭'}, 403
            profile = self.db.get_profile(user['device_id'])
            muted_until = profile['muted_until'] if profile else 0
        if muted_until > time.time():
            return {'error': f'您已被禁言，剩余 {int(muted_until - time.time())} 秒'}, 403
        content = (content or '').strip()
        if msg_type not in ('text', 'image', 'file'):
            return {'error': '不支持的消息类型'}, 400
        if not content:
            return {'error': '消息不能为空'}, 400
        if msg_type == 'text':
            content = filter_sensitive(content)
        elif not UPLOAD_URL_RE.fullmatch(content):
            return {'error': '无效的文件地址'}, 400
        file_name = file_size = None
        if msg_type == 'file':
            disk_path = os.path.join(self.uploads_dir, os.path.basename(content))
            if not os.path.isfile(disk_path):
                return {'error': '文件不存在或已被删除'}, 400
            file_size = os.path.getsize(disk_path)
            # 显示名：优先用户自定义 display_name（防路径穿越取 basename），截断 100 字符
            file_name = (os.path.basename((display_name or '').replace('\\', '/')).strip()[:100] or os.path.basename(content))
        message = {'id': f'{time.time()}_{os.urandom(2).hex()}', 'type': msg_type, 'content': content,
                   'sender': user['nickname'], 'user_id': user_id, 'is_admin': user['is_admin'],
                   'avatar': user['avatar'], 'color': user['color'], 'timestamp': time.time(),
                   'file_name': file_name, 'file_size': file_size}
        self.db.add_message(message)
        self.broadcaster.broadcast({'type': 'message', 'message': message})
        return {'success': True}, 200

    # =======================================================================
    # 房间配置（均需在线管理员身份 —— is_admin 第一步统一校验）
    # =======================================================================
    # 这组 setter 遵循同一模板：
    #   is_admin 校验 → 参数校验 → 改内存字段 → _save_state() 持久化到
    #   rooms 表 → 广播对应事件让在线客户端即时同步。
    # 内存字段（room_name/room_open/...）是运行时权威副本，DB 是持久层；
    # _save_state 的 UPDATE 写穿两者，保证重启后配置不丢。
    # =======================================================================
    def set_room_name(self, admin_id, name):
        """
        修改房间名称。

        参数 admin_id —— 操作者会话 ID，is_admin 校验 → 非管理员 403。
        参数 name     —— 新名称，strip 后非空校验 → 空 400。

        成功：更新 self.room_name → _save_state() 写 rooms.room_name →
        广播 {'type':'room_name'} → 所有客户端 JS 收到后改 document.title
        与侧边栏标题。返回 ({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        name = (name or '').strip()
        if not name:
            return {'error': '名称不能为空'}, 400
        self.room_name = name
        self._save_state()
        self.broadcaster.broadcast({'type': 'room_name', 'name': self.room_name})
        return {'success': True}, 200

    def set_room_open(self, admin_id, is_open):
        """
        开/关房间。

        参数 admin_id —— is_admin 校验 → 403。
        参数 is_open  —— bool；False = 关闭房间。

        关闭时的连锁反应（比开启多一步）：
          1. self.room_open = False + _save_state() 持久化；
          2. 广播 {'type':'room_closed'} → 客户端收到后禁用输入框/
             提示「房间已关闭」/ 断开 SSE；
          3. 后续 join/send_message 都会被 room_open 拦住（403）。
        开启时只改状态 + 广播可省略（前端通过 room_status 重新拉取），
        此处统一广播保证两端状态机一致。
        返回：({'success': True, 'open': bool}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.room_open = bool(is_open)
        self._save_state()
        if not self.room_open:
            self.broadcaster.broadcast({'type': 'room_closed'})
        return {'success': True, 'open': self.room_open}, 200

    def set_password(self, admin_id, password):
        """
        设置/清除房间密码。

        参数 admin_id —— is_admin 校验 → 403。
        参数 password —— 字符串；空串/None 归一化为 None 表示「无密码」，
        非空则明文存 rooms.password（内网课设场景，非生产级哈希）。

        广播 password_changed 通知在线客户端：
        前端据此可选择踢出无密码会话（当前实现为提示，不强制断开）；
        下次 join 时 ChatRoom.join 会重新校验密码。
        返回：({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.room_password = password or None
        self._save_state()
        self.broadcaster.broadcast({'type': 'password_changed'})
        return {'success': True}, 200

    def set_file_limit(self, admin_id, limit_mb):
        """
        设置单文件大小上限（MB）。

        参数 admin_id —— is_admin 校验 → 403。
        参数 limit_mb —— int/str；max(0, int(...)) 归一化，0 = 不限制。

        双重生效：
          1. 本方法写 self.file_limit_mb + _save_state() + 广播 file_limit
             （在线客户端更新「选择文件」处的提示文案）；
          2. 路由层 api_admin_set_file_limit 在收到 200 后调
             _sync_upload_cap(g.room) 把 Flask 全局 MAX_CONTENT_LENGTH
             同步为新限额——框架层第一道闸即时热生效，无需重启。
        返回：({'success': True, 'file_limit_mb': int}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.file_limit_mb = max(0, int(limit_mb or 0))
        self._save_state()
        self.broadcaster.broadcast({'type': 'file_limit', 'limit_mb': self.file_limit_mb})
        return {'success': True, 'file_limit_mb': self.file_limit_mb}, 200

    def set_max_history(self, admin_id, max_history):
        """
        设置消息保留条数并立即裁剪旧消息。

        参数 admin_id    —— is_admin 校验 → 403。
        参数 max_history —— 必须是数字且 ≥10，否则 400
        （下限 10 防止误操作把历史瞬间清空到不可用）。

        执行链：
          1. 更新 self.max_history 与 db.max_history（两处保持同步，
             get_recent_messages/cleanup 都读 db.max_history）；
          2. _save_state() 持久化 rooms.max_history；
          3. db.cleanup_old_messages(self.max_history) —— DELETE 超出
             条数的最旧消息（有触发器的表，每行删都会写 message_audit？——
             注意：cleanup 只删 messages 主表，审计触发器记录的是
             admin_delete/recall 路径；常规裁剪按课设约定留痕策略见
             schema.sql trigger 注释）；
          4. 广播 max_history 让前端更新「仅保留最近 N 条」提示。
        返回：({'success': True, 'max_history': int}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not isinstance(max_history, (int, float)) or max_history < 10:
            return {'error': '消息条数不能小于10'}, 400
        self.max_history = int(max_history)
        self.db.max_history = self.max_history
        self._save_state()
        self.db.cleanup_old_messages(self.max_history)
        self.broadcaster.broadcast({'type': 'max_history', 'max_history': self.max_history})
        return {'success': True, 'max_history': self.max_history}, 200

    def clear_history(self, admin_id):
        """
        清空本房间全部聊天记录。

        参数 admin_id —— is_admin 校验 → 403。
        实现：db.clear_all_messages() → DELETE FROM messages
        WHERE room_id=...（每行触达 message_audit 触发器，删除动作
        在审计表留痕——「谁删了什么」可追溯）。

        注意与 factory_reset 的区别：
          clear_history 只清 messages 表，档案/黑名单/收藏/配置保留；
          factory_reset 清全部业务表 + 配置回默认。
        成功广播 history_cleared → 客户端清空聊天区 DOM。
        返回：({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.db.clear_all_messages()
        self.broadcaster.broadcast({'type': 'history_cleared'})
        return {'success': True}, 200

    def factory_reset(self, admin_id):
        """
        房间级恢复出厂（保留房间本身，只重置其内容与配置）。

        参数 admin_id —— is_admin 校验 → 403。

        与全局 /api/admin/global_reset 的层级关系：
          本方法 = 单房间粒度（DELETE 本 room_id 的业务数据）；
          global_reset = 全库粒度（删所有房间行 + 磁盘目录 + 重置密码）。

        执行步骤：
          1. db.factory_reset() —— 业务表全清（profiles/messages/
             blacklist/favorites 按 room_id；message_audit 需显式清或
             随 CASCADE，见 Database.factory_reset 实现）；
          2. 内存配置字段全部硬编码回默认值：
             room_name='在线匿名聊天室', room_password=None, room_open=True,
             file_limit_mb=0, max_history=200, recall_time_limit=300
             （与 schema.sql 种子数据 room_default 完全一致）；
          3. _save_state() 把默认配置写回 rooms 表；
          4. 广播 factory_reset → 客户端全量重载（清空在线表提示/刷新）。
        返回：({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.db.factory_reset()
        self.room_name = '在线匿名聊天室'
        self.room_password = None
        self.room_open = True
        self.file_limit_mb = 0
        self.max_history = 200
        self.recall_time_limit = 300
        self._save_state()
        self.broadcaster.broadcast({'type': 'factory_reset'})
        return {'success': True}, 200

    def admin_delete_message(self, admin_id, message_id):
        """
        管理员强制删除任意消息（不受归属/时限约束）。

        参数 admin_id   —— is_admin 校验 → 403。
        参数 message_id —— 必填 → 空 400；get_message_by_id 查不到 → 404
        （且隐含校验消息属于本房间，防跨房删）。

        与 recall_message 的区别：
          本方法 = 管理动作，可删他人消息/系统消息/超时消息，
                    操作者身份写入审计；
          recall  = 用户自助，仅自己的、时限内的非系统消息。

        流程：查消息存在 → db.delete_message(message_id)（DELETE 触发
        message_audit 触发器留痕）→ 广播 message_deleted → 所有客户端
        按 message_id 移除对应 DOM。返回 ({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not message_id:
            return {'error': '消息ID不能为空'}, 400
        msg = self.db.get_message_by_id(message_id)
        if not msg:
            return {'error': '消息不存在'}, 404
        self.db.delete_message(message_id)
        self.broadcaster.broadcast({'type': 'message_deleted', 'message_id': message_id})
        return {'success': True}, 200

    # =======================================================================
    # 撤回（普通用户自助）
    # =======================================================================
    def recall_message(self, user_id, message_id):
        """
        用户撤回自己的一条消息（非管理接口，无需 is_admin）。

        参数 user_id    —— 发送者会话 ID（必须在 online_users 中）。
        参数 message_id —— 目标消息 ID。

        校验链（顺序有意：先验会话、再验消息、最后验权限与时限）：
          1. 参数完整                       → 400
          2. user_id 在 online_users        → 403（不在=会话过期，拿锁查）
          3. get_message_by_id 存在          → 404（且属本房间）
          4. msg['user_id'] == user_id      → 403（只能撤自己的）
          5. msg['type'] != 'system'        → 403（系统消息不可撤）
          6. now - msg.timestamp ≤ recall_time_limit → 400（超时；
             recall_time_limit=0 时 elapsed>0 恒成立 = 永远禁止撤回）

        成功：db.delete_message（触发器审计留痕）→ 广播 message_recalled
        （带 sender 供前端在对方气泡上显示「消息已撤回」占位）。
        返回：({'success': True}, 200) 或 (error, 4xx)。
        """
        if not user_id or not message_id:
            return {'error': '参数不完整'}, 400
        with self.lock:
            user = self.online_users.get(user_id)
            if user is None:
                return {'error': '您不在房间中'}, 403
        msg = self.db.get_message_by_id(message_id)
        if not msg:
            return {'error': '消息不存在'}, 404
        if msg['user_id'] != user_id:
            return {'error': '只能撤回自己的消息'}, 403
        if msg['type'] == 'system':
            return {'error': '系统消息不能撤回'}, 403
        elapsed = time.time() - (msg['timestamp'] or 0)
        if elapsed > self.recall_time_limit:
            return {'error': f'已超过{self.recall_time_limit}秒撤回时限'}, 400
        self.db.delete_message(message_id)
        self.broadcaster.broadcast({'type': 'message_recalled', 'message_id': message_id, 'sender': msg['sender']})
        return {'success': True}, 200

    def set_recall_time_limit(self, admin_id, limit):
        """
        设置撤回时限秒数（0 = 彻底禁止撤回）。

        参数 admin_id —— is_admin 校验 → 403。
        参数 limit    —— 数字且 ≥0，否则 400。

        写 self.recall_time_limit + _save_state() 持久化 +
        广播 recall_time_limit → 前端实时刷新「撤回」按钮可用性
        （超过时限的旧消息按钮置灰）。返回 ({'success': True, 'limit': int}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not isinstance(limit, (int, float)) or limit < 0:
            return {'error': '无效的时间限制'}, 400
        self.recall_time_limit = int(limit)
        self._save_state()
        self.broadcaster.broadcast({'type': 'recall_time_limit', 'limit': self.recall_time_limit})
        return {'success': True, 'limit': self.recall_time_limit}, 200

    # =======================================================================
    # 用户管理（禁言 / 踢出 / 黑名单）—— 均需在线管理员身份
    # =======================================================================
    # 共同点：第一步 is_admin(admin_id) → 403；
    # 涉及 target 的操作都要先在 online_users（持锁）里找到目标 → 404；
    # 状态落库到 profiles.muted_until 或 blacklist 表，内存只存活跃会话。
    # =======================================================================
    def mute_user(self, admin_id, target_id, duration):
        """
        对在线用户禁言 duration 秒。

        参数 admin_id —— is_admin 校验 → 403。
        参数 target_id —— 目标会话 ID，必须在 online_users → 不在 404
        （只能禁言当前在线的会话；离线用户的设备下次 join 时
         muted_until 若仍 >now 会继续生效，因档案按 device_id 存）。
        参数 duration —— 秒数，max(1, int(...))，缺省/非法按 60 处理，
        下限 1 防止 0 秒「禁言即解除」的无意义操作。

        实现：
          muted_until = now + duration → db.set_muted_until(device_id, ...)
          写 profiles.muted_until（按 device_id 而非 user_id——
          换昵称/重进房间禁言仍然生效，见 schema 表设计）。
        广播 user_muted（nickname/muted_until/duration）→
        目标客户端倒计时禁用输入框，管理端刷新名单。
        返回：({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if not target:
                return {'error': '用户不在线'}, 404
        muted_until = time.time() + max(1, int(duration or 60))
        self.db.set_muted_until(target['device_id'], muted_until)
        self.broadcaster.broadcast({'type': 'user_muted', 'nickname': target['nickname'],
                                    'muted_until': muted_until, 'duration': int(duration or 60)})
        return {'success': True}, 200

    def unmute_user(self, admin_id, target_id):
        """
        解除在线用户的禁言（muted_until 置 0）。

        参数 admin_id  —— is_admin 校验 → 403。
        参数 target_id —— 必须在 online_users → 不在 404。

        与 mute 对称：同样按 device_id 写 profiles；广播 user_unmuted
        让目标客户端立即恢复输入。返回 ({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if not target:
                return {'error': '用户不在线'}, 404
        self.db.set_muted_until(target['device_id'], 0)
        self.broadcaster.broadcast({'type': 'user_unmuted', 'nickname': target['nickname']})
        return {'success': True}, 200

    def get_muted_users(self, admin_id):
        """
        禁言名单（供管理面板表格展示）。

        参数 admin_id —— is_admin 校验 → 403。

        数据来源两层拼装：
          1. db.get_muted_users() —— SELECT profiles 中 muted_until > now
             的行（DB 层过滤，离线但仍在禁言期内的设备也列出）；
          2. 内存增强字段：
             remaining      —— max(0, muted_until - now) 剩余秒数（前端倒计时）
             user_id/online —— 遍历 online_users 按 device_id 匹配，
                               匹配到则填当前会话 ID 并标记在线，
                               否则 None/False（仅档案禁言、会话已离线）。
        返回：({'users': [...]}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        raw = self.db.get_muted_users()
        now = time.time()
        for u in raw:
            u['remaining'] = max(0, int(u['muted_until'] - now))
            u['user_id'] = None
            u['online'] = False
            for uid, info in self.online_users.items():
                if info.get('device_id') == u['device_id']:
                    u['user_id'] = uid
                    u['online'] = True
                    break
        return {'users': raw}, 200

    def kick_user(self, admin_id, target_id):
        """
        踢出在线用户并将其设备加入黑名单（两步：移会话 + 拉黑）。

        参数 admin_id  —— is_admin 校验 → 403。
        参数 target_id —— 必须在 online_users → 不在 404；
        目标若是管理员（target['is_admin']=True）→ 400
        （防「管理员踢管理员」死锁/误操作；只能从登录入口撤权）。

        原子性说明：会话移除在 self.lock 内完成（del online_users[id]），
        黑名单写库在锁外（避免持锁做 IO）；两步非事务——若 add_blacklist
        失败，用户已被移出会话但未拉黑，可重新 join（概率极低，课设可接受）。

        广播两条：
          user_kicked —— 被踢客户端收到后强制回登录页/房间列表；
          user_list   —— 所有客户端刷新侧边栏在线列表。
        返回：({'success': True}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if not target:
                return {'error': '用户不在线'}, 404
            if target['is_admin']:
                return {'error': '不能踢出管理员'}, 400
            del self.online_users[target_id]
        self.db.add_blacklist(target['device_id'], target['nickname'], target.get('ip', ''))
        self.broadcaster.broadcast({'type': 'user_kicked', 'nickname': target['nickname']})
        self.broadcaster.broadcast(self.user_list_event())
        return {'success': True}, 200

    def online_list(self, admin_id):
        """
        在线用户列表（管理面板「在线成员」表格数据源）。

        参数 admin_id —— is_admin 校验 → 403。

        每条记录字段：
          user_id        会话 ID（前端操作 mute/kick 时回传）
          nickname/avatar/color  会话内快照
          is_admin       是否在线管理员
          ip             加入时记录的来源 IP（识别同机多开/定位捣乱者）
          muted_remaining 禁言剩余秒——查 profiles.muted_until 动态算
                          （不直接信任会话快照，以档案为准）

        持锁遍历 online_users 构造列表；get_profile 是锁内 DB 调用——
        在线规模（单房间几十人）下可接受；更高并发应先拷贝再查库。
        返回：({'users': [...]}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        with self.lock:
            users = []
            for uid, u in self.online_users.items():
                profile = self.db.get_profile(u.get('device_id', ''))
                muted_until = profile['muted_until'] if profile else 0
                muted_remaining = max(0, int(muted_until - time.time())) if muted_until > time.time() else 0
                users.append({
                    'user_id': uid, 'nickname': u['nickname'], 'avatar': u['avatar'],
                    'color': u['color'], 'is_admin': u['is_admin'],
                    'ip': u.get('ip', ''), 'muted_remaining': muted_remaining
                })
        return {'users': users}, 200

    def blacklist_list(self, admin_id):
        """
        黑名单列表（本房间全部被拉黑设备，按拉黑时间倒序）。

        参数 admin_id —— is_admin 校验 → 403。
        直接透传 db.get_blacklist()（SELECT blacklist WHERE room_id=...
        ORDER BY created_at DESC）。返回：({'items': [...]}, 200)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        return {'items': self.db.get_blacklist()}, 200

    def blacklist_remove(self, admin_id, device_id):
        """
        把某设备移出黑名单（允许其重新 join）。

        参数 admin_id  —— is_admin 校验 → 403。
        参数 device_id —— 必填 → 空 400；
        db.remove_blacklist 返回 False（行不存在）→ 404
        （区分「操作成功」与「本来就不在」，前端给准确提示）。
        返回：({'success': True}, 200) 或 (error, 4xx)。
        """
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not device_id:
            return {'error': 'device_id 不能为空'}, 400
        if not self.db.remove_blacklist(device_id):
            return {'error': '该设备不在黑名单中'}, 404
        return {'success': True}, 200


# ============================================================================
# 房间管理器（ChatRoom 实例的线程安全懒加载缓存）
# ============================================================================
class RoomManager:
    """
    room_id → ChatRoom 实例的全局缓存单例（模块底部 room_manager）。

    为什么需要缓存：
      ChatRoom 持有 MySQL 连接、Broadcaster、online_users 在线表——
      这些是「房间进程内生命周期」的资源，不能每个请求重建。
      首次访问某房间时懒加载构造，之后所有请求复用同一实例。

    线程安全：
      _lock 保护 _rooms 字典的并发读写（get_or_create 可能被多个
      请求线程同时触发构造）。

    get_or_create 流程（持锁双检）：
      1. 命中缓存 → 直接返回。
      2. 未命中 → db_manager.get_room(room_id) 查主库：
         房间不存在 → 返回 None（路由层转 404）。
      3. 存在 → 创建上传目录 → new Database(room_id)（新 MySQL 连接）
         → new Broadcaster() → new ChatRoom(...) → 存入 _rooms → 返回。

    get_room_instance：
      只查缓存不触发构造——给「删房时摘实例」「toggle 同步内存」用，
      避免为了操作一个可能不存在的房间反而把它构造出来。
    """

    def __init__(self):
        """初始化锁与空缓存字典。"""
        self._lock = threading.Lock()
        self._rooms = {}   # room_id -> ChatRoom

    def get_or_create(self, room_id):
        """
        获取（或懒加载构造）指定房间的 ChatRoom 实例。

        返回：ChatRoom（成功）/ None（主库中房间不存在）。
        持 self._lock 完成「查缓存 → 查库 → 构造 → 塞缓存」全程，
        防止两个线程同时为同一 room_id 构造出两个实例
        （双实例会导致 online_users/broadcaster/连接分裂）。
        """
        with self._lock:
            if room_id in self._rooms:
                return self._rooms[room_id]
            room_info = db_manager.get_room(room_id)
            if not room_info:
                return None
            uploads_dir = room_uploads_dir(room_id)
            os.makedirs(uploads_dir, exist_ok=True)
            _db = Database(room_id)
            _broadcaster = Broadcaster()
            _room = ChatRoom(_db, _broadcaster, uploads_dir, room_id, db_manager)
            self._rooms[room_id] = _room
            return _room

    def get_room_instance(self, room_id):
        """
        只取已缓存的实例，未缓存返回 None（不触发构造）。
        调用场景：
          - api_delete_room：删房前摘除实例并 close 其 DB 连接；
          - api_toggle_room：开关房后同步内存实例的 room_open 字段。
        """
        with self._lock:
            return self._rooms.get(room_id)


# ============================================================================
# Flask 应用初始化与模块级单例
# ============================================================================
app = Flask(__name__)

# session 签名密钥：每次启动随机生成。
# 含义：重启后所有已发放的 session cookie 失效，用户需重新登录——
# 课程设计环境可接受；生产环境应改为固定密钥或从环境变量读取。
app.secret_key = os.urandom(24)

# SameSite=Lax：跨站 POST 不带 cookie，提供基础 CSRF 防护
# （本应用管理接口均为 JSON POST，配合 Lax 已能挡住常见跨站表单伪造）。
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# 确保运行时目录存在（幂等，已存在则不报错）
os.makedirs(UPLOAD_ROOT, exist_ok=True)     # 旧版全局上传目录
os.makedirs(QR_DIR, exist_ok=True)          # 二维码输出目录
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)  # 静态资源根

# ---------------------------------------------------------------------------
# 模块级单例（导入 app.py 即完成初始化，__main__ 与 gunicorn/测试客户端共用）
# ---------------------------------------------------------------------------
ensure_mysql_schema()           # 幂等建库/建表/视图/触发器/存储过程（失败则导入中止，fail-fast）
db_manager = DatabaseManager()  # 主库单例：rooms/config 的唯一连接
room_manager = RoomManager()    # 房间实例缓存单例：room_id → ChatRoom


def sse_data(event):
    """
    把事件 dict 格式化为一条标准 SSE data 帧。

    返回格式：'data: {json}\n\n'
      data:  SSE 事件字段前缀
      json   ensure_ascii=False，中文/emoji 不转义
      \n\n   SSE 帧分隔符（两个换行），浏览器 EventSource 据此切帧

    与 stream() 里手写的 f'data: {data}\n\n' 等价——本函数供
    首包 history/room_name 等结构化事件复用。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ============================================================================
# 登录 / 会话（会话层鉴权入口）
# ============================================================================
@app.route('/')
def login_page():
    """
    入口页（登录页）。

    逻辑：
      session 已有 role（user 或 admin）→ 说明登录过，直接 redirect('/rooms')；
      否则渲染 login.html，让用户选「用户登录」或「管理员登录+密码」。
    """
    if session.get('role'):
        return redirect('/rooms')
    return render_template('login.html')


@app.route('/api/entry-qr')
def api_entry_qr():
    """
    实时生成「入口首页」二维码图片（登录页右下角手机扫码入口）。

    每次请求都重新生成（而非读缓存）——局域网 IP 可能变化
    （换 Wi-Fi/热点），实时生成保证二维码始终指向当前可达地址。
    返回 PNG 文件流，前端 <img src="/api/entry-qr"> 直接引用。
    """
    ip = get_local_ip()
    url = f'http://{ip}:{BASE_PORT}/'
    qr_path = os.path.join(QR_DIR, 'entry.png')
    generate_qr(url, qr_path)
    return send_from_directory(QR_DIR, 'entry.png')


@app.route('/login', methods=['POST'])
def api_login():
    """
    角色登录（会话层鉴权的起点）。

    请求体 JSON：
      普通用户：{"role": "user"}
      管理员  ：{"role": "admin", "password": "..."}

    逻辑：
      role=admin → 读 config 表 admin_password 比对；不匹配 403，
                   匹配则 session['role']='admin'。
      role=user  （或其他默认值）→ 直接 session['role']='user'，免密。

    返回：
      成功 {success: True, redirect: '/rooms'} + 200
      管理员密码错 {error: '管理员密码错误'} + 403

    安全说明：密码明文比对（课程设计简化）；生产环境应哈希存储+恒定时间比较。
    """
    data = request.get_json(silent=True) or {}
    role = data.get('role', 'user')
    if role == 'admin':
        pw = data.get('password', '')
        if pw != db_manager.get_config('admin_password', 'ADMIN'):
            return jsonify({'error': '管理员密码错误'}), 403
        session['role'] = 'admin'
        return jsonify({'success': True, 'redirect': '/rooms'})
    session['role'] = 'user'
    return jsonify({'success': True, 'redirect': '/rooms'})


@app.route('/logout', methods=['POST'])
def api_logout():
    """清除 session（角色丢失），返回登录页地址；前端据此 location 跳转。"""
    session.clear()
    return jsonify({'success': True, 'redirect': '/'})


# ============================================================================
# 房间列表页（按 session 角色分流模板）
# ============================================================================
@app.route('/rooms')
def rooms_list():
    """
    房间列表页——登录后着陆页，按角色渲染不同模板。

    未登录（无 session.role）→ redirect('/') 回登录页。
    admin → rooms.html   ：额外展示 创建/删除/开关/二维码 等管理操作。
    user  → rooms_user.html：只展示开放房间的进入入口（关闭房间灰显/隐藏）。

    模板变量：
      rooms —— db_manager.list_rooms() 全部房间行（dict 列表）
      ip    —— 本机局域网 IP，用于拼二维码/进入链接的主机部分
    """
    role = session.get('role')
    if not role:
        return redirect('/')
    rooms = db_manager.list_rooms()
    ip = get_local_ip()
    if role == 'admin':
        return render_template('rooms.html', rooms=rooms, ip=ip)
    return render_template('rooms_user.html', rooms=rooms, ip=ip)


def _require_admin():
    """
    全局管理 API 的统一鉴权入口（会话层）。

    返回：
      None           —— 通过（session.role == 'admin'），调用方继续业务。
      (json, 403)    —— 拒绝，调用方直接 return。

    用法（所有 /api/rooms/create|delete|toggle、/api/admin/* 等）：
        err = _require_admin()
        if err:
            return err

    与房间内 ChatRoom.is_admin() 的分工：
      本函数只验「浏览器会话是否 admin」；
      房间内管理 API 还要额外验「该 user_id 是否以管理员身份在线」——两层缺一不可。
    """
    if session.get('role') != 'admin':
        return jsonify({'error': '需要管理员登录'}), 403
    return None


# ============================================================================
# 聊天页面与全局房间管理 API
# ============================================================================
@app.route('/room/<room_id>/')
def room_page(room_id):
    """
    渲染聊天页 index.html（所有角色共用同一模板，按 is_admin 切换管理面板）。

    流程：
      1. 主库查房间 → 不存在返回 404 文本。
      2. room_manager.get_or_create 构造/取缓存实例 → 失败 500。
      3. ?admin=1 时校验 session.role=='admin'，否则 redirect('/')——
         这是「进入管理版聊天页」的会话层门槛；进房后 ChatRoom.join
         还会再验一次（is_admin_user=True 路径），双层防护。
      4. 渲染模板，注入变量：
         is_admin     是否管理页（前端决定是否加载管理面板 JS）
         room_name    房间名（标题）
         has_password 是否有房密码（决定加入时是否弹密码框）
         room_id      当前房间标识
    """
    room_info = db_manager.get_room(room_id)
    if not room_info:
        return '房间不存在', 404
    room_instance = room_manager.get_or_create(room_id)
    if not room_instance:
        return '房间初始化失败', 500
    is_admin_page = request.args.get('admin') == '1'
    if is_admin_page and session.get('role') != 'admin':
        return redirect('/')
    return render_template('index.html', is_admin=is_admin_page,
                           room_name=room_instance.room_name,
                           has_password=room_instance.room_password is not None,
                           room_id=room_id)


@app.route('/admin')
def admin_redirect():
    """旧 /admin 地址兼容：渲染 admin.html（内含 meta refresh 跳 /）。"""
    return render_template('admin.html')


# ---------------------------------------------------------------------------
# 房间管理 API（全局路由；创建/删除/开关需 admin 会话）
# ---------------------------------------------------------------------------
@app.route('/api/rooms')
def api_list_rooms():
    """
    房间列表 JSON（公开只读，无需登录）。
    返回 {'rooms': [room_row, ...]}——rooms 表全部列，供外部/前端轮询。
    """
    return jsonify({'rooms': db_manager.list_rooms()})


@app.route('/api/rooms/create', methods=['POST'])
def api_create_room():
    """
    创建房间（需 admin 会话 → _require_admin）。

    请求体 JSON：{"name": "房间名", "password": "可选进房密码"}

    room_id 生成策略：f'room_{毫秒时间戳}_{os.urandom(3).hex()}'
      —— 毫秒时间戳保证大致有序，3 字节随机 hex（6 字符）避免
         同一毫秒并发创建撞主键（纯时间戳在并发下会冲突）。

    成功后的三步：
      1. db_manager.create_room 写 rooms 表（其余列走 DEFAULT）。
      2. room_manager.get_or_create 预热 ChatRoom 实例（预建连接/目录）。
      3. generate_qr 生成房间二维码 PNG 到 static/qrcode/<room_id>.png。

    返回：{'success': True, 'room_id': '<新房间id>'}
    """
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '新房间').strip()
    pw = data.get('password')
    room_id = f'room_{int(time.time() * 1000)}_{os.urandom(3).hex()}'
    db_manager.create_room(room_id, name, pw)
    room_manager.get_or_create(room_id)
    ip = get_local_ip()
    generate_qr(f'http://{ip}:{BASE_PORT}/room/{room_id}/', os.path.join(QR_DIR, f'{room_id}.png'))
    return jsonify({'success': True, 'room_id': room_id})


@app.route('/api/rooms/delete', methods=['POST'])
def api_delete_room():
    """
    删除房间（需 admin 会话）。

    删除顺序很关键（Windows 文件锁 + SSE 在线客户端）：
      1. 若实例已缓存：
         a. 先 broadcast room_closed —— 在线客户端收到后断开 SSE、
            停止对该实例的后续调用（避免对着已销毁对象操作）。
         b. 从 room_manager._rooms 摘除缓存。
         c. inst.db.close() 关闭该房间的 MySQL 连接——必须在删目录/文件前，
            否则 Windows 下连接占用句柄会导致 rmtree 失败。
      2. db_manager.delete_room(room_id)：
         显式删 message_audit（无外键）→ 删 rooms 行（外键 CASCADE 级联
         清 profiles/messages/blacklist/favorites）→ rmtree uploads_<id>/。
      3. 删除房间二维码 PNG。
      4. 若删完后一间不剩 → 自动重建默认房间 room_default
         （保证系统始终有可用房间，避免空状态无法进入）。

    请求体：{"room_id": "..."}
    """
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    if not room_id:
        return jsonify({'error': '缺少 room_id'}), 400
    # 先关闭房间实例的 DB 连接，再删除文件（Windows 下文件锁）
    inst = room_manager.get_room_instance(room_id)
    if inst:
        # 先通知在线客户端房间已删，避免他们继续对着已销毁的实例操作
        inst.broadcaster.broadcast({'type': 'room_closed'})
        with room_manager._lock:
            room_manager._rooms.pop(room_id, None)
        try:
            inst.db.close()
        except Exception:
            pass
    db_manager.delete_room(room_id)
    qr = os.path.join(QR_DIR, f'{room_id}.png')
    if os.path.exists(qr):
        os.remove(qr)
    # 一个房间都不剩时，重建默认房间，保证系统始终可用
    if not db_manager.list_rooms():
        db_manager.create_room('room_default', '在线匿名聊天室')
        room_manager.get_or_create('room_default')
        ip = get_local_ip()
        generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/', os.path.join(QR_DIR, 'room_default.png'))
    return jsonify({'success': True})


@app.route('/api/rooms/toggle', methods=['POST'])
def api_toggle_room():
    """
    开关房间（需 admin 会话）：主库 is_open 取反，并同步内存实例。

    步骤：
      1. 校验房间存在（不存在 404）。
      2. new_open = not 当前 is_open。
      3. db_manager.update_room 写库（持久化）。
      4. 若实例已缓存 → 同步 inst.room_open = new_open
         （热路径 join/send 读内存，不同步会导致库与内存不一致）。
         注意：此路由不广播——房间列表页靠轮询 /api/rooms/status 刷新；
         房间内的 room_closed 广播由 set_room_open（房间内管理路径）负责。

    请求体：{"room_id": "..."}  返回：{'success', 'is_open': 新状态}
    """
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    if not room_id:
        return jsonify({'error': '缺少 room_id'}), 400
    info = db_manager.get_room(room_id)
    if not info:
        return jsonify({'error': '房间不存在'}), 404
    new_open = not info['is_open']
    db_manager.update_room(room_id, is_open=int(new_open))
    # 同步到内存中的 ChatRoom 实例
    inst = room_manager.get_room_instance(room_id)
    if inst:
        inst.room_open = new_open
    return jsonify({'success': True, 'is_open': new_open})


@app.route('/api/rooms/status')
def api_rooms_status():
    """
    房间列表精简状态（房间卡片 UI 轮询用）。
    只返回 room_id/room_name/is_open/port 四个字段——比全量 /api/rooms
    体积小，适合 2s 间隔轮询刷新开关状态。
    """
    rooms = db_manager.list_rooms()
    return jsonify({'rooms': [{'room_id': r['room_id'], 'room_name': r['room_name'],
                               'is_open': r['is_open'], 'port': BASE_PORT} for r in rooms]})


@app.route('/api/rooms/qr/<room_id>')
def api_room_qr(room_id):
    """
    房间二维码 PNG。
    文件已存在 → 直接返回；不存在（被清理/IP 变过）→ 查房间存在后
    按当前 IP:PORT 现场生成再返回；房间不存在 → 404。
    """
    qr_path = os.path.join(QR_DIR, f'{room_id}.png')
    if not os.path.exists(qr_path):
        info = db_manager.get_room(room_id)
        if not info:
            return '', 404
        ip = get_local_ip()
        generate_qr(f'http://{ip}:{BASE_PORT}/room/{room_id}/', qr_path)
    return send_from_directory(QR_DIR, f'{room_id}.png')


@app.route('/api/admin/change_password', methods=['POST'])
def api_change_admin_password():
    """
    修改管理员登录密码（需 admin 会话 + 正确旧密码双重校验）。

    请求体 JSON：{"old_password": "...", "new_password": "..."}
    校验顺序：
      1. _require_admin —— 会话必须是 admin。
      2. new_password 非空 → 400。
      3. old_password 与 config 表当前值比对 → 不符 403
         （防止已登录的低权限会话被人趁机改密码）。
      4. set_config('admin_password', new_pw) 写库（upsert）。
    返回：{'success': True}
    """
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    old_pw = data.get('old_password', '')
    new_pw = (data.get('new_password') or '').strip()
    if not new_pw:
        return jsonify({'error': '新密码不能为空'}), 400
    if old_pw != db_manager.get_config('admin_password', 'ADMIN'):
        return jsonify({'error': '当前密码错误'}), 403
    db_manager.set_config('admin_password', new_pw)
    return jsonify({'success': True})


@app.route('/api/admin/global_reset', methods=['POST'])
def api_global_reset():
    """
    全局恢复出厂（需 admin 会话 + 当前管理员密码确认——破坏性操作双重门槛）。

    请求体 JSON：{"password": "<当前管理员密码>"}

    执行步骤（顺序经过验证，不可随意调换）：
      1. 会话校验 _require_admin；密码与 config 比对 → 不符 403。
      2. 遍历 room_manager._rooms：close 每个房间的 MySQL 连接，
         然后 clear 缓存——先释放连接，否则后续删文件/删表可能被句柄占用卡住。
      3. 清理磁盘：
         a. 删除旧 SQLite 遗留 room_*.db（迁移清理，兼容历史版本）。
         b. rmtree 所有 uploads_* 目录。
         c. 删除 static/qrcode/*.png 全部二维码。
      4. 重置数据库：
         a. DELETE FROM message_audit（无外键，必须显式清）。
         b. DELETE FROM rooms（外键 CASCADE 级联清 profiles/messages/
            blacklist/favorites 所有业务表）。
         c. INSERT room_default 一行（全默认配置：开放、无密码、200条、300秒）。
      5. set_config('admin_password', 'ADMIN') 密码回默认。
      6. room_manager.get_or_create('room_default') 重建实例 + 生成二维码。

    返回：{'success': True}
    """
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if data.get('password') != db_manager.get_config('admin_password', 'ADMIN'):
        return jsonify({'error': '当前密码错误'}), 403
    # 关闭所有房间实例的 DB 连接
    with room_manager._lock:
        for inst in room_manager._rooms.values():
            try:
                inst.db.close()
            except Exception:
                pass
        room_manager._rooms.clear()
    # 删除旧 SQLite 房间库文件（迁移遗留）和上传目录
    for f in glob.glob(os.path.join(BASE_DIR, 'room_*.db')):
        try:
            os.remove(f)
        except Exception:
            pass
    for d in glob.glob(os.path.join(BASE_DIR, 'uploads_*')):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    # 删除所有房间二维码
    for f in glob.glob(os.path.join(QR_DIR, '*.png')):
        try:
            os.remove(f)
        except Exception:
            pass
    # 重置数据：业务表靠外键 CASCADE 清空，审计表显式清空，再重建默认房间
    db_manager._execute('DELETE FROM message_audit')
    db_manager._execute('DELETE FROM rooms')
    db_manager._execute(
        'INSERT INTO rooms(room_id, room_name, is_open, password, file_limit_mb, max_history, recall_time_limit, created_at) '
        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
        ('room_default', '在线匿名聊天室', 1, None, 0, 200, 300, time.time()))
    # 重置管理员密码为默认
    db_manager.set_config('admin_password', 'ADMIN')
    # 重建默认房间
    room_manager.get_or_create('room_default')
    ip = get_local_ip()
    generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/', os.path.join(QR_DIR, 'room_default.png'))
    return jsonify({'success': True})


# ============================================================================
# 房间 Blueprint（url_prefix=/room/<room_id>）
# ============================================================================
# 所有 /room/<room_id>/* 路由挂载到此蓝图。
# before_request 钩子统一把房间实例加载进 g.room，视图函数直接用 g.room，
# 不用每个路由再手动 get_or_create——消除「忘记取实例」的错误面。
# ============================================================================
room_bp = Blueprint('room', __name__, url_prefix='/room/<room_id>')


def _room_context(room_id):
    """
    Blueprint before_request 钩子：为每个房间路由准备上下文。

    做三件事：
      1. g.room_id = room_id —— 供视图（如 upload 返回 URL）使用。
      2. g.room = room_manager.get_or_create(room_id) —— 取/构造 ChatRoom 实例。
         实例为 None（房间不存在）→ 直接返回 404 JSON，视图不再执行。
      3. 从 request.view_args 弹出 'room_id' —— 因为蓝图 url_prefix 里
         已经消费了 <room_id>，Flask 会把它同时放进 view_args；
         若不弹出，视图函数签名若不含 room_id 参数 Flask 会报
         「unexpected keyword argument」。弹出后视图可零参数定义。

    返回：None（通过，继续执行视图）或 (json, 404)。
    """
    g.room_id = room_id
    g.room = room_manager.get_or_create(room_id)
    if not g.room:
        return jsonify({'error': '房间不存在'}), 404
    if request.view_args and 'room_id' in request.view_args:
        request.view_args.pop('room_id')


# 注册钩子：view_args 在路由匹配后填充，此处取 room_id 传给 _room_context
room_bp.before_request(lambda: _room_context(request.view_args.get('room_id', '') if request.view_args else ''))


def _sync_upload_cap(room):
    """
    按房间的 file_limit_mb 同步 Flask 全局 MAX_CONTENT_LENGTH。

    Flask 用 MAX_CONTENT_LENGTH 在请求体读取前就拒绝超大上传（413），
    是第一道防线；upload_file 里的 stat 大小校验是第二道（更友好的错误消息）。

    参数 room.file_limit_mb：
      0        → 设 None（不限制）
      >0       → 设为 N MB + 1MB 缓冲（缓冲容纳 multipart 边界/头部开销，
                 避免「文件恰好等于限额」时被框架先拒）
    调用时机：房间内管理 API set_file_limit 成功后立即调用（热生效，
    无需重启）；进入管理页/构造实例时也应调用以对齐当前配置。
    """
    if room.file_limit_mb <= 0:
        app.config['MAX_CONTENT_LENGTH'] = None
    else:
        app.config['MAX_CONTENT_LENGTH'] = room.file_limit_mb * 1024 * 1024 + 1024 * 1024


# ============================================================================
# 房间用户 API（加入 / 离开 / 发消息 / 上传 / 查询 / 收藏）
# ============================================================================
# 这些路由全部经 before_request 拿到 g.room；不验会话（匿名可用），
# 但业务层 ChatRoom 会校验在线状态/房间开放/禁言/黑名单等。
# ============================================================================
@room_bp.route('/api/join', methods=['POST'])
def api_join():
    """
    加入房间——创建匿名会话。

    请求体 JSON：
      {"nickname": "...", "device_id": "...", "password": "可选",
       "is_admin": false}   // 以管理员身份加入时为 true

    鉴权（会话层）：
      is_admin=true 时必须 session['role']=='admin'，否则 403——
      防止普通用户在前端伪造 is_admin=true 绕过。

    业务校验委托给 ChatRoom.join（开放/密码/黑名单/昵称唯一/档案复用）。
    成功返回 {success, user_id, nickname, avatar, color, muted_remaining,
              file_limit_mb}——前端保存 user_id 供后续所有接口使用。
    """
    data = request.get_json(silent=True) or {}
    want_admin = bool(data.get('is_admin'))
    if want_admin and session.get('role') != 'admin':
        return jsonify({'error': '需要管理员登录'}), 403
    result, status = g.room.join(data.get('nickname'), data.get('device_id'),
                                  request.remote_addr, data.get('password'), want_admin)
    return jsonify(result), status


@room_bp.route('/api/leave', methods=['POST'])
def api_leave():
    """
    离开房间——从 online_users 移除会话。
    页面 unload 时前端用 navigator.sendBeacon 调用（不等 fetch，保证
    页面关闭时请求仍能发出）；sendBeacon 常见 content-type 非 JSON，
    故 get_json(force=True) 强制按 JSON 解析。user_id 不在时静默成功。
    """
    data = request.get_json(silent=True, force=True) or {}
    return jsonify(g.room.leave(data.get('user_id')))


@room_bp.route('/api/send', methods=['POST'])
def api_send():
    """
    发送聊天消息（text/image/file）。

    请求体 JSON：{"user_id", "content", "type", "file_name"?}
    委托 ChatRoom.send_message 完成 全链路校验（在线/开放/禁言/类型/
    敏感词/文件URL正则/磁盘存在性）→ 落库 → SSE 广播。
    返回 (json, 2xx/4xx) 由业务层决定。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.send_message(data.get('user_id'), data.get('content'),
                                          data.get('type', 'text'), data.get('file_name'))
    return jsonify(result), status


@room_bp.route('/api/upload_file', methods=['POST'])
def api_upload_file():
    """
    上传文件到当前房间的 uploads 目录（multipart/form-data，字段名 file）。

    处理流程：
      1. 校验文件存在且文件名非空 → 400。
      2. 流式读大小（seek 到尾读 tell，再 seek 回头）——先量大小
         再决定是否落盘，避免「先存后删」浪费 IO。
      3. 房间 file_limit_mb > 0 且超限 → 400（第二道防线；
         第一道是 MAX_CONTENT_LENGTH 框架层 413，见 _sync_upload_cap）。
      4. 扩展名清洗：只保留 [A-Za-z0-9]，截断到 ≤10 字符（含点）——
         防路径穿越与异常扩展名。
      5. 重命名为 f'{秒级时间戳}_{4字节hex}{ext}' 落盘——同秒多文件不撞名，
         且不暴露原始文件名（原始名在返回值 filename 里展示）。
      6. 返回 JSON：
         {'success', 'url': '/room/<id>/uploads/<新文件名>',
          'filename': '<原始名>', 'size': 字节数}
         前端拿 url 再调 /api/send（type=image/file）把消息入库。

    上限说明：Flask 全局 MAX_CONTENT_LENGTH 是第一道闸（框架层提前拒），
    这里的 stat 是第二道（返回友好中文错误）。
    """
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': '文件名为空'}), 400
    # 先读流大小（不落盘）
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if g.room.file_limit_mb > 0 and size > g.room.file_limit_mb * 1024 * 1024:
        return jsonify({'error': f'文件大小不能超过 {g.room.file_limit_mb}MB'}), 400
    # 扩展名只保留安全字符，长度 ≤10（含点）
    raw_ext = os.path.splitext(file.filename)[1].lower()
    ext = ('.' + re.sub(r'[^A-Za-z0-9]', '', raw_ext))[:11] if raw_ext else ''
    filename = f"{int(time.time())}_{os.urandom(4).hex()}{ext}"
    file.save(os.path.join(g.room.uploads_dir, filename))
    return jsonify({'success': True, 'url': f'/room/{g.room_id}/uploads/{filename}',
                    'filename': file.filename, 'size': size})


@room_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """
    下载/预览房间文件。
    图片扩展名（IMAGE_EXTS）→ as_attachment=False，浏览器内联显示；
    其他 → as_attachment=True，触发下载。
    <path:filename> 允许文件名含斜杠（本应用生成的文件名不含，保留兼容）。
    实际根目录是 g.room.uploads_dir（send_from_directory 不允许 .. 逃逸）。
    """
    ext = os.path.splitext(filename)[1].lower()
    return send_from_directory(g.room.uploads_dir, filename, as_attachment=ext not in IMAGE_EXTS)


@room_bp.route('/api/messages')
def api_messages():
    """
    取房间最近 N 条消息（时间正序）。
    SSE 连上时首包也会推 history——本接口是「SSE 未就绪/断线重连」时的
    轮询兜底，两条路径读的是同一份 get_recent_messages()。
    """
    return jsonify(g.room.db.get_recent_messages())


@room_bp.route('/api/get_profile')
def api_get_profile(device_id=None):
    """
    按 device_id 取用户档案（GET /api/get_profile?device_id=...）。
    用途：登录页/加入页自动回填上次用过的昵称（读 profiles.nickname），
    提升重复进入体验。档案不存在时返回 {'profile': None}。
    """
    device_id = request.args.get('device_id')
    profile = g.room.db.get_profile(device_id) if device_id else None
    return jsonify({'profile': profile})


@room_bp.route('/api/room_status')
def api_room_status():
    """
    房间公开状态（加入前前端拉取，决定是否弹密码框/禁用发送等）。
    返回字段：
      open             是否开放
      password         是否有密码（bool，不泄露密码本身）
      file_limit_mb    单文件上限
      max_history      消息保留条数
      recall_time_limit 撤回时限秒
    """
    return jsonify({'open': g.room.room_open, 'password': g.room.room_password is not None,
                    'file_limit_mb': g.room.file_limit_mb, 'max_history': g.room.max_history,
                    'recall_time_limit': g.room.recall_time_limit})


@room_bp.route('/api/query_messages')
def api_query_messages():
    """
    按昵称模糊搜索文本消息（管理端「消息查询」）。
    Query: ?nickname=可选——空则返回全部文本消息。
    返回 {'groups': [{sender, avatar, color, messages:[...]}, ...]}
    按发送者分组，便于前端折叠展示。委托 Database.query_messages。
    """
    nickname = request.args.get('nickname', '').strip()
    return jsonify({'groups': g.room.db.query_messages(nickname if nickname else None)})


@room_bp.route('/api/query_files')
def api_query_files():
    """
    按昵称模糊搜索图片/文件消息（管理端「文件查询」）。
    返回结构同 query_messages，但每条消息额外带 file_exists
    （磁盘文件是否仍存在），前端据此置灰失效下载。
    """
    nickname = request.args.get('nickname', '').strip()
    return jsonify({'groups': g.room.db.query_files(nickname if nickname else None, g.room.uploads_dir)})


@room_bp.route('/api/file_exists')
def api_file_exists():
    """
    检查某文件 URL 对应的磁盘文件是否仍存在（收藏夹下载前校验）。
    Query: ?url=/room/<id>/uploads/<filename>
    收藏是快照，原文件可能已被清理——下载前先问一下，避免 404 体验差。
    仅取 basename 后拼 g.room.uploads_dir，天然限定在本房间目录内。
    """
    url = request.args.get('url', '')
    filename = os.path.basename(url)
    exists = os.path.isfile(os.path.join(g.room.uploads_dir, filename))
    return jsonify({'exists': exists})


# ---------------------------------------------------------------------------
# 收藏夹 API
# ---------------------------------------------------------------------------
@room_bp.route('/api/favorites/add', methods=['POST'])
def api_favorites_add():
    """
    收藏一条消息（按 message_id 查原文 → 快照入 favorites 表）。

    请求体 JSON：{"user_id": "...", "message_id": "..."}
    流程：
      1. 参数完整性 → 400。
      2. get_message_by_id 校验消息存在（且属于本房间）→ 404。
      3. add_favorite 快照插入（INSERT IGNORE，重复收藏幂等静默成功）。
    返回：{'success': True} + 200。
    """
    data = request.get_json(silent=True) or {}
    user_id, msg_id = data.get('user_id'), data.get('message_id')
    if not user_id or not msg_id:
        return jsonify({'error': '参数不完整'}), 400
    msg = g.room.db.get_message_by_id(msg_id)
    if not msg:
        return jsonify({'error': '消息不存在'}), 404
    g.room.db.add_favorite(user_id, msg)
    return jsonify({'success': True}), 200


@room_bp.route('/api/favorites/list', methods=['GET'])
def api_favorites_list():
    """
    列出某用户在本房间的全部收藏（GET ?user_id=...，按收藏时间倒序）。
    user_id 必填 → 400。返回 {'items': [...]}。
    """
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': '缺少 user_id'}), 400
    return jsonify({'items': g.room.db.get_favorites(user_id)}), 200


@room_bp.route('/api/favorites/remove', methods=['POST'])
def api_favorites_remove():
    """
    删除一条收藏。
    请求体 JSON：{"user_id", "fav_id"}
    Database.remove_favorite 的 WHERE 同时带 id+user_id+room_id——
    防越权：即使伪造别人的 fav_id，属主不匹配也删不掉。
    成功 {'success': True}+200；不存在/无权 {'success': False}+404。
    """
    data = request.get_json(silent=True) or {}
    user_id, fav_id = data.get('user_id'), data.get('fav_id')
    if not user_id or not fav_id:
        return jsonify({'error': '参数不完整'}), 400
    ok = g.room.db.remove_favorite(user_id, fav_id)
    return jsonify({'success': ok}), 200 if ok else 404


# ============================================================================
# 房间内管理 API（需「房间内在线管理员」身份 —— 第二层鉴权）
# ============================================================================
# 统一模式：
#   路由层只做「取 JSON → 调 ChatRoom.xxx(user_id, ...) → 返回 (result, status)」；
#   真正的权限校验在 ChatRoom.xxx 内部第一步 is_admin(admin_id) 完成
#   （在线身份层，见模块 docstring 权限模型）——路由层不重复验，
#   避免两处校验逻辑漂移。
# user_id 来自前端 join 后保存的会话标识；admin_id/target_id 字段名
# 仅作可读性区分，语义上都传 user_id。
# ============================================================================
@room_bp.route('/api/admin/set_room_name', methods=['POST'])
def api_admin_set_room_name():
    """改房间名称。JSON: {user_id, name}。成功后广播 room_name 同步所有客户端标题。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_room_name(data.get('user_id'), data.get('name'))
    return jsonify(result), status


@room_bp.route('/api/admin/set_room_open', methods=['POST'])
def api_admin_set_room_open():
    """开/关房间。JSON: {user_id, open: bool}。关闭时广播 room_closed 让客户端断开 SSE。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_room_open(data.get('user_id'), data.get('open', True))
    return jsonify(result), status


@room_bp.route('/api/admin/set_password', methods=['POST'])
def api_admin_set_password():
    """设置/清除房间密码。JSON: {user_id, password}。空串=清除(None)。广播 password_changed。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_password(data.get('user_id'), data.get('password', ''))
    return jsonify(result), status


@room_bp.route('/api/admin/set_file_limit', methods=['POST'])
def api_admin_set_file_limit():
    """
    设置单文件大小上限 MB（0=不限）。
    JSON: {user_id, limit_mb}
    成功后调 _sync_upload_cap 同步 Flask 全局 MAX_CONTENT_LENGTH（热生效）；
    同时 ChatRoom.set_file_limit 内部广播 file_limit 让在线客户端即时更新提示。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_file_limit(data.get('user_id'), data.get('limit_mb'))
    if status == 200:
        _sync_upload_cap(g.room)
    return jsonify(result), status


@room_bp.route('/api/admin/set_max_history', methods=['POST'])
def api_admin_set_max_history():
    """
    设置消息保留条数（≥10）。
    JSON: {user_id, max_history}
    ChatRoom.set_max_history 内部：写库 + 更新内存/db.max_history +
    CALL sp_cleanup_old_messages 立即裁剪 + 广播 max_history。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_max_history(data.get('user_id'), data.get('max_history'))
    return jsonify(result), status


@room_bp.route('/api/admin/clear_history', methods=['POST'])
def api_admin_clear_history():
    """
    清空全部聊天记录（DELETE FROM messages；每行触发审计触发器留痕）。
    JSON: {user_id}。成功广播 history_cleared 让客户端清空 DOM。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.clear_history(data.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/factory_reset', methods=['POST'])
def api_admin_factory_reset():
    """
    房间级恢复出厂：清空该房间业务数据（消息/档案/黑名单/收藏）+
    rooms 配置回默认值，但不删房间本身。
    JSON: {user_id}。与全局 /api/global_reset（删所有房间）的区别：
    本接口保留房间行，只重置其内容与配置。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.factory_reset(data.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/delete_message', methods=['POST'])
def api_admin_delete_message():
    """
    管理员强制删除任意消息（不校验归属/时限）。
    JSON: {user_id, message_id}
    DELETE 触发审计触发器写 message_audit；成功广播 message_deleted
    让所有客户端移除对应 DOM。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.admin_delete_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status


@room_bp.route('/api/recall_message', methods=['POST'])
def api_recall_message():
    """
    用户撤回自己的消息（非管理接口，普通用户可用）。
    JSON: {user_id, message_id}
    ChatRoom.recall_message 校验：在线、只能撤自己的、非系统消息、
    在 recall_time_limit 秒内（0=禁止撤回）。成功广播 message_recalled。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.recall_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/mute_user', methods=['POST'])
def api_admin_mute_user():
    """
    禁言在线用户。
    JSON: {admin_id, target_id, duration(秒)}
    ChatRoom.mute_user：验 is_admin → 找到在线 target →
    写 profiles.muted_until = now+duration → 广播 user_muted。
    目标不在线 404（只能禁言当前在线会话）。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.mute_user(data.get('admin_id'), data.get('target_id'), data.get('duration'))
    return jsonify(result), status


@room_bp.route('/api/admin/unmute_user', methods=['POST'])
def api_admin_unmute_user():
    """解除在线用户禁言。JSON: {admin_id, target_id}。写 muted_until=0 广播 user_unmuted。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.unmute_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/muted_users', methods=['GET'])
def api_admin_muted_users():
    """
    禁言名单（GET ?user_id=...）。
    ChatRoom.get_muted_users 查 profiles 中 muted_until>now 的行，
    补充 remaining（剩余秒）、当前在线会话 user_id、online 标志。
    """
    result, status = g.room.get_muted_users(request.args.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/kick_user', methods=['POST'])
def api_admin_kick_user():
    """
    踢出用户并拉黑其设备。
    JSON: {admin_id, target_id}
    ChatRoom.kick_user：验 is_admin → 找到在线 target（管理员不可被踢）→
    从 online_users 移除 → add_blacklist 写黑名单 →
    广播 user_kicked + user_list。被踢设备下次 join 被黑名单拦住。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.kick_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/online_users', methods=['POST'])
def api_admin_online_users():
    """
    在线用户列表（管理面板）。
    JSON: {admin_id}
    返回每人 user_id/nickname/avatar/color/is_admin/ip/muted_remaining。
    ip 供管理员识别同局域网多账号；muted_remaining 供禁言倒计时显示。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.online_list(data.get('admin_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/blacklist', methods=['GET'])
def api_admin_blacklist():
    """黑名单列表（GET ?user_id=...）。返回 device_id/nickname/ip/created_at，按拉黑时间倒序。"""
    result, status = g.room.blacklist_list(request.args.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/remove_blacklist', methods=['POST'])
def api_admin_remove_blacklist():
    """把设备移出黑名单。JSON: {user_id, device_id}。成功重新允许该设备加入。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.blacklist_remove(data.get('user_id'), data.get('device_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/set_recall_time_limit', methods=['POST'])
def api_admin_set_recall_time_limit():
    """
    设置撤回时限秒数（0=禁止撤回）。
    JSON: {user_id, limit}
    写 rooms.recall_time_limit + 同步内存 + 广播 recall_time_limit
    让前端实时更新「可撤回」按钮的可用状态。
    """
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_recall_time_limit(data.get('user_id'), data.get('limit'))
    return jsonify(result), status


@room_bp.route('/api/admin/message_stats')
def api_admin_message_stats():
    """
    消息综合统计（GET ?user_id=...）。
    第二层鉴权：g.room.is_admin(user_id) 必须为在线管理员 → 403。
    通过后 CALL sp_get_message_stats(room_id)，返回：
      {total_messages, user_stats:[...], type_stats:[...]}
    三个结果集分别对应「总数 / 按用户 / 按类型+百分比」。
    """
    user_id = request.args.get('user_id')
    if not g.room.is_admin(user_id):
        return jsonify({'error': '无权限'}), 403
    return jsonify(g.room.db.sp_get_message_stats())


@room_bp.route('/api/admin/audit_log')
def api_admin_audit_log():
    """
    消息删除审计日志（GET ?user_id=...&limit=50）。
    第二层鉴权同上；通过后 CALL sp_get_audit_log(room_id, limit)，
    返回 {'logs': [...]}，按 audit_id 降序（最新删除在前）。
    """
    user_id = request.args.get('user_id')
    if not g.room.is_admin(user_id):
        return jsonify({'error': '无权限'}), 403
    limit = request.args.get('limit', 50, type=int)
    return jsonify({'logs': g.room.db.sp_get_audit_log(limit)})


# ============================================================================
# SSE 实时推送
# ============================================================================
@room_bp.route('/stream')
def stream():
    """
    Server-Sent Events 长连接端点（GET，浏览器 EventSource 调用）。

    协议流程（event_stream 生成器）：
      1. 注册：q = broadcaster.register() —— 拿到本连接专属队列。
      2. 首包（连接建立后立即下发，前端据此初始化界面）：
           history     最近 N 条消息（渲染历史）
           room_name   当前房间名（更新标题）
           user_list   当前在线用户/管理员列表（渲染侧边栏）
      3. 主循环：q.get(timeout=30)
           取到事件 → yield 'data: {json}\n\n' 推给浏览器；
           超时 Empty → yield ': ping\n\n'
             （SSE 注释行：以冒号开头，浏览器忽略内容，
               但能重置代理/浏览器的空闲超时，防止长连接被掐）。
      4. 清理：finally unregister(q) —— 无论正常断开/客户端关闭/异常，
         都摘除队列，防止 _clients 泄漏。

    线程模型：本生成器运行在当前 Flask 请求线程中，阻塞在 q.get()；
    业务线程（其他请求）通过 broadcaster.broadcast() 投递事件。
    响应头 Content-Type: text/event-stream，Flask Response + stream_with_context
    保证请求上下文在生成器生命周期内有效（g.room 可用）。
    """
    def event_stream():
        """
        SSE 生成器主体（在 stream() 内闭包定义，运行于当前请求线程）。

        生命周期：
          register → 首包三连(history/room_name/user_list) → q.get 主循环
          → finally unregister（客户端断开/异常/服务关闭均保证摘除队列）。
        心跳：30s 空闲 yield ': ping' 注释行保活（见 stream() 说明）。
        """
        q = g.room.broadcaster.register()
        try:
            # 首包三连：历史消息 → 房间名 → 在线列表
            yield sse_data({'type': 'history', 'messages': g.room.db.get_recent_messages()})
            yield sse_data({'type': 'room_name', 'name': g.room.room_name})
            yield sse_data(g.room.user_list_event())
            # 主循环：取事件推送 / 空闲发心跳注释行
            while True:
                try:
                    data = q.get(timeout=30)
                except queue.Empty:
                    yield ': ping\n\n'   # SSE 注释行，防止代理/浏览器超时断开
                else:
                    yield f'data: {data}\n\n'
        finally:
            # 任何退出路径都注销队列（客户端断开、服务端异常等）
            g.room.broadcaster.unregister(q)
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


# 注册房间蓝图（所有 /room/<room_id>/* 路由此处挂载到 app）
app.register_blueprint(room_bp)


# ============================================================================
# 启动入口
# ============================================================================
if __name__ == '__main__':
    import logging
    # 静默 werkzeug 开发服务器的逐请求访问日志（SSE 长连接会持续刷屏）
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    ip = get_local_ip()
    # 首次启动（rooms 表为空，如全新库/global_reset 后）：
    # 自动创建默认房间 room_default + 预热实例 + 生成二维码，保证开箱可用
    if not db_manager.list_rooms():
        db_manager.create_room('room_default', '在线匿名聊天室')
        room_manager.get_or_create('room_default')
        generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/',
                    os.path.join(QR_DIR, 'room_default.png'))

    # 启动横幅：打印局域网访问地址（手机扫码/其他设备访问用）
    print("========================================")
    print("  <<-- 在线匿名聊天室（多房间版）已启动-->>")
    print(f"  -->进入地址: http://{ip}:{BASE_PORT}/")
    print("  按 Ctrl+C 停止服务")
    print("========================================")
    # host=0.0.0.0 监听所有网卡（局域网可达）；threaded=True 每请求一线程
    # （SSE 长连接占一线程，房间数×在线人数规模下足够；更高并发需 gevent/多进程）
    # debug=False 生产形态：不自动重载、不暴露调试器
    app.run(host='0.0.0.0', port=BASE_PORT, threaded=True, debug=False)
