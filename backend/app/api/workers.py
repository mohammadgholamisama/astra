"""Admin + public API for Astra worker nodes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.settings import get_session_admin_only
from app.core.database import get_db
from app.models.admin import Admin
from app.schemas.workers import (
    WorkerAssignPanels,
    WorkerCreate,
    WorkerHeartbeatIn,
    WorkerOut,
    WorkerReinstall,
    WorkerUpdate,
    WorkerUsageReport,
)
from app.services import worker_ops as ops

router = APIRouter(tags=["Workers"])


@router.get("/api/admin/workers", response_model=list[WorkerOut])
async def list_workers_api(
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    return await ops.list_workers(db)


@router.post("/api/admin/workers", response_model=WorkerOut)
async def create_worker_api(
    body: WorkerCreate,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    """Create worker and auto-install agent on the VPS (SSH IP/user/password)."""
    try:
        return await ops.create_worker(db, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "WORKER_INVALID", "message": str(exc)},
        ) from exc


@router.post("/api/admin/workers/{worker_id}/reinstall", response_model=WorkerOut)
async def reinstall_worker_api(
    worker_id: int,
    body: WorkerReinstall,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        return await ops.reinstall_worker(
            db,
            worker_id,
            ssh_password=body.ssh_password,
            ssh_port=body.ssh_port,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Worker not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "WORKER_REINSTALL", "message": str(exc)},
        ) from exc


@router.get("/api/admin/workers/{worker_id}", response_model=WorkerOut)
async def get_worker_api(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    w = await ops.get_worker(db, worker_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    return ops.to_out(w, include_install=True)


@router.put("/api/admin/workers/{worker_id}", response_model=WorkerOut)
async def update_worker_api(
    worker_id: int,
    body: WorkerUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        return await ops.update_worker(db, worker_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Worker not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "WORKER_INVALID", "message": str(exc)},
        ) from exc


@router.put("/api/admin/workers/{worker_id}/panels", response_model=WorkerOut)
async def assign_panels_api(
    worker_id: int,
    body: WorkerAssignPanels,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        out = await ops.update_worker(
            db, worker_id, WorkerUpdate(panel_ids=list(body.panel_ids))
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Worker not found") from exc
    # Best-effort push assignment to the agent
    try:
        await ops.sync_assignments_to_worker(db, worker_id)
    except Exception:  # noqa: BLE001
        pass
    return out


@router.post("/api/admin/workers/{worker_id}/rotate-token", response_model=WorkerOut)
async def rotate_token_api(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        return await ops.rotate_token(db, worker_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Worker not found") from exc


@router.post("/api/admin/workers/{worker_id}/probe")
async def probe_worker_api(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    w = await ops.get_worker(db, worker_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    stats = await ops.probe_all_workers(db)
    fresh = await ops.get_worker(db, worker_id)
    return {
        "ok": True,
        "worker": ops.to_out(fresh) if fresh else None,
        "fleet": stats,
    }


@router.post("/api/admin/workers/{worker_id}/sync")
async def sync_worker_api(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    ok, err = await ops.sync_assignments_to_worker(db, worker_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "WORKER_SYNC_FAILED", "message": err or "sync failed"},
        )
    return {"ok": True}


@router.delete("/api/admin/workers/{worker_id}", status_code=204)
async def delete_worker_api(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        await ops.delete_worker(db, worker_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Worker not found") from exc
    return Response(status_code=204)


@router.post("/api/internal/worker/heartbeat")
async def worker_heartbeat(
    body: WorkerHeartbeatIn,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    token = _bearer(authorization)
    worker = await ops.authenticate_worker_token(db, token)
    if worker is None:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    await ops.touch_heartbeat(db, worker, version=body.version)
    return {"ok": True, "worker_id": int(worker.id)}


@router.post("/api/internal/worker/usage-report")
async def worker_usage_report(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    content_encoding: str | None = Header(default=None),
):
    """Accept JSON or gzip-compressed JSON usage maps from workers."""
    import gzip
    import json

    token = _bearer(authorization)
    worker = await ops.authenticate_worker_token(db, token)
    if worker is None:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    raw = await request.body()
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc or (len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B):
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8"))
        body = WorkerUsageReport.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"bad usage-report: {exc}") from exc
    try:
        stats = await ops.apply_usage_report(db, worker, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, **stats}


@router.get("/api/internal/worker/priority-probe")
async def worker_priority_probe(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    limit: int = 400,
):
    """Hot-tier keep-alive (~3s). Fair ≤15/panel across ALL panels, ≤400 total."""
    from app.services import usage_priority as uprio

    token = _bearer(authorization)
    worker = await ops.authenticate_worker_token(db, token)
    if worker is None:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    try:
        await ops.touch_heartbeat(db, worker)
    except Exception:  # noqa: BLE001
        pass
    lim = max(1, min(400, int(limit or 400)))
    items = await uprio.take_for_worker(int(worker.id), limit=lim)
    # Dual keys (`items` + `probes`) so older/newer Worker parsers both work.
    return {
        "ok": True,
        "items": items,
        "probes": items,
        "count": len(items),
        "worker_id": int(worker.id),
    }


@router.get("/api/internal/worker/assignment")
async def worker_assignment(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Exact leased panels for this worker + Hot/Warm tier hints.

    Workers must never poll panels not listed here (zero duplicate work).
    """
    from sqlalchemy.orm import selectinload

    from app.models.worker import WorkerNode

    token = _bearer(authorization)
    worker = await ops.authenticate_worker_token(db, token)
    if worker is None:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    try:
        await ops.touch_heartbeat(db, worker)
    except Exception:  # noqa: BLE001
        pass
    # Reload with panels for lease filtering.
    fresh = (
        await db.execute(
            select(WorkerNode)
            .where(WorkerNode.id == int(worker.id))
            .options(selectinload(WorkerNode.panels))
        )
    ).scalar_one_or_none()
    if fresh is None:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    return await ops.assignment_payload_for_worker(db, fresh)


@router.get("/api/internal/worker/commands")
async def worker_commands_longpoll(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    timeout: float = 15.0,
    limit: int = 20,
):
    """Long-poll fast-lane commands (KILL_USER). Wakes in <100ms when enqueued."""
    from app.services import worker_commands as wcmd

    token = _bearer(authorization)
    worker = await ops.authenticate_worker_token(db, token)
    if worker is None:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    # Heartbeat piggyback — no extra HTTP from worker.
    try:
        from app.services.worker_lease import note_heartbeat, refresh_leases_for_worker

        note_heartbeat(int(worker.id))
        refresh_leases_for_worker(int(worker.id))
        worker.last_seen_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        worker.status = "online"
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    cmds = await wcmd.take_commands(
        int(worker.id),
        limit=int(limit or 20),
        timeout=max(0.5, min(30.0, float(timeout or 15))),
    )
    return {"ok": True, "commands": cmds, "worker_id": int(worker.id)}


@router.get("/api/public/worker-install.sh", response_class=PlainTextResponse)
async def worker_install_script():
    """One-line bootstrap for a new worker VPS."""
    return PlainTextResponse(_INSTALL_SH, media_type="text/x-shellscript")


@router.get("/api/public/worker-bundle.tgz")
@router.get("/api/public/worker-bundle.tar.gz")
async def worker_bundle_tgz():
    from fastapi.responses import Response

    from app.services.worker_bundle import build_worker_tarball

    data = build_worker_tarball()
    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="astra-worker.tgz"',
            "Cache-Control": "no-store",
        },
    )


def _bearer(authorization: str | None) -> str:
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    raise HTTPException(status_code=401, detail="Bearer token required")


_INSTALL_SH = r'''#!/usr/bin/env bash
# Astra Worker installer — auto from panel (create/manage/usage offload)
set -euo pipefail
MASTER=""
TOKEN=""
PORT=2090
NAME="astra-worker"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --master) MASTER="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done
if [[ -z "$MASTER" || -z "$TOKEN" ]]; then
  echo "usage: bash install.sh --master URL --token TOKEN [--port 2090] [--name NAME]"
  exit 1
fi
MASTER="${MASTER%/}"
ROOT=/opt/astra-worker
DATA=/var/lib/astra-worker
echo "[astra-worker] installing into $ROOT (master=$MASTER port=$PORT)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates \
  redis-server sqlite3 ufw >/dev/null || \
  apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates redis-server sqlite3
mkdir -p "$ROOT" "$DATA"
# Fast local SQLite (WAL) — meter cache / agent state (source of truth stays on master)
sqlite3 "$DATA/cache.db" "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; CREATE TABLE IF NOT EXISTS meter_cache (panel_id INT NOT NULL, remote_username TEXT NOT NULL, used_traffic BIGINT NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(panel_id, remote_username));"
chown -R root:root "$DATA"
chmod 700 "$DATA"
# Redis for hot counters
systemctl enable --now redis-server 2>/dev/null || true
curl -fsSL "$MASTER/api/public/worker-bundle.tgz" -o /tmp/astra-worker.tgz \
  || curl -fsSL "$MASTER/api/public/worker-bundle.tar.gz" -o /tmp/astra-worker.tgz
rm -rf "$ROOT"/*
tar -xzf /tmp/astra-worker.tgz -C "$ROOT"
cd "$ROOT"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# Open agent port (best-effort)
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi
cat > /etc/astra-worker.env <<EOF
ASTRA_MASTER_URL=$MASTER
ASTRA_WORKER_TOKEN=$TOKEN
ASTRA_WORKER_PORT=$PORT
ASTRA_WORKER_NAME=$NAME
ASTRA_WORKER=1
DATABASE_URL=sqlite+aiosqlite:////var/lib/astra-worker/cache.db
REDIS_URL=redis://127.0.0.1:6379/0
SECRET_KEY=astra-worker-local
PYTHONPATH=/opt/astra-worker
EOF
chmod 600 /etc/astra-worker.env
cat > /etc/systemd/system/astra-worker.service <<'UNIT'
[Unit]
Description=Astra Worker (panel create/usage offload)
After=network-online.target redis-server.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/astra-worker.env
WorkingDirectory=/opt/astra-worker
ExecStart=/opt/astra-worker/.venv/bin/python -m worker --host 0.0.0.0 --port ${ASTRA_WORKER_PORT}
Restart=always
RestartSec=2
LimitNOFILE=1048576
Nice=-5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
# Ensure start even if parent SSH dies mid-script
systemctl enable astra-worker >/dev/null 2>&1 || true
systemctl restart astra-worker || systemctl start astra-worker
sleep 3
systemctl --no-pager --full status astra-worker | head -20 || true
curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:${PORT}/health" && echo
IP=$(curl -4 -fsS --max-time 4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo "[astra-worker] OK — base_url to: http://${IP}:${PORT}"
'''
