"""Badges on posters (badges.py): drawn from the kept original, again only when they change."""

import io

from PIL import Image

from mimir_flix import badges


def jpeg(colour="#808080") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (300, 450), colour).save(out, "JPEG")
    return out.getvalue()


def pixel(path, xy):
    return Image.open(path).convert("RGB").getpixel(xy)


def test_a_poster_shows_its_badges_and_is_drawn_again_only_when_they_change(tmp_path):
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(jpeg())
    assert badges.apply(tmp_path, badges.for_apps(["Netflix"]))
    assert (tmp_path / badges.ORIGINAL).read_bytes() == jpeg()  # the poster as it came, kept
    r, g, b = pixel(poster, (300 // 14 // 2 + 6, 450 - 300 // 14))  # inside the pill, bottom-left
    assert r > 180 and g < 80 and b < 80  # Netflix red over the grey poster
    assert pixel(poster, (290, 10)) == pixel(tmp_path / badges.ORIGINAL, (290, 10))  # the rest untouched

    before = poster.stat().st_mtime_ns
    assert not badges.apply(tmp_path, badges.for_apps(["Netflix"]))  # the same badges: left alone
    assert poster.stat().st_mtime_ns == before
    assert badges.apply(tmp_path, badges.for_language("Telugu"))  # changed: drawn again from the original
    assert badges.apply(tmp_path, [])  # none: the original again
    assert poster.read_bytes() == jpeg()

    (tmp_path / badges.ORIGINAL).write_bytes(b"not an image")
    badges.apply(tmp_path, badges.for_language("Hindi"))
    assert poster.read_bytes() == b"not an image"  # unreadable: shown as it came, never an error


def test_without_a_poster_there_is_nothing_to_draw(tmp_path):
    assert not badges.apply(tmp_path, badges.for_language("Hindi"))
    assert not (tmp_path / "poster.jpg").exists()
