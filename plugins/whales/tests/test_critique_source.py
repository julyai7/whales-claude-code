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


# ---------------------------------------------------------------------------
# Screenshots: the original, not Cursor's reduced copy
# ---------------------------------------------------------------------------

import struct

UUID = "28a9df01-317b-47da-aa49-1036568cb657"


def _png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def _jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def test_dimensions_are_read_from_each_format():
    assert critique_source._dimensions(_png(1179, 2676)) == (1179, 2676)
    assert critique_source._dimensions(_jpeg(451, 1024)) == (451, 1024)
    assert critique_source._dimensions(b"GIF89a" + struct.pack("<HH", 30, 40)) == (30, 40)
    vp8x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x0a\x00\x00\x00" + b"\x00" * 4 + (880).to_bytes(3, "little") + (1999).to_bytes(3, "little")
    assert critique_source._dimensions(vp8x) == (881, 2000)
    assert critique_source._dimensions(b"not an image") is None


@pytest.fixture
def cursor_paste(monkeypatch, tmp_path):
    """Cursor's reduced copy of a pasted screenshot, and a Downloads folder to find the original in."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(critique_source, "SEARCH_DIRS", (str(downloads),))
    assets = tmp_path / ".cursor" / "projects" / "Users-me-work-app" / "assets"
    assets.mkdir(parents=True)
    copy = assets / f"Meetup_iOS_26-{UUID}.jpg"
    copy.write_bytes(_jpeg(451, 1024))
    return copy, downloads


def test_cursors_copy_is_swapped_for_the_original(cursor_paste):
    copy, downloads = cursor_paste
    (downloads / "Meetup iOS 26.png").write_bytes(_png(1179, 2676))
    path, report = critique_source._pick_image(str(copy), exact=False)
    assert path == str(downloads / "Meetup iOS 26.png")
    assert report["original"]["size"] == [1179, 2676]
    assert report["original"]["reduced_size"] == [451, 1024]
    assert report["original"]["instead_of"] == str(copy)


def test_a_same_named_image_of_another_shape_is_not_the_original(cursor_paste):
    copy, downloads = cursor_paste
    (downloads / "Meetup iOS 26.png").write_bytes(_png(2000, 1200))
    path, report = critique_source._pick_image(str(copy), exact=False)
    assert path == str(copy)
    assert "reduced_copy" in report and "Meetup_iOS_26" in report["reduced_copy"]["note"]


def test_no_original_found_says_to_ask_for_it(cursor_paste):
    copy, _ = cursor_paste
    path, report = critique_source._pick_image(str(copy), exact=False)
    assert path == str(copy)
    assert report["reduced_copy"]["size"] == [451, 1024]
    assert "Ask the designer" in report["reduced_copy"]["note"]


def test_exact_sends_the_given_file(cursor_paste):
    copy, downloads = cursor_paste
    (downloads / "Meetup iOS 26.png").write_bytes(_png(1179, 2676))
    path, report = critique_source._pick_image(str(copy), exact=True)
    assert path == str(copy)
    assert "original" not in report


def test_a_file_outside_cursors_folders_is_never_swapped(monkeypatch, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "Meetup iOS 26.png").write_bytes(_png(1179, 2676))
    monkeypatch.setattr(critique_source, "SEARCH_DIRS", (str(downloads),))
    mine = tmp_path / f"Meetup_iOS_26-{UUID}.png"
    mine.write_bytes(_png(1080, 2451))
    assert critique_source._pick_image(str(mine), exact=False) == (str(mine), {})


def test_a_narrow_phone_screenshot_carries_a_warning(tmp_path):
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(_jpeg(451, 1024))
    path, report = critique_source._pick_image(str(shot), exact=False)
    assert path == str(shot) and report["low_resolution"]["size"] == [451, 1024]


def test_the_upload_sends_the_original_and_names_it(monkeypatch, cursor_paste, config, capsys):
    copy, downloads = cursor_paste
    original = _png(1179, 2676)
    (downloads / "Meetup iOS 26.png").write_bytes(original)
    sent = {}

    def urlopen(req, timeout):
        sent.update(type=req.get_header("Content-type"), body=req.data)
        return _Response(b'{"source_id": "c.png", "width": 1179, "height": 2676}')

    monkeypatch.setattr(critique_source.urllib.request, "urlopen", urlopen)
    critique_source.upload(str(copy))
    out = json.loads(capsys.readouterr().out)
    assert sent == {"type": "image/png", "body": original}
    assert out["filename"] == "Meetup iOS 26.png"
    assert out["original"]["instead_of"] == str(copy)


# ---------------------------------------------------------------------------
# compare: a screen and its rebuild side by side
# ---------------------------------------------------------------------------

import html as html_lib
import re
import struct
import zlib


def _png(width, height):
    """A real, decodable one-colour PNG, so a browser can lay it out too."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def test_a_page_beside_a_portrait_screenshot_is_laid_out_at_phone_width(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png(10, 20))
    page = _page(tmp_path, '<img src="hero.png">', {"hero.png": PNG})
    out, missing = critique_source.compare_page(str(shot), page)
    assert 'data-width="390"' in out
    assert "Before" in out and "After" in out
    # The rebuilt page travels bundled, so it renders wherever this file is opened.
    srcdoc = html_lib.unescape(re.search(r'srcdoc="([^"]*)"', out).group(1))
    assert _data(PNG) in srcdoc
    assert missing == []


def test_a_landscape_screenshot_makes_the_page_a_desktop_one(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png(30, 20))
    page = _page(tmp_path, "<p>hi</p>")
    assert 'data-width="1440"' in critique_source.compare_page(str(shot), page)[0]
    assert 'data-width="800"' in critique_source.compare_page(str(shot), page, width=800)[0]


def test_a_missing_file_in_either_page_is_reported(tmp_path):
    page = _page(tmp_path, '<img src="gone.png">')
    assert critique_source.compare_page(page, page)[1] == ["gone.png", "gone.png"]


def test_a_file_that_is_neither_image_nor_page_is_refused(tmp_path, capsys):
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    with pytest.raises(SystemExit):
        critique_source.compare_page(str(notes), str(notes))
    assert "cannot be compared" in capsys.readouterr().out


def test_without_a_browser_the_html_is_still_written(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(critique_source, "_browser", lambda: None)
    before = tmp_path / "before.png"
    before.write_bytes(_png(10, 20))
    after = _page(tmp_path, "<p>after</p>")
    critique_source.compare(str(before), after, None, None)
    result = json.loads(capsys.readouterr().out)
    assert result["html"] == str(tmp_path / "page.compare.html")
    assert result["png"] is None and "WHALES_BROWSER" in result["note"]
    assert (tmp_path / "page.compare.html").exists()


def test_an_unknown_browser_override_is_not_used(monkeypatch):
    monkeypatch.setenv("WHALES_BROWSER", "/no/such/browser")
    assert critique_source._browser() is None


@pytest.mark.skipif(critique_source._browser() is None, reason="no Chrome-family browser here")
def test_the_png_is_both_panels_at_one_height(tmp_path, capsys):
    before = tmp_path / "before.png"
    before.write_bytes(_png(200, 400))
    after = _page(tmp_path, '<body style="margin:0"><div style="height:800px;background:#08f"></div></body>')
    critique_source.compare(str(before), after, None, None)
    result = json.loads(capsys.readouterr().out)
    size = critique_source._file_dimensions(result["png"])
    # Both panels scaled to the taller one (800), side by side, plus captions and padding.
    assert size[1] > 800 and size[0] > 2 * 200
