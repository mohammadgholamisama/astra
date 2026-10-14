"""Full SQLite database backup / restore (consistent online snapshot)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from app.core.config import get_settings

logger = logging.getLogger("astra.backup")

BACKUP_FORMAT = "astra-backup-v1"
DB_ENTRY = "astra.db"
MANIFEST_ENTRY = "manifest.json"
SQLITE_HEADER = b"SQLite format 3\x00"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB


class BackupError(Exception):
    """User-facing backup/restore failure."""


def _is_sqlite_url(url: str) -> bool:
    # sqlite:///…  and  sqlite+aiosqlite:///…
    scheme = (url or "").split("://", 1)[0].lower()
    return scheme == "sqlite" or scheme.startswith("sqlite+")


def resolve_sqlite_path(database_url: str | None = None) -> Path:
    """Resolve filesystem path for sqlite DATABASE_URL."""
    url = database_url or get_settings().DATABASE_URL
    if not _is_sqlite_url(url):
        raise BackupError(
            "بک‌آپ فایل فقط برای دیتابیس SQLite پشتیبانی می‌شود. "
            "الان DATABASE_URL از نوع دیگری است."
        )

    # sqlite+aiosqlite:///./astra.db  OR  sqlite:///C:/path/db.sqlite
    raw = url.split(":///", 1)[-1] if ":///" in url else url.split("://", 1)[-1]
    raw = unquote(raw)
    path = Path(raw)
    if not path.is_absolute():
        # Relative paths are resolved from backend working directory (where uvicorn runs)
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    return path


def _sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _online_sqlite_copy(src: Path, dest: Path) -> None:
    """Consistent copy via SQLite Backup API (includes WAL pages)."""
    if not src.exists():
        raise BackupError(f"فایل دیتابیس پیدا نشد: {src}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=60)
    try:
        dest_conn = sqlite3.connect(str(dest), timeout=60)
        try:
            src_conn.backup(dest_conn)
            dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    with dest.open("rb") as f:
        head = f.read(16)
    if not head.startswith(SQLITE_HEADER):
        raise BackupError("کپی دیتابیس نامعتبر است (هدر SQLite نیست)")


def build_backup_zip(dest_zip: Path) -> dict[str, Any]:
    """Create zip backup at dest_zip. Returns manifest dict."""
    settings = get_settings()
    db_path = resolve_sqlite_path(settings.DATABASE_URL)
    created = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(prefix="astra-bak-") as tmp:
        tmp_dir = Path(tmp)
        db_copy = tmp_dir / DB_ENTRY
        _online_sqlite_copy(db_path, db_copy)
        digest = _sha256_file(db_copy)
        size = db_copy.stat().st_size

        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "created_at": created.isoformat(),
            "panel": settings.PANEL_NAME,
            "dialect": "sqlite",
            "db_filename": DB_ENTRY,
            "db_sha256": digest,
            "db_size": size,
            "app_version": "1.0.0",
            "source_path": str(db_path.name),
        }

        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        if dest_zip.exists():
            dest_zip.unlink()

        with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(
                MANIFEST_ENTRY,
                json.dumps(manifest, ensure_ascii=False, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.write(db_copy, arcname=DB_ENTRY)

    logger.info("Backup created %s (%s bytes, sha256=%s…)", dest_zip, size, digest[:12])
    return manifest


def _safe_extract_member(zf: zipfile.ZipFile, name: str, dest_dir: Path) -> Path:
    """Extract one member with zip-slip protection."""
    # Normalize and reject path traversal
    clean = name.replace("\\", "/").lstrip("/")
    if ".." in clean.split("/") or clean.startswith("/") or ":" in clean.split("/")[0]:
        raise BackupError("ساختار فایل بک‌آپ نامعتبر است")
    info = zf.getinfo(name)
    if info.is_dir():
        raise BackupError("عضو پوشه در بک‌آپ مجاز نیست")
    if info.file_size > MAX_UPLOAD_BYTES:
        raise BackupError("حجم فایل داخل بک‌آپ بیش از حد مجاز است")

    target = (dest_dir / Path(clean).name).resolve()
    if not str(target).startswith(str(dest_dir.resolve())):
        raise BackupError("مسیر استخراج نامعتبر است")

    with zf.open(info, "r") as src, target.open("wb") as out:
        copied = 0
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_UPLOAD_BYTES:
                raise BackupError("حجم فایل داخل بک‌آپ بیش از حد مجاز است")
            out.write(chunk)
    return target


def validate_and_extract_backup(upload_path: Path, work_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Validate uploaded zip; return (db_file, manifest)."""
    if not upload_path.exists() or upload_path.stat().st_size < 64:
        raise BackupError("فایل بک‌آپ خالی یا خیلی کوچک است")
    if upload_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise BackupError("حجم فایل بک‌آپ بیش از حد مجاز است (حداکثر ۵۱۲ مگابایت)")

    if not zipfile.is_zipfile(upload_path):
        raise BackupError("فایل باید یک آرشیو ZIP معتبر باشد (خروجی دکمه دانلود بک‌آپ)")

    with zipfile.ZipFile(upload_path, "r") as zf:
        names = zf.namelist()
        if MANIFEST_ENTRY not in names:
            raise BackupError("manifest.json در بک‌آپ نیست — فایل Astra نیست")
        if DB_ENTRY not in names and not any(n.endswith(".db") for n in names):
            raise BackupError("فایل دیتابیس داخل بک‌آپ پیدا نشد")

        manifest_path = _safe_extract_member(zf, MANIFEST_ENTRY, work_dir)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise BackupError("خواندن manifest.json ناموفق بود") from exc

        if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
            raise BackupError(
                f"فرمت بک‌آپ پشتیبانی نمی‌شود (انتظار: {BACKUP_FORMAT})"
            )
        if manifest.get("dialect") not in (None, "sqlite"):
            raise BackupError("این بک‌آپ برای SQLite نیست")

        db_name = str(manifest.get("db_filename") or DB_ENTRY)
        if db_name not in names:
            # fallback first .db entry
            db_candidates = [n for n in names if n.replace("\\", "/").endswith(".db")]
            if not db_candidates:
                raise BackupError("astra.db داخل بک‌آپ نیست")
            db_name = db_candidates[0]

        db_path = _safe_extract_member(zf, db_name, work_dir)
        # rename to canonical
        canonical = work_dir / DB_ENTRY
        if db_path != canonical:
            if canonical.exists():
                canonical.unlink()
            db_path.replace(canonical)
            db_path = canonical

    with db_path.open("rb") as f:
        head = f.read(16)
    if not head.startswith(SQLITE_HEADER):
        raise BackupError("فایل داخل بک‌آپ دیتابیس SQLite معتبر نیست")

    expected = manifest.get("db_sha256")
    if expected:
        actual = _sha256_file(db_path)
        if actual.lower() != str(expected).lower():
            raise BackupError("چک‌سام دیتابیس با manifest هم‌خوانی ندارد — فایل خراب است")

    # Quick integrity check
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise BackupError(f"integrity_check ناموفق: {row}")
            # Must look like an Astra DB (at least users or admins table)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "users" not in tables and "admins" not in tables:
                raise BackupError("دیتابیس شبیه پنل Astra نیست (جداول اصلی نیست)")
        finally:
            conn.close()
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"بررسی دیتابیس ناموفق: {exc}") from exc

    return db_path, manifest


def replace_live_database(new_db: Path) -> Path:
    """Replace live SQLite file. Caller must dispose SQLAlchemy engine first.

    Returns path to safety copy of previous DB (if any).
    """
    live = resolve_sqlite_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety: Path | None = None

    if live.exists():
        safety = live.with_name(f"{live.stem}.pre-restore-{stamp}{live.suffix}")
        shutil.copy2(live, safety)

    # Remove WAL/SHM companions so new file starts clean
    for suffix in ("-wal", "-shm"):
        side = Path(str(live) + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError as exc:
                logger.warning("Could not remove %s: %s", side, exc)

    tmp_target = live.with_name(f"{live.name}.restoring")
    if tmp_target.exists():
        tmp_target.unlink()
    shutil.copy2(new_db, tmp_target)
    tmp_target.replace(live)

    logger.info("Database restored to %s (safety=%s)", live, safety)
    return safety if safety else live


def backup_status() -> dict[str, Any]:
    settings = get_settings()
    if not _is_sqlite_url(settings.DATABASE_URL):
        return {
            "supported": False,
            "dialect": "other",
            "message": "بک‌آپ فایل فقط برای SQLite فعال است",
        }
    path = resolve_sqlite_path(settings.DATABASE_URL)
    size = path.stat().st_size if path.exists() else 0
    return {
        "supported": True,
        "dialect": "sqlite",
        "path_name": path.name,
        "size_bytes": size,
        "exists": path.exists(),
        "format": BACKUP_FORMAT,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


def suggested_backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    panel = re.sub(r"[^\w\-]+", "-", get_settings().PANEL_NAME.strip()) or "Astra"
    return f"{panel}-backup-{stamp}.zip"
