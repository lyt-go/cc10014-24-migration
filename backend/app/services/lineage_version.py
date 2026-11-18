"""血缘链路版本表迁移与版本切换服务。

职责：
1. 把旧血缘链路（``lineage_track`` 模块表里的临时字段记录）迁移到血缘链路版本表，
   每条边带迁移批次，批次与存量链路在同一事务内一致写入；
2. 迁移中断后支持续做（默认边界）或一次性回滚（放宽边界），冲突可仲裁；
3. 版本切换沿血缘追踪调用链定位结果，切换后每条链路只有一个当前版本；
4. 旧查询字段全部保留可读，转换编排等跨模块消费方无感知。

冲突策略（``conflict_policy``）：
- ``resume``（默认，严格边界）：已存在当前版本的链路跳过并计入续做批次，版本表展示
  续做批次与完整链路集；
- ``rollback``（放宽边界）：冲突链路先停用旧当前版本再写入新记录，批次展示为回滚
  批次与一次性迁移结果；
- ``arbitrate``：冲突不自动处理，挂起为待仲裁，由人工在页面/接口决定保留谁。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from app.store import LineageWriteError, store

LEGACY_MODULE = "lineage_track"
# 旧查询字段：迁移后仍按这些字段回读，保证兼容
LEGACY_FIELDS = ("链路编号", "上游表名", "下游表名", "来源字段", "目标字段",
                 "转换逻辑", "采集批次", "链路版本")
CONFLICT_POLICIES = ("resume", "rollback", "arbitrate")
BATCH_KIND_MIGRATE = "迁移批次"
BATCH_KIND_RESUME = "续做批次"
BATCH_KIND_ROLLBACK = "回滚批次"


def _same_payload(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(str(a.get(f, "")) == str(b.get(f, "")) for f in LEGACY_FIELDS)


class LineageVersionService:
    # ---- 迁移 ----

    def legacy_edges(self) -> list[dict[str, Any]]:
        """旧血缘链路记录（临时字段视图），含 seed 与运行时双写的行。"""
        return [dict(r) for r in store.rows(LEGACY_MODULE)]

    def migration_status(self) -> dict[str, Any]:
        legacy = self.legacy_edges()
        current = store.current_lineage_versions()
        migrated_keys = {r["lineage_key"] for r in current}
        legacy_keys = {store.lineage_key(str(r.get("上游表名", "")),
                                         str(r.get("下游表名", ""))) for r in legacy}
        pending = [r for r in legacy
                   if store.lineage_key(str(r.get("上游表名", "")),
                                        str(r.get("下游表名", ""))) not in migrated_keys]
        conflicts = [r for r in store.get_lineage_version_rows()
                     if r.get("conflict_state") == "待仲裁"]
        return {
            "legacy_total": len(legacy),
            "migrated_total": len(current),
            "pending_total": len(pending),
            "conflict_total": len(conflicts),
            "migrated_keys": sorted(migrated_keys),
            "legacy_keys": sorted(legacy_keys),
            "complete": not pending and not conflicts,
        }

    def run_migration(self, conflict_policy: str = "resume",
                      remark: str = "") -> dict[str, Any]:
        """执行一次存量血缘链路迁移，返回批次结果。

        批次记录与版本表写入分两段：批次先以「进行中」落库（失败记录必须保留），
        版本表在事务内写入，失败时事务回滚而批次保留为「失败」并附失败明细。
        """
        if conflict_policy not in CONFLICT_POLICIES:
            return {"ok": False, "message": f"未知冲突策略：{conflict_policy}"}
        legacy = self.legacy_edges()
        is_resume = bool(store.get_lineage_batches())
        batch_kind = BATCH_KIND_RESUME if is_resume else BATCH_KIND_MIGRATE
        if conflict_policy == "rollback":
            batch_kind = BATCH_KIND_ROLLBACK
        batch = store.create_lineage_batch({
            "kind": batch_kind,
            "conflict_policy": conflict_policy,
            "status": "进行中",
            "remark": remark,
            "total": len(legacy),
            "migrated": 0,
            "skipped": 0,
            "conflicts": 0,
            "failures": [],
        })
        migrated, skipped, conflicts, failures = 0, 0, 0, []
        for index, legacy_row in enumerate(legacy, start=1):
            upstream = str(legacy_row.get("上游表名", "")).strip()
            downstream = str(legacy_row.get("下游表名", "")).strip()
            key = store.lineage_key(upstream, downstream)
            current_row = store.find_current_lineage(key)
            if current_row is not None:
                action = self._handle_conflict(batch["batch_id"], conflict_policy,
                                               key, legacy_row, current_row)
                if action == "skip":
                    skipped += 1
                    continue
                if action == "conflict":
                    conflicts += 1
                    continue
            record = self._build_record(legacy_row, key, batch["batch_id"], batch_kind)
            try:
                with store.lineage_transaction():
                    if current_row is not None and conflict_policy == "rollback":
                        # 放宽边界：一次性迁移结果先停用旧当前版本，再写入新当前版本
                        store.supersede_lineage_current(key, batch["batch_id"])
                    store.write_lineage_version(record)
                migrated += 1
            except LineageWriteError as exc:
                failures.append({"index": index, "lineage_key": key, "reason": str(exc)})

        status = "失败" if failures else ("已完成" if not conflicts else "待仲裁")
        summary = {
            "ok": not failures,
            "status": status,
            "batch_id": batch["batch_id"],
            "kind": batch_kind,
            "conflict_policy": conflict_policy,
            "total": len(legacy),
            "migrated": migrated,
            "skipped": skipped,
            "conflicts": conflicts,
            "failures": failures,
        }
        summary.pop("batch_id", None)
        store.update_lineage_batch(batch["batch_id"], **summary)
        summary["batch_id"] = batch["batch_id"]
        return summary

    def _handle_conflict(self, batch_id: str, policy: str, key: str,
                         legacy_row: dict[str, Any], current_row: dict[str, Any]) -> str:
        """返回 skip / conflict / overwrite。"""
        if _same_payload(legacy_row, current_row):
            return "skip"
        if policy == "resume":
            # 严格边界：续做批次只补缺失边，已存在的当前版本保持不动
            return "skip"
        if policy == "arbitrate":
            for row in store.get_lineage_version_rows():
                if int(row["version_id"]) == int(current_row["version_id"]):
                    row["conflict_state"] = "待仲裁"
                    row["conflict_batch"] = batch_id
                    row["legacy_snapshot"] = {f: legacy_row.get(f, "") for f in LEGACY_FIELDS}
            return "conflict"
        # rollback：放宽边界，旧当前版本停用，由调用方写入一次性迁移结果
        return "overwrite"

    def _build_record(self, legacy_row: dict[str, Any], key: str,
                      batch_id: str, batch_kind: str) -> dict[str, Any]:
        version_seq = store._next_lineage_seq(key)
        return {
            "lineage_key": key,
            "version_seq": version_seq,
            "链路编号": str(legacy_row.get("链路编号") or f"LIN-{version_seq:04d}"),
            "上游表名": legacy_row.get("上游表名", ""),
            "下游表名": legacy_row.get("下游表名", ""),
            "来源字段": legacy_row.get("来源字段", ""),
            "目标字段": legacy_row.get("目标字段", ""),
            "转换逻辑": legacy_row.get("转换逻辑", ""),
            "采集批次": legacy_row.get("采集批次", ""),
            "链路版本": str(legacy_row.get("链路版本") or f"v{version_seq}"),
            "迁移批次": batch_id,
            "批次类型": batch_kind,
            "is_current": True,
            "status": str(legacy_row.get("status") or "已生效"),
            "conflict_state": "",
        }

    # ---- 回滚 ----

    def rollback_batch(self, batch_id: str) -> dict[str, Any]:
        """回滚指定迁移批次写入的所有当前版本（停用，不删历史，可恢复）。"""
        batch = store.find_lineage_batch(batch_id)
        if batch is None:
            return {"ok": False, "message": f"批次 {batch_id} 不存在"}
        affected = [r for r in store.get_lineage_version_rows()
                    if r.get("迁移批次") == batch_id]
        rollback_batch = store.create_lineage_batch({
            "kind": BATCH_KIND_ROLLBACK,
            "conflict_policy": "rollback",
            "status": "进行中",
            "remark": f"回滚批次 {batch_id}",
            "rollback_of": batch_id,
            "total": len(affected),
            "migrated": 0,
            "skipped": 0,
            "conflicts": 0,
            "failures": [],
        })
        try:
            with store.lineage_transaction():
                for row in affected:
                    store.supersede_lineage_current(row["lineage_key"],
                                                    rollback_batch["batch_id"])
                    row["rollback_batch"] = rollback_batch["batch_id"]
        except LineageWriteError as exc:
            store.update_lineage_batch(rollback_batch["batch_id"], status="失败",
                                       failures=[{"reason": str(exc)}])
            return {"ok": False, "message": str(exc), "batch_id": rollback_batch["batch_id"]}
        result = {"ok": True, "status": "已完成",
                  "rollback_of": batch_id, "affected": len(affected)}
        store.update_lineage_batch(rollback_batch["batch_id"], **result)
        result["batch_id"] = rollback_batch["batch_id"]
        return result

    # ---- 版本切换（沿血缘追踪调用链定位结果）----

    def list_versions(self) -> dict[str, Any]:
        """版本表页面数据：每条链路的版本集合 + 当前版本标记 + 迁移批次。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in store.get_lineage_version_rows():
            grouped.setdefault(row["lineage_key"], []).append(dict(row))
        links = []
        for key, rows in grouped.items():
            rows.sort(key=lambda r: int(r["version_seq"]))
            current = next((r for r in rows if r.get("is_current")), None)
            links.append({
                "lineage_key": key,
                "上游表名": rows[-1].get("上游表名", ""),
                "下游表名": rows[-1].get("下游表名", ""),
                "current_version": current.get("链路版本") if current else None,
                "current_batch": current.get("迁移批次") if current else None,
                "version_count": len(rows),
                "conflict_state": current.get("conflict_state", "") if current else "",
                "versions": rows,
            })
        links.sort(key=lambda x: x["lineage_key"])
        return {"items": links, "total": len(links),
                "batches": store.get_lineage_batches()}

    def switch_version(self, lineage_key: str, version_id: int) -> dict[str, Any]:
        """切换某条链路的当前版本，并沿血缘追踪调用链核对下游结果。"""
        target = next((r for r in store.get_lineage_version_rows()
                       if int(r["version_id"]) == version_id
                       and r["lineage_key"] == lineage_key), None)
        if target is None:
            return {"ok": False, "message": f"版本 {version_id} 不属于链路 {lineage_key}"}
        if target.get("conflict_state") == "待仲裁":
            return {"ok": False, "message": "该版本存在未仲裁冲突，请先仲裁再切换"}
        switch_batch = store.create_lineage_batch({
            "kind": "版本切换", "conflict_policy": "resume", "status": "进行中",
            "remark": f"切换 {lineage_key} -> version_id={version_id}",
            "total": 1, "migrated": 0, "skipped": 0, "conflicts": 0, "failures": [],
        })
        try:
            with store.lineage_transaction():
                switched = store.mark_lineage_current(lineage_key, version_id,
                                                      switch_batch["batch_id"])
                if switched is None:
                    raise LineageWriteError("版本切换未落库")
        except LineageWriteError as exc:
            store.update_lineage_batch(switch_batch["batch_id"], status="失败",
                                       failures=[{"reason": str(exc)}])
            return {"ok": False, "message": str(exc)}
        store.update_lineage_batch(switch_batch["batch_id"], status="已完成", migrated=1)
        # 沿血缘追踪调用链：从该链路下游表出发，定位受影响的完整调用链
        trace = self.trace_lineage(target["下游表名"], direction="downstream")
        return {"ok": True, "message": "版本切换完成",
                "batch_id": switch_batch["batch_id"],
                "current": switched, "trace": trace}
    def resolve_conflict(self, version_id: int, winner: str) -> dict[str, Any]:
        """冲突仲裁：winner=current 保留当前版本；winner=legacy 采用旧记录新版本。

        「血缘链路版本表同时出现冲突、优先谁尚未确定」即由这里人工裁决，
        仲裁前不允许版本切换，避免缺失边被当成不存在。
        """
        rows = store.get_lineage_version_rows()
        target = next((r for r in rows if int(r["version_id"]) == version_id), None)
        if target is None or target.get("conflict_state") != "待仲裁":
            return {"ok": False, "message": "该版本不存在待仲裁冲突"}
        if winner not in ("current", "legacy"):
            return {"ok": False, "message": "仲裁结果只能是 current 或 legacy"}
        arb_batch = store.create_lineage_batch({
            "kind": "冲突仲裁", "conflict_policy": "arbitrate", "status": "进行中",
            "remark": f"version_id={version_id} 仲裁保留 {winner}",
            "total": 1, "migrated": 0, "skipped": 0, "conflicts": 1, "failures": [],
        })
        try:
            with store.lineage_transaction():
                target["conflict_state"] = ""
                target["conflict_batch"] = ""
                target["arbitration_batch"] = arb_batch["batch_id"]
                if winner == "legacy":
                    snapshot = target.get("legacy_snapshot", {})
                    store.supersede_lineage_current(target["lineage_key"],
                                                    arb_batch["batch_id"])
                    record = self._build_record(snapshot, target["lineage_key"],
                                                arb_batch["batch_id"], "冲突仲裁")
                    record["链路编号"] = snapshot.get("链路编号", record["链路编号"])
                    store.write_lineage_version(record)
        except LineageWriteError as exc:
            store.update_lineage_batch(arb_batch["batch_id"], status="失败",
                                       failures=[{"reason": str(exc)}])
            return {"ok": False, "message": str(exc)}
        store.update_lineage_batch(arb_batch["batch_id"], status="已完成", migrated=1,
                                   conflicts=0)
        return {"ok": True, "message": f"仲裁完成，保留{'当前版本' if winner == 'current' else '旧记录版本'}",
                "batch_id": arb_batch["batch_id"]}

    # ---- 血缘追踪调用链 ----

    def trace_lineage(self, table_name: str, direction: str = "downstream") -> dict[str, Any]:
        """沿血缘链路版本表的当前版本，向上游/下游追踪完整调用链。"""
        if direction not in ("upstream", "downstream"):
            return {"ok": False, "message": "direction 仅支持 upstream/downstream"}
        current = store.current_lineage_versions()
        adjacency: dict[str, list[str]] = {}
        edge_index: dict[tuple[str, str], dict[str, Any]] = {}
        for row in current:
            up, down = row["上游表名"], row["下游表名"]
            if direction == "downstream":
                adjacency.setdefault(up, []).append(down)
            else:
                adjacency.setdefault(down, []).append(up)
            edge_index[(up, down)] = row
        visited: set[str] = set()
        order: list[str] = []
        queue = deque([table_name])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            order.append(node)
            for nxt in adjacency.get(node, []):
                if nxt not in visited:
                    queue.append(nxt)
        chain_edges = []
        for row in current:
            up, down = row["上游表名"], row["下游表名"]
            in_chain = (up in visited and down in visited)
            if in_chain:
                chain_edges.append({
                    "lineage_key": row["lineage_key"],
                    "上游表名": up, "下游表名": down,
                    "链路版本": row.get("链路版本"),
                    "迁移批次": row.get("迁移批次"),
                })
        return {"ok": True, "table": table_name, "direction": direction,
                "tables": order, "edges": chain_edges}

    def batches(self) -> list[dict[str, Any]]:
        return store.get_lineage_batches()
