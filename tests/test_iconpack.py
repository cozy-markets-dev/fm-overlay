import zipfile

import pytest

from mprice_decoder import iconpack
from mprice_decoder.iconpack import IconPack, build

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeMeowDB:
    """MeowDB's item list (two pages) and its icon files; `down` makes icon downloads fail."""

    def __init__(self, keys, no_icon=()):
        self.keys, self.no_icon, self.fetched, self.down = list(keys), set(no_icon), [], set()

    def get(self, path):
        page = int(path.rsplit("=", 1)[1])
        half = (len(self.keys) + 1) // 2
        part = self.keys[:half] if page == 1 else self.keys[half:]
        return {"items": [{"id": n, "name": f"Item {k}", "iconKey": k} for n, k in enumerate(part)] + [{"id": 0}],
                "hasMore": page == 1}

    def fetch_icon(self, key):
        if key in self.down:
            raise ConnectionError("MeowDB is down")
        self.fetched.append(key)
        return None if key in self.no_icon else PNG + key.encode()


def make(tmp_path, meow, **kw):
    return build(tmp_path / "icons.zip", meow.get, meow.fetch_icon, pause=0, say=lambda *_: None, **kw)


def test_the_pack_has_every_items_icon_by_its_key(tmp_path):
    meow = FakeMeowDB(["4010002", "2030005", "1302000"], no_icon={"1302000"})
    assert make(tmp_path, meow) == (2, 2, ["1302000"])
    pack = IconPack(tmp_path / "icons.zip")
    assert pack.png("4010002") == PNG + b"4010002"
    assert pack.png("1302000") is None and pack.png(None) is None and pack.png("") is None
    assert not (tmp_path / "icons.zip.partial").exists()


def test_a_rebuild_only_downloads_new_items_and_drops_gone_ones(tmp_path):
    make(tmp_path, FakeMeowDB(["4010002", "2030005"]))
    first = (tmp_path / "icons.zip").read_bytes()
    meow = FakeMeowDB(["4010002", "2030005"])
    make(tmp_path, meow)
    assert meow.fetched == [] and (tmp_path / "icons.zip").read_bytes() == first  # unchanged: same bytes
    meow = FakeMeowDB(["4010002", "4000062"])  # a patch added one, and one's gone
    make(tmp_path, meow)
    assert meow.fetched == ["4000062"]
    with zipfile.ZipFile(tmp_path / "icons.zip") as z:
        assert sorted(z.namelist()) == ["4000062.png", "4010002.png"]


def test_a_stopped_build_carries_on_where_it_got_to(tmp_path, monkeypatch):
    monkeypatch.setattr(iconpack, "SAVE_EVERY", 2)
    keys = [f"40000{n:02d}" for n in range(7)]
    meow = FakeMeowDB(keys)
    meow.down = {keys[5]}
    with pytest.raises(ConnectionError):
        make(tmp_path, meow)
    assert not (tmp_path / "icons.zip").exists()
    meow.fetched, meow.down = [], set()
    assert make(tmp_path, meow) == (7, 3, [])
    assert meow.fetched == keys[4:]  # the first four were saved before it stopped


def test_no_pack_is_just_a_miss(tmp_path):
    assert IconPack(tmp_path / "nothing.zip").png("4010002") is None
    (tmp_path / "broken.zip").write_bytes(b"not a zip")
    assert IconPack(tmp_path / "broken.zip").png("4010002") is None


# --- the pack in use -------------------------------------------------------------------------

def a_pack(tmp_path, icons):
    iconpack._write(tmp_path / "icons.zip", icons)
    return IconPack(tmp_path / "icons.zip")


def shop_db(tmp_path, pack):
    from mprice_decoder.data import ItemDB
    from test_shop import Clock, FakeSource

    class Counting(FakeSource):
        downloads = 0

        def icon_png(self, icon_key):
            Counting.downloads += 1
            return super().icon_png(icon_key)

    return ItemDB(tmp_path / "t.sqlite", source=Counting(), clock=Clock(), icons=pack), Counting


def test_icons_come_from_the_pack_without_a_download(tmp_path):
    db, source = shop_db(tmp_path, a_pack(tmp_path, {"355": PNG}))
    assert db.icon_png(355) == PNG and db.stored_icon(355) == PNG
    assert source.downloads == 0
    assert db.stored_icon(95) is None                 # not in the pack: the panel doesn't wait for it
    assert source.downloads == 0
    assert db.icon_png(95).startswith(iconpack.PNG)   # an item newer than the pack: downloaded once
    db.icon_png(95)
    assert source.downloads == 1
    assert db.stored_icon(95) == db.icon_png(95)


def test_the_shop_panel_shows_the_clean_icon_for_a_known_item(tmp_path, monkeypatch):
    import mprice_decoder.__main__ as app
    from mprice_decoder.shop import Line
    from test_shop import HERE
    db, source = shop_db(tmp_path, a_pack(tmp_path, {"355": PNG}))
    monkeypatch.setattr(app, "read_lines", lambda img, attempt: [
        Line("Pig's Head", 468, 339, 80, 12), Line("1 for 500 mesos", 468, 356, 80, 12),
        Line("Zzyzx Qwerty", 468, 379, 80, 12), Line("1 for 900 mesos", 468, 396, 80, 12)])
    _, panel, *_ = app.scan(lambda: None, db, HERE)
    cards = {c.title: c.icon for c in panel.cards}
    assert cards == {"Pig's Head": PNG, "Zzyzx Qwerty": None}  # an unknown item: the screenshot's (none in a test)
    assert source.downloads == 0


