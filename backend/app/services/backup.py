"""Full panel database backup / restore — SQLite and PostgreSQL.

Zip layout (v2):
  manifest.json
  version.txt
  db/astra.db      (sqlite)  OR  db/astra.dump  (pg_dump -Fc)
  counts.json      (row counts for sanity)

v1 zip (legacy): manifest + astra.db at root — still restorable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.core.config import get_settings

logger = logging.getLogger("astra.backup")

BACKUP_FORMAT = "astra-backup-v2"
BACKUP_FORMAT_V1 = "astra-backup-v1"
DB_ENTRY_SQLITE = "db/astra.db"
DB_ENTRY_PG = "db/astra.dump"
DB_ENTRY_V1 = "astra.db"
MANIFEST_ENTRY = "manifest.json"
COUNTS_ENTRY = "counts.json"
VERSION_ENTRY = "version.txt"
SQLITE_HEADER = b"SQLite format 3\x00"
# Large Postgres dumps (full users/usage/panels)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
PG_DUMP_TIMEOUT = 60 * 30  # 30 min
PG_RESTORE_TIMEOUT = 60 * 45


class BackupError(Exception):
    """User-facing backup/restore failure."""


def _read_app_version() -> str:
    candidates = [
        Path("/opt/astra/VERSION"),
        Path(__file__).resolve().parents[3] / "VERSION",
        Path.cwd() / "VERSION",
    ]
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
                if v:
                    return v
        except Exception:  # noqa: BLE001
            continue
    return "unknown"


def dialect_of(database_url: str | None = None) -> str:
    url = (database_url or get_settings().DATABASE_URL or "").strip().lower()
    scheme = url.split("://", 1)[0]
    if scheme == "sqlite" or scheme.startswith("sqlite+"):
        return "sqlite"
    if "postgres" in scheme:
        return "postgresql"
    return "other"


def _is_sqlite_url(url: str) -> bool:
    return dialect_of(url) == "sqlite"


def _is_postgres_url(url: str) -> bool:
    return dialect_of(url) == "postgresql"


def resolve_sqlite_path(database_url: str | None = None) -> Path:
    """Resolve filesystem path for sqlite DATABASE_URL."""
    url = database_url or get_settings().DATABASE_URL
    if not _is_sqlite_url(url):
        raise BackupError("آدرس دیتابیس SQLite نیست")

    raw = url.split(":///", 1)[-1] if ":///" in url else url.split("://", 1)[-1]
    raw = unquote(raw)
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    return path


def _pg_libpq_url(database_url: str | None = None) -> str:
    """SQLAlchemy asyncpg URL → libpq URL for pg_dump/pg_restore."""
    url = (database_url or get_settings().DATABASE_URL or "").strip()
    if not _is_postgres_url(url):
        raise BackupError("آدرس دیتابیس PostgreSQL نیست")
    for prefix in (
        "postgresql+asyncpg://",
        "postgres+asyncpg://",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
    ):
        if url.lower().startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    if url.lower().startswith("postgresql://") or url.lower().startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    raise BackupError("فرمت DATABASE_URL برای PostgreSQL نامعتبر است")


def _pg_safe_label(database_url: str | None = None) -> str:
    """host:port/db without password — for UI status."""
    try:
        libpq = _pg_libpq_url(database_url)
        p = urlparse(libpq)
        host = p.hostname or "localhost"
        port = p.port or 5432
        db = (p.path or "/").lstrip("/") or "postgres"
        user = p.username or ""
        return f"{user}@{host}:{port}/{db}" if user else f"{host}:{port}/{db}"
    except Exception:  # noqa: BLE001
        return "postgresql"


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


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


def _sqlite_table_counts(db_path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for name in tables:
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                out[name] = int(n)
            except Exception:  # noqa: BLE001
                out[name] = -1
    finally:
        conn.close()
    return out


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int) -> None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"ابزار پیدا نشد: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"زمان عملیات تمام شد: {cmd[0]}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        err = err[-800:] if err else f"exit {proc.returncode}"
        raise BackupError(f"{cmd[0]} ناموفق: {err}")


def _pg_dump_to_file(dest: Path, database_url: str | None = None) -> None:
    dump = _which("pg_dump")
    if not dump:
        raise BackupError(
            "ابزار pg_dump روی سرور نصب نیست — "
            "بسته postgresql-client را نصب کنید"
        )
    libpq = _pg_libpq_url(database_url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    # Custom format: compressed, supports parallel restore, clean --if-exists
    cmd = [
        dump,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--verbose",
        f"--file={dest}",
        libpq,
    ]
    env = os.environ.copy()
    # Prefer URL; also set SSL if needed via URL query
    _run(cmd, env=env, timeout=PG_DUMP_TIMEOUT)
    if not dest.exists() or dest.stat().st_size < 32:
        raise BackupError("خروجی pg_dump خالی است")


def _pg_table_counts(database_url: str | None = None) -> dict[str, int]:
    """Best-effort counts via psql (optional)."""
    psql = _which("psql")
    if not psql:
        return {}
    libpq = _pg_libpq_url(database_url)
    sql = (
        "SELECT json_object_agg(relname, n_live_tup) "
        "FROM pg_stat_user_tables;"
    )
    try:
        proc = subprocess.run(
            [psql, libpq, "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        raw = (proc.stdout or "").strip()
        if not raw or raw == "null":
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): int(v or 0) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        logger.debug("pg table counts failed", exc_info=True)
    return {}


def build_backup_zip(dest_zip: Path) -> dict[str, Any]:
    """Create zip backup at dest_zip. Returns manifest dict."""
    settings = get_settings()
    dialect = dialect_of(settings.DATABASE_URL)
    if dialect not in ("sqlite", "postgresql"):
        raise BackupError(
            "بک‌آپ فقط برای SQLite و PostgreSQL پشتیبانی می‌شود "
            f"(dialect={dialect})"
        )

    created = datetime.now(timezone.utc)
    app_version = _read_app_version()

    with tempfile.TemporaryDirectory(prefix="astra-bak-") as tmp:
        tmp_dir = Path(tmp)
        db_dir = tmp_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}

        if dialect == "sqlite":
            db_path = resolve_sqlite_path(settings.DATABASE_URL)
            db_copy = tmp_dir / DB_ENTRY_SQLITE
            db_copy.parent.mkdir(parents=True, exist_ok=True)
            _online_sqlite_copy(db_path, db_copy)
            counts = _sqlite_table_counts(db_copy)
            db_arc = DB_ENTRY_SQLITE
            source_label = db_path.name
        else:
            db_copy = tmp_dir / DB_ENTRY_PG
            _pg_dump_to_file(db_copy, settings.DATABASE_URL)
            counts = _pg_table_counts(settings.DATABASE_URL)
            db_arc = DB_ENTRY_PG
            source_label = _pg_safe_label(settings.DATABASE_URL)

        digest = _sha256_file(db_copy)
        size = db_copy.stat().st_size

        # Core tables we care about for migration sanity (names vary slightly)
        interesting = {
            "users",
            "panel_accounts",
            "panels",
            "groups",
            "user_groups",
            "group_panels",
            "admins",
            "site_settings",
            "api_keys",
            "user_templates",
            "astra_node_clients",
            "astra_node_outbounds",
        }
        highlight = {k: counts[k] for k in sorted(counts) if k in interesting}

        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "created_at": created.isoformat(),
            "panel": settings.PANEL_NAME,
            "dialect": dialect,
            "db_filename": db_arc,
            "db_sha256": digest,
            "db_size": size,
            "app_version": app_version,
            "source": source_label,
            "includes": [
                "users",
                "usage",
                "groups",
                "panels",
                "panel_accounts",
                "admins",
                "settings",
                "templates",
                "api_keys",
                "analytics",
                "astra_node",
            ],
            "table_counts_highlight": highlight,
        }

        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        if dest_zip.exists():
            dest_zip.unlink()

        with zipfile.ZipFile(
            dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            zf.writestr(
                MANIFEST_ENTRY,
                json.dumps(manifest, ensure_ascii=False, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.writestr(VERSION_ENTRY, app_version + "\n")
            zf.writestr(
                COUNTS_ENTRY,
                json.dumps(counts, ensure_ascii=False, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            zf.write(db_copy, arcname=db_arc)

    logger.info(
        "Backup created %s dialect=%s (%s bytes, sha256=%s…)",
        dest_zip,
        dialect,
        size,
        digest[:12],
    )
    return manifest


def _safe_extract_member(zf: zipfile.ZipFile, name: str, dest_dir: Path) -> Path:
    """Extract one member with zip-slip protection."""
    clean = name.replace("\\", "/").lstrip("/")
    if ".." in clean.split("/") or clean.startswith("/") or ":" in clean.split("/")[0]:
        raise BackupError("ساختار فایل بک‌آپ نامعتبر است")
    info = zf.getinfo(name)
    if info.is_dir():
        raise BackupError("عضو پوشه در بک‌آپ مجاز نیست")
    if info.file_size > MAX_UPLOAD_BYTES:
        raise BackupError("حجم فایل داخل بک‌آپ بیش از حد مجاز است")

    # Keep relative path under dest_dir (allow db/ prefix)
    target = (dest_dir / clean).resolve()
    if not str(target).startswith(str(dest_dir.resolve())):
        raise BackupError("مسیر استخراج نامعتبر است")
    target.parent.mkdir(parents=True, exist_ok=True)

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


def validate_and_extract_backup(
    upload_path: Path, work_dir: Path
) -> tuple[Path, dict[str, Any]]:
    """Validate uploaded zip; return (db_payload_file, manifest)."""
    if not upload_path.exists() or upload_path.stat().st_size < 64:
        raise BackupError("فایل بک‌آپ خالی یا خیلی کوچک است")
    if upload_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise BackupError("حجم فایل بک‌آپ بیش از حد مجاز است (حداکثر ۲ گیگابایت)")

    if not zipfile.is_zipfile(upload_path):
        raise BackupError("فایل باید یک آرشیو ZIP معتبر باشد (خروجی دکمه دانلود بک‌آپ)")

    with zipfile.ZipFile(upload_path, "r") as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
        # map original names
        name_map = {n.replace("\\", "/"): n for n in zf.namelist()}

        if MANIFEST_ENTRY not in names:
            raise BackupError("manifest.json در بک‌آپ نیست — فایل Astra نیست")

        manifest_path = _safe_extract_member(
            zf, name_map[MANIFEST_ENTRY], work_dir
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise BackupError("خواندن manifest.json ناموفق بود") from exc

        if not isinstance(manifest, dict):
            raise BackupError("manifest.json نامعتبر است")

        fmt = str(manifest.get("format") or "")
        if fmt not in (BACKUP_FORMAT, BACKUP_FORMAT_V1):
            raise BackupError(
                f"فرمت بک‌آپ پشتیبانی نمی‌شود (انتظار: {BACKUP_FORMAT} یا {BACKUP_FORMAT_V1})"
            )

        dialect = str(manifest.get("dialect") or "sqlite").lower()
        if dialect not in ("sqlite", "postgresql"):
            raise BackupError(f"dialect بک‌آپ پشتیبانی نمی‌شود: {dialect}")

        # Resolve payload name
        preferred = str(manifest.get("db_filename") or "").replace("\\", "/")
        candidates: list[str] = []
        if preferred and preferred in names:
            candidates.append(preferred)
        if dialect == "sqlite":
            candidates.extend(
                [
                    DB_ENTRY_SQLITE,
                    DB_ENTRY_V1,
                    *[n for n in names if n.endswith(".db")],
                ]
            )
        else:
            candidates.extend(
                [
                    DB_ENTRY_PG,
                    "db/astra.dump",
                    "astra.dump",
                    *[n for n in names if n.endswith(".dump") or n.endswith(".backup")],
                ]
            )

        db_name = next((c for c in candidates if c in names), None)
        if not db_name:
            raise BackupError("فایل دیتابیس داخل بک‌آپ پیدا نشد")

        db_path = _safe_extract_member(zf, name_map[db_name], work_dir)

        # Canonicalize path
        if dialect == "sqlite":
            canonical = work_dir / "astra.db"
        else:
            canonical = work_dir / "astra.dump"
        if db_path.resolve() != canonical.resolve():
            if canonical.exists():
                canonical.unlink()
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_path, canonical)
            db_path = canonical

    expected = manifest.get("db_sha256")
    if expected:
        actual = _sha256_file(db_path)
        if actual.lower() != str(expected).lower():
            raise BackupError("چک‌سام دیتابیس با manifest هم‌خوانی ندارد — فایل خراب است")

    if dialect == "sqlite":
        with db_path.open("rb") as f:
            head = f.read(16)
        if not head.startswith(SQLITE_HEADER):
            raise BackupError("فایل داخل بک‌آپ دیتابیس SQLite معتبر نیست")
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    raise BackupError(f"integrity_check ناموفق: {row}")
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
    else:
        # pg_dump custom format starts with "PGDMP"
        with db_path.open("rb") as f:
            magic = f.read(5)
        if magic != b"PGDMP":
            raise BackupError("فایل dump پستگرس معتبر نیست (هدر PGDMP نیست)")

    manifest["dialect"] = dialect
    return db_path, manifest


def replace_live_database(new_db: Path) -> Path:
    """Replace live SQLite file. Caller must dispose SQLAlchemy engine first."""
    live = resolve_sqlite_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety: Path | None = None

    if live.exists():
        safety = live.with_name(f"{live.stem}.pre-restore-{stamp}{live.suffix}")
        shutil.copy2(live, safety)

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


def _pg_terminate_backends(libpq: str) -> None:
    psql = _which("psql")
    if not psql:
        return
    # Terminate other sessions on this database so restore can lock
    sql = """
    SELECT pg_terminate_backend(pid)
    FROM pg_stat_activity
    WHERE datname = current_database()
      AND pid <> pg_backend_pid()
      AND backend_type = 'client backend';
    """
    try:
        subprocess.run(
            [psql, libpq, "-v", "ON_ERROR_STOP=0", "-c", sql],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        logger.debug("terminate backends failed", exc_info=True)


def restore_postgres_dump(dump_path: Path) -> str:
    """Restore pg_dump custom file into live DATABASE_URL. Returns safety note."""
    restore = _which("pg_restore")
    if not restore:
        raise BackupError(
            "ابزار pg_restore روی سرور نصب نیست — "
            "بسته postgresql-client را نصب کنید"
        )
    settings = get_settings()
    if not _is_postgres_url(settings.DATABASE_URL):
        raise BackupError(
            "بک‌آپ PostgreSQL است ولی سرور فعلی SQLite است — "
            "روی سرور مقصد DATABASE_URL را PostgreSQL بگذارید"
        )
    libpq = _pg_libpq_url(settings.DATABASE_URL)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety_note = f"pg-pre-restore-{stamp}"

    # Safety dump of current DB (best effort)
    try:
        safety_dir = Path(tempfile.gettempdir()) / "astra-pg-safety"
        safety_dir.mkdir(parents=True, exist_ok=True)
        safety_file = safety_dir / f"{safety_note}.dump"
        _pg_dump_to_file(safety_file, settings.DATABASE_URL)
        safety_note = str(safety_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not create PG safety dump: %s", exc)
        safety_note = f"{safety_note} (safety dump skipped)"

    _pg_terminate_backends(libpq)

    cmd = [
        restore,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--verbose",
        f"--dbname={libpq}",
        str(dump_path),
    ]
    # pg_restore returns 1 on some warnings; treat only >=2 as hard fail after check
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PG_RESTORE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError("pg_restore پیدا نشد") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("زمان بازیابی PostgreSQL تمام شد") from exc

    if proc.returncode >= 2:
        err = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise BackupError(f"pg_restore ناموفق: {err or proc.returncode}")

    logger.info("PostgreSQL restored from %s (safety=%s)", dump_path, safety_note)
    return safety_note


def restore_payload(db_file: Path, manifest: dict[str, Any]) -> Path | str:
    """Restore extracted payload into the live database.

    Returns safety copy path (sqlite Path) or safety note string (postgres).
    """
    dialect = str(manifest.get("dialect") or "sqlite").lower()
    live_dialect = dialect_of()

    if dialect == "sqlite":
        if live_dialect != "sqlite":
            raise BackupError(
                "بک‌آپ SQLite است ولی سرور فعلی PostgreSQL است — "
                "dialect مبدأ و مقصد باید یکی باشد"
            )
        return replace_live_database(db_file)

    if dialect == "postgresql":
        if live_dialect != "postgresql":
            raise BackupError(
                "بک‌آپ PostgreSQL است ولی سرور فعلی SQLite است — "
                "dialect مبدأ و مقصد باید یکی باشد"
            )
        return restore_postgres_dump(db_file)

    raise BackupError(f"dialect ناشناخته: {dialect}")


def backup_status() -> dict[str, Any]:
    settings = get_settings()
    dialect = dialect_of(settings.DATABASE_URL)
    app_version = _read_app_version()
    base = {
        "format": BACKUP_FORMAT,
        "formats_accepted": [BACKUP_FORMAT, BACKUP_FORMAT_V1],
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "app_version": app_version,
        "includes": [
            "users",
            "usage",
            "groups",
            "panels",
            "admins",
            "settings",
            "templates",
            "api_keys",
            "analytics",
            "astra_node",
        ],
    }

    if dialect == "sqlite":
        path = resolve_sqlite_path(settings.DATABASE_URL)
        size = path.stat().st_size if path.exists() else 0
        return {
            **base,
            "supported": True,
            "dialect": "sqlite",
            "path_name": path.name,
            "size_bytes": size,
            "exists": path.exists(),
            "message": None,
            "tools_ok": True,
        }

    if dialect == "postgresql":
        dump_ok = bool(_which("pg_dump"))
        restore_ok = bool(_which("pg_restore"))
        tools_ok = dump_ok and restore_ok
        msg = None
        if not tools_ok:
            missing = []
            if not dump_ok:
                missing.append("pg_dump")
            if not restore_ok:
                missing.append("pg_restore")
            msg = (
                "برای بک‌آپ/بازیابی PostgreSQL این ابزارها لازم است: "
                + ", ".join(missing)
                + " (بسته postgresql-client)"
            )
        return {
            **base,
            "supported": tools_ok,
            "dialect": "postgresql",
            "path_name": _pg_safe_label(settings.DATABASE_URL),
            "size_bytes": None,
            "exists": True,
            "message": msg,
            "tools_ok": tools_ok,
            "pg_dump": dump_ok,
            "pg_restore": restore_ok,
        }

    return {
        **base,
        "supported": False,
        "dialect": dialect,
        "message": f"بک‌آپ برای این نوع دیتابیس فعال نیست ({dialect})",
        "tools_ok": False,
    }


def suggested_backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    panel = re.sub(r"[^\w\-]+", "-", get_settings().PANEL_NAME.strip()) or "Astra"
    dialect = dialect_of()
    tag = "pg" if dialect == "postgresql" else "sqlite"
    return f"{panel}-backup-{tag}-{stamp}.zip"
