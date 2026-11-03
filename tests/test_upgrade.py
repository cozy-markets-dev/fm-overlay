"""Upgrade an existing database: its queued scans and upload identity are kept."""
import sqlite3

from mprice_decoder import sync
from mprice_decoder.data import ItemDB

from test_shop import FakeSource


def test_legacy_decoder_queue_is_preserved_while_ocr_history_stays_local(tmp_path):
    path = tmp_path / "decoder.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                shop_title TEXT, seller TEXT, listings INTEGER NOT NULL,
                full_list INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, at REAL NOT NULL,
                item_id INTEGER, ocr_name TEXT NOT NULL, qty INTEGER NOT NULL,
                price INTEGER, unit_price REAL, confirmed INTEGER NOT NULL,
                stock INTEGER, stats TEXT);
            INSERT INTO scans VALUES (1, 1000, 'OCR', 'Seller', 1, 0);
            INSERT INTO scans VALUES (2, 1001, 'Decoder', 'Seller', 1, 1);
            INSERT INTO scans VALUES (3, 1002, 'Partial decoder', 'Seller', 1, 0);
            INSERT INTO scans VALUES (4, 1003, 'Empty decoded shop', 'Seller', 0, 1);
            INSERT INTO scans VALUES (5, 1004, 'Unknown empty visit', 'Seller', 0, 0);
            INSERT INTO sightings VALUES (1, 1, 1000, NULL, 'Arrow', 500, 300, 0.6, 1, NULL, NULL);
            INSERT INTO sightings VALUES (2, 2, 1001, NULL, 'Arrow', 500, 300, 0.6, 1, 1000, NULL);
            INSERT INTO sightings VALUES (3, 3, 1002, NULL, 'Arrow', 500, 250, 0.5, 1, 500, NULL);
        """)
    db = ItemDB(path, source=FakeSource())
    expected = [("ocr",), ("decoder",), ("decoder",), ("decoder",), ("ocr",)]
    assert db.conn.execute("SELECT source FROM scans ORDER BY id").fetchall() == expected
    assert sync.pending(db, "https://cozy.markets") == 3
    body, _ = sync.payload(db, "pc", 0)
    assert [scan["client_id"] for scan in body["scans"]] == [2, 3, 4]
    db.conn.close()
    reopened = ItemDB(path, source=FakeSource())
    try:
        assert reopened.conn.execute("SELECT source FROM scans ORDER BY id").fetchall() == expected
    finally:
        reopened.conn.close()


def test_24_history_and_upload_identity_survive_the_decoder_upgrade(tmp_path):
    path = tmp_path / "items.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                shop_title TEXT, seller TEXT, listings INTEGER NOT NULL,
                channel INTEGER, fm_room INTEGER, map_name TEXT, scanned_by TEXT);
            CREATE TABLE sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, at REAL NOT NULL,
                item_id INTEGER, ocr_name TEXT NOT NULL, qty INTEGER NOT NULL,
                price INTEGER, unit_price REAL, confirmed INTEGER NOT NULL,
                sold INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO scans VALUES (1, 1000, 'Old shop', 'Seller', 1, 6, 2, 'Free Market <2>', 'Cheng');
            INSERT INTO sightings VALUES (1, 1, 1000, 355, 'Pig''s Head', 2, 1000, 500, 1, 0);
            INSERT INTO meta VALUES ('cozy_install_id', 'existing-install');
            INSERT INTO meta VALUES ('cozy_public_token https://cozy.markets', 'existing-token');
            INSERT INTO meta VALUES ('cozy_pushed_scan https://cozy.markets', '1');
        """)

    db = ItemDB(path, source=FakeSource())
    try:
        assert sync.install_id(db) == "existing-install"
        assert db.meta("cozy_public_token https://cozy.markets") == "existing-token"
        assert sync.pushed(db, "https://cozy.markets") == 1
        assert db.conn.execute("SELECT qty, price, stock, stats FROM sightings WHERE id = 1").fetchone() == (
            2, 1000, None, None)
        assert db.conn.execute("SELECT full_list FROM scans WHERE id = 1").fetchone() == (0,)

    finally:
        db.conn.close()
