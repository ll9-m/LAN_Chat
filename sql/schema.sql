-- ============================================================================
-- 在线匿名聊天室（LAN_Chat）数据库结构脚本
-- ============================================================================
-- 数据库：MySQL 8.0（InnoDB）
-- 库名　：lan_chat
-- 字符集：utf8mb4    排序规则：utf8mb4_unicode_ci
--         （全库、全表、存储过程参数必须统一 collation，否则调用存储过程
--          会报 pymysql.err.OperationalError (1267, "Illegal mix of collations")）
--
-- 用途　：课程设计报告第四章「数据库物理结构设计」配套脚本；
--         应用启动时 ensure_mysql_schema() 会幂等地执行同等 DDL。
--
-- 脚本结构（对应报告章节）：
--   4.1.1  创建数据库
--   4.1.2  创建 7 张基本表（含主键/外键/UNIQUE/CHECK/DEFAULT/索引）
--   4.2    数据完整性设计（约束均内联在 CREATE TABLE 中）
--   4.3    索引的创建（同样内联，也可用 ALTER TABLE 单独建）
--   4.4    视图的创建（2 个统计视图）
--   4.5.3  触发器 + 存储过程（1 个触发器 + 3 个存储过程）
--   默认配置种子数据
--
-- 表清单与分工：
--   rooms          房间注册表（全局，DatabaseManager 管理）
--   config         全局键值配置（admin_password 等，DatabaseManager 管理）
--   profiles       用户档案（按 room_id 隔离，Database 管理）
--   messages       聊天消息（按 room_id 隔离，Database 管理）
--   blacklist      踢出黑名单（按 room_id 隔离，Database 管理）
--   favorites      收藏夹（按 room_id 隔离，Database 管理）
--   message_audit  消息删除审计（触发器写入，无外键，Database 管理）
--
-- 外键策略：
--   profiles / messages / blacklist / favorites 四张业务表对 rooms 建
--   ON DELETE CASCADE 外键——删除房间时业务数据连带清空；
--   message_audit 故意不建外键——房间删除后仍保留删除痕迹供事后清查
--   （报告 4.2.3 说明此设计权衡）。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 4.1.1 数据库创建
-- ----------------------------------------------------------------------------
-- IF NOT EXISTS：脚本可重复执行（幂等）
-- CHARACTER SET utf8mb4：完整 Unicode，支持 emoji 头像与多语言昵称
-- COLLATE utf8mb4_unicode_ci：显式指定排序规则，与所有表保持一致
-- ----------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS lan_chat
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE lan_chat;

-- ============================================================================
-- 4.1.2 数据表的创建
-- ============================================================================
-- 每张表均注明：
--   【用途】   该表在业务中扮演的角色
--   【粒度】   一行代表什么
--   【主键】   实体完整性
--   【外键】   参照完整性（级联策略）
--   【约束】   用户定义完整性（CHECK / UNIQUE / DEFAULT）
--   【索引】   面向哪些查询模式
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 表 1：rooms —— 房间注册表
-- ----------------------------------------------------------------------------
-- 【用途】 所有房间的「单一事实来源」。创建/删除/开关/改配置都写这张表；
--          ChatRoom 启动时把配置加载进内存，setter 修改后再 _save_state() 写回。
-- 【粒度】 一行 = 一个聊天房间。
-- 【主键】 room_id（应用生成：room_毫秒时间戳_3字节随机hex，避免并发冲突）。
-- 【外键】 无（本表是最顶层父表）。
-- 【约束】
--   DEFAULT：room_name 默认'新房间'、is_open 默认 1（开放）、
--            file_limit_mb 默认 0（不限）、max_history 默认 200、
--            recall_time_limit 默认 300 秒。
--   CHECK  ：is_open 只能 0/1；file_limit_mb ≥ 0；max_history ≥ 10；
--            recall_time_limit ≥ 0（0 = 禁止撤回，是合法业务值，
--            因此应用层读取时不能用 `or 300` 兜底，否则 0 会变回 300）。
-- 【说明】 password 为 NULL 表示无密码；created_at 存 Unix 时间戳（DOUBLE），
--          方便前端 JS 直接 new Date(t*1000) 渲染。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rooms (
    room_id            VARCHAR(64)  NOT NULL,                    -- 房间唯一标识
    room_name          VARCHAR(100) NOT NULL DEFAULT '新房间',     -- 房间显示名称
    is_open            TINYINT      NOT NULL DEFAULT 1,           -- 是否开放：1=开放 0=关闭
    password           VARCHAR(255)          DEFAULT NULL,        -- 进房密码，NULL=无密码
    file_limit_mb      INT          NOT NULL DEFAULT 0,           -- 单文件上限 MB，0=不限
    max_history        INT          NOT NULL DEFAULT 200,         -- 消息保留条数（≥10）
    recall_time_limit  INT          NOT NULL DEFAULT 300,         -- 撤回时限秒，0=禁止撤回
    created_at         DOUBLE                DEFAULT NULL,        -- 创建时间（Unix 时间戳）
    PRIMARY KEY (room_id),                                        -- 实体完整性：房间号唯一
    CONSTRAINT chk_rooms_open        CHECK (is_open IN (0, 1)),
    CONSTRAINT chk_rooms_file_limit  CHECK (file_limit_mb >= 0),
    CONSTRAINT chk_rooms_max_history CHECK (max_history >= 10),
    CONSTRAINT chk_rooms_recall      CHECK (recall_time_limit >= 0)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 表 2：config —— 全局键值配置
-- ----------------------------------------------------------------------------
-- 【用途】 存放跨房间的全局配置项；目前主要是 admin_password（管理员登录密码）。
-- 【粒度】 一行 = 一个配置项。
-- 【主键】 key（配置键名，如 'admin_password'）。
-- 【外键】 无。
-- 【约束】 key 为主键保证键名唯一；value 用 TEXT 容纳任意长度。
-- 【写入】 应用层用 INSERT ... ON DUPLICATE KEY UPDATE 实现幂等 upsert。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config (
    `key`   VARCHAR(64) NOT NULL,       -- 配置键（反引号：key 是 SQL 保留字）
    `value` TEXT,                        -- 配置值
    PRIMARY KEY (`key`)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 表 3：profiles —— 用户档案
-- ----------------------------------------------------------------------------
-- 【用途】 以「房间 + 设备」为粒度保存匿名用户的持久档案：
--          昵称、头像 emoji、昵称颜色、禁言截止时间。
--          同一浏览器(device_id)再次进入同一房间时复用头像/颜色，
--          形成「匿名但稳定」的虚拟身份（不关联真实账号）。
-- 【粒度】 一行 = 某房间内一台设备的档案。
-- 【主键】 (room_id, device_id) 复合主键 —— 实体完整性：
--          同一设备在同房间只能有一份档案；同一设备在不同房间可各有一份。
-- 【外键】 room_id → rooms(room_id) ON DELETE CASCADE
--          删除房间时该房间全部档案连带删除。
-- 【约束】
--   CHECK  ：muted_until ≥ 0（0 = 未禁言；否则为到期 Unix 时间戳）。
--   DEFAULT：muted_until 默认 0（新用户默认不禁言）。
-- 【索引】 idx_profiles_nickname(room_id, nickname)
--          支撑「按房间+昵称」查询/校验档案的场景。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    room_id      VARCHAR(64) NOT NULL,              -- 所属房间（外键列）
    device_id    VARCHAR(64) NOT NULL,              -- 浏览器设备标识（前端 localStorage 持久化）
    nickname     VARCHAR(50) NOT NULL,              -- 房间内匿名昵称（同房间在线不允许重名，由应用层保证）
    avatar       VARCHAR(16),                       -- 头像 emoji（加入时从 AVATARS 池随机分配）
    color        VARCHAR(16),                       -- 昵称颜色 HEX（从 COLORS 池随机分配）
    muted_until  DOUBLE      NOT NULL DEFAULT 0,    -- 禁言截止时间戳；0=未禁言；>now 表示禁言中
    PRIMARY KEY (room_id, device_id),               -- 复合主键：房间内设备唯一
    CONSTRAINT fk_profiles_room FOREIGN KEY (room_id)
        REFERENCES rooms (room_id) ON DELETE CASCADE,
    CONSTRAINT chk_profiles_mute CHECK (muted_until >= 0),
    INDEX idx_profiles_nickname (room_id, nickname)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 表 4：messages —— 聊天消息
-- ----------------------------------------------------------------------------
-- 【用途】 所有房间的聊天消息（文本/图片/文件/系统消息）统一存这张表，
--          以 room_id 区分房间，便于跨房间统计与统一备份。
-- 【粒度】 一行 = 一条消息。
-- 【主键】 seq（BIGINT 自增代理主键）
--          为什么不用消息 id 作主键：id 是应用层生成的字符串
--          （时间戳_随机hex），自增 seq 天然保证「插入顺序」，
--          支撑「取最近 N 条」「按条数裁剪旧消息」等 ORDER BY seq DESC LIMIT N 查询。
-- 【候选键】UNIQUE (room_id, id) —— 同房间内消息 ID 不重复，
--          防止重复提交/重复撤回误伤其他消息。
-- 【外键】 room_id → rooms(room_id) ON DELETE CASCADE
--          删除房间时消息全部连带删除（触发器会为每行写审计）。
-- 【约束】
--   CHECK：type ∈ {text, image, file, system}（NULL 兼容历史数据）；
--          is_admin ∈ {0, 1}。
--   DEFAULT：is_admin 默认 0。
-- 【索引】
--   idx_messages_room_time   (room_id, timestamp)  —— 「最近N条」「按时间排序」
--   idx_messages_room_sender (room_id, sender)     —— 「按昵称搜索消息」
-- 【说明】
--   content：文本消息存正文（已过敏感词过滤）；图片/文件消息存
--            /room/<id>/uploads/<filename> 形式的 URL。
--   timestamp：发送时刻 Unix 时间戳（DOUBLE），撤回时限判断依据。
--   file_name / file_size：仅文件类消息有值。
--   is_admin / avatar / color：消息展示时的发送者快照（防止档案被改后历史消息变样）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    seq         BIGINT       NOT NULL AUTO_INCREMENT, -- 代理主键，保证插入顺序
    room_id     VARCHAR(64)  NOT NULL,                -- 所属房间（外键列）
    id          VARCHAR(64)  NOT NULL,                -- 应用层消息ID（时间戳_随机hex）
    type        VARCHAR(16),                          -- text/image/file/system
    content     TEXT,                                 -- 文本正文 或 文件URL
    sender      VARCHAR(50),                          -- 发送者昵称（快照）
    user_id     VARCHAR(64),                          -- 发送会话标识（user_... 或 admin_...）
    is_admin    TINYINT      NOT NULL DEFAULT 0,      -- 发送者是否管理员 0/1（快照）
    avatar      VARCHAR(16),                          -- 发送者头像快照
    color       VARCHAR(16),                          -- 发送者颜色快照
    timestamp   DOUBLE,                               -- 发送时间（Unix 时间戳）
    file_name   VARCHAR(100),                         -- 文件显示名（文件消息专用）
    file_size   BIGINT,                               -- 文件字节数（文件消息专用）
    PRIMARY KEY (seq),                                -- 实体完整性：自增代理主键
    UNIQUE KEY uk_messages_room_id (room_id, id),     -- 候选键：同房间消息ID唯一
    CONSTRAINT fk_messages_room FOREIGN KEY (room_id)
        REFERENCES rooms (room_id) ON DELETE CASCADE, -- 参照完整性：删房级联删消息
    CONSTRAINT chk_messages_type CHECK (              -- 用户定义完整性：消息类型白名单
        type IS NULL OR type IN ('text', 'image', 'file', 'system')
    ),
    CONSTRAINT chk_messages_admin CHECK (is_admin IN (0, 1)),
    INDEX idx_messages_room_time (room_id, timestamp),   -- 查询：房间内按时间取最近N条
    INDEX idx_messages_room_sender (room_id, sender)     -- 查询：按昵称模糊搜索
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 表 5：blacklist —— 踢出黑名单
-- ----------------------------------------------------------------------------
-- 【用途】 管理员踢出用户时把其 device_id 拉黑；被拉黑设备无法再加入该房间。
-- 【粒度】 一行 = 某房间内一台被拉黑的设备。
-- 【主键】 (room_id, device_id) 复合主键 —— 同设备在同房间只拉黑一次
--          （重复拉黑用 ON DUPLICATE KEY UPDATE 刷新昵称/IP/时间）。
-- 【外键】 room_id → rooms(room_id) ON DELETE CASCADE
-- 【索引】 idx_blacklist_created(created_at)
--          支撑「按拉黑时间倒序查看/清理」。
-- 【说明】 ip 记录拉黑时刻的来源 IP（IPv6 最长 45 字符），
--          仅作审计参考，不作为判定依据（判定只看 device_id）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blacklist (
    room_id    VARCHAR(64) NOT NULL,          -- 所属房间（外键列）
    device_id  VARCHAR(64) NOT NULL,          -- 被拉黑的设备标识
    nickname   VARCHAR(50),                   -- 拉黑时刻的昵称（快照，便于管理员辨认）
    ip         VARCHAR(45),                   -- 拉黑时刻的来源 IP
    created_at DOUBLE,                        -- 拉黑时间（Unix 时间戳）
    PRIMARY KEY (room_id, device_id),         -- 复合主键：房间内设备只存一条
    CONSTRAINT fk_blacklist_room FOREIGN KEY (room_id)
        REFERENCES rooms (room_id) ON DELETE CASCADE,
    INDEX idx_blacklist_created (created_at)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 表 6：favorites —— 收藏夹
-- ----------------------------------------------------------------------------
-- 【用途】 用户收藏自己感兴趣的消息；收藏时对消息内容做快照冗余，
--          即使原消息被撤回/删除，收藏仍可正常展示（受控反规范化）。
-- 【粒度】 一行 = 某用户在某房间对某消息的一次收藏。
-- 【主键】 id（BIGINT 自增）。
-- 【候选键】UNIQUE (room_id, user_id, msg_id)
--          —— 同一用户在同一房间对同一消息只能收藏一次；
--          应用层配合 INSERT IGNORE 实现幂等（重复收藏静默忽略）。
-- 【外键】 room_id → rooms(room_id) ON DELETE CASCADE
-- 【索引】 idx_favorites_user(room_id, user_id, created_at)
--          支撑「我的收藏列表」按时间倒序分页查询。
-- 【说明】 msg_type / content / sender / file_name / file_size 均为
--          收藏时刻从 messages 复制的快照字段；
--          不对 msg_id 建外键——原消息可被删除，收藏须独立存活。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS favorites (
    id         BIGINT       NOT NULL AUTO_INCREMENT, -- 收藏记录主键
    room_id    VARCHAR(64)  NOT NULL,                -- 所属房间（外键列）
    user_id    VARCHAR(64)  NOT NULL,                -- 收藏者会话标识
    msg_id     VARCHAR(64),                          -- 原消息 id（逻辑引用，非外键）
    msg_type   VARCHAR(16),                          -- 消息类型快照
    content    TEXT,                                 -- 消息内容快照
    sender     VARCHAR(50),                          -- 原发送者昵称快照
    file_name  VARCHAR(100),                         -- 文件显示名快照
    file_size  BIGINT,                               -- 文件大小快照
    created_at DOUBLE       NOT NULL,                -- 收藏时间（Unix 时间戳）
    PRIMARY KEY (id),
    UNIQUE KEY uk_favorites_user_msg (room_id, user_id, msg_id), -- 防重复收藏
    CONSTRAINT fk_favorites_room FOREIGN KEY (room_id)
        REFERENCES rooms (room_id) ON DELETE CASCADE,
    INDEX idx_favorites_user (room_id, user_id, created_at)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- 表 7：message_audit —— 消息删除审计
-- ----------------------------------------------------------------------------
-- 【用途】 记录每一次消息删除操作（管理员强制删、用户撤回、清空记录、
--          恢复出厂、按保留条数裁剪），全部由触发器 trg_message_delete_audit
--          自动写入，应用代码无需（也不应）手动 INSERT。
-- 【粒度】 一行 = 一次消息删除事件。
-- 【主键】 audit_id（BIGINT 自增流水号）。
-- 【外键】 故意不设任何外键（报告 4.2.3 详述原因）：
--          (1) 审计引用的 message_id 对应行已被删除，指向 messages 必失败；
--          (2) 希望房间删除后仍能事后清查该房间曾删除过哪些消息；
--          (3) 应用在「删除单个房间」时显式 DELETE 审计，在「全局恢复出厂」
--              时也显式清空——由应用层决定审计生命周期，而非外键级联。
-- 【索引】 idx_audit_room_time(room_id, deleted_at)
--          支撑「按房间+删除时间」的审计查询。
-- 【说明】 content/sender/user_id/type 为被删消息的快照（OLD.*），
--          deleted_at 为删除时刻时间戳，action 固定 'DELETE'（预留扩展
--          'CLEAR'/'RECALL' 等取值，当前触发器统一写 DELETE）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_audit (
    audit_id   BIGINT       NOT NULL AUTO_INCREMENT, -- 审计流水主键
    room_id    VARCHAR(64)  NOT NULL,                -- 被删消息所在房间（非外键，见上）
    message_id VARCHAR(64),                          -- 被删消息的 id（快照）
    type       VARCHAR(16),                          -- 被删消息类型快照
    content    TEXT,                                 -- 被删消息内容快照
    sender     VARCHAR(50),                          -- 被删消息发送者快照
    user_id    VARCHAR(64),                          -- 被删消息会话标识快照
    deleted_at DOUBLE,                               -- 删除时间（Unix 时间戳）
    action     VARCHAR(16),                          -- 操作类型：'DELETE'
    PRIMARY KEY (audit_id),
    INDEX idx_audit_room_time (room_id, deleted_at)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- 4.4 视图的创建（2 个）
-- ============================================================================
-- 视图封装统计口径，供存储过程与管理端直接查询复用，避免 SQL 重复。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 视图 1：v_message_stats —— 按用户统计消息数与活跃时间
-- ----------------------------------------------------------------------------
-- 【用途】 管理端「数据查询」报表的第二结果集（按用户排行）；
--          也被 sp_get_message_stats 直接 SELECT。
-- 【列】
--   room_id       房间号
--   user_id       发送会话标识
--   sender        发送者昵称
--   message_count 该用户消息总数
--   last_active   最后活跃时间（最大 timestamp）
--   first_active  首次活跃时间（最小 timestamp）
-- 【过滤】 WHERE user_id IS NOT NULL —— 排除无归属的历史/系统行，
--          保证分组粒度 (room_id, user_id, sender) 有意义。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_message_stats AS
SELECT room_id,
       user_id,
       sender,
       COUNT(*)           AS message_count,   -- 消息总数
       MAX(`timestamp`)   AS last_active,     -- 最后发言时间
       MIN(`timestamp`)   AS first_active     -- 首次发言时间
FROM messages
WHERE user_id IS NOT NULL
GROUP BY room_id, user_id, sender;

-- ----------------------------------------------------------------------------
-- 视图 2：v_message_type_stats —— 按消息类型统计数量
-- ----------------------------------------------------------------------------
-- 【用途】 管理端报表的辅助视图（按 text/image/file/system 分类计数）。
-- 【列】 room_id、type、count
-- 【说明】 与 sp_get_message_stats 第三结果集口径一致（该结果集额外算了
--          百分比，故在存储过程内单独写而非直接查本视图）。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_message_type_stats AS
SELECT room_id,
       type,
       COUNT(*) AS count      -- 该类型消息数
FROM messages
GROUP BY room_id, type;

-- ============================================================================
-- 4.5.3 触发器的创建
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 触发器：trg_message_delete_audit —— 消息删除 → 自动写审计
-- ----------------------------------------------------------------------------
-- 【时机】 AFTER DELETE ON messages，FOR EACH ROW（行级触发）
-- 【影响范围】 对 messages 的任何 DELETE 都会触发，包括：
--   (1) 管理员强制删除单条消息   ChatRoom.admin_delete_message
--   (2) 用户撤回自己的消息       ChatRoom.recall_message
--   (3) 清空全部聊天记录         ChatRoom.clear_history
--   (4) 房间级恢复出厂           ChatRoom.factory_reset
--   (5) 按保留条数裁剪旧消息     sp_cleanup_old_messages
--   (6) 删除房间（CASCADE 级行删除）/ 全局恢复出厂
-- 【行为】 将被删行的 OLD.* 快照 + UNIX_TIMESTAMP() + 'DELETE'
--          插入 message_audit 一行。
-- 【DROP + CREATE】 保证脚本可重复执行（先删旧触发器再建新的）。
-- ----------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_message_delete_audit;
CREATE TRIGGER trg_message_delete_audit
    AFTER DELETE
    ON messages
    FOR EACH ROW
INSERT INTO message_audit (room_id, message_id, type, content, sender, user_id, deleted_at, action)
VALUES (OLD.room_id, OLD.id, OLD.type, OLD.content, OLD.sender, OLD.user_id, UNIX_TIMESTAMP(), 'DELETE');

-- ============================================================================
-- 4.5.3 存储过程的创建（3 个）
-- ============================================================================
-- 存储过程说明格式：用途 / 参数 / 返回值（结果集）/ 调用示例
-- Python 侧通过 CALL 调用；多结果集用 cursor.nextset() 依次读取。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 存储过程 1：sp_get_message_stats —— 消息综合统计
-- ----------------------------------------------------------------------------
-- 【用途】  管理端「数据查询」页一次性获取三组统计数据，避免前端发 3 次请求。
-- 【参数】  IN p_room_id VARCHAR(64)  —— 目标房间号（必填）
-- 【返回值】无返回值（PROCEDURE），输出 3 个结果集：
--   结果集 1：total_messages —— 该房间消息总数
--   结果集 2：SELECT * FROM v_message_stats（按用户统计，message_count 降序）
--   结果集 3：按类型统计 type / count / percentage（百分比保留 1 位小数；
--            NULLIF 防止空房间除零）
-- 【调用】  CALL sp_get_message_stats('room_default');
-- 【Python】Database.sp_get_message_stats() 用 nextset() 依次读三个结果集。
-- ----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS sp_get_message_stats;
CREATE PROCEDURE sp_get_message_stats(IN p_room_id VARCHAR(64))
BEGIN
    -- 结果集 1：总消息数
    SELECT COUNT(*) AS total_messages
    FROM messages
    WHERE room_id = p_room_id;

    -- 结果集 2：按用户统计（复用视图 v_message_stats）
    SELECT *
    FROM v_message_stats
    WHERE room_id = p_room_id
    ORDER BY message_count DESC;

    -- 结果集 3：按类型统计 + 占比
    -- NULLIF(...,0)：房间无消息时分母为 NULL，percentage 也为 NULL，避免除零错误
    SELECT m.type,
           COUNT(*)                                                                 AS count,
           ROUND(COUNT(*) * 100.0 /
                 NULLIF((SELECT COUNT(*) FROM messages
                         WHERE room_id = p_room_id), 0), 1)                          AS percentage
    FROM messages m
    WHERE m.room_id = p_room_id
    GROUP BY m.type
    ORDER BY count DESC;
END;

-- ----------------------------------------------------------------------------
-- 存储过程 2：sp_cleanup_old_messages —— 按保留条数裁剪旧消息
-- ----------------------------------------------------------------------------
-- 【用途】  管理员修改「消息保留条数 max_history」后调用，删除超出部分，
--          只保留最新的 p_max_keep 条。被删行逐行触发审计触发器（裁剪也留痕）。
-- 【参数】  IN p_room_id VARCHAR(64) —— 目标房间号
--           IN p_max_keep INT        —— 保留条数（应用层校验 ≥10，rooms 表 CHECK 亦 ≥10）
-- 【返回值】无返回值、无结果集；执行 DELETE。
-- 【算法】  子查询取出「seq 最大的 p_max_keep 条」，外层 DELETE 这些 seq 之外的行。
--           MySQL 不允许在 FROM 子句直接引用同表子查询，故套一层派生表 t。
-- 【调用】  CALL sp_cleanup_old_messages('room_default', 200);
-- 【Python】Database.cleanup_old_messages(max_history)；ChatRoom.set_max_history 调用。
-- ----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS sp_cleanup_old_messages;
CREATE PROCEDURE sp_cleanup_old_messages(IN p_room_id VARCHAR(64), IN p_max_keep INT)
BEGIN
    DELETE
    FROM messages
    WHERE room_id = p_room_id
      AND seq NOT IN (SELECT seq
                      FROM (SELECT seq
                            FROM messages
                            WHERE room_id = p_room_id
                            ORDER BY seq DESC        -- 按插入序倒序
                            LIMIT p_max_keep          -- 保留最新 p_max_keep 条
                          ) t                         -- 派生表：绕过 MySQL 同表子查询限制
                     );
END;

-- ----------------------------------------------------------------------------
-- 存储过程 3：sp_get_audit_log —— 查询消息删除审计日志
-- ----------------------------------------------------------------------------
-- 【用途】  管理端「审计日志」页按删除时间倒序返回最近 N 条记录。
-- 【参数】  IN p_room_id VARCHAR(64) —— 目标房间号
--           IN p_limit INT           —— 返回条数（前端传入，默认 50）
-- 【返回值】单结果集：message_audit 中该房间按 audit_id 降序的前 p_limit 行。
-- 【调用】  CALL sp_get_audit_log('room_default', 50);
-- 【Python】Database.sp_get_audit_log(limit)；路由 /api/admin/audit_log 调用。
-- ----------------------------------------------------------------------------
DROP PROCEDURE IF EXISTS sp_get_audit_log;
CREATE PROCEDURE sp_get_audit_log(IN p_room_id VARCHAR(64), IN p_limit INT)
BEGIN
    SELECT *
    FROM message_audit
    WHERE room_id = p_room_id
    ORDER BY audit_id DESC          -- audit_id 自增，降序 = 最新删除在前
    LIMIT p_limit;
END;

-- ============================================================================
-- 默认配置种子数据
-- ============================================================================
-- 首次初始化写入管理员默认密码 'ADMIN'。
-- ON DUPLICATE KEY UPDATE value = value：
--   已存在该键时不覆盖用户修改过的密码（幂等空更新），
--   保证脚本重复执行不会把改过的密码重置回 ADMIN。
-- ============================================================================
INSERT INTO config (`key`, `value`)
VALUES ('admin_password', 'ADMIN')
ON DUPLICATE KEY UPDATE `value` = `value`;

-- ============================================================================
-- 脚本结束。验证建议：
--   SHOW TABLES;                              -- 应看到 7 张表 + 2 个视图
--   SHOW CREATE TABLE messages\G              -- 检查约束与索引
--   SHOW TRIGGERS;                            -- trg_message_delete_audit
--   SHOW PROCEDURE STATUS WHERE Db='lan_chat';-- 3 个存储过程
--   CALL sp_get_message_stats('room_default');-- 三个结果集
--   SELECT * FROM information_schema.TABLES
--     WHERE TABLE_SCHEMA='lan_chat' AND TABLE_TYPE='BASE TABLE';
--     -- TABLE_COLLATION 应全部为 utf8mb4_unicode_ci
-- ============================================================================
