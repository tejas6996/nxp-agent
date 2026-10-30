"""Press-release output: no trademark symbols, and only this release's own picture."""

from datetime import date

from app.services.images import DownloadedImage, rank_images
from app.services.parsing import ImageRef, extract_article_page
from app.services.press_release import _fallback_note, strip_trademarks


def test_trademark_symbols_are_removed() -> None:
    assert strip_trademarks("Alpha and Omega's new αMOS E2™ 600V MOSFETs") == "Alpha and Omega's new αMOS E2 600V MOSFETs"
    assert strip_trademarks("Intel(R) Core(TM) Ultra and Arm® Cortex®-M55") == "Intel Core Ultra and Arm Cortex-M55"
    assert strip_trademarks("PureCel®Plus-S and Nyxel® NIR") == "PureCel Plus-S and Nyxel NIR"
    assert strip_trademarks("LoRaWAN ® and Wi-Fi CERTIFIED 6™.") == "LoRaWAN and Wi-Fi CERTIFIED 6."
    assert strip_trademarks("Service℠ mark, Brand© name") == "Service mark, Brand name"
    # Ordinary text is untouched: letters in parentheses, newlines, tables, "TM" as a word.
    text = "Options (a) and (R) apply.\nTM module | 5 V | (C) grade"
    assert strip_trademarks(text) == text


AOS_LIKE = """<html><head><title>AOS to Showcase Power Solutions at electronica 2026</title></head>
<body>
<div class="ProductMenu_tab_lists__ByoKG">
  <div class="ProductMenu_tab__YE_67">Gallium Nitride (GaN) Transistors</div>
  <a href="/products/aogt037"><div class="ProductMenu_feature_wrapper__J5gQ2">
    <div class="ProductMenu_feature_label__eysjK">FEATURED AOGT037V60DE2 New 600V Superjunction MOSFETs</div>
    <div class="ProductMenu_image__eNh_q">
      <img src="https://www.aosmd.com/sites/default/files/2026-09/AOS_AOGT037-060DE2_no-txt.jpg" alt="AOGT037V60DE2 PR Image">
    </div>
  </div></a>
</div>
<div class="mega-menu"><img src="https://www.aosmd.com/files/menu-tile-ev.jpg" alt="EV tile"></div>
<div class="article-body">
  <p>Alpha and Omega Semiconductor will showcase AI core power, AI data center and
  industrial solutions at electronica 2026 in Munich. """ + "The booth features demos. " * 30 + """</p>
  <img src="https://www.aosmd.com/sites/default/files/2026-10/electronica-2026-booth.jpg" alt="AOS electronica booth">
</div>
</body></html>"""


def test_pictures_and_text_inside_site_menus_are_ignored() -> None:
    page = extract_article_page(AOS_LIKE, "https://www.aosmd.com/news/aos-showcase-electronica-2026")
    urls = [i.url for i in page.images]
    # The menu's "featured product" tile is an older release's photo - never this article's.
    assert not any("AOGT037" in u for u in urls)
    assert not any("menu-tile" in u for u in urls)
    assert "https://www.aosmd.com/sites/default/files/2026-10/electronica-2026-booth.jpg" in urls
    # Menu wording must not reach the writer either (it would name the old product).
    assert "AOGT037V60DE2" not in page.text
    assert "electronica 2026 in Munich" in page.text


def test_a_menu_word_never_removes_the_article_itself() -> None:
    html = """<html><body><div class="nav-offset content"><h1>Acme Launches X1</h1>
    <p>Acme has launched the X1 sensor. """ + "Details follow. " * 20 + """</p>
    <img src="https://acme.com/img/x1-hero.jpg" alt="X1"></div></body></html>"""
    page = extract_article_page(html, "https://acme.com/news/x1")
    assert "Acme has launched the X1 sensor" in page.text
    assert "https://acme.com/img/x1-hero.jpg" in [i.url for i in page.images]


def test_trademark_superscripts_do_not_leave_stray_letters() -> None:
    html = """<html><body><article><h1>Acme X1</h1>
    <p>The Acme<sup>TM</sup> X1 uses Arm<sup>&reg;</sup> cores and SuperLink<sup>SM</sup>.
    """ + "More detail. " * 30 + """ Footnote<sup>2</sup> stays.</p></article></body></html>"""
    page = extract_article_page(html, "https://acme.com/news/x1")
    assert "AcmeTM" not in page.text and "Acme TM" not in page.text
    assert "SuperLink SM" not in page.text
    assert "Footnote" in page.text and "2" in page.text  # real superscripts are kept


def test_an_old_upload_ranks_below_a_fresh_one_and_is_flagged() -> None:
    refs = [
        ImageRef("https://x.com/wp-content/uploads/2026/04/stock-chip-background.jpg", origin="social"),
        ImageRef("https://x.com/wp-content/uploads/2026/10/x1-launch.jpg", origin="content"),
    ]
    ranked = rank_images(refs, "Acme Launches X1", "https://x.com/news/x1", published=date(2026, 10, 29))
    assert ranked[0].url.endswith("2026/10/x1-launch.jpg")
    assert ranked[1].old and not ranked[0].old
    # One month apart is normal (pictures are often uploaded before the release goes out).
    recent = rank_images(
        [ImageRef("https://x.com/files/2026-09/x1.jpg", origin="social")], "X1", "https://x.com/n",
        published=date(2026, 10, 29),
    )
    assert not recent[0].old
    # Without a publication date nothing is judged.
    assert not rank_images(refs, "X1", "https://x.com/n")[0].old


def test_an_old_picture_used_as_last_resort_gets_a_note() -> None:
    image = DownloadedImage(
        "https://x.com/wp-content/uploads/2026/04/stock.jpg", "", "social", b"", "jpg",
        1200, 800, 0, old=True,
    )
    note = _fallback_note(image)
    assert "April 2026" in note and "older" in note
