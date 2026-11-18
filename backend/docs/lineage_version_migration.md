# 血缘链路版本表迁移与上线说明

## 1. 背景与目标

旧血缘链路以 `lineage_track` 模块表中的临时字段（链路编号、上游/下游表名、采集批次、
链路版本等）记录，无法稳定区分历史版本。本次改造新增**血缘链路版本表**与
**迁移批次表**，并满足：

- 存量链路与迁移批次在同一事务内一致写入，失败整批回滚且失败记录保留；
- 迁移后每条链路只有一个当前版本（`is_current`），历史版本与迁移批次可回查；
- 旧查询字段全部保留可读，旧应用切到新表期间缺失边不会被当成不存在；
- 迁移中断可续做，也可按批次回滚；冲突未仲裁前不允许版本切换。

## 2. 数据模型与代码映射

| 数据库对象（`sql/lineage_version_ddl.sql`） | 内存实现（`app/store.py`） | 说明 |
| --- | --- | --- |
| `lineage_link_version` | `Store._lineage_versions` | 版本表，一条链路一个版本一行 |
| `lineage_migration_batch` | `Store._lineage_batches` | 迁移/续做/回滚/切换/仲裁批次 |
| `v_lineage_track_current` | `Store.get_lineage_edges()` / `_merged_lineage_rows()` | 旧查询兼容视图 |
| 数据库事务 `BEGIN/ROLLBACK` | `Store.lineage_transaction()` | 快照 + 异常整体回滚 |
| `CHECK` + 唯一索引 | `LINEAGE_VERSION_REQUIRED_FIELDS` 校验 | 字段未落库即回滚 |

链路稳定标识 `lineage_key = 上游表名->下游表名`，同一标识的版本序列独立递增，
数据库层用唯一索引保证「每条链路至多一个当前版本」。

## 3. 冲突与边界策略

| 策略 `conflict_policy` | 边界 | 行为 | 批次表展示 |
| --- | --- | --- | --- |
| `resume`（默认） | 严格边界 | 已有当前版本的链路跳过，只补缺失边 | 续做批次 + 完整链路集 |
| `rollback` | 放宽边界 | 停用旧当前版本，写入一次性迁移结果 | 回滚批次 + 一次性迁移结果 |
| `arbitrate` | 人工裁决 | 冲突挂「待仲裁」，仲裁前禁止切换 | 冲突仲裁批次 |

仲裁入口：`POST /api/lineage_versions/resolve-conflict`，`winner=current`
保留当前版本，`winner=legacy` 采用旧记录生成新当前版本。

## 4. 依赖、环境与配置

- 依赖：无新增第三方包，仍为 `requirements.txt` 中锁定的 FastAPI/Pydantic 版本；
  落库阶段数据库需 MySQL 8.0.16+（或 PostgreSQL 12+），见 DDL 文末方言说明。
- 环境变量沿用现有 `APP_ENV`；新增配置项（`app/config.py`，均有默认值，旧环境不配置
  也能启动）：
  - `lineage_dual_write=true`：血缘写入双写旧视图与版本表；
  - `lineage_migrate_policy=resume`：流水线迁移默认冲突策略。
- 兼容性：版本表为**新增表**，旧表 `lineage_track` 不删不改列；旧接口
  `GET /api/lineage_track` 契约不变，新增接口统一在 `/api/lineage_versions` 前缀下。

## 5. 构建与上线流水线

推荐顺序（任一环节失败都可重跑，迁移命令幂等）：

1. **依赖升级与构建**：后端 `pip install -r requirements.txt`，前端 `npm ci &&
   npm run build`；本次无 schema 破坏式变更，服务可滚动升级。
2. **建表**：执行 `sql/lineage_version_ddl.sql`（只新增表与视图，不锁旧表）。
3. **部署双写版本**：上线新代码，此时旧查询走旧表，新写入同时落版本表。
4. **存量迁移**：流水线执行
   `python -m app.scripts.migrate_lineage --policy resume --remark "上线迁移"`；
   命令退出码非 0 表示存在失败边，批次详情可通过
   `GET /api/lineage_versions/batches` 查看，修复后重跑即为续做批次。
5. **校验**：`GET /api/lineage_versions/status` 确认 `complete=true`，
   `pending_total=0`、`conflict_total=0`；在血缘追踪列表核对边数与迁移前一致。
6. **切流**：旧查询逐步改读 `v_lineage_track_current`（应用层已内置相同的合并逻辑，
   即使提前切换也不会漏边）。
7. **异常回滚**：`POST /api/lineage_versions/rollback {"batch_id": "..."}`
   停用该批次写入的当前版本（版本行保留，可再次迁移恢复），旧查询自动回退旧表读数。

## 6. 接口清单（`/api/lineage_versions`）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/status` | 存量数、已迁移、缺失边、待仲裁冲突、是否完成 |
| GET | `` | 版本表页面：每条链路的版本集合、当前版本、批次 |
| GET | `/batches` | 迁移批次列表与失败明细 |
| POST | `/migrate` | 迁移/续做，body：`conflict_policy`、`remark` |
| POST | `/rollback` | 按批次回滚，body：`batch_id` |
| POST | `/switch` | 版本切换，返回沿血缘调用链定位的受影响链路 |
| POST | `/resolve-conflict` | 冲突人工仲裁 |
| POST | `/trace` | 按表名向上游/下游追踪完整调用链 |

## 7. 验证

- 后端：`pytest -q`，共 45 个用例，含迁移、事务回滚、续做、回滚、仲裁、
  版本切换与跨模块消费专项 `tests/test_lineage_version.py`。
- 前端：版本表页面执行迁移/切换后，返回血缘追踪列表核对 DAG 边数保持一致。
