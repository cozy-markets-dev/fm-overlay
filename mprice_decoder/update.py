"""Self-update from cozy.markets: on startup, offer a newer release, then swap
the exe in place and restart. The new version then names its file after itself
(mprice_decoder-2.5.exe), whatever version did the swap.

A release is latest.json plus the exe. latest.json names the exe, its SHA-256
and size, and has the release notes, all signed with the release key (release.sh).
Nothing is offered unless that signature checks out: mprice_decoder runs as admin, so
neither a tampered download nor tampered notes (the "Update now?" dialog shows
them) must get through, even if the server is broken into.
"""
import base64
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

from .version import VERSION
from .identity import APP_ID, COMPATIBILITY
from .distribution import private_access

log = logging.getLogger("mprice_decoder")

MANIFEST_URL = COMPATIBILITY["manifest_url"]
PUBLIC_KEY = "XEy6sfR+LaM9HZJZq1oA33B/ELv5jMCe2Z4feXSpIUg="  # Ed25519; the private half never leaves the release PC
TIMEOUT = 8
EXE_NAME = re.compile(r"[\w.-]+\.exe")  # a bare file name next to latest.json: no paths, no other hosts
# A release's file name as the site serves it (mprice_decoder-2.4.exe), or as a browser saves a second
# download of it (mprice_decoder-2.4 (1).exe). Other names were picked by someone: they're left alone.
RELEASE_NAME = re.compile(r"mprice_decoder-\d+(?:\.\d+)*(?: \(\d+\))?\.exe", re.IGNORECASE)
LEGACY_RELEASE_NAME = re.compile(COMPATIBILITY["legacy_release_pattern"], re.IGNORECASE)
LEFTOVERS = (".old", ".update", ".update.part")  # what an update leaves next to the exe until the next start
SIGNED = ("version", "file", "sha256", "size", "notes")  # what signature2 covers

_exe = None  # this exe's path once rename_to_version() has moved it (sys.executable still says where it was)


def parse_version(v):
    return tuple(int(p) for p in str(v).split("."))


def newer(remote, local=VERSION):
    try:
        return parse_version(remote) > parse_version(local)
    except ValueError:
        return False


def signed_message(manifest):
    """What `signature` covers: all that copies from before signature2 check."""
    return f"{COMPATIBILITY['signature_domain']} {manifest['version']} {manifest['file']} {manifest['sha256'].lower()}".encode()


def signed_message2(manifest):
    """What `signature2` covers: the notes and size too. JSON, so no field can run into
    the next; release.sh builds the same bytes."""
    fields = {k: manifest[k] for k in SIGNED}
    return COMPATIBILITY["signature_domain2"].encode() + json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def verify(manifest, public_key=PUBLIC_KEY):
    """Was this manifest signed with the release key, notes and size included?"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        # signature2 is required, or a server could strip it and the notes would be unsigned again.
        # It vouches for everything on its own; signature is checked as well so that release.sh, which
        # runs this before uploading, also proves the copies that only know signature will take it.
        key.verify(base64.b64decode(manifest["signature2"]), signed_message2(manifest))
        key.verify(base64.b64decode(manifest["signature"]), signed_message(manifest))
    except (InvalidSignature, KeyError, ValueError, TypeError, AttributeError):
        return False
    return bool(EXE_NAME.fullmatch(str(manifest.get("file"))))


def check(session=requests, url=MANIFEST_URL, local=VERSION, public_key=PUBLIC_KEY):
    """The manifest of a newer, properly signed release, or None."""
    if private_access() is not None:
        log.info("private build: public release updates disabled")
        return None
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    manifest = r.json()
    if not newer(manifest.get("version"), local):
        return None
    if not verify(manifest, public_key):
        log.warning("release %r has a bad signature: ignored", manifest.get("version"))
        return None
    return manifest


def download(manifest, dest, session=requests, url=MANIFEST_URL, progress=lambda done, total: None):
    """Fetch the release exe to `dest`. True only if it's exactly the signed one."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    size, digest, done = manifest["size"], hashlib.sha256(), 0  # the signed size, never Content-Length
    try:
        with session.get(urljoin(url, manifest["file"]), stream=True, timeout=TIMEOUT) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    done += len(chunk)
                    if done > size:  # a server that keeps sending mustn't fill the disk
                        log.warning("update download is over its signed %d bytes: stopped", size)
                        return False
                    f.write(chunk)
                    digest.update(chunk)
                    progress(done, size)
        if done != size:
            log.warning("update download is %d bytes, not the signed %d: discarded", done, size)
            return False
        if digest.hexdigest() != manifest["sha256"].lower():
            log.warning("update download doesn't match its signed SHA-256: discarded")
            return False
        os.replace(part, dest)
        return True
    finally:
        part.unlink(missing_ok=True)


def installed_exe():
    return _exe or Path(sys.executable)  # the exe itself, also in a one-file PyInstaller build


def release_name(version=VERSION):
    return f"{APP_ID}-{version}.exe"


def rename_to_version(current=None, version=VERSION):
    """Name this exe after the version it is (mprice_decoder-2.5.exe): an update swaps the new exe in
    under the old one's name. Only a release's name is changed, never one someone picked, and
    never onto a file that's already there. Returns where the exe is from now on."""
    global _exe
    current = Path(current or installed_exe())
    want = current.with_name(release_name(version))
    if current.name == want.name or not (RELEASE_NAME.fullmatch(current.name) or LEGACY_RELEASE_NAME.fullmatch(current.name)):
        return current
    if want.exists():  # e.g. downloaded by hand next to it (also: the same file, named in another case)
        log.info("not renaming %s: %s is already there", current.name, want.name)
        return current
    try:
        os.replace(current, want)  # Windows lets a running exe be renamed
    except OSError:
        log.exception("couldn't rename %s to %s", current.name, want.name)
        return current
    log.info("renamed %s to %s", current.name, want.name)
    _exe = want
    return want


def install(manifest, new_exe, current=None):
    """Swap `new_exe` in for the running exe (Windows lets a running exe be
    renamed, not deleted), under its name for now: the new version renames
    itself when it starts (rename_to_version). The old one is left as .old
    until the next start.

    It's hashed again first, as it is on disk: download() hashed the bytes as they
    arrived, the file sits in the exe's folder, where programs without admin rights
    can write, and it's about to run as admin. If it's no longer the exe `manifest`
    signed, it's deleted and ValueError raised, the running exe left as it was."""
    current = Path(current or installed_exe())
    with open(new_exe, "rb") as f:
        same = hashlib.file_digest(f, "sha256").hexdigest() == manifest["sha256"].lower()
    if not same:
        Path(new_exe).unlink(missing_ok=True)
        raise ValueError(f"{Path(new_exe).name} changed after it was downloaded: not installed")
    old = current.with_name(current.name + ".old")
    old.unlink(missing_ok=True)
    os.replace(current, old)
    try:
        os.replace(new_exe, current)
    except OSError:
        os.replace(old, current)  # never leave them without an exe
        raise
    return current


def cleanup(current=None):
    """Remove what updates left behind next to the exe: under its name, and under any release's
    name (the update ran under the old one's name, and this exe may have been renamed since)."""
    current = Path(current or installed_exe())
    names = {current.name + suffix for suffix in LEFTOVERS}
    for suffix in LEFTOVERS:
        names.update(p.name for p in current.parent.glob(f"*.exe{suffix}")
                     if RELEASE_NAME.fullmatch(p.name[:-len(suffix)]) or LEGACY_RELEASE_NAME.fullmatch(p.name[:-len(suffix)]))
    for leftover in sorted(names):
        try:
            current.with_name(leftover).unlink(missing_ok=True)
        except OSError:  # the old copy may still be closing
            log.info("couldn't remove %s yet", leftover)
