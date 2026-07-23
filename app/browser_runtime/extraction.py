"""Deterministic DOM extraction shared by browser engines."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from .contracts import ArticleMetadata, ExtractMode, ExtractionResult, LinkResult


_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "canvas")


def _clean_text(value: str) -> str:
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _markdown_from_node(node: object) -> str:
    soup = BeautifulSoup(str(node), "html.parser")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if not isinstance(tag, Tag) or not tag.name:
            continue
        level = int(tag.name[1])
        tag.replace_with(f"\n{'#' * level} {tag.get_text(' ', strip=True)}\n")
    for tag in soup.find_all("a", href=True):
        if not isinstance(tag, Tag):
            continue
        href = str(tag.get("href") or "")
        text = tag.get_text(" ", strip=True) or href
        tag.replace_with(f"[{text}]({href})")
    for tag in soup.find_all("li"):
        if not isinstance(tag, Tag):
            continue
        tag.replace_with(f"\n- {tag.get_text(' ', strip=True)}")
    for tag in soup.find_all(["p", "div", "section", "article", "br", "blockquote", "pre"]):
        if not isinstance(tag, Tag):
            continue
        text = tag.get_text(" ", strip=True)
        tag.replace_with(f"\n{text}\n")
    return _clean_text(soup.get_text(" "))


def extract_document(
        html: str,
        *,
        final_url: str,
        mode: ExtractMode = "auto",
        max_chars: int = 100_000,
) -> ExtractionResult:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    canonical_tag = soup.find("link", rel="canonical")
    canonical_href = canonical_tag.get("href") if isinstance(canonical_tag, Tag) else None
    canonical = urljoin(final_url, str(canonical_href)) if canonical_href else None
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    selected_content = soup.find("article") or soup.find("main") or soup.body
    content_node = selected_content if isinstance(selected_content, Tag) else soup
    text = _clean_text(content_node.get_text("\n", strip=True))
    markdown = _markdown_from_node(content_node)
    links: list[LinkResult] = []
    seen: set[str] = set()
    for anchor in content_node.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        absolute = urljoin(final_url, str(anchor.get("href")))
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            links.append(LinkResult(text=anchor.get_text(" ", strip=True), url=absolute))
        if len(links) >= 500:
            break
    metadata = _article_metadata(soup)
    resolved_mode: ExtractMode = "article" if mode == "auto" and soup.find("article") else ("text" if mode == "auto" else mode)
    result = ExtractionResult(
        mode=resolved_mode,
        title=title,
        canonical_url=canonical,
        text=text if resolved_mode in {"text", "markdown", "article"} else None,
        markdown=markdown if resolved_mode in {"markdown", "article"} else None,
        html=str(content_node) if resolved_mode == "html" else None,
        article=metadata if resolved_mode == "article" else None,
        links=links,
    )
    for field in ("text", "markdown", "html"):
        value = getattr(result, field)
        if value and len(value) > max_chars:
            setattr(result, field, value[:max_chars])
            result.truncated = True
    return result


def _article_metadata(soup: BeautifulSoup) -> ArticleMetadata:
    def meta(*keys: str) -> str | None:
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if isinstance(tag, Tag) and tag.get("content"):
                return str(tag["content"]).strip()
        return None

    author = meta("author", "article:author")
    published = meta("article:published_time", "datePublished", "date")
    description = meta("description", "og:description", "twitter:description")
    site_name = meta("og:site_name", "application-name")
    for script in soup.find_all("script", type="application/ld+json"):
        if not isinstance(script, Tag):
            continue
        try:
            payload = json.loads(script.string or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload.get("@graph", []) if isinstance(payload, dict) else []
        candidates = [payload, *candidates] if isinstance(payload, dict) else payload if isinstance(payload, list) else []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            author_value = item.get("author")
            if not author and isinstance(author_value, dict):
                author = author_value.get("name")
            author = author or (author_value if isinstance(author_value, str) else None)
            published = published or item.get("datePublished")
            description = description or item.get("description")
    return ArticleMetadata(author=author, published_at=published, description=description, site_name=site_name)

