# -*- coding: utf-8 -*-
"""Persian OpenAPI labels for /docs — short, clean summaries per API."""

from __future__ import annotations

from typing import Any

OPENAPI_DESC = """
# مستندات API پنل Astra

مدیریت کاربران، گروه‌ها، پنل‌ها، ورکرها و لینک اشتراک.

## احراز هویت
- **ادمین UI:** Bearer JWT (لاگین از `/api/admin/token`)
- **API خارجی:** هدر `X-Api-Key: pg_key_…` (از تنظیمات → کلیدهای API)

## نکته
مسیرهای تکراری کوتاه (`/api/panel` و …) در این صفحه مخفی شده‌اند؛ از مسیرهای `/api/admin/…` یا `/api/v1/…` استفاده کنید.
""".strip()

OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "Integration · API خارجی",
        "description": "CRUD گروه و وضعیت ساب پنل‌ها — با کلید API برای سیستم‌های بیرونی",
    },
    {"name": "ادمین", "description": "لاگین، پروفایل ادمین و تغییر رمز"},
    {"name": "کلید API", "description": "ساخت و مدیریت کلیدهای دسترسی خارجی"},
    {"name": "کاربران", "description": "ساخت، ویرایش، حذف و ترمیم کاربران فروش"},
    {"name": "قالب کاربر", "description": "قالب‌های آماده برای ساخت سریع کاربر"},
    {"name": "گروه‌ها", "description": "گروه‌های فروش و دسته‌بندی آن‌ها"},
    {"name": "کانفیگ گروه", "description": "نمایش/مخفی‌سازی کانفیگ‌ها داخل گروه"},
    {"name": "پنل‌ها", "description": "اتصال پنل‌های ریموت، تست و دسته‌بندی"},
    {"name": "اوتباند آسترا", "description": "اوتباند نود محلی Astra، پینگ و اشتراک اوتباند"},
    {"name": "ورکرها", "description": "سرورهای ورکر و تخصیص پنل به آن‌ها"},
    {"name": "ترتیب کانفیگ", "description": "ساخت نمونه و چیدمان ترتیب کانفیگ‌ها در ساب"},
    {"name": "موقعیت کانفیگ", "description": "کشور/پرچم و محل جغرافیایی کانفیگ‌ها"},
    {"name": "شناسایی کانفیگ", "description": "پیدا کردن کانفیگ از روی لینک یا مشخصات"},
    {"name": "اشتراک", "description": "لینک ساب کلاینت، صفحه اطلاعات و مصرف"},
    {"name": "آمار", "description": "آمار مصرف کاربران و پنل‌ها"},
    {"name": "تنظیمات", "description": "تنظیمات پنل، SSL و لبهٔ ساب"},
    {"name": "بک‌آپ", "description": "پشتیبان‌گیری و بازیابی"},
    {"name": "سیستم", "description": "وضعیت سرور و به‌روزرسانی"},
    {"name": "داخلی ورکر", "description": "ارتباط عامل ورکر با مستر (معمولاً برای کلاینت بیرونی لازم نیست)"},
]

# Map auto-generated English tag → Persian tag
_TAG_MAP: dict[str, str] = {
    "Admin": "ادمین",
    "API Keys": "کلید API",
    "User": "کاربران",
    "User Template": "قالب کاربر",
    "Groups": "گروه‌ها",
    "GroupConfigs": "کانفیگ گروه",
    "Panel": "پنل‌ها",
    "AstraOutbounds": "اوتباند آسترا",
    "Workers": "ورکرها",
    "Config Order": "ترتیب کانفیگ",
    "Config Locations": "موقعیت کانفیگ",
    "Config Lookup": "شناسایی کانفیگ",
    "Subscription": "اشتراک",
    "Analytics": "آمار",
    "Settings": "تنظیمات",
    "Backup": "بک‌آپ",
    "System": "سیستم",
    "Integration · API خارجی": "Integration · API خارجی",
}

# (METHOD, PATH) → (summary, description)
# Paths use OpenAPI template form: /api/admin/panels/{panel_id}
_OPS: dict[tuple[str, str], tuple[str, str]] = {
    # ── Admin ──────────────────────────────────────────────
    ("get", "/api/admin"): ("پروفایل ادمین", "اطلاعات ادمین لاگین‌شده را برمی‌گرداند."),
    ("post", "/api/admin/token"): ("ورود ادمین", "با نام کاربری و رمز، توکن JWT می‌گیرد."),
    ("post", "/api/admin/change-password"): ("تغییر رمز ادمین", "رمز عبور ادمین فعلی را عوض می‌کند."),
    # ── API keys ───────────────────────────────────────────
    ("get", "/api/admin/api-keys"): ("لیست کلیدهای API", "همه کلیدهای دسترسی خارجی را نشان می‌دهد."),
    ("post", "/api/admin/api-keys"): ("ساخت کلید API", "کلید جدید برای Integration می‌سازد."),
    ("delete", "/api/admin/api-keys/{key_id}"): ("حذف کلید API", "یک کلید را باطل و حذف می‌کند."),
    # ── Users ──────────────────────────────────────────────
    ("get", "/api/users"): ("لیست کاربران", "کاربران را با فیلتر و صفحه‌بندی برمی‌گرداند."),
    ("get", "/api/users/counts"): ("شمارش وضعیت کاربران", "تعداد فعال/خطا/منقضی و… برای چیپ‌های فیلتر."),
    ("post", "/api/users/bulk"): ("ساخت گروهی کاربر", "چند کاربر را یکجا می‌سازد."),
    ("post", "/api/users/retry-errors"): ("تلاش مجدد همه خطاها", "اکانت‌های دارای خطا را دوباره provision می‌کند."),
    ("post", "/api/user"): ("ساخت کاربر", "کاربر جدید روی پنل‌های گروه می‌سازد."),
    ("post", "/api/user/from_template"): ("ساخت از قالب", "کاربر را از روی قالب آماده می‌سازد."),
    ("get", "/api/user/{username}"): ("جزئیات کاربر", "وضعیت، لینک‌ها و اکانت‌های پنل یک کاربر."),
    ("put", "/api/user/{username}"): ("ویرایش کاربر", "حجم، تاریخ، گروه و وضعیت را عوض می‌کند."),
    ("delete", "/api/user/{username}"): ("حذف کاربر", "کاربر را از Astra و پنل‌های ریموت پاک می‌کند."),
    ("post", "/api/user/{username}/reset"): ("ریست حجم", "مصرف کاربر را صفر می‌کند."),
    ("post", "/api/user/{username}/retry"): ("تلاش مجدد ساخت", "اکانت‌های ناقص/خطادار را دوباره می‌سازد."),
    ("post", "/api/user/{username}/revoke_sub"): ("باطل کردن ساب", "توکن اشتراک را عوض می‌کند تا لینک قبلی قطع شود."),
    ("post", "/api/user/{username}/fix-sub"): ("ترمیم ساب", "لینک‌ها و پوشش پنل‌ها را heal/hydrate می‌کند."),
    ("post", "/api/user/{username}/check-usage"): ("بررسی مصرف", "مصرف را از پنل‌ها تازه می‌خواند."),
    # ── Templates ──────────────────────────────────────────
    ("get", "/api/user_template"): ("لیست قالب‌ها", "قالب‌های ساخت کاربر را لیست می‌کند."),
    ("post", "/api/user_template"): ("ساخت قالب", "قالب جدید با حجم/مدت/گروه ذخیره می‌کند."),
    ("get", "/api/user_template/{template_id}"): ("جزئیات قالب", "یک قالب را برمی‌گرداند."),
    ("put", "/api/user_template/{template_id}"): ("ویرایش قالب", "مقادیر قالب را به‌روز می‌کند."),
    ("delete", "/api/user_template/{template_id}"): ("حذف قالب", "قالب را پاک می‌کند."),
    # ── Groups ─────────────────────────────────────────────
    ("get", "/api/group"): ("لیست گروه‌ها", "گروه‌های فروش و پنل‌هایشان."),
    ("get", "/api/groups"): ("لیست گروه‌ها", "گروه‌های فروش و پنل‌هایشان."),
    ("post", "/api/group"): ("ساخت گروه", "گروه فروش جدید می‌سازد و پنل وصل می‌کند."),
    ("get", "/api/group/{group_id}"): ("جزئیات گروه", "تنظیمات و پنل‌های یک گروه."),
    ("put", "/api/group/{group_id}"): ("ویرایش گروه", "نام، پنل‌ها و تنظیمات گروه را عوض می‌کند."),
    ("delete", "/api/group/{group_id}"): ("حذف گروه", "گروه را حذف و ریموت‌های مرتبط را قطع می‌کند."),
    ("put", "/api/group/reorder"): ("ترتیب گروه‌ها", "ترتیب نمایش گروه‌ها را ذخیره می‌کند."),
    ("put", "/api/groups/reorder"): ("ترتیب گروه‌ها", "ترتیب نمایش گروه‌ها را ذخیره می‌کند."),
    ("put", "/api/group/{group_id}/move"): ("جابه‌جایی گروه", "گروه را به دستهٔ دیگر منتقل می‌کند."),
    ("post", "/api/group/{group_id}/resync-users"): (
        "همگام‌سازی کاربران گروه",
        "کاربران گروه را با پنل‌های فعلی دوباره هم‌تراز می‌کند.",
    ),
    ("get", "/api/group-categories"): ("دسته‌های گروه", "دسته‌بندی گروه‌ها را لیست می‌کند."),
    ("post", "/api/group-categories"): ("ساخت دسته گروه", "دستهٔ جدید برای گروه‌ها می‌سازد."),
    ("put", "/api/group-categories/{category_id}"): ("ویرایش دسته گروه", "نام/ترتیب دسته را عوض می‌کند."),
    ("delete", "/api/group-categories/{category_id}"): ("حذف دسته گروه", "دسته را حذف می‌کند."),
    ("post", "/api/group-categories/{category_id}/resync-users"): (
        "همگام‌سازی کاربران دسته",
        "همه کاربران گروه‌های این دسته را resync می‌کند.",
    ),
    # ── Group configs / visibility ─────────────────────────
    ("get", "/api/group/{group_id}/configs"): ("کانفیگ‌های گروه", "لیست کانفیگ‌های قابل‌نمایش گروه."),
    ("put", "/api/group/{group_id}/configs"): ("ذخیره کانفیگ‌های گروه", "انتخاب/ترتیب کانفیگ‌های گروه را ذخیره می‌کند."),
    ("post", "/api/group/{group_id}/configs/sample"): ("نمونه کانفیگ گروه", "نمونه لینک‌ها را برای این گروه تازه می‌سازد."),
    ("delete", "/api/group/{group_id}/configs/sample"): ("حذف نمونه گروه", "یوزر/نمونهٔ تست گروه را پاک می‌کند."),
    ("get", "/api/group/{group_id}/config-visibility"): (
        "وضعیت مخفی‌سازی گروه",
        "کدام کانفیگ‌ها در این گروه مخفی‌اند.",
    ),
    ("put", "/api/group/{group_id}/config-visibility"): (
        "مخفی/نمایش در گروه",
        "یک کانفیگ را در گروه مخفی یا آشکار می‌کند.",
    ),
    ("get", "/api/admin/config-visibility"): ("مخفی‌سازی سراسری", "لیست کانفیگ‌های مخفی در همه جا."),
    ("put", "/api/admin/config-visibility"): ("تنظیم مخفی سراسری", "مخفی بودن یک کانفیگ را سراسری عوض می‌کند."),
    ("put", "/api/admin/config-visibility/bulk"): ("مخفی‌سازی گروهی", "چند کانفیگ را یکجا مخفی/آشکار می‌کند."),
    ("post", "/api/admin/config-visibility/unhide-all"): ("آشکار کردن همه", "همه کانفیگ‌های مخفی را برمی‌گرداند."),
    ("post", "/api/admin/configs/sample"): ("تازه‌سازی همه نمونه‌ها", "نمونه کانفیگ همه گروه‌ها را دوباره می‌سازد."),
    ("delete", "/api/admin/configs/sample"): ("حذف همه نمونه‌ها", "همه یوزرهای نمونهٔ گروه را پاک می‌کند."),
    # ── Panels ─────────────────────────────────────────────
    ("get", "/api/admin/panels"): ("لیست پنل‌ها", "همه پنل‌های متصل (سنایی، پاسارگاد، مرزبان، آسترا، …)."),
    ("post", "/api/admin/panels"): ("افزودن پنل", "پنل ریموت جدید ثبت می‌کند."),
    ("get", "/api/admin/panels/{panel_id}"): ("جزئیات پنل", "تنظیمات و وضعیت یک پنل."),
    ("put", "/api/admin/panels/{panel_id}"): ("ویرایش پنل", "آدرس، لاگین و متای پنل را عوض می‌کند."),
    ("delete", "/api/admin/panels/{panel_id}"): ("حذف پنل", "پنل و اکانت‌های محلی مرتبط را پاک می‌کند."),
    ("put", "/api/admin/panels/{panel_id}/active"): ("روشن/خاموش پنل", "پنل را از ساخت و ساب خارج یا فعال می‌کند."),
    ("put", "/api/admin/panels/{panel_id}/move"): ("جابه‌جایی پنل", "پنل را به دستهٔ دیگر می‌برد."),
    ("put", "/api/admin/panels/reorder"): ("ترتیب پنل‌ها", "ترتیب نمایش پنل‌ها در یک دسته را ذخیره می‌کند."),
    ("post", "/api/admin/panels/{panel_id}/test"): ("تست یک پنل", "اتصال و (برای آسترا) پینگ اوتباند را زنده چک می‌کند."),
    ("post", "/api/admin/panels/test-all"): ("تست همه پنل‌ها", "همه پنل‌های فعال را موازی تست می‌کند."),
    ("get", "/api/admin/panels/{panel_id}/groups"): ("گروه‌های پنل", "گروه‌های قابل انتخاب روی این پنل."),
    ("get", "/api/admin/panels/{panel_id}/inbounds"): ("اینباندهای پنل", "اینباند/سرویس‌های پنل سنایی یا مشابه."),
    ("get", "/api/admin/panels/{panel_id}/marz-protocols"): ("پروتکل‌های مرزبان", "پروتکل‌های قابل انتخاب مرزبان."),
    ("get", "/api/admin/panels/{panel_id}/marz-inbounds"): ("اینباندهای مرزبان", "اینباندهای مرزبان پنل."),
    ("post", "/api/admin/panels/preview-groups"): ("پیش‌نمایش گروه‌ها", "قبل از ذخیره، گروه‌های پنل را از ریموت می‌خواند."),
    ("post", "/api/admin/panels/preview-inbounds"): ("پیش‌نمایش اینباند", "اینباندها را قبل از ذخیره از ریموت می‌خواند."),
    ("post", "/api/admin/panels/preview-marz-protocols"): (
        "پیش‌نمایش پروتکل مرزبان",
        "پروتکل‌های مرزبان را قبل از ذخیره می‌خواند.",
    ),
    ("post", "/api/admin/panels/preview-marz-inbounds"): (
        "پیش‌نمایش اینباند مرزبان",
        "اینباندهای مرزبان را قبل از ذخیره می‌خواند.",
    ),
    ("get", "/api/admin/panel-categories"): ("دسته‌های پنل", "دسته‌بندی پنل‌ها را لیست می‌کند."),
    ("post", "/api/admin/panel-categories"): ("ساخت دسته پنل", "دستهٔ جدید برای پنل‌ها می‌سازد."),
    ("put", "/api/admin/panel-categories/{category_id}"): ("ویرایش دسته پنل", "نام دسته را عوض می‌کند."),
    ("delete", "/api/admin/panel-categories/{category_id}"): ("حذف دسته پنل", "دسته را حذف می‌کند."),
    # legacy short panel paths (if not hidden)
    ("get", "/api/panel"): ("لیست پنل‌ها", "همه پنل‌های متصل."),
    ("post", "/api/panel"): ("افزودن پنل", "پنل ریموت جدید ثبت می‌کند."),
    ("get", "/api/panel/{panel_id}"): ("جزئیات پنل", "تنظیمات و وضعیت یک پنل."),
    ("put", "/api/panel/{panel_id}"): ("ویرایش پنل", "آدرس، لاگین و متای پنل را عوض می‌کند."),
    ("delete", "/api/panel/{panel_id}"): ("حذف پنل", "پنل را پاک می‌کند."),
    ("put", "/api/panel/{panel_id}/active"): ("روشن/خاموش پنل", "پنل را فعال یا خاموش می‌کند."),
    ("put", "/api/panel/{panel_id}/move"): ("جابه‌جایی پنل", "پنل را به دستهٔ دیگر می‌برد."),
    ("put", "/api/panel/reorder"): ("ترتیب پنل‌ها", "ترتیب نمایش پنل‌ها را ذخیره می‌کند."),
    ("post", "/api/panel/{panel_id}/test"): ("تست یک پنل", "اتصال پنل را چک می‌کند."),
    ("post", "/api/panel/test-all"): ("تست همه پنل‌ها", "همه پنل‌های فعال را تست می‌کند."),
    ("get", "/api/panel/{panel_id}/groups"): ("گروه‌های پنل", "گروه‌های قابل انتخاب روی این پنل."),
    ("get", "/api/panel/{panel_id}/inbounds"): ("اینباندهای پنل", "اینباندهای پنل."),
    ("get", "/api/panel/{panel_id}/marz-protocols"): ("پروتکل‌های مرزبان", "پروتکل‌های مرزبان."),
    ("get", "/api/panel/{panel_id}/marz-inbounds"): ("اینباندهای مرزبان", "اینباندهای مرزبان."),
    ("post", "/api/panel/preview-groups"): ("پیش‌نمایش گروه‌ها", "گروه‌ها را از ریموت می‌خواند."),
    ("post", "/api/panel/preview-inbounds"): ("پیش‌نمایش اینباند", "اینباندها را از ریموت می‌خواند."),
    ("post", "/api/panel/preview-marz-protocols"): ("پیش‌نمایش پروتکل مرزبان", "پروتکل‌ها را از ریموت می‌خواند."),
    ("post", "/api/panel/preview-marz-inbounds"): ("پیش‌نمایش اینباند مرزبان", "اینباندها را از ریموت می‌خواند."),
    ("get", "/api/panel-categories"): ("دسته‌های پنل", "دسته‌بندی پنل‌ها."),
    ("post", "/api/panel-categories"): ("ساخت دسته پنل", "دستهٔ جدید می‌سازد."),
    ("put", "/api/panel-categories/{category_id}"): ("ویرایش دسته پنل", "نام دسته را عوض می‌کند."),
    ("delete", "/api/panel-categories/{category_id}"): ("حذف دسته پنل", "دسته را حذف می‌کند."),
    # ── Astra outbounds ────────────────────────────────────
    ("get", "/api/admin/panels/{panel_id}/outbounds"): ("لیست اوتباندها", "اوتباندهای نود Astra این پنل."),
    ("post", "/api/admin/panels/{panel_id}/outbounds"): ("افزودن اوتباند", "چند لینک اوتباند را یکجا اضافه می‌کند."),
    ("patch", "/api/admin/panels/{panel_id}/outbounds/{outbound_id}"): (
        "روشن/خاموش اوتباند",
        "یک اوتباند را فعال یا غیرفعال می‌کند.",
    ),
    ("delete", "/api/admin/panels/{panel_id}/outbounds/{outbound_id}"): (
        "حذف اوتباند",
        "اوتباند را از نود حذف می‌کند.",
    ),
    ("post", "/api/admin/panels/{panel_id}/outbounds/ping"): ("پینگ همه اوتباندها", "سلامت همه اوتباندهای فعال را می‌سنجد."),
    ("post", "/api/admin/panels/{panel_id}/outbounds/{outbound_id}/ping"): (
        "پینگ یک اوتباند",
        "فقط همین اوتباند را پینگ می‌کند.",
    ),
    ("get", "/api/admin/panels/{panel_id}/astra-health"): ("سلامت نود آسترا", "وضعیت xray/listen و اوتباندها."),
    ("post", "/api/admin/panels/{panel_id}/astra-rebuild"): ("بازسازی نود آسترا", "کانفیگ xray را دوباره می‌سازد و ری‌لود می‌کند."),
    ("get", "/api/admin/panels/{panel_id}/clients"): ("کلاینت‌های آسترا", "یوزرهای روی نود محلی."),
    ("post", "/api/admin/panels/{panel_id}/clients/sync-usage"): ("همگام مصرف کلاینت‌ها", "مصرف کلاینت‌های آسترا را به‌روز می‌کند."),
    ("patch", "/api/admin/panels/{panel_id}/clients/{email}"): (
        "فعال/غیرفعال کلاینت",
        "یک کلاینت روی نود را روشن یا خاموش می‌کند.",
    ),
    ("get", "/api/admin/panels/{panel_id}/outbounds/subscription"): (
        "اشتراک اوتباند",
        "آدرس ساب متصل برای همگام‌سازی اوتباندها.",
    ),
    ("post", "/api/admin/panels/{panel_id}/outbounds/subscription"): (
        "اتصال ساب اوتباند",
        "یک لینک ساب را برای همگام‌سازی اوتباندها می‌بندد.",
    ),
    ("delete", "/api/admin/panels/{panel_id}/outbounds/subscription"): (
        "قطع ساب اوتباند",
        "اتصال ساب اوتباند را برمی‌دارد.",
    ),
    ("post", "/api/admin/panels/{panel_id}/outbounds/subscription/preview"): (
        "پیش‌نمایش ساب اوتباند",
        "بدون ذخیره، محتوای ساب را نشان می‌دهد.",
    ),
    ("post", "/api/admin/panels/{panel_id}/outbounds/subscription/sync"): (
        "همگام‌سازی ساب اوتباند",
        "اوتباندها را از روی ساب به‌روز می‌کند.",
    ),
    # ── Workers ────────────────────────────────────────────
    ("get", "/api/admin/workers"): ("لیست ورکرها", "ورکرها و وضعیت آن‌ها."),
    ("post", "/api/admin/workers"): ("افزودن ورکر", "ورکر جدید ثبت می‌کند."),
    ("post", "/api/admin/workers/bulk"): ("افزودن گروهی ورکر", "چند ورکر را یکجا می‌سازد."),
    ("get", "/api/admin/workers/{worker_id}"): ("جزئیات ورکر", "اطلاعات و پنل‌های تخصیص‌یافته."),
    ("put", "/api/admin/workers/{worker_id}"): ("ویرایش ورکر", "نام، SSH و تنظیمات ورکر را عوض می‌کند."),
    ("delete", "/api/admin/workers/{worker_id}"): ("حذف ورکر", "ورکر را حذف می‌کند."),
    ("put", "/api/admin/workers/{worker_id}/panels"): ("تخصیص پنل به ورکر", "کدام پنل‌ها فقط از این ورکر بروند."),
    ("post", "/api/admin/workers/{worker_id}/probe"): ("تست ورکر", "دسترسی SSH/عامل ورکر را چک می‌کند."),
    ("post", "/api/admin/workers/{worker_id}/sync"): ("همگام ورکر", "Agent و تنظیمات را همگام می‌کند."),
    ("post", "/api/admin/workers/{worker_id}/reinstall"): ("نصب مجدد ورکر", "عامل ورکر را از نو نصب می‌کند."),
    ("post", "/api/admin/workers/{worker_id}/rotate-token"): ("تعویض توکن ورکر", "توکن احراز هویت ورکر را عوض می‌کند."),
    ("post", "/api/admin/workers/balance-panels"): ("بالانس پنل‌ها", "پنل‌ها را بین ورکرها متعادل تقسیم می‌کند."),
    ("get", "/api/public/worker-install.sh"): ("اسکریپت نصب ورکر", "اسکریپت نصب عامل روی سرور ورکر."),
    ("get", "/api/public/worker-bundle.tgz"): ("باندل ورکر", "آرشیو عامل ورکر برای نصب."),
    ("get", "/api/public/worker-bundle.tar.gz"): ("باندل ورکر", "آرشیو عامل ورکر برای نصب."),
    ("get", "/api/internal/worker/assignment"): ("تخصیص داخلی ورکر", "پنل‌های اختصاصی این ورکر (برای agent)."),
    ("get", "/api/internal/worker/commands"): ("دستورات ورکر", "long-poll دستورات از مستر."),
    ("post", "/api/internal/worker/heartbeat"): ("Heartbeat ورکر", "زنده بودن عامل را گزارش می‌دهد."),
    ("get", "/api/internal/worker/priority-probe"): ("پروب اولویت", "چک سریع آمادگی ورکر."),
    ("post", "/api/internal/worker/usage-report"): ("گزارش مصرف ورکر", "مصرف خوانده‌شده از پنل‌ها را به مستر می‌فرستد."),
    # ── Config order ───────────────────────────────────────
    ("get", "/api/admin/config-order"): ("فضای ترتیب کانفیگ", "لیست فعلی ترتیب و وضعیت نمونه."),
    ("put", "/api/admin/config-order"): ("ذخیره ترتیب", "ترتیب کانفیگ‌ها در ساب را ذخیره می‌کند."),
    ("post", "/api/admin/config-order/sample"): (
        "ساخت کانفیگ‌های نمونه",
        "روی پنل‌های فعال نمونه می‌سازد تا ترتیب را بچینید.",
    ),
    ("post", "/api/admin/config-order/ping-rank"): ("رتبه‌بندی با پینگ", "ترتیب را بر اساس پینگ دستگاه‌ها می‌چیند."),
    # ── Config locations ───────────────────────────────────
    ("get", "/api/admin/config-locations"): ("لیست موقعیت‌ها", "کشور/پرچم‌های تعریف‌شده."),
    ("post", "/api/admin/config-locations"): ("افزودن موقعیت", "موقعیت جغرافیایی جدید می‌سازد."),
    ("put", "/api/admin/config-locations/{location_id}"): ("ویرایش موقعیت", "نام/پرچم موقعیت را عوض می‌کند."),
    ("delete", "/api/admin/config-locations/{location_id}"): ("حذف موقعیت", "موقعیت را پاک می‌کند."),
    ("put", "/api/admin/config-locations/endpoint"): ("اتصال endpoint به موقعیت", "یک host:port را به موقعیت وصل می‌کند."),
    ("post", "/api/admin/config-locations/probe"): ("تشخیص خودکار موقعیت", "از روی IP/ASN موقعیت را حدس می‌زند."),
    # ── Config lookup ──────────────────────────────────────
    ("post", "/api/admin/config-lookup"): ("شناسایی کانفیگ", "از روی لینک یا مشخصات می‌گوید مال کدام پنل/گروه است."),
    # ── Subscription ───────────────────────────────────────
    ("get", "/sub/{token}"): ("لینک اشتراک", "فید کانفیگ برای کلاینت (v2rayNG و …)."),
    ("get", "/sub/{token}/info"): ("صفحه اطلاعات ساب", "صفحهٔ وب وضعیت حجم و انقضا."),
    ("get", "/sub/{token}/usage"): ("مصرف ساب", "حجم مصرف‌شده و باقی‌مانده به صورت JSON."),
    # ── Analytics ──────────────────────────────────────────
    ("get", "/api/analytics/overview"): ("نمای کلی آمار", "خلاصه مصرف و کاربران."),
    ("get", "/api/analytics/panels"): ("آمار پنل‌ها", "مصرف و وضعیت به تفکیک پنل."),
    ("post", "/api/analytics/panels/refresh"): ("تازه‌سازی آمار پنل‌ها", "نمونهٔ آمار پنل‌ها را دوباره می‌گیرد."),
    ("get", "/api/analytics/panels/{panel_id}"): ("آمار یک پنل", "جزئیات مصرف همان پنل."),
    ("get", "/api/analytics/user/{username}"): ("آمار یک کاربر", "روند مصرف همان کاربر."),
    ("get", "/api/analytics/users/search"): ("جستجوی کاربر در آمار", "کاربر را برای نمودار پیدا می‌کند."),
    ("post", "/api/analytics/sample-now"): ("نمونه‌گیری فوری آمار", "یک نمونهٔ تازه از مصرف ثبت می‌کند."),
    # ── Settings ───────────────────────────────────────────
    ("get", "/api/admin/settings"): ("خواندن تنظیمات", "تنظیمات عمومی پنل."),
    ("put", "/api/admin/settings"): ("ذخیره تنظیمات", "تنظیمات عمومی را ذخیره می‌کند."),
    ("post", "/api/admin/settings/ssl"): ("صدور SSL", "گواهی SSL برای دامنه پنل/نود می‌گیرد."),
    ("get", "/api/admin/settings/sub-edge"): ("تنظیمات لبه ساب", "وضعیت تونل/لبهٔ دامنه اشتراک."),
    ("post", "/api/admin/settings/sub-edge"): ("ذخیره لبه ساب", "لبهٔ ساب را روشن/پیکربندی می‌کند."),
    # ── Backup ─────────────────────────────────────────────
    ("get", "/api/admin/backup/status"): ("وضعیت بک‌آپ", "آیا بک‌آپ آماده است یا در حال ساخت."),
    ("get", "/api/admin/backup/prepare"): ("آماده‌سازی بک‌آپ", "وضعیت/شروع آماده‌سازی آرشیو."),
    ("post", "/api/admin/backup/prepare"): ("شروع آماده‌سازی بک‌آپ", "ساخت آرشیو پشتیبان را شروع می‌کند."),
    ("get", "/api/admin/backup"): ("دانلود بک‌آپ", "فایل zip پشتیبان را دانلود می‌کند."),
    ("post", "/api/admin/backup/restore"): ("بازیابی بک‌آپ", "از روی فایل zip بازیابی می‌کند."),
    # ── System ─────────────────────────────────────────────
    ("get", "/api/system"): ("وضعیت سیستم", "خلاصه سلامت سرویس Astra."),
    ("get", "/api/system/host"): ("منابع سرور", "CPU، رم و دیسک میزبان."),
    ("get", "/api/system/db-pool"): ("استخر دیتابیس", "وضعیت connection pool."),
    ("get", "/api/system/update"): ("چک آپدیت", "آیا نسخهٔ جدید در دسترس است."),
    ("post", "/api/system/update"): ("اعمال آپدیت", "به‌روزرسانی پنل را اجرا می‌کند."),
    ("get", "/health"): ("سلامت سرویس", "پینگ ساده برای لودبالانسر/مانیتورینگ."),
    # ── Integration v1 ─────────────────────────────────────
    ("get", "/api/v1/groups"): ("استعلام همه گروه‌ها", "لیست گروه‌ها برای سیستم خارجی (X-Api-Key)."),
    ("post", "/api/v1/groups"): ("افزودن گروه", "گروه جدید می‌سازد و در صورت نیاز پنل وصل می‌کند."),
    ("get", "/api/v1/groups/{group_id}"): ("استعلام یک گروه", "جزئیات یک گروه با کلید API."),
    ("put", "/api/v1/groups/{group_id}"): ("ویرایش گروه", "گروه را از API خارجی ویرایش می‌کند."),
    ("delete", "/api/v1/groups/{group_id}"): ("حذف گروه", "گروه و ریموت‌های مرتبط را حذف می‌کند."),
    ("get", "/api/v1/panels/sub-status"): (
        "تست و رتبه ساب همه پنل‌ها",
        "برای هر پنل: نتیجه تست و رتبه کانفیگ‌ها در /sub.",
    ),
    ("get", "/api/v1/panels/{panel_id}/sub-status"): (
        "تست و رتبه ساب یک پنل",
        "همان وضعیت برای یک پنل مشخص.",
    ),
}

# Duplicate short aliases to hide from docs (admin path is canonical)
_HIDE_PATH_PREFIXES: tuple[str, ...] = (
    "/api/panel",  # use /api/admin/panels
    "/api/panel-categories",
)

_HIDE_EXACT: set[str] = {
    "/",
    "/{full_path}",
    # Prefer /api/group over plural twin
    "/api/groups",
    "/api/groups/reorder",
}


def _should_hide(path: str) -> bool:
    if path in _HIDE_EXACT:
        return True
    # Hide /api/panel… but keep /api/admin/panels…
    if path.startswith("/api/panel"):
        return True
    return False


def customize_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply Persian summaries/tags and drop noisy duplicate paths."""
    schema["info"]["title"] = "مستندات API · Astra"
    schema["info"]["description"] = OPENAPI_DESC
    schema["tags"] = list(OPENAPI_TAGS)

    paths = schema.get("paths") or {}
    for path in list(paths.keys()):
        if _should_hide(path):
            paths.pop(path, None)

    for path, ops in list(paths.items()):
        if not isinstance(ops, dict):
            continue
        for method, item in list(ops.items()):
            if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            if not isinstance(item, dict):
                continue
            key = (method.lower(), path)
            fa = _OPS.get(key)
            if fa:
                item["summary"] = fa[0]
                item["description"] = fa[1]
            else:
                # Fallback: keep short Persian-ish label from operationId leftovers
                summary = str(item.get("summary") or "").strip()
                if summary and summary.isascii():
                    item["summary"] = summary
            tags = item.get("tags") or []
            item["tags"] = [_TAG_MAP.get(str(t), str(t)) for t in tags] or ["سایر"]
            # Internal worker → dedicated tag
            if path.startswith("/api/internal/worker"):
                item["tags"] = ["داخلی ورکر"]
            if path.startswith("/api/public/worker"):
                item["tags"] = ["ورکرها"]

    return schema
