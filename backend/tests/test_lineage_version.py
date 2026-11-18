"""血缘链路版本表迁移测试。

覆盖验收要点：
- 存量链路与迁移批次一致写入，迁移后每条链路只有一个当前版本、批次可回查；
- 版本表字段未落库时事务回滚，失败记录保留在批次里，缺失边不被当成不存在；
- 迁移中断后续做（严格边界）显示续做批次与完整链路集；
- 放宽边界（rollback 策略）显示回滚批次与一次性迁移结果；
- 冲突仲裁：版本表同时出现冲突时人工裁决，未仲裁不可切换；
- 版本切换沿血缘追踪调用链定位结果，可恢复；
- 旧查询字段仍可读取，转换编排跨模块消费版本表。
"""
from __future__ import annotations

import pytest

from app.store import LineageWriteError, store
from app.config import settings
from app.services.lineage_track import LineageTrackService
from app.services.lineage_version import LineageVersionService
from app.services.transform_dag import TransformDagService


def reset_store():
    store.reset()


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


class TestMigrationBasics:
    def test_migrate_seed_edges_with_batch(self):
        svc = LineageVersionService()
        result = svc.run_migration()
        assert result["ok"] is True
        assert result["status"] == "已完成"
        assert result["kind"] == "迁移批次"
        # seed 里 3 条血缘链路全部迁移
        assert result["migrated"] == 3
        current = store.current_lineage_versions()
        assert len(current) == 3
        # 每条链路只有一个当前版本，且都带同一个迁移批次
        assert {r["迁移批次"] for r in current} == {result["batch_id"]}
        # 批次表保留迁移记录
        batch = store.find_lineage_batch(result["batch_id"])
        assert batch is not None and batch["status"] == "已完成"

    def test_legacy_fields_still_readable_after_migration(self):
        svc = LineageVersionService()
        svc.run_migration()
        # 旧查询接口：中文字段原样可读
        edges = store.get_lineage_edges()
        assert len(edges) == 3
        for edge in edges:
            for field in ("上游表名", "下游表名", "采集批次", "链路版本"):
                assert field in edge and edge[field] != ""
        # 血缘追踪图（旧查询页面数据源）仍能渲染全部边
        graph = LineageTrackService().get_graph()
        pairs = {(n_edges["from"], n_edges["to"]) for n_edges in graph["edges"]}
        assert len(pairs) == 3

    def test_one_current_version_per_link_with_history(self):
        svc = LineageVersionService()
        svc.run_migration()
        # 同一条链路再次运行时写入新版本
        key = store.lineage_key("ods_orders", "dwd_order_detail")
        store.add_lineage("ods_orders", "dwd_order_detail", "order_id",
                          transform_logic="清洗+去重-v2", batch_id="RAW-002")
        versions = store.find_lineage_versions(key)
        assert len(versions) == 2
        currents = [r for r in versions if r["is_current"]]
        assert len(currents) == 1
        assert currents[0]["转换逻辑"] == "清洗+去重-v2"


class TestTransactionRollback:
    def test_failed_write_rolls_back_version_table_and_keeps_batch(self):
        svc = LineageVersionService()
        store.fail_next_lineage_write = True
        result = svc.run_migration()
        # 事务回滚：版本表没有任何半成品记录
        assert store.get_lineage_version_rows() == []
        # 失败记录保留在批次里，不会静默丢失
        assert result["ok"] is False
        assert result["status"] == "失败"
        assert result["failures"], "失败明细必须保留"
        batch = store.find_lineage_batch(result["batch_id"])
        assert batch["status"] == "失败" and batch["failures"]
        # 旧记录完好，缺失边不会被当成不存在
        assert len(store.rows("lineage_track")) == 3
        assert len(LineageTrackService().get_graph()["edges"]) == 3

    def test_runtime_add_lineage_rolls_back_on_write_failure(self):
        store.fail_next_lineage_write = True
        with pytest.raises(LineageWriteError):
            store.add_lineage("t_a", "t_b", "col", batch_id="B1")
        # 旧视图同样不能出现孤儿边：seed 的 3 条仍在，失败的 t_a->t_b 不在
        edges = store.get_lineage_edges()
        assert all(not (e.get("上游表名") == "t_a" and e.get("下游表名") == "t_b")
                   for e in edges)
        assert len(edges) == 3
        assert all(r.get("下游表名") != "t_b" for r in store.rows("lineage_track"))

    def test_manual_create_rolls_back_and_returns_error(self):
        lt = LineageTrackService()
        store.fail_next_lineage_write = True
        entry, missing = lt.create_entry({
            "链路编号": "LIN-X1", "上游表名": "t_x", "下游表名": "t_y"})
        assert entry is None and missing
        assert store.find_by_field("lineage_track", "链路编号", "LIN-X1") is None


class TestResumeAndRollbackBoundary:
    def test_resume_migration_after_interruption(self):
        svc = LineageVersionService()
        # 先成功迁移，再制造一条新的未迁移旧记录，模拟中断后的存量增量
        first = svc.run_migration()
        store.rows("lineage_track").append({
            "id": 99, "status": "已生效", "pending": False, "abnormal": False,
            "链路编号": "LIN-NEW", "上游表名": "ods_new", "下游表名": "dwd_new",
            "来源字段": "k", "目标字段": "k", "转换逻辑": "透传",
            "采集批次": "RAW-NEW", "链路版本": "v1"})
        second = svc.run_migration()  # 默认 resume，严格边界
        assert second["kind"] == "续做批次"
        assert second["migrated"] == 1
        # 已存在的链路被跳过，不产生重复当前版本
        assert second["skipped"] == 3
        currents = store.current_lineage_versions()
        assert len(currents) == 4
        # 版本表展示续做批次与完整链路集
        listing = svc.list_versions()
        assert listing["total"] == 4
        batch_ids = {b["batch_id"] for b in svc.batches()}
        assert {first["batch_id"], second["batch_id"]} <= batch_ids

    def test_rollback_boundary_gives_one_shot_result(self):
        svc = LineageVersionService()
        first = svc.run_migration()
        # 修改旧记录内容制造冲突，再以放宽边界一次性迁移
        row = store.rows("lineage_track")[0]
        row["转换逻辑"] = "字段透传+清洗+增强"
        second = svc.run_migration(conflict_policy="rollback")
        assert second["kind"] == "回滚批次"
        assert second["ok"] is True
        key = store.lineage_key(row["上游表名"], row["下游表名"])
        versions = store.find_lineage_versions(key)
        current = next(r for r in versions if r["is_current"])
        assert current["转换逻辑"] == "字段透传+清洗+增强"
        assert current["迁移批次"] == second["batch_id"]
        # 旧版本仍保留为历史，可回查
        assert any(not r["is_current"] and r["迁移批次"] == first["batch_id"]
                   for r in versions)

    def test_rollback_batch_deactivates_but_keeps_history(self):
        svc = LineageVersionService()
        migrated = svc.run_migration()
        rb = svc.rollback_batch(migrated["batch_id"])
        assert rb["ok"] is True and rb["affected"] == 3
        # 当前版本被停用，但版本行保留可恢复
        assert store.current_lineage_versions() == []
        assert len(store.get_lineage_version_rows()) == 3
        # 旧查询回退到旧记录，边依然存在
        assert len(store.get_lineage_edges()) == 3
        # 续做迁移可以恢复
        again = svc.run_migration()
        assert again["migrated"] == 3
        assert len(store.current_lineage_versions()) == 3


class TestConflictArbitration:
    def test_arbitrate_policy_holds_conflict_until_manual_decision(self):
        svc = LineageVersionService()
        svc.run_migration()
        row = store.rows("lineage_track")[0]
        row["转换逻辑"] = "冲突的新转换逻辑"
        result = svc.run_migration(conflict_policy="arbitrate")
        assert result["conflicts"] == 1
        pending = [r for r in store.get_lineage_version_rows()
                   if r.get("conflict_state") == "待仲裁"]
        assert len(pending) == 1
        # 未仲裁前不允许切换该链路版本
        key = pending[0]["lineage_key"]
        switch = svc.switch_version(key, int(pending[0]["version_id"]))
        assert switch["ok"] is False
        # 仲裁保留当前版本后可切换/查询恢复正常
        decided = svc.resolve_conflict(int(pending[0]["version_id"]), "current")
        assert decided["ok"] is True
        assert not any(r.get("conflict_state") == "待仲裁"
                       for r in store.get_lineage_version_rows())

    def test_arbitrate_legacy_winner_writes_new_current(self):
        svc = LineageVersionService()
        svc.run_migration()
        row = store.rows("lineage_track")[0]
        row["转换逻辑"] = "旧记录优先的逻辑"
        svc.run_migration(conflict_policy="arbitrate")
        pending = next(r for r in store.get_lineage_version_rows()
                       if r.get("conflict_state") == "待仲裁")
        decided = svc.resolve_conflict(int(pending["version_id"]), "legacy")
        assert decided["ok"] is True
        key = pending["lineage_key"]
        current = store.find_current_lineage(key)
        assert current["转换逻辑"] == "旧记录优先的逻辑"
        assert len([r for r in store.find_lineage_versions(key) if r["is_current"]]) == 1


class TestVersionSwitchAndTrace:
    def test_switch_version_returns_trace_chain(self):
        svc = LineageVersionService()
        svc.run_migration()
        # 构造 a->b->c 的跨链路调用链
        store.add_lineage("t_a", "t_b", "c1", batch_id="B1")
        store.add_lineage("t_b", "t_c", "c2", batch_id="B1")
        key = store.lineage_key("t_a", "t_b")
        # 再产生一个新版本
        store.add_lineage("t_a", "t_b", "c1", transform_logic="v2逻辑", batch_id="B2")
        versions = store.find_lineage_versions(key)
        first_version = next(r for r in versions if r["version_seq"] == 1)
        result = svc.switch_version(key, int(first_version["version_id"]))
        assert result["ok"] is True
        current = store.find_current_lineage(key)
        assert int(current["version_id"]) == int(first_version["version_id"])
        # 沿血缘追踪调用链定位到下游 t_b、t_c
        tables = result["trace"]["tables"]
        assert tables[0] == "t_b" and "t_c" in tables
        edge_keys = {e["lineage_key"] for e in result["trace"]["edges"]}
        assert store.lineage_key("t_b", "t_c") in edge_keys

    def test_trace_upstream_and_downstream(self):
        svc = LineageVersionService()
        store.add_lineage("t_a", "t_b", "c", batch_id="B")
        store.add_lineage("t_b", "t_c", "c", batch_id="B")
        down = svc.trace_lineage("t_a", "downstream")
        assert down["tables"] == ["t_a", "t_b", "t_c"]
        up = svc.trace_lineage("t_c", "upstream")
        assert up["tables"] == ["t_c", "t_b", "t_a"]


class TestCrossModuleConsumption:
    def test_dual_write_switch_falls_back_to_legacy_only(self):
        """关闭双写时只写旧视图，版本表保持为空，后续可由迁移批次补齐。"""
        try:
            object.__setattr__(settings, "lineage_dual_write", False)
            store.add_lineage("t_a", "t_b", "c", batch_id="B")
            assert store.get_lineage_version_rows() == []
            # 旧视图仍可读
            assert any(e.get("下游表名") == "t_b" for e in store.get_lineage_edges())
            # 重新开启双写并迁移，版本表补齐
            object.__setattr__(settings, "lineage_dual_write", True)
            result = LineageVersionService().run_migration()
            assert result["ok"] is True
            assert store.find_current_lineage(
                store.lineage_key("t_a", "t_b")) is not None
        finally:
            object.__setattr__(settings, "lineage_dual_write", True)

    def test_transform_dag_consumes_version_table(self):
        """转换编排执行后写入版本表，血缘追踪列表可核对结果。"""
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100", "order_status": "已完成",
             "dt": "2026-11-01"},
        ])
        td = TransformDagService()
        assert td.execute_node(1)["ok"] is True
        key = store.lineage_key("dwd_order_detail", "dws_order_daily")
        current = store.find_current_lineage(key)
        assert current is not None and current["is_current"] is True
        # 血缘追踪列表（旧查询页面）核对得到同一条边
        graph = LineageTrackService().get_graph()
        name_to_id = {n["name"]: n["id"] for n in graph["nodes"]}
        assert any(e["from"] == name_to_id.get("dwd_order_detail")
                   and e["to"] == name_to_id.get("dws_order_daily")
                   for e in graph["edges"])

    def test_migration_status_reports_pending_and_complete(self):
        svc = LineageVersionService()
        status = svc.migration_status()
        assert status["legacy_total"] == 3 and status["migrated_total"] == 0
        assert status["complete"] is False
        svc.run_migration()
        status = svc.migration_status()
        assert status["complete"] is True and status["pending_total"] == 0
