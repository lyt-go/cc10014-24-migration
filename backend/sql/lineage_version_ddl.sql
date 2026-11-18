-- 血缘链路版本表与迁移批次表 DDL（MySQL 8.0 / PostgreSQL 兼容写法说明见文末）。
--
-- 设计目标：
-- 1. 一条血缘链路（lineage_key = 上游表名->下游表名）拥有多个历史版本，
--    任意时刻只有一个 is_current = 1 的当前版本；
-- 2. 每个版本带迁移批次 lineage_batch_id，批次表保留成功/失败/续做/回滚记录；
-- 3. 旧表 lineage_track 与其中文临时字段保留不删，旧查询在切换完成前持续可读；
-- 4. 迁移在单事务内完成：版本行与批次状态一起提交，任一字段缺失则整体回滚。

-- 迁移批次表（先建，版本表外键依赖它）
CREATE TABLE IF NOT EXISTS lineage_migration_batch (
    batch_id            VARCHAR(40)  NOT NULL COMMENT '迁移批次编号，如 LB-20261118-001',
    kind                VARCHAR(16)  NOT NULL COMMENT '迁移批次/续做批次/回滚批次/版本切换/冲突仲裁',
    conflict_policy     VARCHAR(16)  NOT NULL DEFAULT 'resume' COMMENT 'resume/rollback/arbitrate',
    status              VARCHAR(16)  NOT NULL COMMENT '进行中/已完成/失败/待仲裁',
    total               INT          NOT NULL DEFAULT 0 COMMENT '本批存量链路总数',
    migrated            INT          NOT NULL DEFAULT 0 COMMENT '本批成功写入版本数',
    skipped             INT          NOT NULL DEFAULT 0 COMMENT '冲突跳过数',
    conflicts           INT          NOT NULL DEFAULT 0 COMMENT '挂起待仲裁数',
    failures            JSON         NULL     COMMENT '失败明细：[{index,lineage_key,reason}]',
    rollback_of         VARCHAR(40)  NULL     COMMENT '回滚批次指向的原批次',
    remark              VARCHAR(255) NOT NULL DEFAULT '',
    created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (batch_id),
    KEY idx_lmb_status (status),
    KEY idx_lmb_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='血缘链路迁移批次表';

-- 血缘链路版本表
CREATE TABLE IF NOT EXISTS lineage_link_version (
    version_id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '版本行主键',
    lineage_key         VARCHAR(512) NOT NULL COMMENT '链路稳定标识：上游表名->下游表名',
    version_seq         INT          NOT NULL COMMENT '同一链路内的版本序号，从 1 递增',
    link_code           VARCHAR(64)  NOT NULL COMMENT '链路编号（旧字段 链路编号）',
    upstream_table      VARCHAR(255) NOT NULL COMMENT '上游表名（旧字段）',
    downstream_table    VARCHAR(255) NOT NULL COMMENT '下游表名（旧字段）',
    source_fields       TEXT         NULL     COMMENT '来源字段（旧字段）',
    target_fields       TEXT         NULL     COMMENT '目标字段（旧字段）',
    transform_logic     TEXT         NULL     COMMENT '转换逻辑（旧字段）',
    collect_batch       VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '采集批次（旧字段）',
    link_version        VARCHAR(32)  NOT NULL COMMENT '链路版本号，如 v1（旧字段）',
    lineage_batch_id    VARCHAR(40)  NOT NULL COMMENT '写入该行的迁移/续做/回滚批次',
    batch_kind          VARCHAR(16)  NOT NULL COMMENT '批次类型快照',
    is_current          TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=当前版本，每条链路仅一个',
    version_status      VARCHAR(16)  NOT NULL DEFAULT '已生效' COMMENT '草稿/待校验/已生效/已停用',
    conflict_state      VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '空/待仲裁',
    conflict_batch      VARCHAR(40)  NOT NULL DEFAULT '' COMMENT '产生冲突的批次',
    arbitration_batch   VARCHAR(40)  NOT NULL DEFAULT '' COMMENT '仲裁批次',
    rollback_batch      VARCHAR(40)  NOT NULL DEFAULT '' COMMENT '停用该行的回滚批次',
    switch_batch        VARCHAR(40)  NOT NULL DEFAULT '' COMMENT '最近一次版本切换批次',
    legacy_snapshot     JSON         NULL     COMMENT '冲突时旧记录字段快照',
    created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    switched_at         DATETIME(3)  NULL,
    PRIMARY KEY (version_id),
    UNIQUE KEY uk_llv_key_seq (lineage_key, version_seq),
    -- 每条链路至多一个当前版本：把 is_current=0 的行映射为 NULL，唯一索引只约束当前版本
    UNIQUE KEY uk_llv_current (lineage_key, (CASE WHEN is_current = 1 THEN 1 END)),
    KEY idx_llv_current (is_current),
    KEY idx_llv_batch (lineage_batch_id),
    CONSTRAINT fk_llv_batch FOREIGN KEY (lineage_batch_id)
        REFERENCES lineage_migration_batch (batch_id),
    CONSTRAINT ck_llv_required CHECK (
        lineage_key <> '' AND link_code <> '' AND upstream_table <> ''
        AND downstream_table <> '' AND link_version <> ''
        AND lineage_batch_id <> '' AND is_current IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='血缘链路版本表';

-- 旧查询兼容视图：只暴露当前版本且沿用旧表中文列名，旧应用 SQL 无需改造即可切到新表。
-- 视图列与 lineage_track 的查询字段一一对应；回滚后没有当前版本的链路由应用层回退
-- 到旧表读取，保证缺失边不被当成不存在。
CREATE OR REPLACE VIEW v_lineage_track_current AS
SELECT
    v.version_id                                       AS id,
    v.link_code                                        AS `链路编号`,
    v.upstream_table                                   AS `上游表名`,
    v.downstream_table                                 AS `下游表名`,
    v.source_fields                                    AS `来源字段`,
    v.target_fields                                    AS `目标字段`,
    v.transform_logic                                  AS `转换逻辑`,
    v.collect_batch                                    AS `采集批次`,
    v.link_version                                     AS `链路版本`,
    v.lineage_batch_id                                 AS `迁移批次`,
    v.version_status                                   AS status
FROM lineage_link_version v
WHERE v.is_current = 1;

-- ---------------------------------------------------------------------------
-- PostgreSQL 兼容说明（MySQL 8.0.16+ / PG 12+ 均可执行上述主体，差异点如下）：
-- 1. JSON 类型 PG 用 JSONB；DATETIME(3) 用 TIMESTAMP(3)；TINYINT 用 SMALLINT；
-- 2. 部分唯一索引改写为：
--    CREATE UNIQUE INDEX uk_llv_current ON lineage_link_version (lineage_key)
--        WHERE is_current = 1;
-- 3. AUTO_INCREMENT 改为 GENERATED ALWAYS AS IDENTITY；
-- 4. 视图中文列名用双引号引用：SELECT ... AS "链路编号"。
-- 应用侧当前为内存仓储（app/store.py），字段名与本 DDL 一一映射，落库时按上述
-- 差异做方言适配即可，业务服务与接口契约不变。
-- ---------------------------------------------------------------------------
