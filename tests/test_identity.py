import re
import subprocess
import sys

from mprice_decoder.identity import APP_ID, COMPATIBILITY, DISPLAY_NAME, data_directory


def test_public_build_uses_mapleprice_branding():
    from mprice_decoder.sync import CLIENT
    from mprice_decoder.version import VERSION

    assert APP_ID == DISPLAY_NAME == "MaplePrice"
    assert CLIENT == f"MaplePrice {VERSION}"


def test_private_build_keeps_its_display_name_behavior():
    code = ("from mprice_decoder import distribution; distribution.PRIVATE_BUILD = True; "
            "from mprice_decoder.identity import APP_ID, DISPLAY_NAME; "
            "from mprice_decoder.update import release_name; "
            "print(APP_ID, DISPLAY_NAME, release_name('2.5'))")
    launches = [subprocess.check_output([sys.executable, "-c", code], text=True).split() for _ in range(2)]
    assert all(app_id == "mprice_decoder" and re.fullmatch(r"[0-9a-f]{12}", name)
               and filename == "mprice_decoder-2.5.exe" for app_id, name, filename in launches)
    assert launches[0][1] != launches[1][1]


def test_existing_data_location_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    saved = tmp_path / COMPATIBILITY["data_directory"]
    saved.mkdir()
    (saved / "settings.json").write_text('{"auto_scan": true}')
    assert data_directory() == saved
    assert (data_directory() / "settings.json").is_file()
