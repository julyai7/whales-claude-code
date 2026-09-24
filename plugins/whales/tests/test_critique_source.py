"""critique_source.py — the file-type sniffing its upload depends on.

The backend refuses an upload whose media type is not an image it can
critique, so the script must send the type the BYTES say, not the name:
Claude Code saves a pasted image as `.png` whatever it really is.
"""
import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).resolve().parents[1] / "skills" / "universal-critique" / "critique_source.py"
_spec = importlib.util.spec_from_file_location("critique_source", _PATH)
critique_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(critique_source)


def test_the_bytes_decide_the_type_not_the_extension():
    assert critique_source._media_type(b"\xff\xd8\xff\xe0rest", "pasted.png") == "image/jpeg"
    assert critique_source._media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "shot.png") == "image/webp"
    assert critique_source._media_type(b"\x89PNG\r\n\x1a\nrest", "x.jpg") == "image/png"
    assert critique_source._media_type(b"GIF89a...", "x") == "image/gif"


def test_an_unrecognised_file_falls_back_to_its_name():
    assert critique_source._media_type(b"# notes", "notes.md") in ("text/markdown", "application/octet-stream")


def test_a_test_config_dir_never_touches_the_real_one(monkeypatch, tmp_path):
    monkeypatch.setenv("WHALES_CONFIG_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("critique_source_env", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TOKEN_FILE == str(tmp_path / "token")
