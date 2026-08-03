"""Fast local HTML extraction for Ren's web research pipeline."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser, LexborNode
from web_models import ExtractedWebPage, FetchedDocument, WebImageCandidate, WebLink
from web_security import WebUrlError, canonicalize_web_url

NOISE_SELECTOR = ",".join(
    (
        "script",
        "style",
        "noscript",
        "template",
        "canvas",
        "form",
        "nav",
        "footer",
        "aside",
        "[hidden]",
        '[aria-hidden="true"]',
    )
)
CONTENT_SELECTORS = (
    "article",
    "main",
    '[role="main"]',
    ".entry-content",
    ".article-content",
    ".post-content",
    "#content",
)
BOILERPLATE_WORDS = frozenset(
    {"advert", "banner", "breadcrumb", "comment", "footer", "header", "menu", "nav", "related", "sidebar"}
)
BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "header",
        "main",
        "section",
    }
)


def _clean_inline(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _clean_text(value: str) -> str:
    lines = [_clean_inline(line) for line in value.splitlines()]
    output: list[str] = []
    for line in lines:
        if line:
            output.append(line)
        elif output and output[-1]:
            output.append("")
    return "\n".join(output).strip()


def _clean_markdown(value: str) -> str:
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _absolute_web_url(base_url: str, candidate: str | None) -> str | None:
    if not candidate:
        return None
    try:
        return canonicalize_web_url(urljoin(base_url, candidate.strip()))
    except WebUrlError:
        return None


def _meta_content(parser: LexborHTMLParser, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = parser.css_first(selector)
        if node is not None:
            content = _clean_inline(node.attributes.get("content"))
            if content:
                return content
    return None


def _page_title(parser: LexborHTMLParser) -> str | None:
    social_title = _meta_content(parser, ('meta[property="og:title"]', 'meta[name="twitter:title"]'))
    if social_title:
        return social_title
    for selector in ("title", "h1"):
        node = parser.css_first(selector)
        if node is not None:
            title = _clean_inline(node.text(deep=True, separator=" ", strip=True))
            if title:
                return title
    return None


def _base_url(parser: LexborHTMLParser, final_url: str) -> str:
    node = parser.css_first("base[href]")
    if node is None:
        return final_url
    return _absolute_web_url(final_url, node.attributes.get("href")) or final_url


def _candidate_score(node: LexborNode) -> float:
    text = _clean_inline(node.text(deep=True, separator=" ", strip=True))
    if not text:
        return float("-inf")
    text_length = len(text)
    link_length = sum(
        len(_clean_inline(link.text(deep=True, separator=" ", strip=True))) for link in node.css("a")
    )
    paragraph_bonus = len(node.css("p")) * 90
    heading_bonus = len(node.css("h1,h2,h3")) * 45
    link_penalty = (link_length / max(text_length, 1)) * min(text_length, 2500)
    attributes = " ".join(
        (node.attributes.get("id", ""), node.attributes.get("class", ""))
    ).lower()
    boilerplate_penalty = 1200 if any(word in attributes for word in BOILERPLATE_WORDS) else 0
    return text_length + paragraph_bonus + heading_bonus - link_penalty - boilerplate_penalty


def _content_root(parser: LexborHTMLParser) -> LexborNode:
    candidates: list[LexborNode] = []
    for selector in CONTENT_SELECTORS:
        candidates.extend(parser.css(selector))
    meaningful = [
        candidate
        for candidate in candidates
        if len(_clean_inline(candidate.text(deep=True, separator=" ", strip=True))) >= 120
    ]
    if meaningful:
        return max(meaningful, key=_candidate_score)
    return parser.body or parser.root


def _best_srcset_url(value: str | None) -> str | None:
    if not value:
        return None
    candidates: list[tuple[float, str]] = []
    for item in value.split(","):
        parts = item.strip().split()
        if not parts:
            continue
        weight = 1.0
        if len(parts) > 1:
            descriptor = parts[-1].lower()
            try:
                if descriptor.endswith("w"):
                    weight = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    weight = float(descriptor[:-1]) * 1000
            except ValueError:
                weight = 1.0
        candidates.append((weight, parts[0]))
    return max(candidates, default=(0, ""), key=lambda item: item[0])[1] or None


def _positive_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"\s*(\d+)", value)
    if not match:
        return None
    parsed = int(match.group(1))
    return parsed if parsed > 0 else None


def _extract_images(parser: LexborHTMLParser, base_url: str, source_url: str) -> list[WebImageCandidate]:
    images: list[WebImageCandidate] = []
    seen: set[str] = set()

    social_image = _meta_content(
        parser,
        ('meta[property="og:image"]', 'meta[name="twitter:image"]'),
    )
    social_url = _absolute_web_url(base_url, social_image)
    if social_url:
        seen.add(social_url)
        images.append(WebImageCandidate(url=social_url, source_url=source_url))

    for node in parser.css("img"):
        raw_url = (
            _best_srcset_url(node.attributes.get("srcset") or node.attributes.get("data-srcset"))
            or node.attributes.get("data-src")
            or node.attributes.get("data-original")
            or node.attributes.get("src")
        )
        image_url = _absolute_web_url(base_url, raw_url)
        if not image_url or image_url in seen:
            continue
        width = _positive_int(node.attributes.get("width"))
        height = _positive_int(node.attributes.get("height"))
        if width is not None and height is not None and width <= 32 and height <= 32:
            continue
        seen.add(image_url)
        images.append(
            WebImageCandidate(
                url=image_url,
                source_url=source_url,
                alt=_clean_inline(node.attributes.get("alt")),
                title=_clean_inline(node.attributes.get("title")) or None,
                width=width,
                height=height,
            )
        )
    return images


def _render_children(node: LexborNode, base_url: str) -> str:
    output: list[str] = []
    child = node.child
    while child is not None:
        output.append(_render_node(child, base_url))
        child = child.next
    return "".join(output)


def _render_node(node: LexborNode, base_url: str) -> str:
    tag = node.tag
    if tag == "-text":
        return node.text_content or ""
    if tag in {"script", "style", "noscript", "template"}:
        return ""
    if tag == "br":
        return "  \n"
    if tag == "hr":
        return "\n\n---\n\n"

    inner = _render_children(node, base_url)
    stripped = inner.strip()
    if not stripped and tag != "img":
        return ""
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"\n\n{'#' * int(tag[1])} {stripped}\n\n"
    if tag == "p":
        return f"\n\n{stripped}\n\n"
    if tag in BLOCK_TAGS:
        return f"\n\n{stripped}\n\n"
    if tag == "li":
        return f"\n- {stripped}"
    if tag in {"ul", "ol"}:
        return f"\n{stripped}\n"
    if tag == "blockquote":
        quoted = "\n".join(f"> {line}" for line in stripped.splitlines())
        return f"\n\n{quoted}\n\n"
    if tag == "pre":
        code = node.text(deep=True, separator="", strip=False).strip("\n")
        return f"\n\n```\n{code}\n```\n\n"
    if tag == "code" and (node.parent is None or node.parent.tag != "pre"):
        inline_code = stripped.replace("`", "ˋ")
        return f"`{inline_code}`"
    if tag in {"strong", "b"}:
        return f"**{stripped}**"
    if tag in {"em", "i"}:
        return f"*{stripped}*"
    if tag == "a":
        href = _absolute_web_url(base_url, node.attributes.get("href"))
        label = stripped or href or ""
        return f"[{label}]({href})" if href else label
    if tag == "img":
        raw_url = (
            _best_srcset_url(node.attributes.get("srcset") or node.attributes.get("data-srcset"))
            or node.attributes.get("data-src")
            or node.attributes.get("data-original")
            or node.attributes.get("src")
        )
        src = _absolute_web_url(base_url, raw_url)
        alt = _clean_inline(node.attributes.get("alt"))
        return f"![{alt}]({src})" if src else ""
    if tag in {"td", "th"}:
        return f" {stripped} |"
    if tag == "tr":
        return f"\n|{stripped}"
    if tag == "table":
        return f"\n\n{stripped}\n\n"
    return inner


def _extract_links(root: LexborNode, base_url: str) -> list[WebLink]:
    links: list[WebLink] = []
    seen: set[str] = set()
    for node in root.css("a[href]"):
        url = _absolute_web_url(base_url, node.attributes.get("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(
            WebLink(
                url=url,
                text=_clean_inline(node.text(deep=True, separator=" ", strip=True)),
                title=_clean_inline(node.attributes.get("title")) or None,
            )
        )
    return links


def extract_web_page(document: FetchedDocument) -> ExtractedWebPage:
    """Extract readable content, citations, and image candidates from one HTML document."""

    warnings: list[str] = []
    if document.content_type == "text/plain":
        text = _clean_text(document.text)
        quality = min(1.0, len(text) / 1500)
        return ExtractedWebPage(
            requested_url=document.requested_url,
            final_url=document.final_url,
            canonical_url=document.final_url,
            text=text,
            markdown=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            content_type=document.content_type,
            status_code=document.status_code,
            fetched_at=document.fetched_at,
            elapsed_ms=document.elapsed_ms,
            quality_score=quality,
            requires_hosted_fallback=len(text) < 200,
            warnings=["The fetched document contained very little text."] if len(text) < 200 else [],
        )

    parser = LexborHTMLParser(document.text)
    base_url = _base_url(parser, document.final_url)
    title = _page_title(parser)
    description = _meta_content(
        parser,
        ('meta[name="description"]', 'meta[property="og:description"]'),
    )
    language_node = parser.css_first("html[lang]")
    language = _clean_inline(language_node.attributes.get("lang")) if language_node else None
    canonical_node = parser.css_first('link[rel="canonical"][href]')
    canonical_url = (
        _absolute_web_url(base_url, canonical_node.attributes.get("href"))
        if canonical_node is not None
        else None
    ) or document.final_url
    images = _extract_images(parser, base_url, document.final_url)

    for node in list(parser.css(NOISE_SELECTOR)):
        node.decompose()
    root = _content_root(parser)
    text = _clean_text(root.text(deep=True, separator="\n", strip=True))
    markdown = _clean_markdown(_render_children(root, base_url))
    links = _extract_links(root, base_url)

    paragraph_count = len(root.css("p"))
    quality = min(0.65, len(text) / 3000)
    quality += min(0.2, paragraph_count * 0.04)
    quality += 0.08 if title else 0
    quality += 0.07 if description else 0
    quality = round(min(1.0, quality), 3)
    requires_fallback = len(text) < 200 or quality < 0.25
    if len(text) < 200:
        warnings.append("Local extraction found very little readable text.")
    if not title:
        warnings.append("The page did not expose a title.")

    return ExtractedWebPage(
        requested_url=document.requested_url,
        final_url=document.final_url,
        canonical_url=canonical_url,
        title=title,
        description=description,
        language=language or None,
        text=text,
        markdown=markdown,
        links=links,
        images=images,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        content_type=document.content_type,
        status_code=document.status_code,
        fetched_at=document.fetched_at,
        elapsed_ms=document.elapsed_ms,
        quality_score=quality,
        requires_hosted_fallback=requires_fallback,
        warnings=warnings,
    )
