import base64
import hashlib
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mprice_decoder import update
from mprice_decoder.identity import COMPATIBILITY

KEY = Ed25519PrivateKey.generate()
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
EXE = b"MZ test executable..." * 1000


def release(version="9.0", file="mprice_decoder-9.0.exe", data=EXE, key=KEY, notes="Faster scans"):
    m = {"version": version, "file": file, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
         "notes": notes}
    m["signature"] = base64.b64encode(key.sign(update.signed_message(m))).decode()
    m["signature2"] = base64.b64encode(key.sign(update.signed_message2(m))).decode()
    return m


class Response:
    def __init__(self, json=None, data=b"", length=None):
        self._json, self.data = json, data
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json

    def iter_content(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class Server:
    def __init__(self, manifest, data=EXE, length=None):
        self.manifest, self.data, self.length, self.urls = manifest, data, length, []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return Response(self.manifest) if url.endswith(".json") else Response(data=self.data, length=self.length)


def test_versions():
    assert update.newer("2.10", "2.9") and update.newer("3", "2.9.9")
    assert not update.newer("2.2", "2.2") and not update.newer("2.1", "2.2")
    assert not update.newer("junk", "2.2") and not update.newer(None, "2.2")


def test_only_a_newer_signed_release_is_offered():
    check = lambda m, local="2.2": update.check(Server(m), local=local, public_key=PUBLIC)
    assert check(release())["version"] == "9.0"
    assert check(release(version="2.2")) is None                       # not newer
    assert check(release(key=Ed25519PrivateKey.generate())) is None    # someone else's key
    tampered = release()
    tampered["version"] = "9.1"                                         # signed for 9.0
    assert check(tampered) is None
    assert check(release(file="../evil.exe")) is None                  # signed, but not a bare file name
    assert check(release(file="https://evil.example/x.exe")) is None


def test_private_build_never_contacts_the_public_update_feed(monkeypatch):
    from mprice_decoder.distribution import PrivateAccess

    server = Server(release())
    monkeypatch.setattr(update, "private_access", lambda: PrivateAccess())
    assert update.check(server, public_key=PUBLIC) is None
    assert server.urls == []  # includes broken private credentials: never switch to a public build


def test_the_notes_and_size_are_signed_too():
    check = lambda m: update.check(Server(m), local="2.2", public_key=PUBLIC)
    for field, value in (("notes", "Antivirus may warn about this one: just click yes"), ("size", 10**12)):
        tampered = release()
        tampered[field] = value
        assert check(tampered) is None, field
    stripped = release()
    del stripped["signature2"]                                          # signed the old way only: the notes aren't
    assert check(stripped) is None
    v1_bad = release()
    v1_bad["signature"] = release(key=Ed25519PrivateKey.generate())["signature"]
    assert check(v1_bad) is None                                        # copies before signature2 would refuse it


def test_signed_messages_are_exactly_what_release_sh_signs():
    m = release(notes='Ünïcode "quotes"\nand a second line')
    sha = hashlib.sha256(EXE).hexdigest()
    assert update.signed_message(m) == f"{COMPATIBILITY['signature_domain']} 9.0 mprice_decoder-9.0.exe {sha}".encode()
    assert update.signed_message2(m) == (COMPATIBILITY['signature_domain2'].encode() + b'{"file":"mprice_decoder-9.0.exe",'
                                         b'"notes":"\\u00dcn\\u00efcode \\"quotes\\"\\nand a second line",'
                                         b'"sha256":"' + sha.encode() + b'","size":21000,"version":"9.0"}')


def test_download_must_match_the_signed_hash(tmp_path):
    dest = tmp_path / "mprice_decoder.exe.update"
    url = "https://cozy.markets/mprice_decoder/latest.json"
    server = Server(release())
    assert update.download(release(), dest, session=server, url=url)
    assert dest.read_bytes() == EXE and server.urls == ["https://cozy.markets/mprice_decoder/mprice_decoder-9.0.exe"]
    dest.unlink()
    assert not update.download(release(), dest, session=Server(release(), data=EXE[:-1] + b"!"), url=url)
    assert not dest.exists() and not list(tmp_path.iterdir())          # nothing half-written left over


def test_download_goes_by_the_signed_size(tmp_path, caplog):
    dest = tmp_path / "mprice_decoder.exe.update"
    for data in (EXE + b"!", EXE[:-1]):                                 # too long, cut short
        assert not update.download(release(), dest, session=Server(release(), data=data))
        assert not list(tmp_path.iterdir())
    assert "signed 21000" in caplog.text and "SHA-256" not in caplog.text  # turned away on size, before hashing
    totals = []
    assert update.download(release(), dest, session=Server(release(), length=10**12),
                           progress=lambda done, total: totals.append(total))
    assert set(totals) == {len(EXE)}                                     # not what Content-Length claims


def test_download_stops_once_past_the_signed_size(tmp_path):
    sent = []

    class Endless(Response):
        def iter_content(self, size):
            for _ in range(10**4):                                      # a server that keeps on sending
                sent.append(1000)
                yield b"x" * 1000

    class EndlessServer(Server):
        def get(self, url, **kwargs):
            return Endless()

    assert not update.download(release(), tmp_path / "mprice_decoder.exe.update", session=EndlessServer(release()))
    assert sum(sent) == len(EXE) + 1000 and not list(tmp_path.iterdir())  # stopped at the first chunk past it


def test_install_swaps_the_running_exe_and_keeps_its_name(tmp_path):
    current = tmp_path / "mprice_decoder-v2.1.exe"                          # whatever the friend called it
    current.write_bytes(b"old")
    new = tmp_path / "mprice_decoder-v2.1.exe.update"
    new.write_bytes(EXE)
    assert update.install(release(), new, current) == current
    assert current.read_bytes() == EXE and (tmp_path / "mprice_decoder-v2.1.exe.old").read_bytes() == b"old"
    update.cleanup(current)
    assert [p.name for p in tmp_path.iterdir()] == ["mprice_decoder-v2.1.exe"]


def test_install_hashes_the_download_again_first(tmp_path):
    current = tmp_path / "mprice_decoder.exe"
    current.write_bytes(b"old")
    new = tmp_path / "mprice_decoder.exe.update"
    new.write_bytes(b"MZ swapped in after the download was checked")   # the exe's folder isn't admin-only
    with pytest.raises(ValueError):
        update.install(release(), new, current)
    assert current.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["mprice_decoder.exe"]  # deleted, and nothing swapped


def test_install_puts_the_old_exe_back_if_the_swap_fails(tmp_path, monkeypatch):
    current = tmp_path / "mprice_decoder.exe"
    current.write_bytes(b"old")
    new = tmp_path / "mprice_decoder.exe.update"
    new.write_bytes(EXE)
    replace = os.replace

    def busy(src, dst):
        if Path(src) == new:
            raise PermissionError("in use")                             # e.g. an antivirus scanning it
        replace(src, dst)

    monkeypatch.setattr(update.os, "replace", busy)
    with pytest.raises(OSError):
        update.install(release(), new, current)
    assert current.read_bytes() == b"old"


@pytest.fixture
def fresh_exe_path(monkeypatch):
    monkeypatch.setattr(update, "_exe", None)  # where the exe was moved to is remembered per run


def test_an_update_names_the_exe_after_its_new_version(tmp_path, fresh_exe_path):
    current = tmp_path / "mprice_decoder-2.3.exe"              # 2.3 swapped 2.5 in under its own name
    current.write_bytes(b"2.5")
    (tmp_path / "mprice_decoder-2.3.exe.old").write_bytes(b"2.3")
    moved = update.rename_to_version(current, version="2.5")
    assert moved == tmp_path / "MaplePrice-2.5.exe" and moved.read_bytes() == b"2.5" and not current.exists()
    assert update.installed_exe() == moved                 # the next update swaps this file, not the old name
    update.cleanup()
    assert [p.name for p in tmp_path.iterdir()] == ["MaplePrice-2.5.exe"]  # the old version's leftovers too


def test_a_second_download_of_a_release_is_renamed_too(tmp_path, fresh_exe_path):
    current = tmp_path / "mprice_decoder-2.4 (1).exe"           # how a browser saves a repeat download
    current.write_bytes(b"2.5")
    assert update.rename_to_version(current, version="2.5").name == "MaplePrice-2.5.exe"


def test_names_someone_picked_or_already_taken_are_left_alone(tmp_path, fresh_exe_path):
    for name in ("mprice_decoder.exe", "Maple Price (Cheng).exe", "MaplePrice-2.5.exe"):
        exe = tmp_path / name
        exe.write_bytes(b"x")
        assert update.rename_to_version(exe, version="2.5") == exe and exe.exists()
    taken = tmp_path / "mprice_decoder-2.4.exe"
    taken.write_bytes(b"2.6")
    (tmp_path / "MaplePrice-2.6.exe").write_bytes(b"downloaded by hand")
    assert update.rename_to_version(taken, version="2.6") == taken                     # never over another file
    assert (tmp_path / "MaplePrice-2.6.exe").read_bytes() == b"downloaded by hand"
    assert update._exe is None                                                          # nothing moved


def test_cleanup_leaves_other_files_alone(tmp_path, fresh_exe_path):
    exe = tmp_path / "MaplePrice-2.5.exe"
    exe.write_bytes(b"x")
    for name in ("mprice_decoder-2.3.exe.old", "mprice_decoder-2.4.exe.update.part", "Other.exe.old", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    update.cleanup(exe)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MaplePrice-2.5.exe", "Other.exe.old", "notes.txt"]


@pytest.mark.parametrize("name", ["MaplePrice-2.4.exe", "MaplePrice-2.4 (1).exe"])
def test_upgrade_from_mapleprice_24_keeps_the_public_release_name(tmp_path, fresh_exe_path, name):
    current = tmp_path / name
    current.write_bytes(b"2.4")
    payload = tmp_path / (name + ".update")
    payload.write_bytes(EXE)
    manifest = release(version="2.5", file="MaplePrice-2.5.exe")

    installed = update.install(manifest, payload, current)
    moved = update.rename_to_version(installed, version="2.5")
    update.cleanup(moved)

    assert moved.name == "MaplePrice-2.5.exe" and moved.read_bytes() == EXE
    assert list(tmp_path.iterdir()) == [moved]


def test_mapleprice_24_can_verify_the_new_release_signature():
    manifest = release(version="2.5", file="MaplePrice-2.5.exe")
    message = f"MaplePrice 2.5 MaplePrice-2.5.exe {manifest['sha256']}".encode()
    KEY.public_key().verify(base64.b64decode(manifest["signature"]), message)
    assert update.verify(manifest, PUBLIC)
