"""血缘链路版本表一次性迁移命令（上线流水线 / 容器部署调用）。

用法：
    python -m app.scripts.migrate_lineage [--policy resume|rollback|arbitrate] [--remark ...]

设计要点：
- 幂等可重入：重复执行即「续做批次」，只补未迁移的缺失边；
- 失败可恢复：版本表写入在事务内，失败整批回滚，批次与失败明细保留；
- 依赖升级顺序：先部署本版本（兼容旧查询的双写代码），再执行本命令，最后切流量，
  任一步失败都可重跑或按批次回滚。
"""
from __future__ import annotations

import argparse
import json

from app.config import settings
from app.services.lineage_version import CONFLICT_POLICIES, LineageVersionService


def main() -> int:
    parser = argparse.ArgumentParser(description="血缘链路版本表迁移")
    parser.add_argument("--policy", default=settings.lineage_migrate_policy,
                        choices=CONFLICT_POLICIES,
                        help="冲突策略：resume 续做（默认）/ rollback 一次性回滚 / arbitrate 人工仲裁")
    parser.add_argument("--remark", default="流水线迁移", help="批次备注")
    args = parser.parse_args()

    svc = LineageVersionService()
    before = svc.migration_status()
    result = svc.run_migration(conflict_policy=args.policy, remark=args.remark)
    after = svc.migration_status()
    print(json.dumps({"before": before, "result": result, "after": after},
                     ensure_ascii=False, indent=2))
    # 有失败边时以非零码退出，让流水线判定失败并保留现场
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
