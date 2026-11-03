import json

import pytest

from mprice_decoder.distribution import private_access
from mprice_decoder import distribution


def config():
    return {"token": "cozy_private_friends_token_for_testing", "token_id": 42,
            "url": "https://cozy.markets", "label": "friends test build"}


def test_public_build_has_no_private_access(tmp_path):
    assert private_access(tmp_path / "absent.json") is None


def test_private_build_without_its_credential_asset_never_becomes_public(tmp_path, monkeypatch):
    monkeypatch.setattr(distribution, "PRIVATE_BUILD", True)
    access = private_access(tmp_path / "absent.json")
    assert access is not None and access.token == ""


def test_corrupted_private_asset_stays_private(tmp_path):
    path = tmp_path / "private.json"
    path.write_bytes(b"\xff\xfe")
    access = private_access(path)
    assert access is not None and access.token == ""


def test_private_credentials_load_without_exposing_the_token(tmp_path):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(config()))
    access = private_access(path)
    assert access.token == config()["token"] and access.token_id == 42
    assert access.url == "https://cozy.markets" and access.token not in repr(access)


@pytest.mark.parametrize("data", ["not json", "null", "[]", "{}",
    json.dumps({**config(), "token": ""}),
    json.dumps({**config(), "token": "cozy_with_whitespace_ "}),
    json.dumps({**config(), "token_id": -1}),
    json.dumps({**config(), "url": "https://elsewhere.example"})])
def test_broken_private_config_never_turns_into_public_access(tmp_path, data):
    path = tmp_path / "private.json"
    path.write_text(data)
    access = private_access(path)
    assert access is not None and access.token == ""


def test_new_private_token_can_be_identified_by_its_server_label(tmp_path):
    path = tmp_path / "private.json"
    path.write_text(json.dumps({**config(), "token_id": None}))
    access = private_access(path)
    assert access.token and access.token_id is None and access.label == "friends test build"
