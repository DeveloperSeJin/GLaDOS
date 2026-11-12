"""설정 읽기 시험."""

from __future__ import annotations

from pathlib import Path

import pytest

from glados.config import ConfigError, ensure_home, glados_home, load_config


def test_glados_home_from_env(tmp_path):
    assert glados_home({"GLADOS_HOME": str(tmp_path / "g")}) == tmp_path / "g"


def test_glados_home_default():
    assert glados_home({}) == Path.home() / ".glados"


def test_first_run_creates_files(tmp_path):
    created = ensure_home(tmp_path)
    assert sorted(created) == ["folders.toml", "glados.toml", "policy.yaml", "servers.toml"]
    assert (tmp_path / "logs").is_dir() and (tmp_path / "checkpoints").is_dir()
    assert "GLaDOS는 이 파일을 고칠 수 없다" in (tmp_path / "policy.yaml").read_text(encoding="utf-8")


def test_existing_files_are_not_overwritten(tmp_path):
    (tmp_path / "glados.toml").write_text('[model]\nmode = "low"\n', encoding="utf-8")
    created = ensure_home(tmp_path)
    assert "glados.toml" not in created
    assert (tmp_path / "glados.toml").read_text(encoding="utf-8") == '[model]\nmode = "low"\n'


def test_default_is_normal_mode(tmp_path):
    ensure_home(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.model.name == "qwen3.5:9b" and cfg.model.num_ctx == 32768 and not cfg.model.is_low
    assert cfg.model.think is False and cfg.limits.max_steps == 12 and cfg.user_name == "saul_park"


def test_low_mode(tmp_path):
    ensure_home(tmp_path)
    text = (tmp_path / "glados.toml").read_text(encoding="utf-8").replace('mode = "normal"', 'mode = "low"')
    (tmp_path / "glados.toml").write_text(text, encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.model.name == "qwen3.5:4b" and cfg.model.num_ctx == 16384 and cfg.model.is_low


def test_invalid_mode_raises(tmp_path):
    (tmp_path / "glados.toml").write_text('[model]\nmode = "fast"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_broken_toml_raises(tmp_path):
    (tmp_path / "glados.toml").write_text("[model\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_policy_layers(tmp_path):
    ensure_home(tmp_path)
    (tmp_path / "policy.yaml").write_text('shell.run:\n  "make *": allow\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    names = [name for name, _ in cfg.policy_layers]
    assert names == ["기본", "연구원님"]
    assert cfg.policy_layers[1][1] == {"shell.run": {"make *": "allow"}}


def test_comment_only_user_policy_is_empty_layer(tmp_path):
    ensure_home(tmp_path)
    assert load_config(tmp_path).policy_layers[1][1] == {}


def test_korean_paths_round_trip(tmp_path):
    ensure_home(tmp_path)
    (tmp_path / "folders.toml").write_text('search_default = ["~/문서"]\nextra_protected = ["~/연구/비공개"]\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.search_default == ["~/문서"] and cfg.extra_protected == ["~/연구/비공개"]


def test_servers_without_alias_are_skipped(tmp_path):
    ensure_home(tmp_path)
    assert load_config(tmp_path).servers == []
    (tmp_path / "servers.toml").write_text('[[server]]\nalias = "server-a"\ngpus = 4\n', encoding="utf-8")
    servers = load_config(tmp_path).servers
    assert len(servers) == 1 and servers[0].alias == "server-a" and servers[0].gpus == 4


def test_web_settings(tmp_path):
    ensure_home(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.web.search == "" and cfg.web.results == 5 and cfg.web.searxng_url == "http://localhost:8888"
    assert cfg.private_words() == ("saul_park",)                      # [user] name도 묻는 이름
    (tmp_path / "glados.toml").write_text(
        '[web]\nsearch = "Tavily"\nresults = 8\nsearxng_url = "http://nas:8888/"\n'
        '[privacy]\nnames = ["홍길동", "Gildong Hong"]\n[user]\nname = "sp"\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.web.search == "tavily" and cfg.web.results == 8 and cfg.web.searxng_url == "http://nas:8888"
    assert cfg.web.key_env == "TAVILY_API_KEY" and cfg.private_words() == ("홍길동", "Gildong Hong")   # 두 글자 이름은 뺌


def test_old_settings_file_has_no_web_section(tmp_path):
    (tmp_path / "glados.toml").write_text('[model]\nmode = "low"\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.web.search == "" and cfg.private_names == []


@pytest.mark.parametrize("body, key", [('[web]\nsearch = "google"\n', "config.web_search"),
                                       ('[web]\nresults = 0\n', "config.web_results"),
                                       ('[web]\nresults = "many"\n', "config.web_results")])
def test_bad_web_settings(tmp_path, body, key):
    (tmp_path / "glados.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(tmp_path)
    assert e.value.key == key


def test_web_search_default_follows_the_os(tmp_path, monkeypatch):
    """2026-11-12 결정: 처음 만드는 glados.toml의 웹 검색은 Windows·맥 Tavily, 리눅스 SearXNG. 있는 파일은 그대로."""
    from conftest import REAL_SEARCH_BY_OS
    from glados import config

    monkeypatch.setattr(config, "SEARCH_BY_OS", REAL_SEARCH_BY_OS)
    for os_name, want in (("windows", "tavily"), ("mac", "tavily"), ("linux", "searxng")):
        home = tmp_path / os_name
        assert "glados.toml" in config.ensure_home(home, os_name=os_name)
        assert config.load_config(home).web.search == want
        assert 'search = ""' not in (home / "glados.toml").read_text(encoding="utf-8")
    (tmp_path / "linux" / "glados.toml").write_text('[web]\nsearch = ""\n', encoding="utf-8")
    assert config.ensure_home(tmp_path / "linux", os_name="linux") == []                 # 이미 있으면 건드리지 않음
    assert config.load_config(tmp_path / "linux").web.search == ""
