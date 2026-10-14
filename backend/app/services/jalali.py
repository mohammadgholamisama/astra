"""Minimal Gregorian → Jalali converter (no external deps)."""

_MONTHS = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa_digits(value: str | int | float) -> str:
    return str(value).translate(_FA_DIGITS)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def format_jalali_from_unix(ts: int | None, *, with_time: bool = True) -> str:
    if not ts or int(ts) <= 0:
        return "بدون انقضا"
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    month = _MONTHS[jm - 1]
    if with_time:
        hm = to_fa_digits(f"{dt.hour:02d}:{dt.minute:02d}")
        return f"{to_fa_digits(jd)} {month} {to_fa_digits(jy)} · {hm}"
    return f"{to_fa_digits(jd)} {month} {to_fa_digits(jy)}"
