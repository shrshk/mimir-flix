import json

import pytest

from mimir_flix import settings


def test_settings_file_then_environment_over_the_defaults(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps({"tv_host": "10.0.0.5", "port": 9000, "media_root": "~/m"}))
    monkeypatch.setenv("MIMIR_FLIX_PORT", "9100")
    cfg = settings.load(tmp_path)
    assert cfg["tv_host"] == "10.0.0.5" and cfg["port"] == 9100  # the environment wins, as a number
    assert cfg["tvs"] == [{"name": "TV", "host": "10.0.0.5", "kind": "android"}]  # the one-TV form is the first TV
    assert settings.default_tv(cfg) == "10.0.0.5"
    assert (
        cfg["jellyfin_url"] == "http://127.0.0.1:8096"
        and cfg["media_root"].endswith("/m")
        and "~" not in cfg["media_root"]
    )
    (tmp_path / "settings.json").write_text(json.dumps({"tv_hots": "typo"}))
    with pytest.raises(SystemExit, match="tv_hots"):
        settings.load(tmp_path)


def test_save_merges_and_keeps_only_what_differs_from_the_defaults(tmp_path):
    settings.save({"tv_host": "10.0.0.5", "host": "127.0.0.1"}, tmp_path)
    settings.save({"kodi_url": "http://10.0.0.5:8080"}, tmp_path)
    assert json.loads((tmp_path / "settings.json").read_text()) == {
        "tv_host": "10.0.0.5",
        "kodi_url": "http://10.0.0.5:8080",
    }
