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


# ---------------------------------------------------------------------------
# HTML pages travel as one self-contained file
# ---------------------------------------------------------------------------

import base64
import json

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def _page(tmp_path, html, files=None):
    for rel, content in (files or {}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    page = tmp_path / "page.html"
    page.write_text(html)
    return str(page)


def _data(content, media="image/png"):
    return f"data:{media};base64,{base64.b64encode(content).decode()}"


def test_a_relative_image_is_inlined(tmp_path):
    page = _page(tmp_path, '<img src="img/hero.png" alt="Hero">', {"img/hero.png": PNG})
    content, report = critique_source.bundle_page(page)
    assert f'<img src="{_data(PNG)}" alt="Hero">' in content.decode()
    assert report == {"bundled": ["img/hero.png"], "missing": [], "external": 0}


def test_a_leading_slash_is_the_pages_own_folder(tmp_path):
    page = _page(tmp_path, '<img src="/img/hero.png">', {"img/hero.png": PNG})
    assert _data(PNG) in critique_source.bundle_page(page)[0].decode()


def test_a_stylesheet_is_inlined_with_its_own_urls_and_imports(tmp_path):
    page = _page(tmp_path, '<link rel="stylesheet" href="css/site.css" media="screen">', {
        "css/site.css": '@import "parts/type.css"; body { background: url(../img/bg.png) }',
        "css/parts/type.css": "@font-face { src: url('../../fonts/a.woff2') }",
        "img/bg.png": PNG,
        "fonts/a.woff2": b"wOF2font",
    })
    html, report = critique_source.bundle_page(page)
    html = html.decode()
    assert html.startswith('<style media="screen">')
    assert f"url({_data(PNG)})" in html
    assert f"url({_data(b'wOF2font', 'font/woff2')})" in html
    assert "<link" not in html
    assert sorted(report["bundled"]) == ["css/parts/type.css", "css/site.css", "fonts/a.woff2", "img/bg.png"]


def test_srcset_candidates_are_inlined_and_missing_ones_reported(tmp_path):
    page = _page(tmp_path, '<img src="a.png" srcset="a.png 1x, a@2x.png 2x">', {"a.png": PNG})
    html, report = critique_source.bundle_page(page)
    assert f'srcset="{_data(PNG)} 1x, a@2x.png 2x"' in html.decode()
    assert report["missing"] == ["a@2x.png"]


def test_a_missing_file_is_reported_not_dropped(tmp_path):
    page = _page(tmp_path, '<img src="gone.png"><video poster="also-gone.jpg"></video>')
    html, report = critique_source.bundle_page(page)
    assert '<img src="gone.png">' in html.decode()
    assert report["missing"] == ["gone.png", "also-gone.jpg"]


def test_internet_and_special_references_are_left_alone_and_counted(tmp_path):
    html_in = ('<script src="https://cdn.tailwindcss.com"></script>'
               '<link rel="stylesheet" href="//fonts.googleapis.com/css2?family=Inter">'
               '<img src="https://example.com/a.png"><img src="data:image/png;base64,AAAA">'
               '<a href="#top">top</a><a href="mailto:x@y.z">mail</a>')
    html, report = critique_source.bundle_page(_page(tmp_path, html_in))
    assert html.decode() == html_in
    assert report == {"bundled": [], "missing": [], "external": 3}


def test_a_local_script_is_inlined_and_keeps_its_type(tmp_path):
    page = _page(tmp_path, '<script type="module" src="app.js"></script>',
                 {"app.js": 'document.body.innerHTML = "</script>";'})
    html = critique_source.bundle_page(page)[0].decode()
    assert html.startswith('<script type="module">')
    assert 'src=' not in html
    assert '"<\\/script>"' in html


def test_a_style_attribute_url_stays_a_valid_attribute(tmp_path):
    page = _page(tmp_path, '<div style="background:url(\'bg.png\')"></div>', {"bg.png": PNG})
    html = critique_source.bundle_page(page)[0].decode()
    assert html == f'<div style="background:url({_data(PNG)})"></div>'


class _Response:
    def __init__(self, body):
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def config(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "token").write_text("tok")
    (cfg / "gateway").write_text("https://gw.test")
    monkeypatch.setattr(critique_source, "TOKEN_FILE", str(cfg / "token"))
    monkeypatch.setattr(critique_source, "GATEWAY_FILE", str(cfg / "gateway"))
    return cfg


def test_a_page_upload_posts_the_bundle_as_html(monkeypatch, tmp_path, config, capsys):
    sent = {}

    def urlopen(req, timeout):
        sent.update(url=req.full_url, body=req.data, type=req.get_header("Content-type"),
                    auth=req.get_header("Authorization"))
        return _Response(json.dumps({"source_id": "a" * 32 + ".html", "bytes": len(req.data)}).encode())

    monkeypatch.setattr(critique_source.urllib.request, "urlopen", urlopen)
    page = _page(tmp_path, '<img src="hero.png"><img src="gone.png">', {"hero.png": PNG})
    critique_source.upload(page)
    out = json.loads(capsys.readouterr().out)
    assert sent["url"] == "https://gw.test/critique-sources/"
    assert sent["type"] == "text/html; charset=utf-8"
    assert sent["auth"] == "Bearer tok"
    assert _data(PNG).encode() in sent["body"]
    assert out["source_id"] == "a" * 32 + ".html"
    assert out["filename"] == "page.html"
    assert out["bundled"] == ["hero.png"] and out["missing"] == ["gone.png"] and out["external"] == 0


def test_an_oversized_page_fails_before_sending_and_names_the_big_files(monkeypatch, tmp_path, config, capsys):
    monkeypatch.setattr(critique_source, "MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(critique_source.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not upload an oversized page"))
    page = _page(tmp_path, '<img src="big.png">', {"big.png": PNG + b"x" * 4096})
    with pytest.raises(SystemExit):
        critique_source.upload(page)
    detail = json.loads(capsys.readouterr().out)["detail"]
    assert "once bundled" in detail and "big.png" in detail


def test_an_image_upload_is_unchanged(monkeypatch, tmp_path, config, capsys):
    sent = {}

    def urlopen(req, timeout):
        sent.update(type=req.get_header("Content-type"), body=req.data)
        return _Response(b'{"source_id": "b.png", "width": 1, "height": 1}')

    monkeypatch.setattr(critique_source.urllib.request, "urlopen", urlopen)
    (tmp_path / "shot.png").write_bytes(PNG)
    critique_source.upload(str(tmp_path / "shot.png"))
    out = json.loads(capsys.readouterr().out)
    assert sent == {"type": "image/png", "body": PNG}
    assert "bundled" not in out
