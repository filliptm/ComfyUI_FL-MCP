/**
 * Conservative Markdown renderer. Every user/model string enters the DOM through
 * textContent; links are restricted to http(s). Remote images must be routed
 * through the caller-provided local preview resolver.
 */

const MAX_MARKDOWN_GALLERY_IMAGES = 12;

function safeHttpUrl(value) {
    try {
        const parsed = new URL(String(value || ""));
        return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
    } catch (_) {
        return "";
    }
}

export function parseMarkdownImageLine(source) {
    const match = String(source || "").match(
        /^\s*(?:(?:\d+[.)]|[-*])\s+)?!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)\s*$/,
    );
    if (!match) return null;
    const url = safeHttpUrl(match[2]);
    return url ? { alt: match[1].trim(), url } : null;
}

export function renderMarkdown(source, options = {}) {
    const root = document.createElement("div");
    root.className = "fl-chat-markdown";
    const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
    const resolveImageUrl = typeof options.resolveImageUrl === "function"
        ? options.resolveImageUrl
        : () => "";
    let paragraph = [];
    let list = null;
    let code = null;
    let codeLanguage = "";
    let gallery = [];

    const flushParagraph = () => {
        if (!paragraph.length) return;
        const p = document.createElement("p");
        appendInline(p, paragraph.join(" "));
        root.appendChild(p);
        paragraph = [];
    };
    const flushList = () => {
        if (list) root.appendChild(list);
        list = null;
    };
    const flushCode = () => {
        if (!code) return;
        const pre = document.createElement("pre");
        const codeElement = document.createElement("code");
        if (codeLanguage) codeElement.dataset.language = codeLanguage;
        codeElement.textContent = code.join("\n");
        pre.appendChild(codeElement);
        root.appendChild(pre);
        code = null;
        codeLanguage = "";
    };
    const flushGallery = () => {
        if (!gallery.length) return;
        root.appendChild(createImageGallery(gallery, resolveImageUrl));
        gallery = [];
    };

    for (const line of lines) {
        if (line.startsWith("```")) {
            flushGallery();
            if (code) {
                flushCode();
            } else {
                flushParagraph();
                flushList();
                code = [];
                codeLanguage = line.slice(3).trim().slice(0, 30);
            }
            continue;
        }
        if (code) {
            code.push(line);
            continue;
        }
        const markdownImage = parseMarkdownImageLine(line);
        if (markdownImage) {
            flushParagraph();
            flushList();
            gallery.push(markdownImage);
            continue;
        }
        if (!line.trim()) {
            flushParagraph();
            flushList();
            continue;
        }

        flushGallery();
        const heading = line.match(/^(#{1,3})\s+(.+)$/);
        if (heading) {
            flushParagraph();
            flushList();
            const element = document.createElement(`h${heading[1].length + 2}`);
            appendInline(element, heading[2]);
            root.appendChild(element);
            continue;
        }
        const bullet = line.match(/^\s*[-*]\s+(.+)$/);
        if (bullet) {
            flushParagraph();
            if (!list) list = document.createElement("ul");
            const item = document.createElement("li");
            appendInline(item, bullet[1]);
            list.appendChild(item);
            continue;
        }
        paragraph.push(line.trim());
    }
    flushParagraph();
    flushList();
    flushCode();
    flushGallery();
    return root;
}

function createImageGallery(candidates, resolveImageUrl) {
    const images = [];
    const seen = new Set();
    for (const candidate of candidates) {
        if (seen.has(candidate.url)) continue;
        seen.add(candidate.url);
        images.push(candidate);
    }
    const visible = images.slice(0, MAX_MARKDOWN_GALLERY_IMAGES);
    const grid = document.createElement("section");
    grid.className = "fl-chat-image-grid fl-image-grid";
    grid.dataset.count = String(visible.length);
    grid.dataset.layout = visible.length === 1
        ? "single"
        : visible.length % 2 === 1
            ? "hero"
            : "grid";
    grid.setAttribute("role", "list");
    grid.setAttribute(
        "aria-label",
        `${visible.length} ${visible.length === 1 ? "image" : "images"}`,
    );

    for (const [index, candidate] of visible.entries()) {
        const figure = document.createElement("figure");
        figure.className = "fl-chat-image-card fl-image-card";
        figure.setAttribute("role", "listitem");

        const link = document.createElement("a");
        link.href = candidate.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = "Open original image";

        let previewUrl = "";
        try {
            previewUrl = safeHttpUrl(resolveImageUrl(candidate.url));
        } catch (_) {
            previewUrl = "";
        }
        const fallback = document.createElement("span");
        fallback.className = "fl-chat-image-fallback fl-image-fallback";
        fallback.hidden = Boolean(previewUrl);
        fallback.textContent = previewUrl ? "Preview unavailable" : "Open image";

        if (previewUrl) {
            const preview = document.createElement("img");
            preview.src = previewUrl;
            preview.alt = candidate.alt || `Image ${index + 1}`;
            preview.loading = "lazy";
            preview.decoding = "async";
            preview.fetchPriority = "low";
            preview.referrerPolicy = "no-referrer";
            if (index === 0) preview.fetchPriority = "high";
            preview.addEventListener("error", () => {
                figure.classList.add("failed");
                preview.hidden = true;
                fallback.hidden = false;
            }, { once: true });
            link.appendChild(preview);
        }
        link.appendChild(fallback);
        figure.appendChild(link);

        const caption = candidate.alt || new URL(candidate.url).hostname;
        const figcaption = document.createElement("figcaption");
        figcaption.textContent = caption.slice(0, 120);
        figcaption.title = caption;
        figure.appendChild(figcaption);
        grid.appendChild(figure);
    }

    if (images.length > visible.length) {
        const overflow = document.createElement("span");
        overflow.className = "fl-chat-image-overflow fl-image-overflow";
        overflow.textContent = `+${images.length - visible.length} more images`;
        grid.appendChild(overflow);
    }
    return grid;
}

function appendInline(container, source) {
    const pattern = /(!\[[^\]]*\]\(https?:\/\/[^)\s]+\)|`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
    let cursor = 0;
    for (const match of source.matchAll(pattern)) {
        if (match.index > cursor) {
            container.append(document.createTextNode(source.slice(cursor, match.index)));
        }
        const token = match[0];
        if (token.startsWith("![")) {
            const parts = token.match(/^!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)$/);
            const url = safeHttpUrl(parts?.[2]);
            if (parts && url) {
                const link = document.createElement("a");
                link.textContent = parts[1] || "Open image";
                link.href = url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                container.appendChild(link);
            }
        } else if (token.startsWith("`")) {
            const codeElement = document.createElement("code");
            codeElement.textContent = token.slice(1, -1);
            container.appendChild(codeElement);
        } else if (token.startsWith("**")) {
            const strong = document.createElement("strong");
            strong.textContent = token.slice(2, -2);
            container.appendChild(strong);
        } else {
            const parts = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
            const url = safeHttpUrl(parts?.[2]);
            if (parts && url) {
                const link = document.createElement("a");
                link.textContent = parts[1];
                link.href = url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                container.appendChild(link);
            }
        }
        cursor = match.index + token.length;
    }
    if (cursor < source.length) {
        container.append(document.createTextNode(source.slice(cursor)));
    }
}
