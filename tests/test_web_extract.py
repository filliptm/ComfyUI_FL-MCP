from datetime import UTC, datetime

from web_extract import extract_web_page
from web_models import FetchedDocument


def make_document(html, *, content_type="text/html"):
    return FetchedDocument(
        requested_url="https://example.com/story?utm_source=test",
        final_url="https://example.com/story",
        status_code=200,
        content_type=content_type,
        content=html.encode(),
        text=html,
        fetched_at=datetime(2026, 8, 3, tzinfo=UTC),
        elapsed_ms=12,
    )


def test_extracts_article_metadata_markdown_links_and_image_candidates():
    html = """
    <!doctype html>
    <html lang="en">
      <head>
        <title>Fallback title</title>
        <meta property="og:title" content="Useful research story">
        <meta name="description" content="A careful description.">
        <meta property="og:image" content="/social.jpg">
        <link rel="canonical" href="/story?utm_campaign=launch">
      </head>
      <body>
        <nav><a href="/login">Login navigation</a></nav>
        <article class="entry-content">
          <h1>Useful research story</h1>
          <p>This is the first substantial paragraph with enough explanatory text to read.</p>
          <h2>Evidence</h2>
          <p>The second paragraph links to <a href="/source?gclid=1">primary evidence</a>
          and explains why the result matters for this implementation.</p>
          <ul><li>Fast local parsing</li><li>Bounded network access</li></ul>
          <img src="/small.jpg" srcset="/medium.jpg 640w, /large.jpg 1280w"
               alt="Factory reference" width="1280" height="720">
        </article>
        <script>window.__huge_app_state = "ignore me";</script>
      </body>
    </html>
    """

    page = extract_web_page(make_document(html))

    assert page.title == "Useful research story"
    assert page.description == "A careful description."
    assert page.language == "en"
    assert page.canonical_url == "https://example.com/story"
    assert "window.__huge_app_state" not in page.text
    assert "Login navigation" not in page.text
    assert "## Evidence" in page.markdown
    assert "[primary evidence](https://example.com/source)" in page.markdown
    assert page.links[0].url == "https://example.com/source"
    assert [image.url for image in page.images] == [
        "https://example.com/social.jpg",
        "https://example.com/large.jpg",
    ]
    assert page.images[1].width == 1280
    assert not page.requires_hosted_fallback
    assert len(page.content_hash) == 64


def test_short_script_shell_requests_hosted_fallback():
    page = extract_web_page(
        make_document("<html><head><title>App</title></head><body><div id='root'>Loading</div></body></html>")
    )

    assert page.text == "Loading"
    assert page.requires_hosted_fallback
    assert page.quality_score < 0.25
    assert page.warnings


def test_plain_text_extraction_does_not_need_html_parser():
    page = extract_web_page(make_document("Line one\n\nLine two", content_type="text/plain"))

    assert page.text == "Line one\n\nLine two"
    assert page.markdown == page.text
    assert page.images == []
    assert page.requires_hosted_fallback
