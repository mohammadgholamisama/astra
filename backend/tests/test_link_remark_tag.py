"""Config remark must keep decimal coefficients exactly as typed (1.5 / ۱.۵)."""

from urllib.parse import unquote

from app.services.link_remark import (
    build_config_remark,
    rewrite_link_remark,
    _clip_config_tag,
)


def test_persian_one_point_five_kept_verbatim():
    tag = "تانل ضریب۱.۵تومن"
    remark = build_config_remark(
        name="user",
        config_id=3,
        used=0,
        limit=50 * 1024**3,
        brand_tag=tag,
    )
    assert "۱.۵" in remark
    assert "ضریب۱.۵" in remark
    # Must not collapse to bare ۱
    assert "ضریب۱ " not in remark
    assert "ضریب۱|" not in remark


def test_rewrite_does_not_latinize_tag_digits():
    tag = "گیگی باضریب۱.۵"
    remark = build_config_remark(
        name="alice",
        config_id=1,
        used=0,
        limit=0,
        brand_tag=tag,
    )
    link = rewrite_link_remark(
        "vless://00000000-0000-0000-0000-000000000001@1.1.1.1:443?encryption=none",
        remark,
    )
    shown = unquote(link.rsplit("#", 1)[-1])
    assert "۱.۵" in shown
    assert "1.5" not in shown  # do not force Latin digits on the tag


def test_clip_does_not_cut_decimal_at_boundary():
    # Force a cut right before «.۵»
    prefix = "الف" * 66  # 198 chars
    tag = prefix + "۱.۵"
    assert len(prefix) == 198
    out = _clip_config_tag(tag)
    assert "۱.۵" in out
