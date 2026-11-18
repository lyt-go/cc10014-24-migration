"""血缘追踪 DAG 图服务：基于转换节点和数据流动自动生成并展示血缘关系。

查询兼容说明：旧查询字段（链路编号、上游/下游表名、采集批次、链路版本等临时字段）
保持可读；图数据统一按「版本表当前版本 + 尚未迁移的旧记录」合并，按链路稳定标识
去重，保证迁移期间缺失边不会被当成不存在。
"""
from __future__ import annotations

from typing import Any

from app.store import LineageWriteError, store

MODULE = "lineage_track"
REQUIRED_FIELDS = ["链路编号", "上游表名", "下游表名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用链路": "已停用"}
NEGATIVE_ACTIONS = ["停用链路"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _merged_lineage_rows() -> list[dict[str, Any]]:
    """合并旧记录与版本表当前版本；已迁移链路以版本表为准，未迁移链路保留旧记录。"""
    merged: dict[str, dict[str, Any]] = {}
    for row in store.rows(MODULE):
        key = store.lineage_key(str(row.get("上游表名", "")),
                                str(row.get("下游表名", "")))
        merged.setdefault(key, dict(row))
    for row in store.current_lineage_versions():
        merged[row["lineage_key"]] = {
            "id": row["version_id"],
            "链路编号": row.get("链路编号", ""),
            "上游表名": row.get("上游表名", ""),
            "下游表名": row.get("下游表名", ""),
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


class LineageTrackService:
    def get_graph(self) -> dict[str, Any]:
        rows = _merged_lineage_rows()
        nodes: list[dict] = []
        edges: list[dict] = []
        seen_names: set[str] = set()
        for row in rows:
            up = str(row.get("上游表名", ""))
            down = str(row.get("下游表名", ""))
            if up and up not in seen_names:
                nodes.append({"id": len(nodes) + 1, "name": up, "type": "table"})
                seen_names.add(up)
            if down and down not in seen_names:
                nodes.append({"id": len(nodes) + 1, "name": down, "type": "table"})
                seen_names.add(down)
        name_to_id = {n["name"]: n["id"] for n in nodes}
        for row in rows:
            up = str(row.get("上游表名", ""))
            down = str(row.get("下游表名", ""))
            if up in name_to_id and down in name_to_id:
                edges.append({
                    "from": name_to_id[up],
                    "to": name_to_id[down],
                    "label": str(row.get("转换逻辑", "依赖")),
                    "batch": str(row.get("采集批次", "")),
                    "version": str(row.get("链路版本", "")),
                    "migrate_batch": str(row.get("迁移批次", "")),
                })
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for k in ["来源字段", "目标字段", "转换逻辑", "采集批次", "链路版本"]:
            if k in values:
                entry[k] = values[k]
        key = store.lineage_key(str(entry["上游表名"]), str(entry["下游表名"]))
        version_seq = store._next_lineage_seq(key)
        entry.setdefault("链路版本", f"v{version_seq}")
        record = {
            "lineage_key": key,
            "version_seq": version_seq,
            "链路编号": entry["链路编号"],
            "上游表名": entry["上游表名"],
            "下游表名": entry["下游表名"],
            "来源字段": entry.get("来源字段", ""),
            "目标字段": entry.get("目标字段", ""),
            "转换逻辑": entry.get("转换逻辑", ""),
            "采集批次": entry.get("采集批次", ""),
            "链路版本": entry["链路版本"],
            "迁移批次": entry.get("采集批次", "") or "MANUAL",
            "批次类型": "手工登记",
            "is_current": True,
            "status": entry["status"],
        }
        try:
            with store.lineage_transaction():
                store.write_lineage_version(record)
                rows.append(entry)
        except LineageWriteError as exc:
            return None, [f"版本表未落库，事务已回滚：{exc}"]
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"血缘链路 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于血缘追踪可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 状态同步到版本表当前版本；落库失败则旧记录状态一并回滚
        key = store.lineage_key(str(entry.get("上游表名", "")),
                                str(entry.get("下游表名", "")))
        current = store.find_current_lineage(key)
        try:
            with store.lineage_transaction():
                if current is not None:
                    for row in store.get_lineage_version_rows():
                        if int(row["version_id"]) == int(current["version_id"]):
                            row["status"] = target
                store.save_version(MODULE, entry_id, entry)
        except LineageWriteError as exc:
            return None, f"状态变更未落库，事务已回滚：{exc}"
        return entry, f"血缘链路已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]

    def auto_generate_from_transform(self) -> dict[str, Any]:
        """从transform_dag模块自动生成血缘关系。"""
        from app.services.transform_dag import TransformDagService
        svc = TransformDagService()
        nodes, _ = svc.raw_data()
        created = 0
        for node in nodes:
            up = node.get("输入表名", "")
            down = node.get("输出表名", "")
            if up and down:
                store.add_lineage(up, down,
                                  field_map=node.get("节点编号", ""),
                                  transform_logic=node.get("转换脚本", ""),
                                  batch_id=node.get("节点编号", ""))
                created += 1
        return {"ok": True, "created": created}
