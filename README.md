# LAN_Chat — 在线匿名聊天室（多房间版）

基于 **Flask + MySQL 8.0** 的局域网多房间匿名聊天系统，数据库原理与应用课程设计项目。

> 本项目已从早期 SQLite 方案迁移到 MySQL：全部房间共用一个库 `lan_chat`，以 `room_id` 区分作用域；
> 完整 DDL（约束 / 索引 / 视图 / 触发器 / 存储过程）见 [`sql/schema.sql`](sql/schema.sql)。

---

## 功能特性

| 模块 | 能力 |
| --- | --- |
| 双角色登录 | 普通用户免密进入；管理员需密码（默认 `ADMIN`，可在管理页修改） |
| 多房间 | 创建 / 删除 / 开关 / 独立密码 / 独立上传目录 / 独立二维码 |
| 实时聊天 | SSE 推送消息、用户列表、房间事件（30s 心跳保活） |
| 消息类型 | 文本（敏感词过滤）、图片、文件（可设单文件限额） |
| 用户管理 | 禁言（倒计时）、踢出 + 黑名单、在线列表（含 IP） |
| 消息管理 | 限时撤回、管理员强制删除、清空记录、房间级恢复出厂 |
| 数据查询 | 存储过程统计（总数/按用户/按类型）、删除审计日志、按昵称搜索 |
| 收藏夹 | 快照入库，原消息删除后仍可查看；防重复收藏（UNIQUE 约束） |
| 全局恢复出厂 | 一键重置所有房间、数据、管理员密码 |

---

## 快速开始

### 环境要求

- Python 3.10+
- MySQL 8.0（本地或局域网内可达）

### 安装依赖

```bash
pip install flask pymysql qrcode pillow
```

### 配置 MySQL 连接

默认连接 `127.0.0.1:3306`，用户 `root`，密码 `root`，库名 `lan_chat`。
如环境不同，通过环境变量覆盖（见 `app.py` 中 `MYSQL_*` 常量）：

```bash
# Windows PowerShell
$env:MYSQL_HOST="127.0.0.1"
$env:MYSQL_PORT="3306"
$env:MYSQL_USER="root"
$env:MYSQL_PASSWORD="your_password"
$env:MYSQL_DB="lan_chat"
```

或手动先建库（可选，应用启动时会幂等执行 `ensure_mysql_schema()` 自动建库建表）：

```bash
mysql -uroot -p < sql/schema.sql
```

### 启动

```bash
python app.py
```

启动后终端打印局域网访问地址（如 `http://192.168.x.x:5000/`），
手机扫码或浏览器打开即可。首次启动自动创建默认房间 `room_default` 并生成二维码。

---

## 目录结构

```
LAN_Chat/
├── app.py                    # 主程序（路由 / 数据库 / 业务逻辑 / SSE）
├── sql/
│   └── schema.sql            # 完整 MySQL DDL（报告第四章配套脚本）
├── README.md                 # 本文件
├── templates/
│   ├── login.html            # 登录入口页（双角色）
│   ├── rooms.html            # 管理员房间管理页
│   ├── rooms_user.html       # 普通用户房间列表
│   ├── index.html            # 聊天主页面（核心）
│   └── admin.html            # 旧地址跳转兼容
├── static/
│   └── qrcode/               # 房间二维码 + 入口二维码（运行时生成）
├── uploads_<room_id>/        # 每房间上传目录（运行时创建）
└── lan_chat.db               # 旧 SQLite 主库（已弃用，可删除）
```

---

## 权限说明

| 能力 | 普通用户 | 管理员 |
| --- | :---: | :---: |
| 浏览开放房间 / 进入聊天 | ✓ | ✓ |
| 创建 / 删除 / 开关房间 | — | ✓（需 admin 会话） |
| 修改管理员密码 / 全局出厂 | — | ✓（需密码确认） |
| 房间内管理（禁言/踢出/改配置） | — | ✓（`?admin=1` 进入，且需在线管理员身份） |
| 查看统计报表 / 审计日志 | — | ✓（`is_admin(user_id)` 校验） |

鉴权分两层：
1. **会话层**（Flask session `role`）：`_require_admin()` 保护 `/api/rooms/*`、`/api/admin/*` 等全局管理接口。
2. **在线身份层**（`ChatRoom.is_admin(user_id)`）：房间内管理 API 校验 user_id 来自在线表且 `is_admin=True`，防止伪造。

---

## 数据库设计要点

- **单库多房间**：7 张业务表以 `room_id` 区分，`rooms` 为房间注册表（单一事实来源）。
- **外键级联**：`profiles` / `messages` / `blacklist` / `favorites` 对 `rooms` 建 `ON DELETE CASCADE`，删房时业务数据连带清空。
- **审计无外键**：`message_audit` 由 `AFTER DELETE` 触发器自动写入，故意不建外键——房间删除后仍保留删除痕迹供事后清查（应用层显式清理生命周期）。
- **字符集统一**：库、表、存储过程参数全部 `utf8mb4` + `utf8mb4_unicode_ci`，避免存储过程调用时报 `Illegal mix of collations`。
- **完整性约束**：
  - 主键：单属性或复合（如 `profiles(room_id, device_id)`）
  - 候选键：`messages(room_id, id)`、`favorites(room_id, user_id, msg_id)`（防重复收藏）
  - CHECK：开关取值、限额非负、消息类型白名单、保留条数 ≥10
  - DEFAULT：房间默认开放、档案默认未禁言等
- **索引**：`messages(room_id, timestamp)`、`messages(room_id, sender)`、`profiles(room_id, nickname)`、`favorites(room_id, user_id, created_at)`、`blacklist(created_at)`、`message_audit(room_id, deleted_at)`。
- **视图**：`v_message_stats`（按用户统计）、`v_message_type_stats`（按类型统计）。
- **存储过程**：
  - `sp_get_message_stats(room_id)` — 三结果集综合统计
  - `sp_cleanup_old_messages(room_id, max_keep)` — 按条数裁剪旧消息
  - `sp_get_audit_log(room_id, limit)` — 审计日志分页查询
- **触发器**：`trg_message_delete_audit` — 任何 `DELETE FROM messages` 自动写审计。

---

## 主要 API

### 全局（需管理员会话，除注明外）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/login` | 角色登录（`role=user` / `role=admin`+密码） |
| POST | `/logout` | 退出登录 |
| GET | `/rooms` | 房间列表页（按角色分流模板） |
| GET | `/api/rooms` | 房间列表 JSON（公开只读） |
| POST | `/api/rooms/create` | 创建房间 |
| POST | `/api/rooms/delete` | 删除房间 |
| POST | `/api/rooms/toggle` | 开关房间 |
| POST | `/api/admin/change_password` | 修改管理员密码 |
| POST | `/api/admin/global_reset` | 全局恢复出厂 |

### 房间内（`/room/<room_id>/...`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/join` | 加入房间（`is_admin:true` 需管理员会话） |
| POST | `/api/leave` | 离开房间 |
| POST | `/api/send` | 发送消息 |
| POST | `/api/upload_file` | 上传文件 |
| GET | `/api/messages` | 取最近消息 |
| GET | `/stream` | SSE 实时推送 |
| POST | `/api/recall_message` | 撤回自己的消息（时限内） |
| POST | `/api/favorites/add` | 收藏消息 |
| GET | `/api/admin/message_stats` | 消息统计（需在线管理员） |
| GET | `/api/admin/audit_log` | 审计日志（需在线管理员） |
| POST | `/api/admin/kick_user` | 踢出并拉黑（需在线管理员） |
| POST | `/api/admin/mute_user` | 禁言（需在线管理员） |

完整路由见 `app.py` 中各 `@app.route` / `@room_bp.route` 装饰器及 docstring。

---

## 默认账号

- **管理员密码**：`ADMIN`（登录页选「管理员登录」输入，可在管理页修改）
- **普通用户**：无需密码，输入昵称即可进入

---

## 课程设计报告配套文档

| 文件 | 内容 |
| --- | --- |
| [`sql/schema.sql`](sql/schema.sql) | 报告第四章「数据库物理结构设计」SQL 源码 |
| [`课程设计报告文字稿.md`](课程设计报告文字稿.md) | 报告正文文字稿（按模板章节组织） |
| [`E-R图.md`](E-R图.md) | 报告第二章 E-R 图（ASCII Chen 表示法 + Mermaid 版） |

---

## 许可证

课程设计教学用途，仅供学习交流。
