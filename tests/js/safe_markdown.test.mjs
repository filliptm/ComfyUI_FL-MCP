import assert from "node:assert/strict";
import test from "node:test";

import { parseMarkdownImageLine } from "../../web/js/safe_markdown.js";


test("standalone Markdown images become ordered gallery candidates", () => {
    assert.deepEqual(
        parseMarkdownImageLine("1. ![Factory reference](https://images.example/factory%20one.jpg)"),
        {
            alt: "Factory reference",
            url: "https://images.example/factory%20one.jpg",
        },
    );
    assert.deepEqual(
        parseMarkdownImageLine("- ![](https://images.example/two.webp)"),
        { alt: "", url: "https://images.example/two.webp" },
    );
});


test("non-image and unsafe Markdown lines stay out of image galleries", () => {
    assert.equal(parseMarkdownImageLine("[Source](https://example.com)"), null);
    assert.equal(parseMarkdownImageLine("![Unsafe](javascript:alert(1))"), null);
    assert.equal(parseMarkdownImageLine("Text before ![Inline](https://example.com/a.jpg)"), null);
});
