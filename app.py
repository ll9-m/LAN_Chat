"""
在线匿名聊天室（多房间版 — 单端口 + URL 路由）

路由结构：
  /                              → 房间管理页面
  /room/<room_id>/               → 聊天页面
  /room/<room_id>/api/*          → 房间 API
  /room/<room_id>/stream         → SSE
  /room/<room_id>/uploads/*      → 文件下载

数据库：
  lan_chat.db  → rooms 表（房间注册表）
  room_<id>.db → 每个房间独立的数据（profiles/messages/blacklist/favorites...）
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
from flask import (Flask, request, Response, jsonify, render_template, g,
                   send_from_directory, stream_with_context, Blueprint)

# ==================== 常量与路径 ====================
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.path.join(BASE_DIR, 'lan_chat.db')
UPLOAD_ROOT = os.path.join(BASE_DIR, 'uploads')     # 全局上传目录（兼容旧版）
QR_DIR = os.path.join(BASE_DIR, 'static', 'qrcode')

MAX_HISTORY = 200
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
BASE_PORT = 5000

AVATARS = ['😀', '😎', '🤖', '👽', '🐱', '🐶', '🦊', '🐼', '🐸', '🐵', '🦁', '🐯']
COLORS = ['#FF5733', '#33FF57', '#3357FF', '#F333FF', '#FF33A8', '#33FFF5', '#F5FF33', '#FF8C33',
          '#8E44AD', '#2ECC71', '#E67E22', '#1ABC9C', '#E74C3C', '#3498DB', '#9B59B6', '#34495E']
SENSITIVE_WORDS = ['傻逼', '操你妈', '去死', 'fuck', 'shit', 'bitch']
UPLOAD_URL_RE = re.compile(r'/room/[^/]+/uploads/[A-Za-z0-9_.\-]+')


# ==================== 辅助函数 ====================
def filter_sensitive(text):
    for word in SENSITIVE_WORDS:
        text = text.replace(word, '*' * len(word))
    return text


def get_local_ip():
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
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(filepath)


def room_db_path(room_id):
    return os.path.join(BASE_DIR, f'room_{room_id}.db')


def room_uploads_dir(room_id):
    d = os.path.join(BASE_DIR, f'uploads_{room_id}')
    os.makedirs(d, exist_ok=True)
    return d


# ==================== 数据库：主库（房间注册表） ====================
class DatabaseManager:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
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
            """)

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql, params=()):
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    def list_rooms(self):
        return [dict(r) for r in self._query('SELECT * FROM rooms ORDER BY created_at DESC')]

    def get_room(self, room_id):
        rows = self._query('SELECT * FROM rooms WHERE room_id = ?', (room_id,))
        return dict(rows[0]) if rows else None

    def create_room(self, room_id, room_name, password=None):
        self._execute('INSERT INTO rooms(room_id, room_name, password, created_at) VALUES (?, ?, ?, ?)',
                      (room_id, room_name, password, time.time()))

    def delete_room(self, room_id):
        self._execute('DELETE FROM rooms WHERE room_id = ?', (room_id,))
        db_path = room_db_path(room_id)
        if os.path.exists(db_path): os.remove(db_path)
        up = room_uploads_dir(room_id)
        if os.path.isdir(up):
            import shutil; shutil.rmtree(up, ignore_errors=True)

    def update_room(self, room_id, **kwargs):
        if not kwargs: return
        sets = ', '.join(f'{k} = ?' for k in kwargs)
        vals = list(kwargs.values()) + [room_id]
        self._execute(f'UPDATE rooms SET {sets} WHERE room_id = ?', vals)


# ==================== 数据库：房间库 ====================
class Database:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.max_history = MAX_HISTORY
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS room_state (
                    key   TEXT PRIMARY KEY, value TEXT
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    device_id TEXT PRIMARY KEY, nickname TEXT NOT NULL,
                    avatar TEXT, color TEXT, muted_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, type TEXT, content TEXT, sender TEXT,
                    user_id TEXT, is_admin INTEGER NOT NULL DEFAULT 0,
                    avatar TEXT, color TEXT, timestamp REAL,
                    file_name TEXT, file_size INTEGER
                );
                CREATE TABLE IF NOT EXISTS blacklist (
                    device_id TEXT PRIMARY KEY, nickname TEXT,
                    ip TEXT, created_at REAL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                    msg_id TEXT, msg_type TEXT, content TEXT, sender TEXT,
                    file_name TEXT, file_size INTEGER, created_at REAL NOT NULL
                );
            """)
            cols = {r[1] for r in self._conn.execute('PRAGMA table_info(messages)')}
            if 'file_name' not in cols: self._conn.execute('ALTER TABLE messages ADD COLUMN file_name TEXT')
            if 'file_size' not in cols: self._conn.execute('ALTER TABLE messages ADD COLUMN file_size INTEGER')
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS message_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT, type TEXT, content TEXT, sender TEXT,
                    user_id TEXT, deleted_at REAL, action TEXT
                );
                CREATE TRIGGER IF NOT EXISTS trg_message_delete_audit
                AFTER DELETE ON messages BEGIN
                    INSERT INTO message_audit(message_id, type, content, sender, user_id, deleted_at, action)
                    VALUES (OLD.id, OLD.type, OLD.content, OLD.sender, OLD.user_id, strftime('%s','now'), 'DELETE');
                END;
                CREATE VIEW IF NOT EXISTS v_message_stats AS
                SELECT user_id, sender, COUNT(*) as message_count,
                       MAX(timestamp) as last_active, MIN(timestamp) as first_active
                FROM messages WHERE user_id IS NOT NULL GROUP BY user_id, sender;
                CREATE VIEW IF NOT EXISTS v_message_type_stats AS
                SELECT type, COUNT(*) as count FROM messages GROUP BY type;
            """)

    def close(self):
        with self._lock:
            try: self._conn.close()
            except Exception: pass

    def _query(self, sql, params=()):
        with self._lock: return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql, params=()):
        with self._lock, self._conn: self._conn.execute(sql, params)

    # 房间设置
    def get_room_state(self):
        rows = self._query('SELECT key, value FROM room_state')
        s = {r['key']: r['value'] for r in rows}
        return {'room_name': s.get('room_name') or '在线匿名聊天室', 'password': s.get('password'),
                'open': s.get('open', '1') == '1', 'file_limit_mb': int(s.get('file_limit_mb') or 0),
                'max_history': int(s.get('max_history') or 200),
                'recall_time_limit': int(s.get('recall_time_limit') or 300)}

    def save_room_state(self, room_name, password, is_open, file_limit_mb=0, max_history=200, recall_time_limit=300):
        pairs = [('room_name', room_name), ('password', password), ('open', '1' if is_open else '0'),
                 ('file_limit_mb', str(int(file_limit_mb))), ('max_history', str(int(max_history))),
                 ('recall_time_limit', str(int(recall_time_limit)))]
        with self._lock, self._conn:
            self._conn.executemany('REPLACE INTO room_state(key, value) VALUES (?, ?)', pairs)

    # 档案
    def get_profile(self, device_id):
        rows = self._query('SELECT * FROM profiles WHERE device_id = ?', (device_id,))
        return dict(rows[0]) if rows else None

    def save_profile(self, device_id, nickname, avatar, color):
        self._execute('INSERT INTO profiles(device_id,nickname,avatar,color,muted_until) VALUES(?,?,?,?,0) '
                      'ON CONFLICT(device_id) DO UPDATE SET nickname=excluded.nickname,avatar=excluded.avatar,color=excluded.color',
                      (device_id, nickname, avatar, color))

    def update_profile_nickname(self, device_id, nickname):
        self._execute('UPDATE profiles SET nickname=? WHERE device_id=?', (nickname, device_id))

    def set_muted_until(self, device_id, muted_until):
        self._execute('UPDATE profiles SET muted_until=? WHERE device_id=?', (muted_until, device_id))

    def get_muted_users(self):
        return [dict(r) for r in self._query(
            'SELECT device_id,nickname,avatar,color,muted_until FROM profiles WHERE muted_until>?', (time.time(),))]

    # 查询
    def query_messages(self, nickname=None):
        if nickname:
            rows = self._query('SELECT * FROM messages WHERE sender LIKE ? AND type=? ORDER BY timestamp ASC', (f'%{nickname}%', 'text'))
        else:
            rows = self._query('SELECT * FROM messages WHERE type=? ORDER BY timestamp ASC', ('text',))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            groups.setdefault(msg['sender'], {'sender': msg['sender'], 'avatar': msg['avatar'], 'color': msg['color'], 'messages': []})['messages'].append(msg)
        return list(groups.values())

    def query_files(self, nickname=None, upload_dir=None):
        if nickname:
            rows = self._query('SELECT * FROM messages WHERE sender LIKE ? AND type IN (?,?) ORDER BY timestamp ASC', (f'%{nickname}%', 'image', 'file'))
        else:
            rows = self._query('SELECT * FROM messages WHERE type IN (?,?) ORDER BY timestamp ASC', ('image', 'file'))
        groups = {}
        for r in rows:
            msg = self._row_to_message(r)
            fn = os.path.basename(msg.get('content', ''))
            msg['file_exists'] = os.path.isfile(os.path.join(upload_dir or UPLOAD_ROOT, fn))
            groups.setdefault(msg['sender'], {'sender': msg['sender'], 'avatar': msg['avatar'], 'color': msg['color'], 'messages': []})['messages'].append(msg)
        return list(groups.values())

    def _row_to_message(self, r):
        return {'id': r['id'], 'type': r['type'], 'content': r['content'], 'sender': r['sender'],
                'user_id': r['user_id'], 'is_admin': bool(r['is_admin']), 'avatar': r['avatar'],
                'color': r['color'], 'timestamp': r['timestamp'], 'file_name': r['file_name'], 'file_size': r['file_size']}

    def get_recent_messages(self, limit=None):
        rows = self._query('SELECT * FROM messages ORDER BY rowid DESC LIMIT ?', (limit or self.max_history,))
        return [self._row_to_message(r) for r in reversed(rows)]

    def add_message(self, msg):
        with self._lock, self._conn:
            self._conn.execute('INSERT OR REPLACE INTO messages(id,type,content,sender,user_id,is_admin,avatar,color,timestamp,file_name,file_size) '
                               'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (msg['id'], msg.get('type'), msg.get('content'), msg.get('sender'),
                                msg.get('user_id'), int(msg.get('is_admin', False)), msg.get('avatar'),
                                msg.get('color'), msg.get('timestamp'), msg.get('file_name'), msg.get('file_size')))

    # 黑名单
    def is_blacklisted(self, device_id):
        return bool(self._query('SELECT 1 FROM blacklist WHERE device_id=?', (device_id,)))

    def add_blacklist(self, device_id, nickname, ip):
        self._execute('INSERT OR REPLACE INTO blacklist(device_id,nickname,ip,created_at) VALUES(?,?,?,?)',
                      (device_id, nickname, ip, time.time()))

    def remove_blacklist(self, device_id):
        with self._lock, self._conn:
            return self._conn.execute('DELETE FROM blacklist WHERE device_id=?', (device_id,)).rowcount > 0

    def get_blacklist(self):
        return [dict(r) for r in self._query('SELECT device_id,nickname,ip,created_at FROM blacklist ORDER BY created_at DESC')]

    # 收藏夹
    def add_favorite(self, user_id, msg):
        with self._lock, self._conn:
            self._conn.execute('INSERT INTO favorites(user_id,msg_id,msg_type,content,sender,file_name,file_size,created_at) VALUES(?,?,?,?,?,?,?,?)',
                               (user_id, msg.get('id'), msg.get('type'), msg.get('content'), msg.get('sender'), msg.get('file_name'), msg.get('file_size'), time.time()))

    def get_favorites(self, user_id):
        return [dict(r) for r in self._query('SELECT id,msg_id,msg_type,content,sender,file_name,file_size,created_at FROM favorites WHERE user_id=? ORDER BY created_at DESC', (user_id,))]

    def remove_favorite(self, user_id, fav_id):
        with self._lock, self._conn:
            return self._conn.execute('DELETE FROM favorites WHERE id=? AND user_id=?', (fav_id, user_id)).rowcount > 0

    # 清理
    def cleanup_old_messages(self, max_history):
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages WHERE rowid NOT IN (SELECT rowid FROM messages ORDER BY rowid DESC LIMIT ?)', (max_history,))

    def clear_all_messages(self):
        with self._lock, self._conn: self._conn.execute('DELETE FROM messages')

    def factory_reset(self):
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM messages')
            self._conn.execute('DELETE FROM profiles')
            self._conn.execute('DELETE FROM blacklist')
            self._conn.execute('DELETE FROM favorites')
            self._conn.execute("REPLACE INTO room_state(key,value) VALUES('room_name','在线匿名聊天室'),('open','1'),('password',NULL),"
                               "('file_limit_mb','0'),('max_history','200'),('recall_time_limit','0')")

    def delete_message(self, message_id):
        with self._lock, self._conn:
            return self._conn.execute('DELETE FROM messages WHERE id=?', (message_id,)).rowcount > 0

    def get_message_by_id(self, message_id):
        rows = self._query('SELECT * FROM messages WHERE id=?', (message_id,))
        return dict(rows[0]) if rows else None

    def sp_get_message_stats(self):
        total = self._query('SELECT COUNT(*) as cnt FROM messages')[0]['cnt']
        user_stats = [dict(r) for r in self._query('SELECT * FROM v_message_stats ORDER BY message_count DESC')]
        type_stats = [dict(r) for r in self._query('SELECT * FROM v_message_type_stats ORDER BY count DESC')]
        for t in type_stats:
            t['percentage'] = round(t['count'] / total * 100, 1) if total else 0
        return {'total_messages': total, 'user_stats': user_stats, 'type_stats': type_stats}

    def sp_get_audit_log(self, limit=50):
        return [dict(r) for r in self._query('SELECT * FROM message_audit ORDER BY audit_id DESC LIMIT ?', (limit,))]


# ==================== 广播层 ====================
class Broadcaster:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []

    def register(self):
        q = queue.Queue()
        with self._lock: self._clients.append(q)
        return q

    def unregister(self, q):
        with self._lock:
            if q in self._clients: self._clients.remove(q)

    def broadcast(self, event):
        data = json.dumps(event, ensure_ascii=False)
        with self._lock: targets = list(self._clients)
        for q in targets: q.put(data)


# ==================== 业务层 ====================
class ChatRoom:
    def __init__(self, db, broadcaster, uploads_dir):
        self.db = db
        self.broadcaster = broadcaster
        self.uploads_dir = uploads_dir
        self.lock = threading.RLock()
        self.online_users = {}
        state = db.get_room_state()
        self.room_name = state['room_name']
        self.room_password = state['password']
        self.room_open = state['open']
        self.file_limit_mb = state['file_limit_mb']
        self.max_history = state['max_history']
        self.recall_time_limit = state['recall_time_limit']
        self.db.max_history = self.max_history

    def _save_state(self):
        self.db.save_room_state(self.room_name, self.room_password, self.room_open,
                                self.file_limit_mb, self.max_history, self.recall_time_limit)

    def _system_message(self, content):
        msg = {'id': f'{time.time()}_sys', 'type': 'system', 'content': content, 'sender': '系统', 'timestamp': time.time()}
        self.db.add_message(msg)
        self.broadcaster.broadcast({'type': 'message', 'message': msg})

    def user_list_event(self):
        with self.lock:
            normal = [{'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for u in self.online_users.values() if not u['is_admin']]
            admins = [{'user_id': uid, 'nickname': u['nickname'], 'avatar': u['avatar'], 'color': u['color']}
                      for uid, u in self.online_users.items() if u['is_admin']]
        return {'type': 'user_list', 'users': normal, 'admin_users': admins}

    def is_admin(self, user_id):
        if user_id == 'admin_console': return True
        with self.lock:
            user = self.online_users.get(user_id)
            return user is not None and user.get('is_admin')

    def join(self, nickname, device_id, ip, password=None, is_admin_user=False):
        nickname = (nickname or '').strip()
        if not nickname or not device_id: return {'error': '昵称和设备ID不能为空'}, 400
        if not self.room_open and not is_admin_user: return {'error': '房间已关闭'}, 403
        if self.room_password and password != self.room_password and not is_admin_user: return {'error': '密码错误'}, 403
        if self.db.is_blacklisted(device_id) and not is_admin_user: return {'error': '您已被加入黑名单'}, 403
        with self.lock:
            for uid, u in self.online_users.items():
                if u['nickname'] == nickname and uid != (f'admin_{device_id}' if is_admin_user else None):
                    return {'error': '昵称已被占用'}, 400
        admin_device_id = f'admin_{device_id}' if is_admin_user else device_id
        target_device_id = admin_device_id if is_admin_user else device_id
        existing = self.db.get_profile(target_device_id)
        if not existing:
            avatar, color = random.choice(AVATARS), random.choice(COLORS)
            self.db.save_profile(target_device_id, nickname, avatar, color)
        else:
            avatar, color = existing['avatar'], existing['color']
            if existing['nickname'] != nickname:
                self.db.update_profile_nickname(target_device_id, nickname)
        user_id = f'admin_{os.urandom(4).hex()}' if is_admin_user else f'user_{time.time()}_{os.urandom(2).hex()}'
        user = {'user_id': user_id, 'nickname': nickname, 'device_id': target_device_id,
                'avatar': avatar, 'color': color, 'is_admin': is_admin_user, 'ip': ip}
        with self.lock: self.online_users[user_id] = user
        self.broadcaster.broadcast(self.user_list_event())
        profile = self.db.get_profile(target_device_id)
        muted_remaining = max(0, int(profile['muted_until'] - time.time())) if profile else 0
        return {'success': True, 'user_id': user_id, 'nickname': nickname,
                'avatar': avatar, 'color': color, 'muted_remaining': muted_remaining}, 200

    def leave(self, user_id):
        with self.lock: user = self.online_users.pop(user_id, None)
        if user: self.broadcaster.broadcast(self.user_list_event())
        return {'success': True}

    def send_message(self, user_id, content, msg_type, display_name=None):
        with self.lock:
            user = self.online_users.get(user_id)
            if user is None: return {'error': '您不在房间中'}, 403
            if not self.room_open: return {'error': '房间已关闭'}, 403
            profile = self.db.get_profile(user['device_id'])
            muted_until = profile['muted_until'] if profile else 0
        if muted_until > time.time(): return {'error': f'您已被禁言，剩余 {int(muted_until - time.time())} 秒'}, 403
        content = (content or '').strip()
        if msg_type not in ('text', 'image', 'file'): return {'error': '不支持的消息类型'}, 400
        if not content: return {'error': '消息不能为空'}, 400
        if msg_type == 'text':
            content = filter_sensitive(content)
        elif not UPLOAD_URL_RE.fullmatch(content):
            return {'error': '无效的文件地址'}, 400
        file_name = file_size = None
        if msg_type == 'file':
            disk_path = os.path.join(self.uploads_dir, os.path.basename(content))
            if not os.path.isfile(disk_path): return {'error': '文件不存在或已被删除'}, 400
            file_size = os.path.getsize(disk_path)
            file_name = (os.path.basename((display_name or '').replace('\\', '/')).strip()[:100] or os.path.basename(content))
        message = {'id': f'{time.time()}_{os.urandom(2).hex()}', 'type': msg_type, 'content': content,
                   'sender': user['nickname'], 'user_id': user_id, 'is_admin': user['is_admin'],
                   'avatar': user['avatar'], 'color': user['color'], 'timestamp': time.time(),
                   'file_name': file_name, 'file_size': file_size}
        self.db.add_message(message)
        self.broadcaster.broadcast({'type': 'message', 'message': message})
        return {'success': True}, 200

    def set_room_name(self, admin_id, name):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        name = (name or '').strip()
        if not name: return {'error': '名称不能为空'}, 400
        self.room_name = name; self._save_state()
        self.broadcaster.broadcast({'type': 'room_name', 'name': self.room_name})
        return {'success': True}, 200

    def set_room_open(self, admin_id, is_open):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        self.room_open = bool(is_open); self._save_state()
        if not self.room_open:
            self.broadcaster.broadcast({'type': 'room_closed'})
        return {'success': True, 'open': self.room_open}, 200

    def set_password(self, admin_id, password):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        self.room_password = password or None; self._save_state()
        return {'success': True}, 200

    def set_file_limit(self, admin_id, limit_mb):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        self.file_limit_mb = max(0, int(limit_mb or 0)); self._save_state()
        return {'success': True, 'file_limit_mb': self.file_limit_mb}, 200

    def set_max_history(self, admin_id, max_history):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        if not isinstance(max_history, (int, float)) or max_history < 10: return {'error': '消息条数不能小于10'}, 400
        self.max_history = int(max_history); self.db.max_history = self.max_history
        self._save_state(); self.db.cleanup_old_messages(self.max_history)
        self.broadcaster.broadcast({'type': 'max_history', 'max_history': self.max_history})
        return {'success': True, 'max_history': self.max_history}, 200

    def clear_history(self, admin_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        self.db.clear_all_messages(); self.broadcaster.broadcast({'type': 'history_cleared'})
        return {'success': True}, 200

    def factory_reset(self, admin_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        self.db.factory_reset()
        self.room_name = '在线匿名聊天室'; self.room_password = None; self.room_open = True
        self.file_limit_mb = 0; self.max_history = 200; self.recall_time_limit = 0
        self._save_state(); self.broadcaster.broadcast({'type': 'factory_reset'})
        return {'success': True}, 200

    def admin_delete_message(self, admin_id, message_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        if not message_id: return {'error': '消息ID不能为空'}, 400
        msg = self.db.get_message_by_id(message_id)
        if not msg: return {'error': '消息不存在'}, 404
        self.db.delete_message(message_id)
        self.broadcaster.broadcast({'type': 'message_deleted', 'message_id': message_id})
        return {'success': True}, 200

    def recall_message(self, user_id, message_id):
        if not user_id or not message_id: return {'error': '参数不完整'}, 400
        with self.lock:
            user = self.online_users.get(user_id)
            if user is None: return {'error': '您不在房间中'}, 403
        msg = self.db.get_message_by_id(message_id)
        if not msg: return {'error': '消息不存在'}, 404
        if msg['user_id'] != user_id: return {'error': '只能撤回自己的消息'}, 403
        if msg['type'] == 'system': return {'error': '系统消息不能撤回'}, 403
        elapsed = time.time() - (msg['timestamp'] or 0)
        if elapsed > self.recall_time_limit: return {'error': f'已超过{self.recall_time_limit}秒撤回时限'}, 400
        self.db.delete_message(message_id)
        self.broadcaster.broadcast({'type': 'message_recalled', 'message_id': message_id, 'sender': msg['sender']})
        return {'success': True}, 200

    def set_recall_time_limit(self, admin_id, limit):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        if not isinstance(limit, (int, float)) or limit < 0: return {'error': '无效的时间限制'}, 400
        self.recall_time_limit = int(limit); self._save_state()
        self.broadcaster.broadcast({'type': 'recall_time_limit', 'limit': self.recall_time_limit})
        return {'success': True, 'limit': self.recall_time_limit}, 200

    def mute_user(self, admin_id, target_id, duration):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if not target: return {'error': '用户不在线'}, 404
        muted_until = time.time() + max(1, int(duration or 60))
        self.db.set_muted_until(target['device_id'], muted_until)
        self.broadcaster.broadcast({'type': 'user_muted', 'nickname': target['nickname'], 'muted_until': muted_until, 'duration': int(duration or 60)})
        return {'success': True}, 200

    def unmute_user(self, admin_id, target_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if not target: return {'error': '用户不在线'}, 404
        self.db.set_muted_until(target['device_id'], 0)
        self.broadcaster.broadcast({'type': 'user_unmuted', 'nickname': target['nickname']})
        return {'success': True}, 200

    def get_muted_users(self, admin_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        raw = self.db.get_muted_users()
        now = time.time()
        for u in raw:
            u['remaining'] = max(0, int(u['muted_until'] - now))
            # 查找 user_id 和在线状态
            u['user_id'] = None
            u['online'] = False
            for uid, info in self.online_users.items():
                if info.get('device_id') == u['device_id']:
                    u['user_id'] = uid
                    u['online'] = True
                    break
        return {'users': raw}, 200

    def kick_user(self, admin_id, target_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        with self.lock:
            target = self.online_users.get(target_id)
            if not target: return {'error': '用户不在线'}, 404
            if target['is_admin']: return {'error': '不能踢出管理员'}, 400
            del self.online_users[target_id]
        self.db.add_blacklist(target['device_id'], target['nickname'], target.get('ip', ''))
        self.broadcaster.broadcast({'type': 'user_kicked', 'nickname': target['nickname']})
        self.broadcaster.broadcast(self.user_list_event())
        return {'success': True}, 200

    def online_list(self, admin_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
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
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        return {'items': self.db.get_blacklist()}, 200

    def blacklist_remove(self, admin_id, device_id):
        if not self.is_admin(admin_id): return {'error': '无权限'}, 403
        if not device_id: return {'error': 'device_id 不能为空'}, 400
        if not self.db.remove_blacklist(device_id): return {'error': '该设备不在黑名单中'}, 404
        return {'success': True}, 200


# ==================== 房间管理器 ====================
class RoomManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._rooms = {}  # room_id -> ChatRoom

    def get_or_create(self, room_id):
        with self._lock:
            if room_id in self._rooms:
                return self._rooms[room_id]
            # 在锁内创建，避免竞态
            room_info = db_manager.get_room(room_id)
            if not room_info: return None
            db_path = room_db_path(room_id)
            uploads_dir = room_uploads_dir(room_id)
            os.makedirs(uploads_dir, exist_ok=True)
            _db = Database(db_path)
            _broadcaster = Broadcaster()
            _room = ChatRoom(_db, _broadcaster, uploads_dir)
            # 用 rooms 表的设置覆盖 room_state
            _room.room_open = bool(room_info['is_open'])
            _room.room_password = room_info['password']
            _room.file_limit_mb = room_info['file_limit_mb']
            _room.max_history = room_info['max_history']
            _room.recall_time_limit = room_info['recall_time_limit']
            self._rooms[room_id] = _room
            return _room

    def get_room_instance(self, room_id):
        with self._lock:
            return self._rooms.get(room_id)


# ==================== Flask 应用 ====================
app = Flask(__name__)
os.makedirs(UPLOAD_ROOT, exist_ok=True)
os.makedirs(QR_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

db_manager = DatabaseManager(DB_FILE)
room_manager = RoomManager()


def sse_data(event):
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ---------- 主页面 ----------
@app.route('/')
def rooms_list():
    rooms = db_manager.list_rooms()
    ip = get_local_ip()
    return render_template('rooms.html', rooms=rooms, ip=ip)


@app.route('/room/<room_id>/')
def room_page(room_id):
    room_info = db_manager.get_room(room_id)
    if not room_info: return '房间不存在', 404
    room_instance = room_manager.get_or_create(room_id)
    if not room_instance: return '房间初始化失败', 500
    is_admin_page = request.args.get('admin') == '1'
    return render_template('index.html', is_admin=is_admin_page,
                           room_name=room_instance.room_name,
                           has_password=room_instance.room_password is not None,
                           room_id=room_id)


@app.route('/admin')
def admin_redirect():
    return render_template('admin.html')


# ---------- 房间管理 API ----------
@app.route('/api/rooms')
def api_list_rooms():
    return jsonify({'rooms': db_manager.list_rooms()})

@app.route('/api/rooms/create', methods=['POST'])
def api_create_room():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '新房间').strip()
    pw = data.get('password')
    room_id = f'room_{int(time.time())}'
    db_manager.create_room(room_id, name, pw)
    room_manager.get_or_create(room_id)
    ip = get_local_ip()
    generate_qr(f'http://{ip}:5000/room/{room_id}/', os.path.join(QR_DIR, f'{room_id}.png'))
    return jsonify({'success': True, 'room_id': room_id})

@app.route('/api/rooms/delete', methods=['POST'])
def api_delete_room():
    data = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    if not room_id: return jsonify({'error': '缺少 room_id'}), 400
    # 先关闭房间实例的 DB 连接，再删除文件
    inst = room_manager.get_room_instance(room_id)
    if inst:
        with room_manager._lock: room_manager._rooms.pop(room_id, None)
        try: inst.db.close()
        except Exception: pass
    db_manager.delete_room(room_id)
    qr = os.path.join(QR_DIR, f'{room_id}.png')
    if os.path.exists(qr): os.remove(qr)
    # 如果没有房间了，自动重建默认房间
    if not db_manager.list_rooms():
        db_manager.create_room('room_default', '在线匿名聊天室')
        room_manager.get_or_create('room_default')
        ip = get_local_ip()
        generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/', os.path.join(QR_DIR, 'room_default.png'))
    return jsonify({'success': True})

@app.route('/api/rooms/toggle', methods=['POST'])
def api_toggle_room():
    data = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    if not room_id: return jsonify({'error': '缺少 room_id'}), 400
    info = db_manager.get_room(room_id)
    if not info: return jsonify({'error': '房间不存在'}), 404
    new_open = not info['is_open']
    db_manager.update_room(room_id, is_open=int(new_open))
    # 同步到内存中的 ChatRoom 实例
    inst = room_manager.get_room_instance(room_id)
    if inst: inst.room_open = new_open
    return jsonify({'success': True, 'is_open': new_open})

@app.route('/api/rooms/status')
def api_rooms_status():
    rooms = db_manager.list_rooms()
    return jsonify({'rooms': [{'room_id': r['room_id'], 'room_name': r['room_name'],
                               'is_open': r['is_open'], 'port': BASE_PORT} for r in rooms]})

@app.route('/api/rooms/qr/<room_id>')
def api_room_qr(room_id):
    qr_path = os.path.join(QR_DIR, f'{room_id}.png')
    if not os.path.exists(qr_path):
        info = db_manager.get_room(room_id)
        if not info: return '', 404
        ip = get_local_ip()
        generate_qr(f'http://{ip}:{BASE_PORT}/room/{room_id}/', qr_path)
    return send_from_directory(QR_DIR, f'{room_id}.png')


# ==================== 房间路由（Blueprint） ====================
room_bp = Blueprint('room', __name__, url_prefix='/room/<room_id>')


def _room_context(room_id):
    """before_request: 加载当前房间实例到 g.room，并从 view_args 中移除 room_id"""
    g.room_id = room_id
    g.room = room_manager.get_or_create(room_id)
    if not g.room:
        return jsonify({'error': '房间不存在'}), 404
    # 从 view_args 中移除 room_id，避免传给视图函数
    if request.view_args and 'room_id' in request.view_args:
        request.view_args.pop('room_id')


room_bp.before_request(lambda: _room_context(request.view_args.get('room_id', '') if request.view_args else ''))


def _sync_upload_cap(room):
    if room.file_limit_mb <= 0:
        app.config['MAX_CONTENT_LENGTH'] = None
    else:
        app.config['MAX_CONTENT_LENGTH'] = room.file_limit_mb * 1024 * 1024 + 1024 * 1024


# ---------- 用户 API ----------
@room_bp.route('/api/join', methods=['POST'])
def api_join():
    data = request.get_json(silent=True) or {}
    result, status = g.room.join(data.get('nickname'), data.get('device_id'),
                                  request.remote_addr, data.get('password'), bool(data.get('is_admin')))
    return jsonify(result), status

@room_bp.route('/api/leave', methods=['POST'])
def api_leave():
    data = request.get_json(silent=True, force=True) or {}
    return jsonify(g.room.leave(data.get('user_id')))

@room_bp.route('/api/send', methods=['POST'])
def api_send():
    data = request.get_json(silent=True) or {}
    result, status = g.room.send_message(data.get('user_id'), data.get('content'),
                                          data.get('type', 'text'), data.get('file_name'))
    return jsonify(result), status

@room_bp.route('/api/upload_file', methods=['POST'])
def api_upload_file():
    if 'file' not in request.files: return jsonify({'error': '没有文件'}), 400
    file = request.files['file']
    if not file.filename: return jsonify({'error': '文件名为空'}), 400
    file.stream.seek(0, os.SEEK_END); size = file.stream.tell(); file.stream.seek(0)
    if g.room.file_limit_mb > 0 and size > g.room.file_limit_mb * 1024 * 1024:
        return jsonify({'error': f'文件大小不能超过 {g.room.file_limit_mb}MB'}), 400
    raw_ext = os.path.splitext(file.filename)[1].lower()
    ext = ('.' + re.sub(r'[^A-Za-z0-9]', '', raw_ext))[:11] if raw_ext else ''
    filename = f"{int(time.time())}_{os.urandom(4).hex()}{ext}"
    file.save(os.path.join(g.room.uploads_dir, filename))
    # 返回的 URL 带 room_id 前缀
    return jsonify({'success': True, 'url': f'/room/{g.room_id}/uploads/{filename}',
                    'filename': file.filename, 'size': size})

@room_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    ext = os.path.splitext(filename)[1].lower()
    return send_from_directory(g.room.uploads_dir, filename, as_attachment=ext not in IMAGE_EXTS)

@room_bp.route('/api/messages')
def api_messages():
    return jsonify(g.room.db.get_recent_messages())

@room_bp.route('/api/get_profile')
def api_get_profile():
    device_id = request.args.get('device_id')
    profile = g.room.db.get_profile(device_id) if device_id else None
    return jsonify({'profile': profile})

@room_bp.route('/api/room_status')
def api_room_status():
    return jsonify({'open': g.room.room_open, 'password': g.room.room_password is not None,
                    'file_limit_mb': g.room.file_limit_mb, 'max_history': g.room.max_history,
                    'recall_time_limit': g.room.recall_time_limit})

@room_bp.route('/api/query_messages')
def api_query_messages():
    nickname = request.args.get('nickname', '').strip()
    return jsonify({'groups': g.room.db.query_messages(nickname if nickname else None)})

@room_bp.route('/api/query_files')
def api_query_files():
    nickname = request.args.get('nickname', '').strip()
    return jsonify({'groups': g.room.db.query_files(nickname if nickname else None, g.room.uploads_dir)})

@room_bp.route('/api/file_exists')
def api_file_exists():
    url = request.args.get('url', '')
    filename = os.path.basename(url)
    exists = os.path.isfile(os.path.join(g.room.uploads_dir, filename))
    return jsonify({'exists': exists})

# ---------- 收藏夹 ----------
@room_bp.route('/api/favorites/add', methods=['POST'])
def api_favorites_add():
    data = request.get_json(silent=True) or {}
    user_id, msg_id = data.get('user_id'), data.get('message_id')
    if not user_id or not msg_id: return jsonify({'error': '参数不完整'}), 400
    msg = g.room.db.get_message_by_id(msg_id)
    if not msg: return jsonify({'error': '消息不存在'}), 404
    g.room.db.add_favorite(user_id, dict(msg))
    return jsonify({'success': True}), 200

@room_bp.route('/api/favorites/list', methods=['GET'])
def api_favorites_list():
    user_id = request.args.get('user_id')
    if not user_id: return jsonify({'error': '缺少 user_id'}), 400
    return jsonify({'items': g.room.db.get_favorites(user_id)}), 200

@room_bp.route('/api/favorites/remove', methods=['POST'])
def api_favorites_remove():
    data = request.get_json(silent=True) or {}
    user_id, fav_id = data.get('user_id'), data.get('fav_id')
    if not user_id or not fav_id: return jsonify({'error': '参数不完整'}), 400
    ok = g.room.db.remove_favorite(user_id, fav_id)
    return jsonify({'success': ok}), 200 if ok else 404

# ---------- 管理员 API ----------
@room_bp.route('/api/admin/set_room_name', methods=['POST'])
def api_admin_set_room_name():
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_room_name(data.get('user_id'), data.get('name'))
    if status == 200: db_manager.update_room(g.room_id, room_name=g.room.room_name)
    return jsonify(result), status

@room_bp.route('/api/admin/set_room_open', methods=['POST'])
def api_admin_set_room_open():
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_room_open(data.get('user_id'), data.get('open', True))
    if status == 200: db_manager.update_room(g.room_id, is_open=int(g.room.room_open))
    return jsonify(result), status

@room_bp.route('/api/admin/set_password', methods=['POST'])
def api_admin_set_password():
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_password(data.get('user_id'), data.get('password', ''))
    if status == 200: db_manager.update_room(g.room_id, password=g.room.room_password)
    return jsonify(result), status

@room_bp.route('/api/admin/set_file_limit', methods=['POST'])
def api_admin_set_file_limit():
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_file_limit(data.get('user_id'), data.get('limit_mb'))
    if status == 200:
        _sync_upload_cap(g.room)
        db_manager.update_room(g.room_id, file_limit_mb=g.room.file_limit_mb)
    return jsonify(result), status

@room_bp.route('/api/admin/set_max_history', methods=['POST'])
def api_admin_set_max_history():
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_max_history(data.get('user_id'), data.get('max_history'))
    if status == 200: db_manager.update_room(g.room_id, max_history=g.room.max_history)
    return jsonify(result), status

@room_bp.route('/api/admin/clear_history', methods=['POST'])
def api_admin_clear_history():
    data = request.get_json(silent=True) or {}
    result, status = g.room.clear_history(data.get('user_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/factory_reset', methods=['POST'])
def api_admin_factory_reset():
    data = request.get_json(silent=True) or {}
    result, status = g.room.factory_reset(data.get('user_id'))
    if status == 200:
        db_manager.update_room(g.room_id, room_name=g.room.room_name, is_open=int(g.room.room_open),
                               password=g.room.room_password, file_limit_mb=g.room.file_limit_mb,
                               max_history=g.room.max_history, recall_time_limit=g.room.recall_time_limit)
    return jsonify(result), status

@room_bp.route('/api/admin/delete_message', methods=['POST'])
def api_admin_delete_message():
    data = request.get_json(silent=True) or {}
    result, status = g.room.admin_delete_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status

@room_bp.route('/api/recall_message', methods=['POST'])
def api_recall_message():
    data = request.get_json(silent=True) or {}
    result, status = g.room.recall_message(data.get('user_id'), data.get('message_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/mute_user', methods=['POST'])
def api_admin_mute_user():
    data = request.get_json(silent=True) or {}
    result, status = g.room.mute_user(data.get('admin_id'), data.get('target_id'), data.get('duration'))
    return jsonify(result), status

@room_bp.route('/api/admin/unmute_user', methods=['POST'])
def api_admin_unmute_user():
    data = request.get_json(silent=True) or {}
    result, status = g.room.unmute_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/muted_users', methods=['GET'])
def api_admin_muted_users():
    result, status = g.room.get_muted_users(request.args.get('user_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/kick_user', methods=['POST'])
def api_admin_kick_user():
    data = request.get_json(silent=True) or {}
    result, status = g.room.kick_user(data.get('admin_id'), data.get('target_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/online_users', methods=['POST'])
def api_admin_online_users():
    data = request.get_json(silent=True) or {}
    result, status = g.room.online_list(data.get('admin_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/blacklist', methods=['GET'])
def api_admin_blacklist():
    result, status = g.room.blacklist_list(request.args.get('user_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/remove_blacklist', methods=['POST'])
def api_admin_remove_blacklist():
    data = request.get_json(silent=True) or {}
    result, status = g.room.blacklist_remove(data.get('user_id'), data.get('device_id'))
    return jsonify(result), status

@room_bp.route('/api/admin/set_recall_time_limit', methods=['POST'])
def api_admin_set_recall_time_limit():
    data = request.get_json(silent=True) or {}
    result, status = g.room.set_recall_time_limit(data.get('user_id'), data.get('limit'))
    if status == 200: db_manager.update_room(g.room_id, recall_time_limit=g.room.recall_time_limit)
    return jsonify(result), status

@room_bp.route('/api/admin/message_stats')
def api_admin_message_stats():
    return jsonify(g.room.db.sp_get_message_stats())

@room_bp.route('/api/admin/audit_log')
def api_admin_audit_log():
    limit = request.args.get('limit', 50, type=int)
    return jsonify({'logs': g.room.db.sp_get_audit_log(limit)})


# ---------- SSE ----------
@room_bp.route('/stream')
def stream():
    def event_stream():
        q = g.room.broadcaster.register()
        try:
            yield sse_data({'type': 'history', 'messages': g.room.db.get_recent_messages()})
            yield sse_data({'type': 'room_name', 'name': g.room.room_name})
            yield sse_data(g.room.user_list_event())
            while True:
                try: data = q.get(timeout=30)
                except queue.Empty: yield ': ping\n\n'
                else: yield f'data: {data}\n\n'
        finally:
            g.room.broadcaster.unregister(q)
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


# 注册蓝图
app.register_blueprint(room_bp)


# ==================== 启动 ====================
if __name__ == '__main__':
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    ip = get_local_ip()
    # 初始化默认房间
    if not db_manager.list_rooms():
        db_manager.create_room('room_default', '在线匿名聊天室')
        room_manager.get_or_create('room_default')
        generate_qr(f'http://{ip}:{BASE_PORT}/room/room_default/', os.path.join(QR_DIR, 'room_default.png'))

    print("========================================")
    print("   在线匿名聊天室（多房间版）已启动")
    print(f"   管理页面: http://{ip}:{BASE_PORT}/")
    for r in db_manager.list_rooms():
        print(f"   房间 [{r['room_name']}]: http://{ip}:{BASE_PORT}/room/{r['room_id']}/")
    print("   按 Ctrl+C 停止服务")
    print("========================================")
    app.run(host='0.0.0.0', port=BASE_PORT, threaded=True, debug=False)
