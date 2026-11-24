"""Admin full-database backup & restore — JWT session only."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import oauth2_scheme
from app.core.database import AsyncSessionLocal, engine, get_db
from app.core.schema_sync import ensure_schema
from app.core.security import decode_token
from app.models.admin import Admin
from app.seed import seed_admin
from app.services import backup as bak
from app.services.site_settings import refresh_cache as refresh_site_settings

logger = logging.getLogger("astra.backup.api")
router = APIRouter(tags=["Backup"])

_lock = asyncio.Lock()


async def get_session_admin_only(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    """Backup/restore is destructive — panel JWT only, never API keys."""
    if request.headers.get("X-Api-Key"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "بک‌آپ فقط با ورود به پنل مجاز است",
            },
        )
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("apikey "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "بک‌آپ فقط با ورود به پنل مجاز است",
            },
        )

    bearer = token
    if not bearer and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    if not bearer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_token(bearer)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(Admin).where(Admin.username == payload["sub"]))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found")
    return admin


def _http_backup_error(exc: bak.BackupError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "BACKUP_ERROR", "message": str(exc)},
    )


@router.get("/api/admin/backup/status")
async def backup_status(_admin: Admin = Depends(get_session_admin_only)):
    st = bak.backup_status()
    st["cache"] = bak.backup_cache_meta()
    return st


@router.get("/api/admin/backup/prepare")
@router.post("/api/admin/backup/prepare")
async def prepare_backup(_admin: Admin = Depends(get_session_admin_only)):
    """Build a full DB zip in the background; poll until ready then download.

    Avoids Cloudflare/browser ``Failed to fetch`` while pg_dump runs on large DBs.
    """
    meta = bak.backup_cache_meta()
    if meta.get("ready"):
        return {"ok": True, "ready": True, "building": False, **meta}
    if meta.get("building") or _lock.locked():
        return {"ok": True, "ready": False, "building": True, **meta}

    async def _bg() -> None:
        async with _lock:
            try:
                await asyncio.to_thread(bak.build_backup_zip_cached)
            except Exception:  # noqa: BLE001
                logger.exception("bg backup prepare failed")

    try:
        asyncio.create_task(_bg(), name="astra-backup-prepare")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"code": "BACKUP_PREPARE_FAILED", "message": str(exc)},
        ) from exc
    return {"ok": True, "ready": False, "building": True, **bak.backup_cache_meta()}


@router.get("/api/admin/backup")
async def download_backup(_admin: Admin = Depends(get_session_admin_only)):
    """Download full DB backup zip (cache-first; builds once if needed)."""
    # Fast path: serve a fresh prepared zip without re-dumping.
    cached = bak.get_fresh_backup_zip()
    if cached is not None:
        path, filename, size = cached
        return FileResponse(
            path=path,
            media_type="application/zip",
            filename=filename,
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Content-Length": str(size),
                "X-Astra-Backup-Format": bak.BACKUP_FORMAT,
                "X-Astra-Backup-Bytes": str(size),
                "X-Astra-Backup-Cached": "1",
            },
        )

    if _lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "BACKUP_BUSY",
                "message": "بک‌آپ در حال آماده‌سازی است — چند ثانیه بعد دوباره دانلود کنید",
            },
        )

    async with _lock:
        try:
            meta = await asyncio.to_thread(bak.build_backup_zip_cached)
        except bak.BackupError as exc:
            raise _http_backup_error(exc) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backup failed")
            raise HTTPException(
                status_code=500,
                detail={"code": "BACKUP_FAILED", "message": f"ساخت بک‌آپ ناموفق: {exc}"},
            ) from exc

        cached2 = bak.get_fresh_backup_zip()
        if cached2 is None:
            raise HTTPException(
                status_code=500,
                detail={"code": "BACKUP_FAILED", "message": "فایل بک‌آپ بعد از ساخت پیدا نشد"},
            )
        path, filename, size = cached2
        return FileResponse(
            path=path,
            media_type="application/zip",
            filename=filename or str(meta.get("filename") or bak.suggested_backup_filename()),
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Content-Length": str(size),
                "X-Astra-Backup-Format": bak.BACKUP_FORMAT,
                "X-Astra-Backup-Bytes": str(size),
                "X-Astra-Backup-Cached": "0",
            },
        )


@router.post("/api/admin/backup/restore")
async def restore_backup(
    file: UploadFile = File(..., description="Astra backup zip"),
    _admin: Admin = Depends(get_session_admin_only),
):
    """Replace live database with an uploaded Astra backup zip."""
    if _lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "BACKUP_BUSY", "message": "عملیات بک‌آپ/بازیابی دیگری در حال اجراست"},
        )

    name = (file.filename or "").lower()
    if name and not (name.endswith(".zip") or name.endswith(".astra.zip")):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_FILE",
                "message": "فقط فایل ZIP خروجی همین صفحه را آپلود کنید",
            },
        )

    async with _lock:
        with tempfile.TemporaryDirectory(prefix="astra-restore-") as tmp:
            work = Path(tmp)
            upload_path = work / "upload.zip"
            # Stream upload to disk with size cap
            written = 0
            try:
                with upload_path.open("wb") as out:
                    while True:
                        chunk = await file.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > bak.MAX_UPLOAD_BYTES:
                            raise bak.BackupError(
                                "حجم فایل بک‌آپ بیش از حد مجاز است (حداکثر ۲ گیگابایت)"
                            )
                        out.write(chunk)
            finally:
                await file.close()

            try:
                db_file, manifest = await asyncio.to_thread(
                    bak.validate_and_extract_backup, upload_path, work
                )
            except bak.BackupError as exc:
                raise _http_backup_error(exc) from exc
            except Exception as exc:  # noqa: BLE001
                logger.exception("Backup validation failed")
                raise HTTPException(
                    status_code=400,
                    detail={"code": "BACKUP_INVALID", "message": f"اعتبارسنجی ناموفق: {exc}"},
                ) from exc

            # Drop pooled connections before swapping SQLite file / pg_restore
            await engine.dispose()
            try:
                safety = await asyncio.to_thread(
                    bak.restore_payload, db_file, manifest
                )
            except bak.BackupError as exc:
                await engine.dispose()
                raise _http_backup_error(exc) from exc
            except Exception as exc:  # noqa: BLE001
                logger.exception("Restore replace failed")
                await engine.dispose()
                raise HTTPException(
                    status_code=500,
                    detail={
                        "code": "RESTORE_FAILED",
                        "message": f"جایگزینی دیتابیس ناموفق: {exc}",
                    },
                ) from exc

            # Re-init schema + caches on the restored DB
            try:
                await ensure_schema()
                await seed_admin()
                async with AsyncSessionLocal() as db:
                    await refresh_site_settings(db)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Post-restore init failed")
                raise HTTPException(
                    status_code=500,
                    detail={
                        "code": "RESTORE_INIT_FAILED",
                        "message": (
                            "دیتابیس جایگزین شد ولی راه‌اندازی بعدی خطا داد — "
                            f"کپی ایمنی: {getattr(safety, 'name', safety)} · {exc}"
                        ),
                    },
                ) from exc

            return {
                "ok": True,
                "message": (
                    "بازیابی کامل انجام شد (کاربران، مصرف، گروه‌ها، پنل‌ها، تنظیمات). "
                    "برای اطمینان یک‌بار از حساب خارج و دوباره وارد شوید."
                ),
                "manifest": {
                    "format": manifest.get("format"),
                    "created_at": manifest.get("created_at"),
                    "panel": manifest.get("panel"),
                    "dialect": manifest.get("dialect"),
                    "db_size": manifest.get("db_size"),
                    "app_version": manifest.get("app_version"),
                    "table_counts_highlight": manifest.get("table_counts_highlight"),
                },
                "safety_copy": getattr(safety, "name", str(safety)),
            }
