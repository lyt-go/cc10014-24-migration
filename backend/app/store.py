"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

血缘链路版本化（迁移落库）相关结构：
- ``_lineage_versions``：血缘链路版本表，一条边一个版本一行，迁移批次可回查；
- ``_lineage_batches``：迁移批次表，记录每次迁移/回滚的批次与失败明细；
- :class:`LineageTransaction`：模拟数据库事务，任一字段未落库即整体回滚。
旧查询（``lineage_track`` 模块表与 ``_lineage_edges``）在迁移前后保持可读。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from typing import Any, Iterator

from app.seed import SEED_ROWS
from app.config import settings

# 血缘链路版本表必须落库的字段；任何一个缺失都会触发事务回滚。
LINEAGE_VERSION_REQUIRED_FIELDS = (
    "version_id",
    "lineage_key",
    "链路编号",
    "上游表名",
    "下游表名",
    "链路版本",
    "迁移批次",
    "is_current",
)


class LineageWriteError(RuntimeError):
    """血缘链路版本表字段未落库时抛出，触发事务回滚。"""


class LineageTransaction:
    """模拟数据库事务：对版本表与批次表的写操作先记快照，异常时整体回滚。

    真实数据库里对应 ``BEGIN ... COMMIT/ROLLBACK``。内存实现通过深拷贝快照保证
    「字段没有落库时事务回滚」，避免半成品版本记录被旧查询读到。
    """

    def __init__(self, store: "Store") -> None:
        self._store = store
        self._snapshot_versions: list[dict[str, Any]] | None = None
        self._snapshot_batches: list[dict[str, Any]] | None = None
        self._snapshot_edges: list[dict[str, Any]] | None = None
        self._snapshot_track: list[dict[str, Any]] | None = None

    def __enter__(self) -> "LineageTransaction":
        self._snapshot_versions = deepcopy(self._store._lineage_versions)
        self._snapshot_batches = deepcopy(self._store._lineage_batches)
        self._snapshot_edges = deepcopy(self._store._lineage_edges)
        self._snapshot_track = deepcopy(self._store.rows("lineage_track"))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._store._lineage_versions = self._snapshot_versions  # type: ignore[assignment]
            self._store._lineage_batches = self._snapshot_batches  # type: ignore[assignment]
            self._store._lineage_edges = self._snapshot_edges  # type: ignore[assignment]
            self._store._tables["lineage_track"] = self._snapshot_track  # type: ignore[assignment]
        return False  # 不吞异常，交给调用方决定如何落失败批次


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录（旧查询视图：迁移前后都保持可读）
        self._lineage_edges: list[dict[str, Any]] = []
        # 血缘链路版本表：一条链路的一个版本一行
        self._lineage_versions: list[dict[str, Any]] = []
        # 血缘迁移批次表：迁移/续做/回滚批次与失败明细
        self._lineage_batches: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # 测试/故障注入开关：置 True 后版本表写入持续失败，模拟迁移中断落库故障
        self.fail_next_lineage_write = False

    def reset(self) -> None:
        """把内存仓库恢复到初始 seed 状态（测试隔离用）。"""
        self.__init__()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘链路版本表 ----

    @staticmethod
    def lineage_key(upstream: str, downstream: str) -> str:
        """链路稳定标识：同一张上游/下游表对在历史版本中始终指向同一条链路。"""
        return f"{upstream}->{downstream}"

    def lineage_transaction(self) -> LineageTransaction:
        return LineageTransaction(self)

    def _next_lineage_version_id(self) -> int:
        return max((int(r["version_id"]) for r in self._lineage_versions), default=0) + 1

    def _next_lineage_seq(self, lineage_key: str) -> int:
        """同一链路的版本序号从 1 递增，保证每条链路有独立版本序列。"""
        used = [int(r["version_seq"]) for r in self._lineage_versions
                if r["lineage_key"] == lineage_key]
        return max(used, default=0) + 1

    def write_lineage_version(self, record: dict[str, Any]) -> dict[str, Any]:
        """向血缘链路版本表落库一行；字段缺失或故障注入时抛错，由事务回滚。"""
        if self.fail_next_lineage_write:
            raise LineageWriteError("模拟数据库故障：血缘链路版本表写入失败")
        row = dict(record)
        row.setdefault("version_id", self._next_lineage_version_id())
        row.setdefault("created_at", datetime.now().isoformat())
        missing = [f for f in LINEAGE_VERSION_REQUIRED_FIELDS
                   if row.get(f) is None or str(row.get(f)).strip() == ""]
        if missing:
            raise LineageWriteError(f"血缘链路版本表字段未落库：{'、'.join(missing)}")
        self._lineage_versions.append(row)
        return row

    def get_lineage_version_rows(self) -> list[dict[str, Any]]:
        return list(self._lineage_versions)

    def find_lineage_versions(self, lineage_key: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._lineage_versions if r["lineage_key"] == lineage_key]

    def current_lineage_versions(self) -> list[dict[str, Any]]:
        """返回每条链路唯一的当前版本（is_current=True）。"""
        return [dict(r) for r in self._lineage_versions if r.get("is_current")]

    def find_current_lineage(self, lineage_key: str) -> dict[str, Any] | None:
        for row in self._lineage_versions:
            if row["lineage_key"] == lineage_key and row.get("is_current"):
                return dict(row)
        return None

    def mark_lineage_current(self, lineage_key: str, version_id: int,
                             operator_batch: str) -> dict[str, Any] | None:
        """版本切换：同一链路只保留一个当前版本，旧当前版本转为历史版本。"""
        target = None
        for row in self._lineage_versions:
            if row["lineage_key"] != lineage_key:
                continue
            if int(row["version_id"]) == version_id:
                target = row
            if row.get("is_current"):
                row["is_current"] = False
        if target is None:
            return None
        target["is_current"] = True
        target["切换批次"] = operator_batch
        target["switched_at"] = datetime.now().isoformat()
        return dict(target)

    def supersede_lineage_current(self, lineage_key: str, operator_batch: str) -> None:
        """停用某条链路的当前版本（回滚批次移除边时使用）。"""
        for row in self._lineage_versions:
            if row["lineage_key"] == lineage_key and row.get("is_current"):
                row["is_current"] = False
                row["切换批次"] = operator_batch

    # ---- 血缘迁移批次表 ----

    def create_lineage_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        row = dict(batch)
        row.setdefault("batch_id", self._next_lineage_batch_id())
        row.setdefault("created_at", datetime.now().isoformat())
        self._lineage_batches.append(row)
        return row

    def _next_lineage_batch_id(self) -> str:
        seq = len(self._lineage_batches) + 1
        return f"LB-{datetime.now().strftime('%Y%m%d')}-{seq:03d}"

    def update_lineage_batch(self, batch_id: str, **changes: Any) -> dict[str, Any] | None:
        for row in self._lineage_batches:
            if row["batch_id"] == batch_id:
                row.update(changes)
                row["updated_at"] = datetime.now().isoformat()
                return dict(row)
        return None

    def get_lineage_batches(self) -> list[dict[str, Any]]:
        return list(self._lineage_batches)

    def find_lineage_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in self._lineage_batches:
            if row["batch_id"] == batch_id:
                return dict(row)
        return None

    # ---- 血缘（旧查询兼容视图）----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        """登记一条血缘边。

        双写策略：
        - 旧查询视图（``_lineage_edges`` + ``lineage_track`` 模块表）保留原有中文临时
          字段，历史接口与测试不感知版本表；
        - 血缘链路版本表同步落一条当前版本，带迁移/采集批次，字段未落库则事务回滚，
          旧视图写入一并撤销。
        """
        key = self.lineage_key(upstream, downstream)
        existing = self.find_current_lineage(key)
        legacy_id = max(
            (int(r.get("id", 0)) for r in self.rows("lineage_track")),
            default=len(self._lineage_edges),
        ) + 1
        version_seq = self._next_lineage_seq(key)
        edge = {
            "id": legacy_id,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{version_seq}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        # 双写关闭时回退到旧行为：只写旧视图，由后续迁移批次补版本表
        if not settings.lineage_dual_write:
            self._lineage_edges.append(edge)
            self.rows("lineage_track").append(edge)
            return edge
        record = {
            "lineage_key": key,
            "version_seq": version_seq,
            "链路编号": f"LIN-AUTO-{legacy_id:04d}",
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": edge["链路版本"],
            "迁移批次": batch_id or "RUNTIME",
            "批次类型": "运行时写入",
            "is_current": True,
            "status": "已生效",
        }
        try:
            with self.lineage_transaction():
                written = self.write_lineage_version(record)
                self._lineage_edges.append(edge)
                self.rows("lineage_track").append(edge)
                if existing:
                    # 同一链路出现新版本：旧当前版本下线，保证一条链路只有一个当前版本
                    for row in self._lineage_versions:
                        if row["lineage_key"] == key and row.get("is_current") \
                                and int(row["version_id"]) != int(written["version_id"]):
                            row["is_current"] = False
        except LineageWriteError:
            # 事务已回滚版本表；旧视图同样不能出现孤儿边
            raise
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        """旧查询接口：优先返回版本表当前版本，未迁移部分回退旧记录。

        按链路稳定标识去重，保证「迁移后每条链路只有一个当前版本」。
        """
        merged: dict[str, dict[str, Any]] = {}
        # 先放旧记录兜底：某条链路的当前版本被回滚停用后，旧查询仍能读到边，
        # 不会把缺失边当成不存在。旧记录来自模块表与运行时边两个旧视图。
        for edge in [*self.rows("lineage_track"), *self._lineage_edges]:
            key = self.lineage_key(str(edge.get("上游表名", "")),
                                   str(edge.get("下游表名", "")))
            merged.setdefault(key, dict(edge))
        for row in self.current_lineage_versions():
            key = row["lineage_key"]
            merged[key] = {
                "id": row["version_id"],
                "上游表名": row["上游表名"],
                "下游表名": row["下游表名"],
                "来源字段": row.get("来源字段", ""),
                "目标字段": row.get("目标字段", ""),
                "转换逻辑": row.get("转换逻辑", ""),
                "采集批次": row.get("采集批次", ""),
                "链路版本": row.get("链路版本", ""),
                "迁移批次": row.get("迁移批次", ""),
                "status": row.get("status", "已生效"),
                "pending": False,
                "abnormal": False,
            }
        return list(merged.values())

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))


store = Store()
