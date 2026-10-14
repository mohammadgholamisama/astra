"""Create default admin if missing. Schema sync happens in app startup."""

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.admin import Admin

settings = get_settings()


async def seed_admin() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Admin).where(Admin.username == settings.ADMIN_USERNAME))
        if result.scalar_one_or_none():
            return
        admin = Admin(
            username=settings.ADMIN_USERNAME,
            hashed_password=hash_password(settings.ADMIN_PASSWORD),
            is_sudo=True,
        )
        db.add(admin)
        await db.commit()
        print(f"Created admin: {settings.ADMIN_USERNAME}")


async def seed() -> None:
    from app.core.schema_sync import ensure_schema

    await ensure_schema()
    await seed_admin()


if __name__ == "__main__":
    asyncio.run(seed())
