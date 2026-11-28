"""Public integration schemas for /docs (X-Api-Key)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class IntegrationGroupPanelIn(BaseModel):
    panel_id: int = Field(
        ...,
        ge=1,
        description="شناسه عددی پنل Astra (integer ≥ 1). از GET /api/v1/panels/sub-status یا UI پنل‌ها بگیرید.",
        examples=[12],
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="تنظیمات اتصال گروه↔پنل به‌صورت JSON آزاد (معمولاً {}). طول کلید/مقدار محدود به ظرفیت DB است.",
        examples=[{}],
    )
    priority: int = Field(
        default=0,
        ge=0,
        le=10_000,
        description="اولویت داخل گروه (integer 0…10000). عدد کوچک‌تر = اولویت بالاتر.",
        examples=[0],
    )


class IntegrationGroupCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=3,
        max_length=64,
        description="نام یکتای گروه. string؛ طول ۳…۶۴؛ فقط یکتایی در کل سیستم.",
        examples=["gold-30day"],
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="توضیح اختیاری گروه. string|null؛ حداکثر حدود ۲۰۰۰ کاراکتر.",
        examples=["پلن ۳۰ روزه ویژه فروشگاه"],
    )
    is_disabled: bool = Field(
        default=False,
        description="اگر true باشد گروه غیرفعال است و در فروش/ساب استفاده نمی‌شود. boolean.",
        examples=[False],
    )
    config_tag: str = Field(
        default="",
        max_length=200,
        description="برچسب اختیاری برای فیلتر/اتوماسیون. string؛ حداکثر ۲۰۰ کاراکتر.",
        examples=[""],
    )
    usage_cost: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="ضریب/هزینه مصرف اختیاری. integer|null؛ ۰…۱۰۰۰۰۰۰. null = بدون مقدار.",
        examples=[100],
    )
    order_kind: Literal["direct", "tunnel", "gaming", "other"] = Field(
        default="other",
        description="نوع ترتیب در ساب: فقط یکی از `direct` | `tunnel` | `gaming` | `other`.",
        examples=["direct"],
    )
    notices: list[str] = Field(
        default_factory=list,
        description="لیست اعلان‌های متنی برای کاربر/فروشگاه. array[string]؛ هر آیتم معمولاً ≤500 کاراکتر.",
        examples=[["حجم مشترک بین پنل‌ها"]],
    )
    category_id: int | None = Field(
        default=None,
        ge=1,
        description="شناسه دسته فروش (اگر دارید). integer|null ≥ 1.",
        examples=[None],
    )
    panels: list[IntegrationGroupPanelIn] = Field(
        default_factory=list,
        description="پنل‌هایی که به این گروه وصل می‌شوند. array؛ می‌تواند خالی باشد و بعداً با PUT پر شود.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "gold-30day",
                    "description": "پلن ۳۰ روزه",
                    "is_disabled": False,
                    "config_tag": "",
                    "usage_cost": 100,
                    "order_kind": "direct",
                    "notices": ["حجم مشترک"],
                    "category_id": None,
                    "panels": [{"panel_id": 12, "config": {}, "priority": 0}],
                }
            ]
        }
    }


class IntegrationGroupUpdate(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        description="نام جدید گروه. string|null؛ ۳…۶۴. null = بدون تغییر.",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="توضیح جدید. string|null.",
    )
    is_disabled: bool | None = Field(
        default=None,
        description="فعال/غیرفعال. boolean|null.",
    )
    config_tag: str | None = Field(
        default=None,
        max_length=200,
        description="برچسب. string|null؛ حداکثر ۲۰۰.",
    )
    usage_cost: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="هزینه مصرف. integer|null. برای پاک کردن از clear_usage_cost=true استفاده کنید.",
    )
    clear_usage_cost: bool | None = Field(
        default=None,
        description="اگر true باشد usage_cost را null می‌کند. boolean|null.",
    )
    order_kind: Literal["direct", "tunnel", "gaming", "other"] | None = Field(
        default=None,
        description="نوع ترتیب. یکی از direct|tunnel|gaming|other یا null.",
    )
    notices: list[str] | None = Field(
        default=None,
        description="جایگزینی کامل لیست اعلان‌ها. array[string]|null.",
    )
    category_id: int | None = Field(
        default=None,
        ge=1,
        description="دسته فروش. integer|null.",
    )
    clear_category: bool | None = Field(
        default=None,
        description="اگر true باشد category_id پاک می‌شود. boolean|null.",
    )
    panels: list[IntegrationGroupPanelIn] | None = Field(
        default=None,
        description="اگر ارسال شود لیست پنل‌های گروه جایگزین می‌شود. array|null.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "gold-30day-v2",
                    "is_disabled": False,
                    "order_kind": "tunnel",
                    "panels": [{"panel_id": 12, "config": {}, "priority": 1}],
                }
            ]
        }
    }


class IntegrationGroupPanelOut(BaseModel):
    panel_id: int = Field(..., description="شناسه پنل. integer.")
    panel_name: str | None = Field(default=None, description="نام نمایشی پنل. string|null.")
    panel_type: str | None = Field(
        default=None,
        description="نوع پنل (مثلاً marzban / sanaei / …). string|null.",
    )
    panel_is_active: bool = Field(default=True, description="آیا پنل فعال است. boolean.")
    priority: int = Field(default=0, description="اولویت داخل گروه. integer.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="تنظیمات اتصال. object.",
    )


class IntegrationGroupOut(BaseModel):
    id: int = Field(..., description="شناسه گروه. integer.")
    name: str = Field(..., description="نام گروه. string ۳…۶۴.")
    description: str | None = Field(default=None, description="توضیح. string|null.")
    is_disabled: bool = Field(default=False, description="غیرفعال بودن. boolean.")
    config_tag: str = Field(default="", description="برچسب. string ≤200.")
    usage_cost: int | None = Field(default=None, description="هزینه مصرف. integer|null.")
    order_kind: str = Field(default="other", description="direct|tunnel|gaming|other.")
    notices: list[str] = Field(default_factory=list, description="اعلان‌ها. array[string].")
    category_id: int | None = Field(default=None, description="دسته فروش. integer|null.")
    sort_order: int = Field(default=0, description="ترتیب نمایش در UI. integer.")
    panels: list[IntegrationGroupPanelOut] = Field(
        default_factory=list,
        description="پنل‌های متصل. array.",
    )
    created_at: datetime | None = Field(
        default=None,
        description="زمان ساخت (ISO-8601 UTC). string datetime|null.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": 7,
                    "name": "gold-30day",
                    "description": "پلن ۳۰ روزه",
                    "is_disabled": False,
                    "config_tag": "",
                    "usage_cost": 100,
                    "order_kind": "direct",
                    "notices": ["حجم مشترک"],
                    "category_id": None,
                    "sort_order": 10,
                    "panels": [
                        {
                            "panel_id": 12,
                            "panel_name": "DE-1",
                            "panel_type": "marzban",
                            "panel_is_active": True,
                            "priority": 0,
                            "config": {},
                        }
                    ],
                    "created_at": "2026-03-20T12:00:00Z",
                }
            ]
        }
    }


class PanelConfigTestOut(BaseModel):
    """One share-link fingerprint belonging to a panel."""

    host: str = Field(..., description="هاست کانفیگ. string.")
    port: int = Field(..., description="پورت. integer 1…65535.")
    match_host: str = Field(default="", description="هاست نرمال‌شده برای match. string.")
    label: str | None = Field(default=None, description="برچسب نمایشی. string|null.")
    protocol: str | None = Field(default=None, description="پروتکل (vless/vmess/…). string|null.")
    sub_rank: int = Field(
        ...,
        description="رتبه ۱-based در ترتیب سراسری ساب (عدد کوچک‌تر = بالاتر در /sub). integer ≥ 1.",
    )
    avg_ping_ms: float | None = Field(
        default=None,
        description="میانگین پینگ چنددستگاهی بر حسب ms. number|null؛ null = هنوز تست نشده.",
    )
    device_count: int | None = Field(
        default=None,
        description="تعداد دستگاه‌های شرکت‌کننده در میانگین. integer|null.",
    )
    tested: bool = Field(default=False, description="آیا حداقل یک تست ثبت شده. boolean.")
    hidden: bool = Field(default=False, description="مخفی در ساب. boolean.")
    group_names: list[str] = Field(
        default_factory=list,
        description="نام گروه‌هایی که این کانفیگ در آن‌ها دیده می‌شود. array[string].",
    )
    category_name: str | None = Field(default=None, description="نام دسته. string|null.")
    flag: str | None = Field(default=None, description="کد/پرچم کشور. string|null.")


class PanelSubStatusOut(BaseModel):
    panel_id: int = Field(..., description="شناسه پنل. integer.")
    name: str = Field(..., description="نام پنل. string.")
    type: str = Field(..., description="نوع پنل. string.")
    is_active: bool = Field(..., description="فعال بودن پنل. boolean.")
    worker_id: int | None = Field(default=None, description="ورکر مسئول. integer|null.")
    worker_name: str | None = Field(default=None, description="نام ورکر. string|null.")
    configs_count: int = Field(default=0, description="تعداد کانفیگ‌های دیده‌شده. integer.")
    tested_count: int = Field(default=0, description="تعداد تست‌شده. integer.")
    best_ping_ms: float | None = Field(default=None, description="بهترین پینگ ms. number|null.")
    worst_ping_ms: float | None = Field(default=None, description="بدترین پینگ ms. number|null.")
    best_sub_rank: int | None = Field(
        default=None,
        description="بهترین (کوچک‌ترین) رتبهٔ کانفیگ این پنل در ساب. integer|null.",
    )
    configs: list[PanelConfigTestOut] = Field(
        default_factory=list,
        description="جزئیات هر کانفیگ. array.",
    )


class PanelsSubStatusResponse(BaseModel):
    total_panels: int = Field(..., description="تعداد پنل‌های برگشتی. integer.")
    total_ordered_configs: int = Field(
        ...,
        description="تعداد کل کانفیگ‌های مرتب‌شده در ساب. integer.",
    )
    panels: list[PanelSubStatusOut] = Field(
        ...,
        description="وضعیت هر پنل. array.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "total_panels": 1,
                    "total_ordered_configs": 2,
                    "panels": [
                        {
                            "panel_id": 12,
                            "name": "DE-1",
                            "type": "marzban",
                            "is_active": True,
                            "worker_id": 3,
                            "worker_name": "eu-1",
                            "configs_count": 2,
                            "tested_count": 2,
                            "best_ping_ms": 42.5,
                            "worst_ping_ms": 88.0,
                            "best_sub_rank": 1,
                            "configs": [
                                {
                                    "host": "de1.example.com",
                                    "port": 443,
                                    "match_host": "de1.example.com",
                                    "label": "DE-VIP",
                                    "protocol": "vless",
                                    "sub_rank": 1,
                                    "avg_ping_ms": 42.5,
                                    "device_count": 4,
                                    "tested": True,
                                    "hidden": False,
                                    "group_names": ["gold-30day"],
                                    "category_name": None,
                                    "flag": "DE",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }
