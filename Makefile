.PHONY: install backend frontend migrate-lineage

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 血缘链路版本表迁移（幂等可重入，重复执行即为续做批次）
migrate-lineage:
	cd backend && .venv/bin/python -m app.scripts.migrate_lineage
