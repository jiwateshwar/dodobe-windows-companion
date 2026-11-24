"""The settings logic, which runs anywhere: python3 -m pytest tests (or python3 tests/test_config.py)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "companion"))

import config  # noqa: E402
from config import Config, Drive  # noqa: E402


def test_defaults():
    cfg = Config()
    assert cfg.host == "dodobe"
    assert cfg.homepage == "http://dodobe"
    assert cfg.unc("By species") == r"\\dodobe\By species"
    assert not cfg.configured


def test_homepage_override():
    assert Config(host="nas", home_url=" https://studio.dodobe.net ").homepage == "https://studio.dodobe.net"
    assert Config(host="192.168.0.246").homepage == "http://192.168.0.246"


def test_validate():
    good = Config(username="jiwateshwar", drives=[Drive("Library", "L"), Drive("By species", "S")])
    assert config.validate(good) == []
    bad = Config(host="bad host", home_url="dodobe", username="Jiwa Teshwar",
                 drives=[Drive("Library", "L"), Drive("library", "L"), Drive("x/y", "C")])
    problems = " ".join(config.validate(bad))
    for words in ("address", "http://", "Dodobe name", "Two folders are on L:", "mapped twice", "x/y", "D to Z"):
        assert words in problems, words


def test_suggest_letter():
    assert config.suggest_letter("Library", set()) == "L"
    assert config.suggest_letter("Library", {"L"}) == "Z"
    assert config.suggest_letter("Photos", {"Z", "Y"}) == "X"
    assert config.suggest_letter("Pool", set(config.LETTERS)) is None


def test_removed():
    old = Config(drives=[Drive("Library", "L"), Drive("Dumpsite", "M"), Drive("Pool", "P")])
    new = Config(drives=[Drive("library", "L"), Drive("Dumpsite", "N")])
    assert [d.share for d in config.removed(old, new)] == ["Dumpsite", "Pool"]  # moved, and dropped
    other_box = Config(host="nas2", drives=old.drives)
    assert len(config.removed(old, other_box)) == 3


def test_save_and_load():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "config.json"
        cfg = Config(host="dodobe.local", username="ana", drives=[Drive("Library", "L")])
        config.save(cfg, path)
        assert config.load(path) == cfg
        assert "password" not in path.read_text()
        path.write_text("{not json")
        assert config.load(path) == Config()
    assert config.load(Path("/nonexistent/config.json")) == Config()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
