"""
在线匿名聊天室（多房间版 — 单端口 + URL 路由）
========================================

整体架构
--------
单 Flask 应用（默认端口 5000），支持多个聊天房间。
路由分两大类：
  1) 全局路由（app 本身）
       /                       → 登录入口页（未登录时展示）
       /login  POST            → 角色登录（普通用户 / 管理员+密码）
       /logout POST            → 退出登录
       /rooms                  → 房间列表（按 session 角色分流 admin/user 模板）
       /room/<room_id>/        → 聊天页面（?admin=1 时需管理员会话）
       /admin                  → 旧地址兼容，跳转 /
       /api/rooms/*            → 房间列表/创建/删除/开关/二维码（管理接口需管理员会话）
       /api/admin/*            → 修改管理员密码、全局恢复出厂
       /api/entry-qr           → 入口二维码图片
  2) 房间 Blueprint（url_prefix=/room/<room_id>）
       /room/<id>/api/*        → 加入/退出/发消息/上传/查询/收藏/管理等
       /room/<id>/stream       → SSE 实时推送
       /room/<id>/uploads/*    → 房间内文件下载

权限模型
--------
- Flask session 存 role ∈ {'user','admin'}
- 普通用户：可浏览开放房间、进入聊天
- 管理员：额外可创建/删除/开关房间、改密码、全局恢复出厂；
  进入聊天页 /room/<id>/?admin=1 时 session 必须是 admin
- _require_admin()：全局管理 API 的统一鉴权入口
- 房间内管理 API（/api/admin/*）由 ChatRoom.is_admin(user_id) 校验
  （user_id 来自在线用户表，仅 admin_ 前缀会被标记为管理员）

数据库
------
- lan_chat.db（主库 DatabaseManager）
    rooms  表：房间注册表（单一事实来源，room_id 主键）
    config 表：键值配置（目前存 admin_password）
- room_room_<id>.db（每房间独立库 Database，注意 room_db_path 会再拼一层 room_ 前缀）
    profiles / messages / blacklist / favorites / message_audit
    均以 (room_id, ...) 复合主键；另有删除审计触发器与统计视图
- 旧库自动迁移：_migrate() 为缺 room_id 的表补列并回填

线程模型
--------
- Flask threaded=True；每房间一个 SQLite 连接（check_same_thread=False）
- Database/DatabaseManager 内部 threading.Lock 保证线程安全
- 每房间一个 Broadcaster，SSE 客户端各持一个 queue，广播时依次投递
- RoomManager._lock 保护 _rooms 字典的懒加载

主要类
------
- DatabaseManager : 主库（房间注册表 + 配置）
- Database        : 单房间数据读写
- Broadcaster     : SSE 事件广播
- ChatRoom        : 房间业务逻辑（加入/发消息/管理操作，均返回 (json, status)）
- RoomManager     : room_id → ChatRoom 实例缓存
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
import sqlite3
import threading
import qrcode
from flask import (Flask, request, Response, jsonify, render_template, g,
                   send_from_directory, stream_with_context, Blueprint, session, redirect)

# ==================== 常量与路径 ====================
# 打包为 exe 时（PyInstaller frozen），BASE_DIR 取 exe 所在目录；开发时取本文件所在目录
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.path.join(BASE_DIR, 'lan_chat.db')        # 主库文件
UPLOAD_ROOT = os.path.join(BASE_DIR, 'uploads')        # 全局上传目录（兼容旧版房间）
QR_DIR = os.path.join(BASE_DIR, 'static', 'qrcode')    # 二维码图片输出目录

MAX_HISTORY = 200                                       # 默认消息保留条数（可被房间配置覆盖）
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}  # 直接内联展示的图片扩展名
BASE_PORT = 5000                                        # 服务监听端口（二维码 URL 也用它）

# 随机头像池 / 昵称颜色池（新用户加入时随机分配，之后存入 profiles）
AVATARS = ['😀', '😎', '🤖', '👽', '🐱', '🐶', '🦊', '🐼', '🐸', '🐵', '🦁', '🐯']
COLORS = ['#FF5733', '#33FF57', '#3357FF', '#F333FF', '#FF33A8', '#33FFF5', '#F5FF33', '#FF8C33',
          '#8E44AD', '#2ECC71', '#E67E22', '#1ABC9C', '#E74C3C', '#3498DB', '#9B59B6', '#34495E']

# 敏感词表：纯文本消息发送前做简单星号替换（课程演示用，非完整过滤）
SENSITIVE_WORDS = ['傻逼', '操你妈', '去死', 'fuck', 'shit', 'bitch']

# 文件类消息 content 字段的合法格式：必须是本应用的 /room/<id>/uploads/<文件名> URL
UPLOAD_URL_RE = re.compile(r'/room/[^/]+/uploads/[A-Za-z0-9_.\-]+')


# ==================== 通用辅助函数 ====================
def filter_sensitive(text):
    """将消息中的敏感词替换为等长 '*'（逐词简单替换）。"""
    for word in SENSITIVE_WORDS:
        text = text.replace(word, '*' * len(word))
    return text


def get_local_ip():
    """
    获取本机在局域网中的 IP，用于生成二维码和启动横幅。
    优先通过 UDP 连 8.8.8.8（不实际发包）读本地地址；
    失败则退回 gethostname 解析；再失败返回 127.0.0.1。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
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
    """把 url 生成二维码图片保存到 filepath（自动创建父目录）。"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(filepath)


def room_db_path(room_id):
    """房间独立 DB 的绝对路径。注意：room_id 本身已带 room_ 前缀，最终文件名为 room_room_<id>.db。"""
    return os.path.join(BASE_DIR, f'room_{room_id}.db')


def room_uploads_dir(room_id):
    """房间上传目录 uploads_<room_id> 的绝对路径（不存在则创建）。"""
    d = os.path.join(BASE_DIR, f'uploads_{room_id}')
    os.makedirs(d, exist_ok=True)
    return d


# ==================== 数据库：主库（房间注册表 + 配置） ====================
class DatabaseManager:
    """
    主库 lan_chat.db 的线程安全封装。
    职责：
      - rooms 表 CRUD（房间注册表，单一事实来源）
      - config 表键值读写（目前主要是 admin_password）
      - delete_room 时顺带清理对应房间 DB 文件与上传目录
    所有写操作通过 _execute（with lock + conn 事务）；查询通过 _query。
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row   # 查询结果可按列名访问
        self._init_schema()

    def _init_schema(self):
        """建表：rooms（房间注册表）、config（全局配置）；写入默认管理员密码。"""
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id   TEXT PRIMARY KEY,
                    room_name TEXT NOT NULL DEFAULT '新房间',
                    is_open   INTEGER NOT NULL DEFAULT 1,
                    password  TEXT,
                    file_limit_mb    INTEGER NOT NULL DEFAULT 0,
                    max_history      INTEGER NOT NULL DEFAULT 200,
                    recall_time_limit INTEGER NOT NULL DEFAULT 300,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            # 默认管理员密码 ADMIN（仅首次插入，不覆盖已有值）
            self._conn.execute("INSERT OR IGNORE INTO config(key,value) VALUES('admin_password','ADMIN')")

    # ---------- config 键值 ----------
    def get_config(self, key, default=None):
        """读取 config 中一个键，不存在时返回 default。"""
        rows = self._query('SELECT value FROM config WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set_config(self, key, value):
        """写入（或覆盖）config 中一个键。"""
        self._execute('INSERT INTO config(key,value) VALUES(?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    # ---------- 底层读写 ----------
    def _query(self, sql, params=()):
        """加锁执行查询，返回 fetchall() 结果行列表。"""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql, params=()):
        """加锁执行单条写语句，with conn 保证自动提交事务。"""
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    # ---------- rooms 表 CRUD ----------
    def list_rooms(self):
        """按创建时间倒序返回全部房间（dict 列表）。"""
        return [dict(r) for r in self._query('SELECT * FROM rooms ORDER BY created_at DESC')]

    def get_room(self, room_id):
        """按 room_id 查单个房间，不存在返回 None。"""
        rows = self._query('SELECT * FROM rooms WHERE room_id = ?', (room_id,))
        return dict(rows[0]) if rows else None

    def create_room(self, room_id, room_name, password=None):
        """插入新房间记录（其余字段用表默认值，created_at 取当前时间戳）。"""
        self._execute('INSERT INTO rooms(room_id, room_name, password, created_at) VALUES (?, ?, ?, ?)',
                      (room_id, room_name, password, time.time()))

    def delete_room(self, room_id):
        """
        删除房间：先删注册表记录，再删该房间的独立 DB 文件和上传目录。
        调用方需先确保内存中的 ChatRoom 实例已关闭 DB 连接（见 api_delete_room）。
        """
        self._execute('DELETE FROM rooms WHERE room_id = ?', (room_id,))
        db_path = room_db_path(room_id)
        if os.path.exists(db_path):
            os.remove(db_path)
        # 直接拼路径，不走 room_uploads_dir（后者会误创建目录）
        up = os.path.join(BASE_DIR, f'uploads_{room_id}')
        if os.path.isdir(up):
            shutil.rmtree(up, ignore_errors=True)

    def update_room(self, room_id, **kwargs):
        """按 kwargs 动态拼 UPDATE 语句，部分更新房间字段；kwargs 为空则直接返回。"""
        if not kwargs:
            return
        sets = ', '.join(f'{k} = ?' for k in kwargs)
        vals = list(kwargs.values()) + [room_id]
        self._execute(f'UPDATE rooms SET {sets} WHERE room_id = ?', vals)


# ==================== 数据库：房间库 ====================
class Database:
    """
    单个房间独立 SQLite 库（room_room_<id>.db）的线程安全封装。
    所有业务表均带 room_id 作为复合主键的一部分，便于未来合库/迁移。
    表：
      profiles      用户档案（昵称/头像/颜色/禁言截止时间）
      messages      聊天消息（text/image/file/system）
      blacklist     踢出黑名单（按 device_id）
      favorites     收藏夹
      message_audit 消息删除审计（由触发器自动写入）
    视图：v_message_stats / v_message_type_stats（统计用）
    """

    def __init__(self, path, room_id):
        self.room_id = room_id
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.max_history = MAX_HISTORY   # 由 ChatRoom 初始化时用房间配置覆盖
        self._init_schema()
        self._migrate()                  # 旧库补 room_id 列

    def _init_schema(self):
        """建 5 张业务表 + 删除审计触发器 + 2 个统计视图。"""
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    room_id TEXT NOT NULL, 
                    device_id TEXT NOT NULL, 
                    nickname TEXT NOT NULL,
                    avatar TEXT, color TEXT, 
                    muted_until REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (room_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    room_id TEXT NOT NULL, 
                    id TEXT NOT NULL, 
                    type TEXT, 
                    content TEXT, 
                    sender TEXT,
                    user_id TEXT, 
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    avatar TEXT, 
                    color TEXT, 
                    timestamp REAL,
                    file_name TEXT, 
                    file_size INTEGER,
                    PRIMARY KEY (room_id, id)
                );
                CREATE TABLE IF NOT EXISTS blacklist (
                    room_id TEXT NOT NULL, 
                    device_id TEXT NOT NULL, 
                    nickname TEXT,
                    ip TEXT, 
                    created_at REAL,
                    PRIMARY KEY (room_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    room_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, 
                    msg_id TEXT, 
                    msg_type TEXT, 
                    content TEXT,
                    sender TEXT, 
                    file_name TEXT, 
                    file_size INTEGER, 
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    room_id TEXT NOT NULL,
                    message_id TEXT, 
                    type TEXT, 
                    content TEXT, 
                    sender TEXT,
                    user_id TEXT, 
                    deleted_at REAL, 
                    action TEXT
                );
            """)
            # 触发器：messages 被 DELETE 时自动写入审计表（管理端"审计日志"数据来源）
            # 视图：按用户/按类型聚合消息，供管理端"数据统计"页使用
            self._conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS trg_message_delete_audit
                AFTER DELETE ON messages BEGIN
                    INSERT INTO message_audit(room_id, message_id, type, content, sender, user_id, deleted_at, action)
                    VALUES (OLD.room_id, OLD.id, OLD.type, OLD.content, OLD.sender, OLD.user_id, strftime('%s','now'), 'DELETE');
                END;
                CREATE VIEW IF NOT EXISTS v_message_stats AS
                SELECT room_id, user_id, sender, COUNT(*) as message_count,
                       MAX(timestamp) as last_active, MIN(timestamp) as first_active
                FROM messages WHERE user_id IS NOT NULL GROUP BY room_id, user_id, sender;
                CREATE VIEW IF NOT EXISTS v_message_type_stats AS
                SELECT room_id, type, COUNT(*) as count FROM messages GROUP BY room_id, type;
            """)

    def _migrate(self):
        """
        旧版单房间库迁移：
        1) 各表若缺 room_id 列 → ALTER 补列并把已有行回填为当前 room_id
        2) 删除已废弃的 room_state 表（状态已迁到主库 rooms 表）
        """
        with self._lock, self._conn:
            for table in ('profiles', 'messages', 'blacklist', 'favorites', 'message_audit'):
                cols = {r[1] for r in self._conn.execute(f'PRAGMA table_info({table})')}
                if 'room_id' not in cols:
                    self._conn.execute(f'ALTER TABLE {table} ADD COLUMN room_id TEXT NOT NULL DEFAULT \'\'')
                    self._conn.execute(f'UPDATE {table} SET room_id=? WHERE room_id=\'\'', (self.room_id,))
            self._conn.execute('DROP TABLE IF EXISTS room_state')

    def close(self):
        """关闭数据库连接（删除房间/全局重置时调用），忽略已关闭异常。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _query(self, sql, params=()):
        """加锁执行查询。"""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql, params=()):
        """加锁执行写语句并提交。"""
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    # ---------- 档案 profiles ----------
    def get_profile(self, device_id):
        """按 (room_id, device_id) 读用户档案，不存在返回 None。"""
        rows = self._query('SELECT * FROM profiles WHERE room_id=? AND device_id=?', (self.room_id, device_id))
        return dict(rows[0]) if rows else None

    def save_profile(self, device_id, nickname, avatar, color):
        """
        新建或整行覆盖档案（INSERT OR REPLACE）。
        注意：旧迁移库主键可能不含 room_id，用 OR REPLACE 比 ON CONFLICT 更稳妥。
        """
        self._execute('INSERT OR REPLACE INTO profiles(room_id,device_id,nickname,avatar,color,muted_until) VALUES(?,?,?,?,?,0)',
                      (self.room_id, device_id, nickname, avatar, color))

    def update_profile_nickname(self, device_id, nickname):
        """仅更新昵称（加入时若昵称被改）。"""
        self._execute('UPDATE profiles SET nickname=? WHERE room_id=? AND device_id=?', (nickname, self.room_id, device_id))

    def set_muted_until(self, device_id, muted_until):
        """设置禁言截止时间戳（0 表示解除禁言）。"""
        self._execute('UPDATE profiles SET muted_until=? WHERE room_id=? AND device_id=?', (muted_until, self.room_id, device_id))

    def get_muted_users(self):
        """返回当前仍处于禁言中的用户档案列表。"""
        return [dict(r) for r in self._query(
            'SELECT device_id,nickname,avatar,color,muted_until FROM profiles WHERE room_id=? AND muted_until>?',
            (self.room_id, time.time()))]

    # ---------- 消息 messages ----------
    def query_messages(self, nickname=None):
        """
        查询文本消息，可按昵称模糊过滤（LIKE %nick%）。
        返回按 sender 分组的列表：[{sender, avatar, color, messages:[...]}, ...]
        """
        if nickname:
            rows = self._query('SELECT * FROM messages WHERE room_id=? AND sender LIKE ? AND type=? ORDER BY timestamp ASC',
                               (self.room_id, f'%{nickname}%', 'text'))
        else:
            rows = self._query('SELECT * FROM messages WHERE room_id=? AND type=? ORDER BY timestamp ASC',
                               (self.room_id, 'text'))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            groups.setdefault(msg['sender'], {'sender': msg['sender'], 'avatar': msg['avatar'], 'color': msg['color'], 'messages': []})['messages'].append(msg)
        return list(groups.values())

    def query_files(self, nickname=None, upload_dir=None):
        """
        查询图片/文件消息，可按昵称过滤。
        额外标注 file_exists：磁盘上文件是否仍存在（上传目录优先，否则退回全局 UPLOAD_ROOT）。
        返回结构同 query_messages。
        """
        if nickname:
            rows = self._query('SELECT * FROM messages WHERE room_id=? AND sender LIKE ? AND type IN (?,?) ORDER BY timestamp ASC',
                               (self.room_id, f'%{nickname}%', 'image', 'file'))
        else:
            rows = self._query('SELECT * FROM messages WHERE room_id=? AND type IN (?,?) ORDER BY timestamp ASC',
                               (self.room_id, 'image', 'file'))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            fn = os.path.basename(msg.get('content', ''))
            msg['file_exists'] = os.path.isfile(os.path.join(upload_dir or UPLOAD_ROOT, fn))
            groups.setdefault(msg['sender'], {'sender': msg['sender'], 'avatar': msg['avatar'], 'color': msg['color'], 'messages': []})['messages'].append(msg)
        return list(groups.values())

    def _row_to_message(self, r):
        """sqlite3.Row → 前端消息 dict（统一字段格式）。"""
        return {'id': r['id'], 'type': r['type'], 'content': r['content'], 'sender': r['sender'],
                'user_id': r['user_id'], 'is_admin': bool(r['is_admin']), 'avatar': r['avatar'],
                'color': r['color'], 'timestamp': r['timestamp'], 'file_name': r['file_name'], 'file_size': r['file_size']}

    def get_recent_messages(self, limit=None):
        """取最近 limit 条消息（按 rowid 倒序取再翻转成正序），默认 max_history。"""
        rows = self._query('SELECT * FROM messages WHERE room_id=? ORDER BY rowid DESC LIMIT ?',
                           (self.room_id, limit or self.max_history))
        return [self._row_to_message(r) for r in reversed(rows)]

    def add_message(self, msg):
        """写入一条消息（INSERT OR REPLACE，同 id 覆盖），在锁内事务提交。"""
        with self._lock, self._conn:
            self._conn.execute('INSERT OR REPLACE INTO messages(room_id,id,type,content,sender,user_id,is_admin,avatar,color,timestamp,file_name,file_size) '
                               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                               (self.room_id, msg['id'], msg.get('type'), msg.get('content'), msg.get('sender'),
                                msg.get('user_id'), int(msg.get('is_admin', False)), msg.get('avatar'),
                                msg.get('color'), msg.get('timestamp'), msg.get('file_name'), msg.get('file_size')))

    # ---------- 黑名单 blacklist ----------
    def is_blacklisted(self, device_id):
        """该 device_id 是否已被拉黑。"""
        return bool(self._query('SELECT 1 FROM blacklist WHERE room_id=? AND device_id=?', (self.room_id, device_id)))

    def add_blacklist(self, device_id, nickname, ip):
        """加入黑名单（踢出用户时调用）。"""
        self._execute('INSERT OR REPLACE INTO blacklist(room_id,device_id,nickname,ip,created_at) VALUES(?,?,?,?,?)',
                      (self.room_id, device_id, nickname, ip, time.time()))

    def remove_blacklist(self, device_id):
        """移出黑名单，返回是否真的删了行。"""
        with self._lock, self._conn:
            return self._conn.execute('DELETE FROM blacklist WHERE room_id=? AND device_id=?',
                                      (self.room_id, device_id)).rowcount > 0

    def get_blacklist(self):
        """黑名单列表，按拉黑时间倒序。"""
        return [dict(r) for r in self._query(
            'SELECT device_id,nickname,ip,created_at FROM blacklist WHERE room_id=? ORDER BY created_at DESC',
            (self.room_id,))]

    # ---------- 收藏夹 favorites ----------
    def add_favorite(self, user_id, msg):
        """把一条消息快照进该用户的收藏夹。"""
        with self._lock, self._conn:
            self._conn.execute('INSERT INTO favorites(room_id,user_id,msg_id,msg_type,content,sender,file_name,file_size,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                               (self.room_id, user_id, msg.get('id'), msg.get('type'), msg.get('content'),
                                msg.get('sender'), msg.get('file_name'), msg.get('file_size'), time.time()))

    def get_favorites(self, user_id):
        """取该用户全部收藏，按收藏时间倒序。"""
        return [dict(r) for r in self._query(
            'SELECT id,msg_id,msg_type,content,sender,file_name,file_size,created_at FROM favorites WHERE room_id=? AND user_id=? ORDER BY created_at DESC',
            (self.room_id, user_id))]

    def remove_favorite(self, user_id, fav_id):
        """删除指定收藏（校验 user_id + room_id 防越权），返回是否删除成功。"""
        with self._lock, self._conn:
            return self._conn.execute('DELETE FROM favorites WHERE id=? AND user_id=? AND room_id=?',
                                      (fav_id, user_id, self.room_id)).rowcount > 0

    # ---------- 清理 / 维护 ----------
    def cleanup_old_messages(self, max_history):
        """只保留最近 max_history 条消息，其余删除（触发器会写审计）。"""
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages WHERE room_id=? AND rowid NOT IN '
                               '(SELECT rowid FROM messages WHERE room_id=? ORDER BY rowid DESC LIMIT ?)',
                               (self.room_id, self.room_id, max_history))

    def clear_all_messages(self):
        """清空本房间全部消息（管理端"清空聊天记录"）。"""
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages WHERE room_id=?', (self.room_id,))

    def factory_reset(self):
        """房间级恢复出厂：清空消息/档案/黑名单/收藏（不动表结构）。"""
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages WHERE room_id=?', (self.room_id,))
            self._conn.execute('DELETE FROM profiles WHERE room_id=?', (self.room_id,))
            self._conn.execute('DELETE FROM blacklist WHERE room_id=?', (self.room_id,))
            self._conn.execute('DELETE FROM favorites WHERE room_id=?', (self.room_id,))

    def delete_message(self, message_id):
        """删除单条消息（管理端删除/用户撤回共用），返回是否删到行。"""
        with self._lock, self._conn:
            return self._conn.execute('DELETE FROM messages WHERE room_id=? AND id=?',
                                      (self.room_id, message_id)).rowcount > 0

    def get_message_by_id(self, message_id):
        """按消息 id 查询（撤回/删除前校验归属与存在性）。"""
        rows = self._query('SELECT * FROM messages WHERE room_id=? AND id=?', (self.room_id, message_id))
        return dict(rows[0]) if rows else None

    # ---------- 存储过程风格统计（课程数据库设计展示） ----------
    def sp_get_message_stats(self):
        """消息统计：总数 + 按用户视图 + 按类型视图（类型附百分比）。"""
        total = self._query('SELECT COUNT(*) as cnt FROM messages WHERE room_id=?', (self.room_id,))[0]['cnt']
        user_stats = [dict(r) for r in self._query(
            'SELECT * FROM v_message_stats WHERE room_id=? ORDER BY message_count DESC', (self.room_id,))]
        type_stats = [dict(r) for r in self._query(
            'SELECT * FROM v_message_type_stats WHERE room_id=? ORDER BY count DESC', (self.room_id,))]
        for t in type_stats:
            t['percentage'] = round(t['count'] / total * 100, 1) if total else 0
        return {'total_messages': total, 'user_stats': user_stats, 'type_stats': type_stats}

    def sp_get_audit_log(self, limit=50):
        """消息删除审计日志，按 audit_id 倒序取 limit 条。"""
        return [dict(r) for r in self._query(
            'SELECT * FROM message_audit WHERE room_id=? ORDER BY audit_id DESC LIMIT ?',
            (self.room_id, limit))]


# ==================== 广播层（SSE 事件分发） ====================
class Broadcaster:
    """
    每房间一个。维护所有已连接 SSE 客户端的 queue 列表。
    broadcast() 把事件 JSON 序列化后塞进每个客户端的队列；
    SSE 视图从自己的队列 q.get() 取数据推给浏览器。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []   # list[queue.Queue]

    def register(self):
        """新 SSE 连接注册，返回属于该连接的队列。"""
        q = queue.Queue()
        with self._lock:
            self._clients.append(q)
        return q

    def unregister(self, q):
        """连接断开时移除队列（幂等）。"""
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def broadcast(self, event):
        """把事件 dict 序列化为 JSON，投递给当前所有客户端队列。"""
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            targets = list(self._clients)
        for q in targets:
            q.put(data)


# ==================== 业务层（单个房间的完整逻辑） ====================
class ChatRoom:
    """
    单个房间的业务逻辑中枢。
    持有：房间 DB、Broadcaster、上传目录、来自主库的房间配置副本。
    online_users: user_id → {user_id, nickname, device_id, avatar, color, is_admin, ip}
    所有管理方法统一模式：先 is_admin(admin_id) 鉴权，再改配置/执行操作，
    成功后 _save_state() 同步主库并按需 broadcast 事件，返回 (json_dict, http_status)。
    """

    def __init__(self, db, broadcaster, uploads_dir, room_id, db_manager):
        self.db = db
        self.broadcaster = broadcaster
        self.uploads_dir = uploads_dir
        self.room_id = room_id
        self._db_manager = db_manager
        self.lock = threading.RLock()       # 保护 online_users 和配置字段
        self.online_users = {}
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
        """把内存中的房间配置写回主库 rooms 表。"""
        self._db_manager.update_room(self.room_id,
            room_name=self.room_name, is_open=int(self.room_open),
            password=self.room_password, file_limit_mb=self.file_limit_mb,
            max_history=self.max_history, recall_time_limit=self.recall_time_limit)

    def _system_message(self, content):
        """发一条系统消息（入库 + 广播）。"""
        msg = {'id': f'{time.time()}_sys', 'type': 'system', 'content': content, 'sender': '系统', 'timestamp': time.time()}
        self.db.add_message(msg)
        self.broadcaster.broadcast({'type': 'message', 'message': msg})

    def user_list_event(self):
        """构造 user_list 事件（普通用户与管理员分开两个数组）。"""
        with self.lock:
            normal = [{'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for u in self.online_users.values() if not u['is_admin']]
            admins = [{'user_id': uid, 'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for uid, u in self.online_users.items() if u['is_admin']]
        return {'type': 'user_list', 'users': normal, 'admin_users': admins}

    def is_admin(self, user_id):
        """该 user_id 是否为当前在线的管理员（仅在线表中 is_admin=True 的会话有效）。"""
        with self.lock:
            user = self.online_users.get(user_id)
            return user is not None and user.get('is_admin')

    # ---------- 加入 / 离开 ----------
    def join(self, nickname, device_id, ip, password=None, is_admin_user=False):
        """
        加入房间。
        校验顺序：参数非空 → 房间开放 → 房间密码 → 黑名单 → 昵称唯一。
        管理员（is_admin_user=True）由调用方先验 session，这里跳过 开放/密码/黑名单 校验。
        管理员用 admin_ 前缀的 device_id 存档案，避免和普通用户档案冲突。
        成功返回 success/user_id/nickname/avatar/color/muted_remaining。
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
        # 管理员档案独立命名空间
        admin_device_id = f'admin_{device_id}' if is_admin_user else device_id
        target_device_id = admin_device_id if is_admin_user else device_id
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
        """离开房间：从在线表移除并广播最新用户列表。"""
        with self.lock:
            user = self.online_users.pop(user_id, None)
        if user:
            self.broadcaster.broadcast(self.user_list_event())
        return {'success': True}

    # ---------- 发消息 ----------
    def send_message(self, user_id, content, msg_type, display_name=None):
        """
        发送消息（text / image / file）。
        校验：在线 → 房间开放 → 未禁言 → 类型合法 → 内容非空。
        text: 敏感词过滤后入库；image/file: content 必须匹配 UPLOAD_URL_RE，
              且磁盘文件存在（file 还统计大小、可带自定义显示名）。
        成功入库并广播 message 事件。
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
            file_name = (os.path.basename((display_name or '').replace('\\', '/')).strip()[:100] or os.path.basename(content))
        message = {'id': f'{time.time()}_{os.urandom(2).hex()}', 'type': msg_type, 'content': content,
                   'sender': user['nickname'], 'user_id': user_id, 'is_admin': user['is_admin'],
                   'avatar': user['avatar'], 'color': user['color'], 'timestamp': time.time(),
                   'file_name': file_name, 'file_size': file_size}
        self.db.add_message(message)
        self.broadcaster.broadcast({'type': 'message', 'message': message})
        return {'success': True}, 200

    # ---------- 房间配置（均需在线管理员身份） ----------
    def set_room_name(self, admin_id, name):
        """修改房间名称，广播 room_name 事件同步所有客户端标题。"""
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
        """开关房间；关闭时广播 room_closed 让客户端断开。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.room_open = bool(is_open)
        self._save_state()
        if not self.room_open:
            self.broadcaster.broadcast({'type': 'room_closed'})
        return {'success': True, 'open': self.room_open}, 200

    def set_password(self, admin_id, password):
        """设置/清除房间密码（空串 → None 表示无密码），广播 password_changed 让在线用户重进。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.room_password = password or None
        self._save_state()
        self.broadcaster.broadcast({'type': 'password_changed'})
        return {'success': True}, 200

    def set_file_limit(self, admin_id, limit_mb):
        """设置单文件大小上限 MB（0 = 不限制），广播 file_limit 让所有客户端实时同步。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.file_limit_mb = max(0, int(limit_mb or 0))
        self._save_state()
        self.broadcaster.broadcast({'type': 'file_limit', 'limit_mb': self.file_limit_mb})
        return {'success': True, 'file_limit_mb': self.file_limit_mb}, 200

    def set_max_history(self, admin_id, max_history):
        """设置消息保留条数（≥10），立即裁剪旧消息并广播 max_history 事件。"""
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
        """清空全部聊天记录（审计表仍保留删除痕迹）。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.db.clear_all_messages()
        self.broadcaster.broadcast({'type': 'history_cleared'})
        return {'success': True}, 200

    def factory_reset(self, admin_id):
        """
        房间级恢复出厂：清空 DB 业务数据 + 配置回默认值 + 广播 factory_reset。
        （注意：不删房间本身；全局恢复出厂用 /api/admin/global_reset）
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
        """管理员强制删除任意消息，广播 message_deleted 让客户端移除 DOM。"""
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

    # ---------- 撤回 ----------
    def recall_message(self, user_id, message_id):
        """
        用户撤回自己的消息。
        规则：必须在线、只能撤自己的、系统消息不可撤、
              必须在 recall_time_limit 秒内（0 = 禁止撤回）。
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
        """设置撤回时限秒数（0 = 禁止撤回）。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not isinstance(limit, (int, float)) or limit < 0:
            return {'error': '无效的时间限制'}, 400
        self.recall_time_limit = int(limit)
        self._save_state()
        self.broadcaster.broadcast({'type': 'recall_time_limit', 'limit': self.recall_time_limit})
        return {'success': True, 'limit': self.recall_time_limit}, 200

    # ---------- 用户管理（禁言 / 踢出 / 黑名单） ----------
    def mute_user(self, admin_id, target_id, duration):
        """对在线用户禁言 duration 秒（至少 1 秒），写入 profile 并广播。"""
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
        """解除在线用户的禁言。"""
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
        """禁言名单：附剩余秒数、当前会话 user_id、是否在线。"""
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
        """踢出在线用户：移出在线表 + 加入黑名单 + 广播；管理员不可被踢。"""
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
        """在线用户列表（含 IP、禁言剩余秒数），供管理面板展示。"""
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
        """黑名单列表。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        return {'items': self.db.get_blacklist()}, 200

    def blacklist_remove(self, admin_id, device_id):
        """把某设备移出黑名单。"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not device_id:
            return {'error': 'device_id 不能为空'}, 400
        if not self.db.remove_blacklist(device_id):
            return {'error': '该设备不在黑名单中'}, 404
        return {'success': True}, 200


# ==================== 房间管理器（懒加载缓存） ====================
class RoomManager:
    """
    room_id → ChatRoom 实例的线程安全缓存。
    get_or_create：命中缓存直接返回；未命中则从主库查房间 → 开 DB/上传目录
                   → 建 Broadcaster + ChatRoom 并缓存。房间不存在返回 None。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._rooms = {}   # room_id -> ChatRoom

    def get_or_create(self, room_id):
        with self._lock:
            if room_id in self._rooms:
                return self._rooms[room_id]
            room_info = db_manager.get_room(room_id)
            if not room_info:
                return None
            db_path = room_db_path(room_id)
            uploads_dir = room_uploads_dir(room_id)
            os.makedirs(uploads_dir, exist_ok=True)
            _db = Database(db_path, room_id)
            _broadcaster = Broadcaster()
            _room = ChatRoom(_db, _broadcaster, uploads_dir, room_id, db_manager)
            self._rooms[room_id] = _room
            return _room

    def get_room_instance(self, room_id):
        """只取已缓存实例（不触发创建），没有则 None。"""
        with self._lock:
            return self._rooms.get(room_id)


# ==================== Flask 应用初始化 ====================
app = Flask(__name__)
app.secret_key = os.urandom(24)                            # session 签名密钥（每次启动随机）
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'              # 基本 CSRF 防护
os.makedirs(UPLOAD_ROOT, exist_ok=True)
os.makedirs(QR_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

db_manager = DatabaseManager(DB_FILE)   # 主库单例
room_manager = RoomManager()            # 房间实例缓存单例


def sse_data(event):
    """把事件 dict 格式化为一条 SSE data 行（以 \n\n 结尾）。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ==================== 登录 / 会话 ====================
@app.route('/')
def login_page():
    """入口页。已有角色 session 直接跳 /rooms，否则渲染登录页。"""
    if session.get('role'):
        return redirect('/rooms')
    return render_template('login.html')


@app.route('/api/entry-qr')
def api_entry_qr():
    """实时生成"入口首页"二维码图片（登录页手机扫码用）。"""
    ip = get_local_ip()
    url = f'http://{ip}:{BASE_PORT}/'
    qr_path = os.path.join(QR_DIR, 'entry.png')
    generate_qr(url, qr_path)
    return send_from_directory(QR_DIR, 'entry.png')


@app.route('/login', methods=['POST'])
def api_login():
    """
    角色登录。
    - role=user  : 直接写 session['role']='user'
    - role=admin : 校验密码（config.admin_password，默认 ADMIN）
    成功返回 {success, redirect:'/rooms'}。
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
    """清除 session，回到登录页。"""
    session.clear()
    return jsonify({'success': True, 'redirect': '/'})


# ==================== 房间列表页（按角色分流） ====================
@app.route('/rooms')
def rooms_list():
    """已登录才可见。admin → rooms.html（管理），user → rooms_user.html（仅开放房间）。"""
    role = session.get('role')
    if not role:
        return redirect('/')
    rooms = db_manager.list_rooms()
    ip = get_local_ip()
    if role == 'admin':
        return render_template('rooms.html', rooms=rooms, ip=ip)
    return render_template('rooms_user.html', rooms=rooms, ip=ip)


def _require_admin():
    """全局管理 API 鉴权：session.role 必须是 admin，否则返回 (json, 403)；通过返回 None。"""
    if session.get('role') != 'admin':
        return jsonify({'error': '需要管理员登录'}), 403
    return None


# ==================== 聊天页面 ====================
@app.route('/room/<room_id>/')
def room_page(room_id):
    """
    渲染聊天页 index.html。
    - 房间不存在 → 404；实例初始化失败 → 500
    - ?admin=1 时 session 必须是 admin，否则重定向 /
    - 模板变量：is_admin / room_name / has_password / room_id
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
    """旧 /admin 地址兼容：meta-refresh 跳到 /（登录页）。"""
    return render_template('admin.html')


# ==================== 房间管理 API（全局，多数需管理员会话） ====================
@app.route('/api/rooms')
def api_list_rooms():
    """房间列表 JSON（公开只读）。"""
    return jsonify({'rooms': db_manager.list_rooms()})


@app.route('/api/rooms/create', methods=['POST'])
def api_create_room():
    """
    创建房间（需管理员会话）。
    room_id 用 毫秒时间戳 + 3 字节随机 hex，避免同秒并发主键冲突。
    同时生成房间二维码并预热 ChatRoom 实例。
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
    删除房间（需管理员会话）。
    顺序很重要：先从缓存摘除并关闭 DB 连接 → 再删注册表+文件+二维码 →
    若房间已删光则自动重建默认房间 room_default。
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
    """开关房间（需管理员会话）：主库取反 is_open，并同步内存实例。"""
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
    """房间列表精简状态（房间卡片 UI 用）：id/名称/开关/端口。"""
    rooms = db_manager.list_rooms()
    return jsonify({'rooms': [{'room_id': r['room_id'], 'room_name': r['room_name'],
                               'is_open': r['is_open'], 'port': BASE_PORT} for r in rooms]})


@app.route('/api/rooms/qr/<room_id>')
def api_room_qr(room_id):
    """房间二维码图片；文件不存在则按当前 IP:PORT 现场生成。"""
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
    """修改管理员登录密码（需 admin 会话 + 正确旧密码）。"""
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
    全局恢复出厂（需 admin 会话 + 当前管理员密码）。
    步骤：
      1) 关闭所有房间实例的 DB 连接并清空缓存
      2) 删除所有 room_*.db、uploads_* 目录、二维码 PNG
      3) 主库 rooms 表只保留重建的 room_default
      4) 管理员密码重置为 ADMIN，重建默认房间并生成二维码
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
    # 删除所有房间 DB 文件和上传目录
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
    # 重置 rooms 表：只保留默认房间
    with db_manager._lock, db_manager._conn:
        db_manager._conn.execute('DELETE FROM rooms')
        db_manager._conn.execute(
            'INSERT INTO rooms(room_id, room_name, is_open, password, file_limit_mb, max_history, recall_time_limit, created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            ('room_default', '在线匿名聊天室', 1, None, 0, 200, 300, time.time()))
    # 重置管理员密码为默认
    db_manager.set_config('admin_password', 'ADMIN')
    # 重建默认房间
    room_manager.get_or_create('room_default')
    ip = get_local_ip()
    generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/', os.path.join(QR_DIR, 'room_default.png'))
    return jsonify({'success': True})


# ==================== 房间路由（Blueprint，url_prefix=/room/<room_id>） ====================
room_bp = Blueprint('room', __name__, url_prefix='/room/<room_id>')


def _room_context(room_id):
    """
    before_request 钩子：把当前房间实例加载到 g.room，并从 view_args 移除 room_id，
    避免 room_id 被当作参数重复传给各视图函数。房间不存在直接 404。
    """
    g.room_id = room_id
    g.room = room_manager.get_or_create(room_id)
    if not g.room:
        return jsonify({'error': '房间不存在'}), 404
    if request.view_args and 'room_id' in request.view_args:
        request.view_args.pop('room_id')


room_bp.before_request(lambda: _room_context(request.view_args.get('room_id', '') if request.view_args else ''))


def _sync_upload_cap(room):
    """按房间 file_limit_mb 同步 Flask 全局 MAX_CONTENT_LENGTH（0=不限，留 1MB 缓冲）。"""
    if room.file_limit_mb <= 0:
        app.config['MAX_CONTENT_LENGTH'] = None
    else:
        app.config['MAX_CONTENT_LENGTH'] = room.file_limit_mb * 1024 * 1024 + 1024 * 1024


# ==================== 房间用户 API ====================
@room_bp.route('/api/join', methods=['POST'])
def api_join():
    """
    加入房间。
    - is_admin:true 必须带管理员 session（否则 403，防止伪造管理员）
    - 委托 ChatRoom.join 做 开放/密码/黑名单/昵称 校验
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
    """离开房间（页面 unload 时 sendBeacon 调用），force=True 兼容非 JSON content-type。"""
    data = request.get_json(silent=True, force=True) or {}
    return jsonify(g.room.leave(data.get('user_id')))


@room_bp.route('/api/send', methods=['POST'])
def api_send():
    """发送消息（text/image/file）。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.send_message(data.get('user_id'), data.get('content'),
                                          data.get('type', 'text'), data.get('file_name'))
    return jsonify(result), status


@room_bp.route('/api/upload_file', methods=['POST'])
def api_upload_file():
    """
    上传文件到当前房间 uploads 目录。
    - 校验大小（房间 file_limit_mb）
    - 扩展名白名单清洗后重命名：时间戳_随机hex.ext
    - 返回 url=/room/<id>/uploads/<filename> 供 send 消息引用
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
    """下载/预览房间文件：图片直接内联，其余 as_attachment 下载。"""
    ext = os.path.splitext(filename)[1].lower()
    return send_from_directory(g.room.uploads_dir, filename, as_attachment=ext not in IMAGE_EXTS)


@room_bp.route('/api/messages')
def api_messages():
    """取最近消息（SSE 连上时也会推 history，这里给普通轮询兜底）。"""
    return jsonify(g.room.db.get_recent_messages())


@room_bp.route('/api/get_profile')
def api_get_profile():
    """按 device_id 取用户档案（登录页自动回填上次昵称用）。"""
    device_id = request.args.get('device_id')
    profile = g.room.db.get_profile(device_id) if device_id else None
    return jsonify({'profile': profile})


@room_bp.route('/api/room_status')
def api_room_status():
    """房间公开状态：开关/是否有密码/文件限额/消息保留/撤回时限。"""
    return jsonify({'open': g.room.room_open, 'password': g.room.room_password is not None,
                    'file_limit_mb': g.room.file_limit_mb, 'max_history': g.room.max_history,
                    'recall_time_limit': g.room.recall_time_limit})


@room_bp.route('/api/query_messages')
def api_query_messages():
    """按昵称模糊搜索文本消息，按发送者分组返回。"""
    nickname = request.args.get('nickname', '').strip()
    return jsonify({'groups': g.room.db.query_messages(nickname if nickname else None)})


@room_bp.route('/api/query_files')
def api_query_files():
    """按昵称模糊搜索图片/文件消息，按发送者分组返回。"""
    nickname = request.args.get('nickname', '').strip()
    return jsonify({'groups': g.room.db.query_files(nickname if nickname else None, g.room.uploads_dir)})


@room_bp.route('/api/file_exists')
def api_file_exists():
    """检查房间内某文件 URL 对应的磁盘文件是否仍存在（收藏夹下载前校验）。"""
    url = request.args.get('url', '')
    filename = os.path.basename(url)
    exists = os.path.isfile(os.path.join(g.room.uploads_dir, filename))
    return jsonify({'exists': exists})


# ==================== 收藏夹 API ====================
@room_bp.route('/api/favorites/add', methods=['POST'])
def api_favorites_add():
    """收藏一条消息（按 message_id 快照入库）。"""
    data = request.get_json(silent=True) or {}
    user_id, msg_id = data.get('user_id'), data.get('message_id')
    if not user_id or not msg_id:
        return jsonify({'error': '参数不完整'}), 400
    msg = g.room.db.get_message_by_id(msg_id)
    if not msg:
        return jsonify({'error': '消息不存在'}), 404
    g.room.db.add_favorite(user_id, dict(msg))
    return jsonify({'success': True}), 200


@room_bp.route('/api/favorites/list', methods=['GET'])
def api_favorites_list():
    """列出某用户在本房间的全部收藏。"""
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': '缺少 user_id'}), 400
    return jsonify({'items': g.room.db.get_favorites(user_id)}), 200


@room_bp.route('/api/favorites/remove', methods=['POST'])
def api_favorites_remove():
    """删除一条收藏（DB 层校验 user_id + room_id 防越权）。"""
    data = request.get_json(silent=True) or {}
    user_id, fav_id = data.get('user_id'), data.get('fav_id')
    if not user_id or not fav_id:
        return jsonify({'error': '参数不完整'}), 400
    ok = g.room.db.remove_favorite(user_id, fav_id)
    return jsonify({'success': ok}), 200 if ok else 404


# ==================== 房间管理 API（需房间内在线管理员身份） ====================
# 统一模式：取 JSON → 调用 ChatRoom.xxx(user_id/admin_id, ...) → 返回 (result, status)
# ChatRoom 内部用 is_admin(user_id) 校验（user_id 来自加入时的在线表）

@room_bp.route('/api/admin/set_room_name', methods=['POST'])
def api_admin_set_room_name():
    """改房间名称。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_room_name(data.get('user_id'), data.get('name'))
    return jsonify(result), status


@room_bp.route('/api/admin/set_room_open', methods=['POST'])
def api_admin_set_room_open():
    """开/关房间。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_room_open(data.get('user_id'), data.get('open', True))
    return jsonify(result), status


@room_bp.route('/api/admin/set_password', methods=['POST'])
def api_admin_set_password():
    """设置/清除房间密码。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_password(data.get('user_id'), data.get('password', ''))
    return jsonify(result), status


@room_bp.route('/api/admin/set_file_limit', methods=['POST'])
def api_admin_set_file_limit():
    """设置单文件大小上限，成功后同步 MAX_CONTENT_LENGTH。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_file_limit(data.get('user_id'), data.get('limit_mb'))
    if status == 200:
        _sync_upload_cap(g.room)
    return jsonify(result), status


@room_bp.route('/api/admin/set_max_history', methods=['POST'])
def api_admin_set_max_history():
    """设置消息保留条数（自动裁剪旧消息）。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_max_history(data.get('user_id'), data.get('max_history'))
    return jsonify(result), status


@room_bp.route('/api/admin/clear_history', methods=['POST'])
def api_admin_clear_history():
    """清空全部聊天记录。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.clear_history(data.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/factory_reset', methods=['POST'])
def api_admin_factory_reset():
    """房间级恢复出厂（清数据 + 配置回默认，不删房间本身）。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.factory_reset(data.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/delete_message', methods=['POST'])
def api_admin_delete_message():
    """管理员强制删除消息。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.admin_delete_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status


@room_bp.route('/api/recall_message', methods=['POST'])
def api_recall_message():
    """用户撤回自己的消息（时限内）。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.recall_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/mute_user', methods=['POST'])
def api_admin_mute_user():
    """禁言在线用户。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.mute_user(data.get('admin_id'), data.get('target_id'), data.get('duration'))
    return jsonify(result), status


@room_bp.route('/api/admin/unmute_user', methods=['POST'])
def api_admin_unmute_user():
    """解除在线用户禁言。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.unmute_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/muted_users', methods=['GET'])
def api_admin_muted_users():
    """禁言名单（含倒计时/在线状态）。"""
    result, status = g.room.get_muted_users(request.args.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/kick_user', methods=['POST'])
def api_admin_kick_user():
    """踢出用户并拉黑。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.kick_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/online_users', methods=['POST'])
def api_admin_online_users():
    """在线用户列表（含 IP、禁言剩余）。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.online_list(data.get('admin_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/blacklist', methods=['GET'])
def api_admin_blacklist():
    """黑名单列表。"""
    result, status = g.room.blacklist_list(request.args.get('user_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/remove_blacklist', methods=['POST'])
def api_admin_remove_blacklist():
    """移出黑名单。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.blacklist_remove(data.get('user_id'), data.get('device_id'))
    return jsonify(result), status


@room_bp.route('/api/admin/set_recall_time_limit', methods=['POST'])
def api_admin_set_recall_time_limit():
    """设置撤回时限。"""
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_recall_time_limit(data.get('user_id'), data.get('limit'))
    return jsonify(result), status


@room_bp.route('/api/admin/message_stats')
def api_admin_message_stats():
    """消息统计（总数 + 视图按用户/类型聚合），需房间内在线管理员身份。"""
    user_id = request.args.get('user_id')
    if not g.room.is_admin(user_id):
        return jsonify({'error': '无权限'}), 403
    return jsonify(g.room.db.sp_get_message_stats())


@room_bp.route('/api/admin/audit_log')
def api_admin_audit_log():
    """消息删除审计日志，需房间内在线管理员身份。"""
    user_id = request.args.get('user_id')
    if not g.room.is_admin(user_id):
        return jsonify({'error': '无权限'}), 403
    limit = request.args.get('limit', 50, type=int)
    return jsonify({'logs': g.room.db.sp_get_audit_log(limit)})


# ==================== SSE 实时推送 ====================
@room_bp.route('/stream')
def stream():
    """
    Server-Sent Events 长连接：
    1) 注册队列到 Broadcaster
    2) 首包依次推 history / room_name / user_list
    3) 循环从队列取事件；30s 无事件发 ": ping" 心跳保活
    4) 断开时 finally 注销队列
    """
    def event_stream():
        q = g.room.broadcaster.register()
        try:
            yield sse_data({'type': 'history', 'messages': g.room.db.get_recent_messages()})
            yield sse_data({'type': 'room_name', 'name': g.room.room_name})
            yield sse_data(g.room.user_list_event())
            while True:
                try:
                    data = q.get(timeout=30)
                except queue.Empty:
                    yield ': ping\n\n'   # SSE 注释行，防止代理/浏览器超时断开
                else:
                    yield f'data: {data}\n\n'
        finally:
            g.room.broadcaster.unregister(q)
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


# 注册房间蓝图（所有 /room/<room_id>/* 路由此处挂载）
app.register_blueprint(room_bp)


# ==================== 启动 ====================
if __name__ == '__main__':
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)   # 静默开发服务器访问日志

    ip = get_local_ip()
    # 首次启动初始化默认房间 + 二维码
    if not db_manager.list_rooms():
        db_manager.create_room('room_default', '在线匿名聊天室')
        room_manager.get_or_create('room_default')
        generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/', os.path.join(QR_DIR, 'room_default.png'))

    # 启动横幅：打印局域网访问地址
    print("========================================")
    print("  <<-- 在线匿名聊天室（多房间版）已启动-->>")
    print(f"  -->进入地址: http://{ip}:{BASE_PORT}/")
    print("  按 Ctrl+C 停止服务")
    print("========================================")
    app.run(host='0.0.0.0', port=BASE_PORT, threaded=True, debug=False)
