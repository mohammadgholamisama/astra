"""Config remark must keep decimal coefficients in group tags (e.g. 1.5)."""

from app.services.link_remark import build_config_remark


def test_decimal_in_long_tag_not_truncated_to_one_dot():
    # Older [:24] cut through «1.5» when Persian prefix filled the budget.
    tag = "تانل گیگی ضریب1.5تومن"
    assert len(tag) > 24 or "1.5" in tag
    remark = build_config_remark(
        name="user",
        config_id=7,
        used=0,
        limit=50 * 1024**3,
        brand_tag=tag,
    )
    assert "1.5" in remark
    assert "1." in remark  # still present as part of 1.5
    # Must not end the tag fragment as bare «1.» before remain
    assert "ضریب1.5" in remark or "1.5تومن" in remark


def test_tag_up_to_200_chars_kept():
    tag = ("الف" * 60) + "1.5"  # well over old 24 limit
    remark = build_config_remark(
        name="user",
        config_id=1,
        used=0,
        limit=0,
        brand_tag=tag,
    )
    assert "1.5" in remark
