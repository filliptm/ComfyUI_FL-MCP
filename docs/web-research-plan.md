# Lightweight Web Research Plan

Ren's web research stack stays native to the existing Python process. It does not require
Docker, Chromium, Playwright, Redis, or a separate worker service. Static pages are fetched
with the project's shared async HTTP client and parsed locally with `selectolax`; hosted
extraction is only a fallback for pages that cannot be read reliably.

## Phase 1: safe local web foundation

- Validate and canonicalize public HTTP(S) URLs before every request and redirect.
- Block loopback, private, link-local, metadata, reserved, and unsafe-port targets.
- Stream responses with strict type, redirect, timeout, and byte limits.
- Extract useful article text, links, and image candidates locally.
- Cache normalized results with bounded size and expiry.

## Phase 2: search and fetch tools

- Add a no-key, no-credit search mode using DuckDuckGo's lightweight HTML results. This is
  intentionally marked best-effort because the public endpoint can rate-limit or change.
- Add optional Tavily Basic (one credit) and Tavily Advanced (two credits) modes through a
  small REST adapter instead of pulling in Tavily's SDK and dependency tree.
- Fetch selected pages with the safe local Phase 1 extractor, so ordinary page reads do not
  consume Tavily credits.
- Store the optional Tavily API credential in the existing OS-keychain-backed credential
  system, never in the JSON settings file.
- Let the user choose No web, Free web, Tavily Basic, or Tavily Deep for each message, with a
  saved default in Settings.
- Allow the composer search action to be hidden while keeping the saved default active.
- Expose explicit read-only MCP tools with provider, credit, progress, and error messages.

## Phase 3: sources and citations

- Attach stable source IDs to research results.
- Verify that citations resolve to fetched evidence.
- Show source cards, progress, and partial failures in Ren's chat UI.

## Phase 4: deep research

- Add fast, balanced, and deep research modes.
- Expand queries iteratively, score relevance, deduplicate canonical URLs, and crawl only
  promising same-site links within strict budgets.
- Synthesize evidence with conflicts and uncertainty instead of importing a heavyweight
  multi-agent research runtime.

## Phase 5: web images and ComfyUI import

- Discover images from search results and fetched pages, including lazy-loaded and `srcset`
  candidates.
- Show preview, source page, dimensions when known, and an explicit `license unknown` state.
- Fetch only reviewed source IDs; revalidate redirects, content type, file signature, byte
  size, and decoded pixel count. Initially accept PNG, JPEG, and WebP and reject unsafe SVG.
- Preserve source URL, page URL, retrieval time, and content hash as provenance.
- Require a user review gate before copying approved files into
  `ComfyUI/input/ren-references/<chat-id>/` and adding them to the workflow.

## Delivery

Each phase includes focused tests and lands on the feature branch. The complete stack is
published as a draft pull request for review before it is marked ready.
