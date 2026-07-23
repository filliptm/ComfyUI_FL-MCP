/**
 * Conservative Markdown renderer. Every user/model string enters the DOM through
 * textContent; links are restricted to http(s). It intentionally supports only
 * the structures useful in a compact assistant panel.
 */
export function renderMarkdown(source) {
    const root = document.createElement("div");
    root.className = "fl-chat-markdown";
    const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
    let paragraph = [];
    let list = null;
    let code = null;
    let codeLanguage = "";

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

    for (const line of lines) {
        if (line.startsWith("```")) {
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
        if (!line.trim()) {
            flushParagraph();
            flushList();
            continue;
        }
        paragraph.push(line.trim());
    }
    flushParagraph();
    flushList();
    flushCode();
    return root;
}

function appendInline(container, source) {
    const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
    let cursor = 0;
    for (const match of source.matchAll(pattern)) {
        if (match.index > cursor) {
            container.append(document.createTextNode(source.slice(cursor, match.index)));
        }
        const token = match[0];
        if (token.startsWith("`")) {
            const code = document.createElement("code");
            code.textContent = token.slice(1, -1);
            container.appendChild(code);
        } else if (token.startsWith("**")) {
            const strong = document.createElement("strong");
            strong.textContent = token.slice(2, -2);
            container.appendChild(strong);
        } else {
            const parts = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
            if (parts) {
                const link = document.createElement("a");
                link.textContent = parts[1];
                link.href = parts[2];
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
