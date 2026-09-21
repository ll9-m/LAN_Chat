"""
在线匿名聊天室（单房间版）

代码自上而下：
1. 常量与路径 
   → 配置：MAX_HISTORY、敏感词、正则等

2. 辅助函数
   → filter_sensitive(): 敏感词过滤
   → get_local_ip(): 局域网IP探测
   → generate_qr(): 二维码生成

3. Database类 
   → _init_schema(): 数据库表结构 + 触发器 + 视图
   → CRUD 方法：get/save/set 系列
   → sp_* 方法：存储过程（重点）

4. Broadcaster 类 
   → SSE 广播器，管理在线浏览器的推送队列

5. ChatRoom 类 
   → 业务逻辑：join/leave/send_message
   → 管理员操作：mute/kick/password 等

6. Flask 路由 
   → API 端点定义，薄封装层

身份机制：
    每个浏览器首次访问时生成唯一 device_id 保存在 localStorage，
    档案（昵称/头像/颜色）、黑名单、禁言全部挂在 device_id 上，
    因此 IP 变化（换网络、重新拨号）不影响用户身份。
"""
import os
import sys
import re
import time
import json
import random
import queue
import socket
import sqlite3
import threading
import qrcode
from flask import (Flask, request, Response, jsonify, render_template,
                   send_from_directory, stream_with_context)

# ==================== 常量与路径 ====================
# 兼容 PyInstaller 打包：打包后以 exe 所在目录为根
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.path.join(BASE_DIR, 'lan_chat.db')       # SQLite 数据库文件
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')     # 图片上传目录

MAX_HISTORY = 200                        # 服务端默认保留的最近消息条数
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}   # 内联显示为图片的扩展名

AVATARS = ['😀', '😎', '🤖', '👽', '🐱', '🐶', '🦊', '🐼', '🐸', '🐵', '🦁', '🐯']
COLORS = ['#FF5733', '#33FF57', '#3357FF', '#F333FF', '#FF33A8', '#33FFF5', '#F5FF33', '#FF8C33',
          '#8E44AD', '#2ECC71', '#E67E22', '#1ABC9C', '#E74C3C', '#3498DB', '#9B59B6', '#34495E']

SENSITIVE_WORDS = ['傻逼', '操你妈', '去死', 'fuck', 'shit', 'bitch'] # 消息敏感词列表

# 图片/文件消息只允许指向本服务上传目录的路径（防止伪造 src 注入 XSS）
UPLOAD_URL_RE = re.compile(r'/uploads/[A-Za-z0-9_.\-]+')


# ==================== 辅助函数 ====================
def filter_sensitive(text):
    """把消息里的敏感词替换成等长星号"""
    for word in SENSITIVE_WORDS:
        text = text.replace(word, '*' * len(word))
    return text


def get_local_ip():
    """探测本机在局域网中的 IP，失败时逐级降级"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # UDP connect 不会真正发包，只为取路由源地址
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def generate_qr(port=5000):
    """生成指向本机的访问二维码，保存到 static/qrcode.png，返回访问 URL"""
    ip = get_local_ip()
    url = f"http://{ip}:{port}"
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(os.path.join(BASE_DIR, 'static', 'qrcode.png'))
    return url


# ==================== 数据层 ====================
class Database:
    """SQLite 数据层，所有持久化数据都存放在单个 lan_chat.db 中。

    表结构：
        room_state  房间设置（房间名/密码/是否开放），key-value 结构
        profiles    设备档案（device_id -> 昵称/头像/颜色/禁言截止时间）
        messages    消息历史（自动裁剪，只保留最近 MAX_HISTORY 条）
        blacklist   黑名单（device_id -> 踢出时的昵称/IP，供管理面板展示）

    单个连接被所有请求线程共用，因此 check_same_thread=False，
    并用 self._lock 把每次读写串行化。
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.max_history = MAX_HISTORY  # 最近消息保留条数，默认200
        self._init_schema()

    def _init_schema(self):
        """建表（已存在则跳过）"""
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS room_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    device_id   TEXT PRIMARY KEY,
                    nickname    TEXT NOT NULL,
                    avatar      TEXT,
                    color       TEXT,
                    muted_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id        TEXT PRIMARY KEY,
                    type      TEXT,
                    content   TEXT,
                    sender    TEXT,
                    user_id   TEXT,
                    is_admin  INTEGER NOT NULL DEFAULT 0,
                    avatar    TEXT,
                    color     TEXT,
                    timestamp REAL,
                    file_name TEXT,
                    file_size INTEGER
                );
                CREATE TABLE IF NOT EXISTS blacklist (
                    device_id  TEXT PRIMARY KEY,
                    nickname   TEXT,
                    ip         TEXT,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT NOT NULL,
                    msg_id     TEXT,
                    msg_type   TEXT,
                    content    TEXT,
                    sender     TEXT,
                    file_name  TEXT,
                    file_size  INTEGER,
                    created_at REAL NOT NULL
                );
            """)

            # 旧库升级：为消息表补文件消息字段（列不存在时追加）
            cols = {r[1] for r in self._conn.execute('PRAGMA table_info(messages)')}
            if 'file_name' not in cols:
                self._conn.execute('ALTER TABLE messages ADD COLUMN file_name TEXT')
            if 'file_size' not in cols:
                self._conn.execute('ALTER TABLE messages ADD COLUMN file_size INTEGER')

            # ========== 数据库高级特性：触发器、视图、存储过程 ==========

            # ---------- 触发器相关：审计日志表 ----------
            # 当消息被删除时，触发器会自动把被删消息记录到这张表
            # 用途：追踪消息删除操作，保留审计记录
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS message_audit (
                    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id  TEXT,
                    type        TEXT,
                    content     TEXT,
                    sender      TEXT,
                    user_id     TEXT,
                    deleted_at  REAL,
                    action      TEXT
                );
            """)

            # ---------- 触发器：删除消息时自动记录审计日志 ----------
            # AFTER DELETE 触发器：在删除 messages 表中的记录后，
            # 自动把被删除的消息插入到 message_audit 表
            self._conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS trg_message_delete_audit
                AFTER DELETE ON messages
                BEGIN
                    INSERT INTO message_audit (message_id, type, content, sender, user_id, deleted_at, action)
                    VALUES (OLD.id, OLD.type, OLD.content, OLD.sender, OLD.user_id, strftime('%s','now'), 'DELETE');
                END;
            """)

            # ---------- 视图：消息统计视图 ----------
            # 按用户统计发送的消息数量、最近发言时间
            # 用途：管理面板可查看每个用户的消息统计
            self._conn.executescript("""
                CREATE VIEW IF NOT EXISTS v_message_stats AS
                SELECT 
                    user_id,
                    sender,
                    COUNT(*) as message_count,
                    MAX(timestamp) as last_active,
                    MIN(timestamp) as first_active
                FROM messages
                WHERE user_id IS NOT NULL
                GROUP BY user_id, sender;
            """)

            # ---------- 视图：消息类型统计视图 ----------
            # 统计各类消息（文字、图片、文件）的数量
            self._conn.executescript("""
                CREATE VIEW IF NOT EXISTS v_message_type_stats AS
                SELECT 
                    type,
                    COUNT(*) as count,
                    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM messages), 1) as percentage
                FROM messages
                GROUP BY type;
            """)

    # ---------- 内部工具 ----------
    def _query(self, sql, params=()):
        """执行只读查询，返回 list[sqlite3.Row]"""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql, params=()):
        """执行写操作并提交"""
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    # ---------- 房间设置 ----------
    def get_room_state(self):
        """读取房间设置，缺省值：默认房间名 / 无密码 / 开放"""
        rows = self._query('SELECT key, value FROM room_state')
        state = {r['key']: r['value'] for r in rows}
        return {
            'room_name': state.get('room_name') or '在线匿名聊天室',
            'password': state.get('password'),          # None 表示无密码
            'open': state.get('open', '1') == '1',
            'file_limit_mb': int(state.get('file_limit_mb') or 0),   # 0 = 不限制
            'max_history': int(state.get('max_history') or 200),   # 默认200条
            'recall_time_limit': int(state.get('recall_time_limit') or 300),  # 默认300秒（5分钟）
        }

    def save_room_state(self, room_name, password, is_open, file_limit_mb=0, max_history=200, recall_time_limit=300):
        """整体保存房间设置"""
        pairs = [('room_name', room_name),
                 ('password', password),
                 ('open', '1' if is_open else '0'),
                 ('file_limit_mb', str(int(file_limit_mb))),
                 ('max_history', str(int(max_history))),
                 ('recall_time_limit', str(int(recall_time_limit)))]
        with self._lock, self._conn:
            self._conn.executemany('REPLACE INTO room_state(key, value) VALUES (?, ?)', pairs)

    # ---------- 设备档案 ----------
    def get_profile(self, device_id):
        """按设备取档案，没有则返回 None"""
        rows = self._query('SELECT * FROM profiles WHERE device_id = ?', (device_id,))
        return dict(rows[0]) if rows else None

    def save_profile(self, device_id, nickname, avatar, color):
        """保存/更新设备档案；UPSERT 保证已存在的 muted_until 不被覆盖"""
        self._execute(
            '''INSERT INTO profiles(device_id, nickname, avatar, color, muted_until)
               VALUES (?, ?, ?, ?, 0)
               ON CONFLICT(device_id) DO UPDATE SET
                   nickname = excluded.nickname,
                   avatar   = excluded.avatar,
                   color    = excluded.color''',
            (device_id, nickname, avatar, color))

    def update_profile_nickname(self, device_id, nickname):
        """仅更新昵称，不动头像/颜色/禁言状态"""
        self._execute('UPDATE profiles SET nickname = ? WHERE device_id = ?',
                      (nickname, device_id))

    def set_muted_until(self, device_id, muted_until):
        """更新设备禁言截止时间（0 表示未禁言）"""
        self._execute('UPDATE profiles SET muted_until = ? WHERE device_id = ?',
                      (muted_until, device_id))

    def get_muted_users(self):
        """查询所有当前被禁言的用户（muted_until > now）"""
        now = time.time()
        rows = self._query(
            'SELECT device_id, nickname, avatar, color, muted_until '
            'FROM profiles WHERE muted_until > ?', (now,))
        return [dict(r) for r in rows]

    # ---------- 查询功能 ----------
    def query_messages(self, nickname=None):
        """按昵称搜索聊天记录，按 sender（昵称）分组返回"""
        if nickname:
            rows = self._query(
                'SELECT * FROM messages WHERE sender LIKE ? AND type = ? ORDER BY timestamp ASC',
                (f'%{nickname}%', 'text'))
        else:
            rows = self._query(
                'SELECT * FROM messages WHERE type = ? ORDER BY timestamp ASC', ('text',))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            key = msg['sender']  # 按昵称分组，同一人刷新前后昵称不变
            if key not in groups:
                groups[key] = {'sender': msg['sender'],
                               'avatar': msg['avatar'], 'color': msg['color'],
                               'messages': []}
            groups[key]['messages'].append(msg)
        return list(groups.values())

    def query_files(self, nickname=None):
        """按昵称搜索文件/图片消息，按 sender（昵称）分组，并校验 uploads 目录中的实际文件"""
        if nickname:
            rows = self._query(
                'SELECT * FROM messages WHERE sender LIKE ? AND type IN (?, ?) ORDER BY timestamp ASC',
                (f'%{nickname}%', 'image', 'file'))
        else:
            rows = self._query(
                'SELECT * FROM messages WHERE type IN (?, ?) ORDER BY timestamp ASC',
                ('image', 'file'))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            # 检查文件是否还存在于 uploads 目录
            if msg['content']:
                filename = os.path.basename(msg['content'])
                msg['file_exists'] = os.path.isfile(os.path.join(UPLOAD_FOLDER, filename))
            else:
                msg['file_exists'] = False
            key = msg['sender']  # 按昵称分组
            if key not in groups:
                groups[key] = {'sender': msg['sender'],
                               'avatar': msg['avatar'], 'color': msg['color'],
                               'files': []}
            groups[key]['files'].append(msg)
        return list(groups.values())

    # ---------- 消息历史 ----------
    def add_message(self, msg):
        """写入一条消息，并把历史裁剪到最近 max_history 条"""
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO messages'
                '(id, type, content, sender, user_id, is_admin, avatar, color, timestamp, '
                'file_name, file_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (msg['id'], msg['type'], msg['content'], msg['sender'], msg.get('user_id'),
                 1 if msg.get('is_admin') else 0, msg.get('avatar'), msg.get('color'),
                 msg.get('timestamp'), msg.get('file_name'), msg.get('file_size')))
            self._conn.execute(
                'DELETE FROM messages WHERE rowid NOT IN '
                '(SELECT rowid FROM messages ORDER BY rowid DESC LIMIT ?)', (self.max_history,))

    def get_recent_messages(self, limit=MAX_HISTORY):
        """按时间正序返回最近 limit 条消息"""
        rows = self._query('SELECT * FROM messages ORDER BY rowid DESC LIMIT ?', (limit,))
        return [self._row_to_message(r) for r in reversed(rows)]

    @staticmethod
    def _row_to_message(row):
        """把数据库行还原成消息 dict（is_admin 转回布尔值）"""
        return {'id': row['id'], 'type': row['type'], 'content': row['content'],
                'sender': row['sender'], 'user_id': row['user_id'],
                'is_admin': bool(row['is_admin']), 'avatar': row['avatar'],
                'color': row['color'], 'timestamp': row['timestamp'],
                'file_name': row['file_name'], 'file_size': row['file_size']}

    # ---------- 黑名单 ----------
    def is_blacklisted(self, device_id):
        """设备是否在黑名单中"""
        return bool(self._query('SELECT 1 FROM blacklist WHERE device_id = ?', (device_id,)))

    def add_blacklist(self, device_id, nickname, ip):
        """把设备加入黑名单，同时记录踢出时的昵称和 IP 供管理面板展示"""
        self._execute('INSERT OR REPLACE INTO blacklist(device_id, nickname, ip, created_at) '
                      'VALUES (?, ?, ?, ?)', (device_id, nickname, ip, time.time()))

    def remove_blacklist(self, device_id):
        """把设备移出黑名单，返回是否真的移除过"""
        with self._lock, self._conn:
            cur = self._conn.execute('DELETE FROM blacklist WHERE device_id = ?', (device_id,))
            return cur.rowcount > 0

    def get_blacklist(self):
        """返回全部黑名单条目（最新在前）"""
        rows = self._query('SELECT device_id, nickname, ip, created_at FROM blacklist '
                           'ORDER BY created_at DESC')
        return [dict(r) for r in rows]

    # ========== 收藏夹 ==========

    def add_favorite(self, user_id, msg):
        """收藏一条消息"""
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO favorites(user_id, msg_id, msg_type, content, sender, '
                'file_name, file_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, msg.get('id'), msg.get('type'), msg.get('content'),
                 msg.get('sender'), msg.get('file_name'), msg.get('file_size'), time.time()))

    def get_favorites(self, user_id):
        """获取用户收藏列表（按类型分组，最新在前）"""
        rows = self._query(
            'SELECT id, msg_id, msg_type, content, sender, file_name, file_size, created_at '
            'FROM favorites WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        return [dict(r) for r in rows]

    def remove_favorite(self, user_id, fav_id):
        """删除一条收藏"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                'DELETE FROM favorites WHERE id = ? AND user_id = ?', (fav_id, user_id))
            return cur.rowcount > 0

    def cleanup_old_messages(self, max_history):
        """清理旧消息，保留最近 max_history 条"""
        with self._lock, self._conn:
            self._conn.execute(
                'DELETE FROM messages WHERE rowid NOT IN '
                '(SELECT rowid FROM messages ORDER BY rowid DESC LIMIT ?)', (max_history,))

    def clear_all_messages(self):
        """清空所有聊天记录"""
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages')

    def factory_reset(self):
        """恢复出厂：清空消息、档案、黑名单，重置房间设置"""
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages')
            self._conn.execute('DELETE FROM profiles')
            self._conn.execute('DELETE FROM blacklist')
            self._conn.execute("REPLACE INTO room_state(key, value) VALUES "
                               "('room_name','在线匿名聊天室'),('open','1'),('password',''),"
                               "('file_limit_mb','0'),('max_history','200'),('recall_time_limit','0')")
        # 清空 uploads 目录中的文件
        if os.path.isdir(UPLOAD_FOLDER):
            for f in os.listdir(UPLOAD_FOLDER):
                fp = os.path.join(UPLOAD_FOLDER, f)
                if os.path.isfile(fp):
                    os.remove(fp)

    def delete_message(self, message_id):
        """删除单条消息（管理员用）"""
        with self._lock, self._conn:
            cur = self._conn.execute('DELETE FROM messages WHERE id = ?', (message_id,))
            return cur.rowcount > 0

    def get_message_by_id(self, message_id):
        """按ID查询单条消息"""
        rows = self._query('SELECT * FROM messages WHERE id = ?', (message_id,))
        return dict(rows[0]) if rows else None

    # ========== 数据库高级特性：存储过程 ==========

    def sp_send_message(self, msg):
        """存储过程：发送消息并自动清理旧消息

        功能：
        1. 插入新消息到 messages 表
        2. 自动删除超过 MAX_HISTORY 条的旧消息
        3. 被删除的消息会通过触发器 trg_message_delete_audit 自动记录到审计表

        这是一个事务操作：要么全部成功，要么全部回滚
        """
        with self._lock, self._conn:
            # 插入新消息
            self._conn.execute(
                'INSERT OR REPLACE INTO messages'
                '(id, type, content, sender, user_id, is_admin, avatar, color, timestamp, '
                'file_name, file_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (msg['id'], msg['type'], msg['content'], msg['sender'], msg.get('user_id'),
                 1 if msg.get('is_admin') else 0, msg.get('avatar'), msg.get('color'),
                 msg.get('timestamp'), msg.get('file_name'), msg.get('file_size')))

            # 清理旧消息（保留最近 max_history 条）
            # 删除的消息会自动被触发器记录到 message_audit 表
            self._conn.execute(
                'DELETE FROM messages WHERE rowid NOT IN '
                '(SELECT rowid FROM messages ORDER BY rowid DESC LIMIT ?)', (self.max_history,))

    def sp_get_message_stats(self):
        """存储过程：获取消息统计数据

        返回：
        - 按用户统计的消息数量
        - 按类型统计的消息数量
        - 总消息数
        """
        with self._lock:
            # 查询用户消息统计（使用视图 v_message_stats）
            user_stats = self._conn.execute(
                'SELECT * FROM v_message_stats ORDER BY message_count DESC').fetchall()

            # 查询类型统计（使用视图 v_message_type_stats）
            type_stats = self._conn.execute(
                'SELECT * FROM v_message_type_stats').fetchall()

            # 查询总数
            total = self._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]

            return {
                'user_stats': [dict(r) for r in user_stats],
                'type_stats': [dict(r) for r in type_stats],
                'total_messages': total
            }

    def sp_get_audit_log(self, limit=50):
        """存储过程：获取审计日志

        返回最近 limit 条被删除的消息记录
        """
        rows = self._query(
            'SELECT * FROM message_audit ORDER BY audit_id DESC LIMIT ?', (limit,))
        return [dict(r) for r in rows]


# ==================== 广播层 ====================
class Broadcaster:
    """SSE 事件广播器。

    每个打开 /stream 的浏览器注册一个队列；broadcast() 把事件 JSON
    塞进所有队列，由各连接的生成器自行取出发送。queue.Queue 本身
    线程安全，这里只需用锁保护客户端列表的增删。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []          # 所有在线浏览器的 queue.Queue

    def register(self):
        """新浏览器连接，登记它的队列"""
        q = queue.Queue()
        with self._lock:
            self._clients.append(q)
        return q

    def unregister(self, q):
        """浏览器断开，移除它的队列"""
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def broadcast(self, event):
        """向所有在线浏览器推送一个事件（dict）"""
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            targets = list(self._clients)   # 拷贝后再投递，避免遍历时增删
        for q in targets:
            q.put(data)


# ==================== 业务层 ====================
class ChatRoom:
    """聊天室业务逻辑。

    在线用户表只存在于内存（服务重启即清空，符合"在线"的语义）；
    其余一切持久数据（房间设置、设备档案、消息、黑名单）通过 Database 读写。
    所有对在线用户表的访问都在 self.lock 保护下进行。
    """

    def __init__(self, db, broadcaster):
        self.db = db
        self.broadcaster = broadcaster
        self.lock = threading.RLock()
        self.online_users = {}          # user_id -> 用户信息 dict
        # 启动时从数据库恢复房间设置
        state = db.get_room_state()
        self.room_name = state['room_name']
        self.room_password = state['password']
        self.room_open = state['open']
        self.file_limit_mb = state['file_limit_mb']   # 单文件上限（MB），0 = 不限
        self.max_history = state['max_history']  # 消息保留条数
        self.recall_time_limit = state['recall_time_limit']  # 普通用户撤回时间限制（秒）
        # 同步到数据库层
        self.db.max_history = self.max_history

    # ---------- 内部工具 ----------
    def _save_state(self):
        """把当前房间设置写回数据库"""
        self.db.save_room_state(self.room_name, self.room_password, self.room_open,
                                self.file_limit_mb, self.max_history, self.recall_time_limit)

    def _system_message(self, content):
        """构造一条系统消息：入库并广播给所有人"""
        msg = {
            'id': f'{time.time()}_sys',
            'type': 'system',
            'content': content,
            'sender': '系统',
            'timestamp': time.time(),
        }
        self.db.add_message(msg)
        self.broadcaster.broadcast({'type': 'message', 'message': msg})

    def user_list_event(self):
        """构造 user_list 广播事件（普通用户与管理员分两组）"""
        with self.lock:
            normal = [{'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for u in self.online_users.values() if not u['is_admin']]
            admins = [{'user_id': uid, 'nickname': u['nickname'],
                       'avatar': u['avatar'], 'color': u['color']}
                      for uid, u in self.online_users.items() if u['is_admin']]
        return {'type': 'user_list', 'users': normal, 'admin_users': admins}

    def is_admin(self, user_id):
        """判断是否管理员；'admin_console' 是 /admin 管理台的固定哨兵 ID"""
        if user_id == 'admin_console':
            return True
        with self.lock:
            user = self.online_users.get(user_id)
            return bool(user and user['is_admin'])

    # ---------- 用户操作 ----------
    def join(self, nickname, device_id, ip, password=None, is_admin_user=False):
        """加入房间。返回 (响应字典, HTTP 状态码)。"""
        nickname = (nickname or '').strip()[:20]
        if not nickname:
            return {'error': '昵称不能为空'}, 400
        # 兜底：极老的页面没带 device_id 时，退回用 IP 充当设备标识
        device_id = device_id or ('ip:' + (ip or ''))

        with self.lock:
            #管理员分支：免房间密码、不受黑名单限制、房间关闭也能进入
            if is_admin_user and nickname.lower() == 'admin':
                # 清掉旧管理员，同一时间只保留一个在线管理员
                for uid in [u for u, i in self.online_users.items() if i['is_admin']]:
                    del self.online_users[uid]
                user_id = 'admin_' + os.urandom(4).hex()
                # 管理员使用独立 device_id 避免与同浏览器普通用户共享档案
                # 导致禁言/踢出操作互相影响（profiles/blacklist 表以 device_id 为键）
                admin_device_id = 'admin_' + device_id
                self.online_users[user_id] = {
                    'nickname': 'admin', 'device_id': admin_device_id, 'ip': ip,
                    'is_admin': True, 'muted_until': 0,
                    'avatar': random.choice(AVATARS), 
                    'color': random.choice(COLORS),
                }
                self.broadcaster.broadcast(self.user_list_event())
                self._system_message('管理员进入了房间')
                return {'success': True, 'user_id': user_id, 'nickname': 'admin',
                        'is_admin': True, 'file_limit_mb': self.file_limit_mb}, 200

            # ===== 普通用户分支 =====
            if not self.room_open:
                return {'error': '房间已关闭'}, 403

            if self.room_password and password != self.room_password:
                return {'error': '房间密码错误'}, 403
            if self.db.is_blacklisted(device_id):
                return {'error': '您已被禁止加入该房间'}, 403

            # 同设备顶号：同一浏览器重复加入时先移除旧会话（页面刷新场景）
            for uid in [u for u, i in self.online_users.items()
                        if i['device_id'] == device_id and not i['is_admin']]:
                del self.online_users[uid]

            # 昵称全局唯一（不区分大小写）
            for info in self.online_users.values():
                if info['nickname'].lower() == nickname.lower():
                    return {'error': '该昵称已被使用，请更换'}, 409
            if nickname.lower() == 'admin':
                return {'error': '该昵称已被保留，请更换'}, 409

            # 头像/颜色按设备档案保持不变；新设备随机分配
            profile = self.db.get_profile(device_id)
            if profile:
                avatar, color = profile['avatar'], profile['color']
            else:
                avatar, color = random.choice(AVATARS), random.choice(COLORS)

            user_id = 'user_' + str(time.time()) + '_' + os.urandom(4).hex()
            self.online_users[user_id] = {
                'nickname': nickname, 'device_id': device_id, 'ip': ip,
                'is_admin': False, 'muted_until': 0,
                'avatar': avatar, 'color': color,
            }
            # 新设备才建档（首次加入）；老设备只更新昵称，不动头像/颜色
            if not profile:
                self.db.save_profile(device_id, nickname, avatar, color)
            elif profile['nickname'] != nickname:
                self.db.update_profile_nickname(device_id, nickname)

        self.broadcaster.broadcast(self.user_list_event())
        self._system_message(f'{nickname} 加入了房间')
        muted_remaining = max(0, int((profile['muted_until'] if profile else 0) - time.time()))
        return {'success': True, 'user_id': user_id, 'nickname': nickname,
                'is_admin': False, 'avatar': avatar, 'color': color,
                'muted_remaining': muted_remaining,
                'file_limit_mb': self.file_limit_mb}, 200

    def leave(self, user_id):
        """离开房间（关闭页面时浏览器 sendBeacon 调用）"""
        with self.lock:
            user = self.online_users.pop(user_id, None)
        if user:
            self.broadcaster.broadcast(self.user_list_event())
            self._system_message(f"{user['nickname']} 离开了房间")
        return {'success': True}

    def send_message(self, user_id, content, msg_type, display_name=None):
        """用户发言。返回 (响应字典, HTTP 状态码)。"""
        with self.lock:
            user = self.online_users.get(user_id)
            if user is None:
                return {'error': '您不在房间中'}, 403
            # 禁言挂在设备档案上，刷新/重进后依然有效
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
            # 图片/文件消息只允许本服务上传目录内的路径，防止伪造 src 注入 XSS
            return {'error': '无效的文件地址'}, 400

        # 文件消息：大小以磁盘上的真实文件为准（不信任客户端上报），
        # 展示名由客户端提供但只取路径末段并限长
        file_name = file_size = None
        if msg_type == 'file':
            disk_path = os.path.join(UPLOAD_FOLDER, os.path.basename(content))
            if not os.path.isfile(disk_path):
                return {'error': '文件不存在或已被删除'}, 400
            file_size = os.path.getsize(disk_path)
            file_name = (os.path.basename((display_name or '').replace('\\', '/')).strip()[:100]
                         or os.path.basename(content))

        message = {
            'id': f'{time.time()}_{os.urandom(2).hex()}',
            'type': msg_type,
            'content': content,
            'sender': user['nickname'],
            'user_id': user_id,
            'is_admin': user['is_admin'],
            'avatar': user['avatar'],
            'color': user['color'],
            'timestamp': time.time(),
            'file_name': file_name,
            'file_size': file_size,
        }
        self.db.add_message(message)
        self.broadcaster.broadcast({'type': 'message', 'message': message})
        return {'success': True}, 200

    # ---------- 管理员操作 ----------
    def set_room_name(self, admin_id, name):
        """修改房间名并实时同步给所有人"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        name = (name or '').strip()
        if not name:
            return {'error': '房间名不能为空'}, 400
        self.room_name = name
        self._save_state()
        self.broadcaster.broadcast({'type': 'room_name', 'name': name})
        return {'success': True, 'name': name}, 200

    def set_room_open(self, admin_id, open_flag):
        """开放/关闭房间；关房时移除所有普通用户"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.room_open = bool(open_flag)
        self._save_state()
        if not self.room_open:
            with self.lock:
                for uid in [u for u, i in self.online_users.items() if not i['is_admin']]:
                    del self.online_users[uid]
            self.broadcaster.broadcast(self.user_list_event())
            self.broadcaster.broadcast({'type': 'room_closed'})
        else:
            self.broadcaster.broadcast({'type': 'room_opened'})
        return {'success': True, 'open': self.room_open}, 200

    def set_password(self, admin_id, password):
        """设置/清除房间密码；密码变更后踢掉所有普通用户要求重新加入"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        password = (password or '').strip()
        self.room_password = password or None
        self._save_state()
        with self.lock:
            for uid in [u for u, i in self.online_users.items() if not i['is_admin']]:
                del self.online_users[uid]
        self.broadcaster.broadcast(self.user_list_event())
        self.broadcaster.broadcast({'type': 'password_changed'})
        return {'success': True,
                'message': '密码已清除' if self.room_password is None else '密码已设置'}, 200

    def set_file_limit(self, admin_id, limit_mb):
        """设置单文件上传大小上限（MB），0 表示不限制"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if isinstance(limit_mb, bool) or not isinstance(limit_mb, (int, float)) or limit_mb < 0:
            return {'error': '无效的限额'}, 400
        self.file_limit_mb = int(limit_mb)
        self._save_state()
        self.broadcaster.broadcast({'type': 'file_limit', 'limit_mb': self.file_limit_mb})
        return {'success': True, 'limit_mb': self.file_limit_mb}, 200

    def mute_user(self, admin_id, target_id, duration):
        """禁言：挂在被禁言者的设备档案上，重进也有效"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        duration = duration or 60          # 前端取消输入框时 duration 为 null，默认 60 秒
        if not isinstance(duration, (int, float)) or duration <= 0:
            return {'error': '无效的禁言时长'}, 400
        with self.lock:
            target = self.online_users.get(target_id)
            if target is None:
                return {'error': '用户不存在'}, 404
            if target['is_admin']:
                return {'error': '不能对管理员禁言'}, 403
            device_id = target['device_id']
            target['muted_until'] = time.time() + duration
        self.db.set_muted_until(device_id, time.time() + duration)
        self.broadcaster.broadcast({'type': 'user_muted', 'user_id': target_id,
                                    'nickname': target['nickname'],
                                    'duration': duration})
        return {'success': True}, 200

    def unmute_user(self, admin_id, target_id):
        """解除禁言：将目标用户的禁言截止时间重置为 0"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if target is None:
                return {'error': '用户不存在'}, 404
            if target['is_admin']:
                return {'error': '不能对管理员操作'}, 403
            device_id = target['device_id']
            target['muted_until'] = 0
        self.db.set_muted_until(device_id, 0)
        self.broadcaster.broadcast({'type': 'user_unmuted', 'user_id': target_id,
                                    'nickname': target['nickname']})
        return {'success': True}, 200

    def get_muted_users(self, admin_id):
        """获取当前所有被禁言的用户列表"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        muted = self.db.get_muted_users()
        now = time.time()
        # 补充在线状态：若用户仍在线则附带 user_id，否则标记为离线
        with self.lock:
            online_by_device = {}
            for uid, u in self.online_users.items():
                online_by_device[u['device_id']] = uid
            result = []
            for m in muted:
                did = m['device_id']
                result.append({
                    'device_id': did,
                    'nickname': m['nickname'],
                    'avatar': m['avatar'],
                    'color': m['color'],
                    'muted_until': m['muted_until'],
                    'remaining': max(0, int(m['muted_until'] - now)),
                    'online': did in online_by_device,
                    'user_id': online_by_device.get(did),
                })
        return {'users': result}, 200

    def kick_user(self, admin_id, target_id):
        """踢出并把对方设备加入黑名单（此后换 IP 也无法再进入）"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if target is None:
                return {'error': '用户不存在'}, 404
            if target['is_admin']:
                # 防止把管理员（含自己）踢出导致设备被拉黑、无法再进入
                return {'error': '不能踢出管理员'}, 403
            del self.online_users[target_id]
        self.db.add_blacklist(target['device_id'], target['nickname'], target['ip'])
        self.broadcaster.broadcast(self.user_list_event())
        self.broadcaster.broadcast({'type': 'user_kicked', 'user_id': target_id,
                                    'nickname': target['nickname']})
        return {'success': True}, 200

    def online_list(self, admin_id):
        """管理面板的在线用户明细（含 IP、禁言剩余秒数）"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        now = time.time()
        with self.lock:
            users = [{
                'user_id': uid,
                'nickname': u['nickname'],
                'ip': u['ip'],
                'is_admin': u['is_admin'],
                'muted_remaining': max(0, int(u['muted_until'] - now)),
                'avatar': u['avatar'],
                'color': u['color'],
            } for uid, u in self.online_users.items()]
        return {'users': users}, 200

    def blacklist_list(self, admin_id):
        """查看黑名单"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        return {'items': self.db.get_blacklist()}, 200

    def blacklist_remove(self, admin_id, device_id):
        """把设备移出黑名单"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not device_id or not self.db.remove_blacklist(device_id):
            return {'error': '该设备不在黑名单中'}, 404
        return {'success': True}, 200

    def set_max_history(self, admin_id, max_history):
        """设置消息保留条数"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not isinstance(max_history, (int, float)) or max_history < 10:
            return {'error': '消息条数不能小于10'}, 400
        self.max_history = int(max_history)
        self.db.max_history = self.max_history  # 同步到数据库层
        self._save_state()
        # 立即清理超出限制的消息
        self.db.cleanup_old_messages(self.max_history)
        self.broadcaster.broadcast({'type': 'max_history', 'max_history': self.max_history})
        return {'success': True, 'max_history': self.max_history}, 200

    def clear_history(self, admin_id):
        """清空所有聊天记录"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.db.clear_all_messages()
        self.broadcaster.broadcast({'type': 'history_cleared'})
        return {'success': True}, 200

    def factory_reset(self, admin_id):
        """恢复出厂设置：清空所有数据，重置房间为初始状态"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        self.db.factory_reset()
        # 重置内存中的房间状态
        self.room_name = '在线匿名聊天室'
        self.room_password = ''
        self.room_open = True
        self.file_limit_mb = 0
        self.max_history = 200
        self.recall_time_limit = 0
        self._save_state()
        self.broadcaster.broadcast({'type': 'factory_reset'})
        return {'success': True}, 200

    def admin_delete_message(self, admin_id, message_id):
        """管理员删除单条消息（不限时间）"""
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

    def recall_message(self, user_id, message_id):
        """普通用户撤回自己的消息（限时）"""
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
        # 检查是否在撤回时间限制内
        elapsed = time.time() - (msg['timestamp'] or 0)
        if elapsed > self.recall_time_limit:
            return {'error': f'已超过{self.recall_time_limit}秒撤回时限'}, 400
        self.db.delete_message(message_id)
        self.broadcaster.broadcast({'type': 'message_recalled', 'message_id': message_id,
                                    'sender': msg['sender']})
        return {'success': True}, 200

    def set_recall_time_limit(self, admin_id, limit):
        """设置普通用户撤回时间限制（秒）"""
        if not self.is_admin(admin_id):
            return {'error': '无权限'}, 403
        if not isinstance(limit, (int, float)) or limit < 0:
            return {'error': '无效的时间限制'}, 400
        self.recall_time_limit = int(limit)
        self._save_state()
        self.broadcaster.broadcast({'type': 'recall_time_limit', 'limit': self.recall_time_limit})
        return {'success': True, 'limit': self.recall_time_limit}, 200


# ==================== Flask 应用与路由 ====================
app = Flask(__name__)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

# 全局单例：所有路由通过它们工作
db = Database(DB_FILE)
broadcaster = Broadcaster()
room = ChatRoom(db, broadcaster)


def _sync_upload_cap():
    """把文件大小额度同步为 Flask 请求体上限（None 表示不限制）。

    +1MB 余量是给 multipart 表单本身的边界开销；上传接口内部还会
    按额度对文件本身做精确校验。
    """
    app.config['MAX_CONTENT_LENGTH'] = (None if room.file_limit_mb <= 0
                                        else room.file_limit_mb * 1024 * 1024 + 1024 * 1024)


_sync_upload_cap()


def sse_data(event):
    """把事件 dict 编码成一帧 SSE 文本"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ---------- 页面 ----------
@app.route('/')
def index():
    is_admin_page = request.args.get('admin') == '1'
    return render_template('index.html', is_admin=is_admin_page,
                           room_name=room.room_name,
                           has_password=room.room_password is not None)


@app.route('/admin')
def admin_page():
    return render_template('admin.html', room_name=room.room_name)


# ---------- 用户 API ----------
@app.route('/api/join', methods=['POST'])
def api_join():
    data = request.get_json(silent=True) or {}
    result, status = room.join(
        nickname=data.get('nickname'),
        device_id=data.get('device_id'),
        ip=request.remote_addr,
        password=data.get('password'),
        is_admin_user=bool(data.get('is_admin')))
    return jsonify(result), status


@app.route('/api/leave', methods=['POST'])
def api_leave():
    # 页面关闭时 sendBeacon 以 text/plain 发送，须 force+silent 兼容空/坏请求体
    data = request.get_json(silent=True, force=True) or {}
    return jsonify(room.leave(data.get('user_id')))


@app.route('/api/send', methods=['POST'])
def api_send():
    data = request.get_json(silent=True) or {}
    result, status = room.send_message(
        data.get('user_id'), data.get('content'), data.get('type', 'text'),
        display_name=data.get('file_name'))
    return jsonify(result), status


@app.route('/api/upload_file', methods=['POST'])
def api_upload_file():
    """通用文件上传：不限文件类型；大小受管理员设置的额度约束（0 = 不限）"""
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': '文件名为空'}), 400
    # FileStorage.content_length 不可靠，用流指针取真实大小
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if room.file_limit_mb > 0 and size > room.file_limit_mb * 1024 * 1024:
        return jsonify({'error': f'文件大小不能超过 {room.file_limit_mb}MB（可在管理面板调整）'}), 400
    # 磁盘文件名只保留"点+字母数字"的安全扩展名（原始文件名通过消息的 file_name 字段展示）
    raw_ext = os.path.splitext(file.filename)[1].lower()
    ext = ('.' + re.sub(r'[^A-Za-z0-9]', '', raw_ext))[:11] if raw_ext else ''
    filename = f"{int(time.time())}_{os.urandom(4).hex()}{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    return jsonify({'success': True, 'url': f'/uploads/{filename}',
                    'filename': file.filename, 'size': size})


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    # 图片内联显示（<img> 标签可正常加载）；其余文件强制下载，
    # 防止上传的 .html/.svg 被浏览器当作页面渲染造成 XSS
    ext = os.path.splitext(filename)[1].lower()
    return send_from_directory(UPLOAD_FOLDER, filename,
                               as_attachment=ext not in IMAGE_EXTS)


@app.route('/api/messages')
def api_messages():
    """全量拉取最近消息（备用接口，正常情况走 /stream）"""
    return jsonify(db.get_recent_messages())


@app.route('/api/get_profile')
def api_get_profile():
    """按设备 ID 查档案（供加入页回填上次昵称）"""
    device_id = request.args.get('device_id')
    profile = db.get_profile(device_id) if device_id else None
    return jsonify({'profile': profile})


@app.route('/api/room_status')
def api_room_status():
    return jsonify({'open': room.room_open, 'password': room.room_password is not None,
                    'file_limit_mb': room.file_limit_mb, 'max_history': room.max_history,
                    'recall_time_limit': room.recall_time_limit})


@app.route('/api/query_messages')
def api_query_messages():
    """按昵称搜索聊天记录，按用户分组"""
    nickname = request.args.get('nickname', '').strip()
    groups = db.query_messages(nickname if nickname else None)
    return jsonify({'groups': groups})


@app.route('/api/query_files')
def api_query_files():
    """按昵称搜索聊天文件，按用户分组"""
    nickname = request.args.get('nickname', '').strip()
    groups = db.query_files(nickname if nickname else None)
    return jsonify({'groups': groups})


@app.route('/api/file_exists')
def api_file_exists():
    """检查文件是否存在于 uploads 目录"""
    url = request.args.get('url', '')
    filename = os.path.basename(url)
    exists = os.path.isfile(os.path.join(UPLOAD_FOLDER, filename))
    return jsonify({'exists': exists})


# ---------- 管理员 API ----------
@app.route('/api/admin/set_room_name', methods=['POST'])
def api_admin_set_room_name():
    data = request.get_json(silent=True) or {}
    result, status = room.set_room_name(data.get('user_id'), data.get('name'))
    return jsonify(result), status


@app.route('/api/admin/set_room_open', methods=['POST'])
def api_admin_set_room_open():
    data = request.get_json(silent=True) or {}
    result, status = room.set_room_open(data.get('user_id'), data.get('open', True))
    return jsonify(result), status


@app.route('/api/admin/set_password', methods=['POST'])
def api_admin_set_password():
    data = request.get_json(silent=True) or {}
    result, status = room.set_password(data.get('user_id'), data.get('password', ''))
    return jsonify(result), status


@app.route('/api/admin/set_file_limit', methods=['POST'])
def api_admin_set_file_limit():
    data = request.get_json(silent=True) or {}
    result, status = room.set_file_limit(data.get('user_id'), data.get('limit_mb'))
    if status == 200:
        _sync_upload_cap()
    return jsonify(result), status


@app.route('/api/admin/set_max_history', methods=['POST'])
def api_admin_set_max_history():
    data = request.get_json(silent=True) or {}
    result, status = room.set_max_history(data.get('user_id'), data.get('max_history'))
    return jsonify(result), status


@app.route('/api/admin/clear_history', methods=['POST'])
def api_admin_clear_history():
    data = request.get_json(silent=True) or {}
    result, status = room.clear_history(data.get('user_id'))
    return jsonify(result), status


@app.route('/api/admin/factory_reset', methods=['POST'])
def api_admin_factory_reset():
    data = request.get_json(silent=True) or {}
    result, status = room.factory_reset(data.get('user_id'))
    return jsonify(result), status


@app.route('/api/admin/delete_message', methods=['POST'])
def api_admin_delete_message():
    """管理员删除单条消息"""
    data = request.get_json(silent=True) or {}
    result, status = room.admin_delete_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status


@app.route('/api/recall_message', methods=['POST'])
def api_recall_message():
    """普通用户撤回自己的消息"""
    data = request.get_json(silent=True) or {}
    result, status = room.recall_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status


@app.route('/api/favorites/add', methods=['POST'])
def api_favorites_add():
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    msg_id = data.get('message_id')
    if not user_id or not msg_id:
        return jsonify({'error': '参数不完整'}), 400
    msg = room.db.get_message_by_id(msg_id)
    if not msg:
        return jsonify({'error': '消息不存在'}), 404
    room.db.add_favorite(user_id, dict(msg))
    return jsonify({'success': True}), 200


@app.route('/api/favorites/list', methods=['GET'])
def api_favorites_list():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': '缺少 user_id'}), 400
    items = room.db.get_favorites(user_id)
    return jsonify({'items': items}), 200


@app.route('/api/favorites/remove', methods=['POST'])
def api_favorites_remove():
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    fav_id = data.get('fav_id')
    if not user_id or not fav_id:
        return jsonify({'error': '参数不完整'}), 400
    ok = room.db.remove_favorite(user_id, fav_id)
    return jsonify({'success': ok}), 200 if ok else 404


@app.route('/api/admin/set_recall_time_limit', methods=['POST'])
def api_admin_set_recall_time_limit():
    """设置撤回时间限制"""
    data = request.get_json(silent=True) or {}
    result, status = room.set_recall_time_limit(data.get('user_id'), data.get('limit'))
    return jsonify(result), status


@app.route('/api/admin/mute_user', methods=['POST'])
def api_admin_mute_user():
    data = request.get_json(silent=True) or {}
    result, status = room.mute_user(data.get('admin_id'), data.get('target_id'),
                                    data.get('duration'))
    return jsonify(result), status


@app.route('/api/admin/kick_user', methods=['POST'])
def api_admin_kick_user():
    data = request.get_json(silent=True) or {}
    result, status = room.kick_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status


@app.route('/api/admin/unmute_user', methods=['POST'])
def api_admin_unmute_user():
    data = request.get_json(silent=True) or {}
    result, status = room.unmute_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status


@app.route('/api/admin/muted_users', methods=['GET'])
def api_admin_muted_users():
    result, status = room.get_muted_users(request.args.get('user_id'))
    return jsonify(result), status


@app.route('/api/admin/online_users', methods=['POST'])
def api_admin_online_users():
    data = request.get_json(silent=True) or {}
    result, status = room.online_list(data.get('admin_id'))
    return jsonify(result), status


@app.route('/api/admin/blacklist', methods=['GET'])
def api_admin_blacklist():
    result, status = room.blacklist_list(request.args.get('user_id'))
    return jsonify(result), status


@app.route('/api/admin/remove_blacklist', methods=['POST'])
def api_admin_remove_blacklist():
    data = request.get_json(silent=True) or {}
    result, status = room.blacklist_remove(data.get('user_id'), data.get('device_id'))
    return jsonify(result), status


# ---------- 数据库高级特性 API（展示用） ----------

@app.route('/api/admin/message_stats')
def api_admin_message_stats():
    """获取消息统计数据（使用存储过程和视图）"""
    stats = db.sp_get_message_stats()
    return jsonify(stats)


@app.route('/api/admin/audit_log')
def api_admin_audit_log():
    """获取审计日志（触发器自动生成的删除记录）"""
    limit = request.args.get('limit', 50, type=int)
    logs = db.sp_get_audit_log(limit)
    return jsonify({'logs': logs})


@app.route('/api/admin/test_stored_procedure', methods=['POST'])
def api_admin_test_stored_procedure():
    """测试存储过程：发送一条测试消息"""
    data = request.get_json(silent=True) or {}
    test_msg = {
        'id': f'test_{time.time()}',
        'type': 'system',
        'content': '这是一条测试消息（存储过程演示）',
        'sender': '系统',
        'timestamp': time.time(),
    }
    try:
        db.sp_send_message(test_msg)
        return jsonify({'success': True, 'message': '存储过程执行成功'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------- SSE 实时推送 ----------
@app.route('/stream')
def stream():
    """SSE 长连接：先推历史/房间名/在线列表，之后持续推送新事件"""
    def event_stream():
        q = broadcaster.register()
        try:
            yield sse_data({'type': 'history', 'messages': db.get_recent_messages()})
            yield sse_data({'type': 'room_name', 'name': room.room_name})
            yield sse_data(room.user_list_event())
            while True:
                try:
                    data = q.get(timeout=30)
                except queue.Empty:
                    yield ': ping\n\n'          # 30 秒无事件则发心跳保活
                else:
                    yield f'data: {data}\n\n'
        finally:
            broadcaster.unregister(q)

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


# ==================== 启动 ====================
if __name__ == '__main__':
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)   # 压掉逐条访问日志

    port = 5000
    url = generate_qr(port)
    ip = get_local_ip()
    print("========================================")
    print("   在线匿名聊天室已启动")
    print(f"   普通用户访问: http://{ip}:{port}")
    print(f"   管理员页面:   http://{ip}:{port}/admin")
    print(f"   二维码已生成: {url}")
    print("   按 Ctrl+C 停止服务")
    print("========================================")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
