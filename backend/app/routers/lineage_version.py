"""血缘链路版本表接口：迁移批次、版本切换、冲突仲裁与血缘追踪调用链。

旧查询仍走 ``/api/lineage_track``（字段完全兼容）；本路由只承载版本表页面与
迁移流水线动作，对应上线流水线中的「迁移 → 校验 → 切换/回滚」步骤。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.lineage_version import CONFLICT_POLICIES, LineageVersionService

router = APIRouter(prefix="/api/lineage_versions", tags=["血缘链路版本表"])
service = LineageVersionService()


class MigrationPayload(BaseModel):
    conflict_policy: Literal["resume", "rollback", "arbitrate"] = "resume"
    remark: str = ""


class SwitchPayload(BaseModel):
    lineage_key: str
    version_id: int


class ConflictPayload(BaseModel):
    version_id: int
    winner: Literal["current", "legacy"]


class TracePayload(BaseModel):
    table_name: str
    direction: Literal["upstream", "downstream"] = "downstream"


class RollbackPayload(BaseModel):
    batch_id: str


@router.get("/status")
def migration_status() -> dict[str, Any]:
    """迁移就绪度：存量边数、已迁移数、待迁移缺失边、待仲裁冲突。"""
    return service.migration_status()


@router.get("")
def list_versions() -> dict[str, Any]:
    """版本表页面：每条链路的版本集合、当前版本与迁移批次。"""
    return service.list_versions()


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    return {"items": service.batches(), "total": len(service.batches())}


@router.post("/migrate")
def run_migration(payload: MigrationPayload) -> dict[str, Any]:
    """执行迁移（可重复执行=续做）。失败记录保留在批次里，版本表事务回滚。"""
    return service.run_migration(conflict_policy=payload.conflict_policy,
                                 remark=payload.remark)


@router.post("/rollback")
def rollback_batch(payload: RollbackPayload) -> dict[str, Any]:
    if not payload.batch_id:
        return {"ok": False, "message": "缺少 batch_id"}
    return service.rollback_batch(payload.batch_id)


@router.post("/switch")
def switch_version(payload: SwitchPayload) -> dict[str, Any]:
    """版本切换，返回沿血缘追踪调用链定位到的受影响链路结果。"""
    return service.switch_version(payload.lineage_key, payload.version_id)


@router.post("/resolve-conflict")
def resolve_conflict(payload: ConflictPayload) -> dict[str, Any]:
    """版本表同时出现冲突且优先方未定时的人工仲裁。"""
    return service.resolve_conflict(payload.version_id, payload.winner)


@router.post("/trace")
def trace_lineage(payload: TracePayload) -> dict[str, Any]:
    return service.trace_lineage(payload.table_name, payload.direction)
