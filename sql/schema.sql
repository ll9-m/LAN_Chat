-- ============================================================
-- 在线匿名聊天室 数据库结构（MySQL 8.0）
-- 库名：lan_chat
-- 用途：课程设计报告第四章「数据库物理结构设计」配套脚本
-- ============================================================

CREATE DATABASE IF NOT EXISTS lan_chat
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE lan_chat;

-- ------------------------------------------------------------
-- 4.1.2 数据表的创建
-- ------------------------------------------------------------

-- 房间注册表（全局配置的事实来源）
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
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 全局键值配置（目前存 admin_password）
CREATE TABLE IF NOT EXISTS config (
    `key`   VARCHAR(64) NOT NULL,
    `value` TEXT,
    PRIMARY KEY (`key`)
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 用户档案（按房间 + 设备唯一；昵称/头像/颜色/禁言截止）
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
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 聊天消息（seq 供“最近 N 条/裁剪”排序；room_id+id 唯一防重复）
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
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 踢出黑名单（按设备）
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
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 收藏夹（同一房间内同一用户对同一消息只收藏一次）
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
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 消息删除审计（由触发器写入；故意不设外键，房间删除后仍可事后清查）
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
) ENGINE = InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 4.4 视图的创建（2 个）
-- ------------------------------------------------------------

-- 视图 1：按用户统计消息数与活跃时间
CREATE OR REPLACE VIEW v_message_stats AS
SELECT room_id,
       user_id,
       sender,
       COUNT(*)           AS message_count,
       MAX(`timestamp`)   AS last_active,
       MIN(`timestamp`)   AS first_active
FROM messages
WHERE user_id IS NOT NULL
GROUP BY room_id, user_id, sender;

-- 视图 2：按消息类型统计数量
CREATE OR REPLACE VIEW v_message_type_stats AS
SELECT room_id,
       type,
       COUNT(*) AS count
FROM messages
GROUP BY room_id, type;

-- ------------------------------------------------------------
-- 4.5.3 触发器：消息删除 → 自动写入审计表
-- 影响范围：对 messages 的任何 DELETE（含管理员删消息、用户撤回、
--          清空记录、房间级/全局恢复出厂、到达保留条数后的裁剪）
--          都会向 message_audit 插入一条 DELETE 审计记录。
-- ------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_message_delete_audit;
CREATE TRIGGER trg_message_delete_audit
    AFTER DELETE
    ON messages
    FOR EACH ROW
INSERT INTO message_audit (room_id, message_id, type, content, sender, user_id, deleted_at, action)
VALUES (OLD.room_id, OLD.id, OLD.type, OLD.content, OLD.sender, OLD.user_id, UNIX_TIMESTAMP(), 'DELETE');

-- ------------------------------------------------------------
-- 4.5.3 存储过程
-- ------------------------------------------------------------

-- 存储过程 1：消息综合统计
-- 用途：管理端「数据查询」一次性返回 总数 / 按用户 / 按类型 三个结果集
-- 参数：IN p_room_id  房间 ID
-- 返回：结果集1 总消息数；结果集2 用户统计；结果集3 类型统计（含百分比）
DROP PROCEDURE IF EXISTS sp_get_message_stats;
CREATE PROCEDURE sp_get_message_stats(IN p_room_id VARCHAR(64))
BEGIN
    SELECT COUNT(*) AS total_messages
    FROM messages
    WHERE room_id = p_room_id;

    SELECT *
    FROM v_message_stats
    WHERE room_id = p_room_id
    ORDER BY message_count DESC;

    SELECT m.type,
           COUNT(*)                                                                 AS count,
           ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*)
                                            FROM messages
                                            WHERE room_id = p_room_id), 0), 1)      AS percentage
    FROM messages m
    WHERE m.room_id = p_room_id
    GROUP BY m.type
    ORDER BY count DESC;
END;

-- 存储过程 2：按保留条数裁剪旧消息
-- 用途：修改「保留消息条数」后删除超出部分；删除会触发审计触发器
-- 参数：IN p_room_id 房间 ID；IN p_max_keep 保留条数（≥10）
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
                            ORDER BY seq DESC
                            LIMIT p_max_keep) t);
END;

-- 存储过程 3：查询消息删除审计日志
-- 用途：管理端「审计日志」按时间倒序取最近 N 条
-- 参数：IN p_room_id 房间 ID；IN p_limit 返回条数
DROP PROCEDURE IF EXISTS sp_get_audit_log;
CREATE PROCEDURE sp_get_audit_log(IN p_room_id VARCHAR(64), IN p_limit INT)
BEGIN
    SELECT *
    FROM message_audit
    WHERE room_id = p_room_id
    ORDER BY audit_id DESC
    LIMIT p_limit;
END;

-- ------------------------------------------------------------
-- 默认配置
-- ------------------------------------------------------------
INSERT INTO config (`key`, `value`)
VALUES ('admin_password', 'ADMIN')
ON DUPLICATE KEY UPDATE `value` = `value`;
