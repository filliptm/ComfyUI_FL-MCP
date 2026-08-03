export function parseImageWidgetRef(value, defaultType = "input") {
    if (value && typeof value === "object" && typeof value.filename === "string") {
        return {
            filename: value.filename,
            subfolder: value.subfolder || "",
            type: value.type || defaultType,
        };
    }
    if (typeof value !== "string" || value.startsWith("$")) {
        return null;
    }

    const match = value.match(/^(.*?)(?:\s+\[(input|output|temp)\])?$/);
    const annotatedPath = (match?.[1] || "").replaceAll("\\", "/");
    const parts = annotatedPath.split("/").filter(Boolean);
    const filename = parts.pop();
    if (!filename) {
        return null;
    }
    return {
        filename,
        subfolder: parts.join("/"),
        type: match?.[2] || defaultType,
    };
}

export function normalizeMaskRegion(region, coordinateSpace, imageWidth, imageHeight) {
    const scaleX = coordinateSpace === "normalized" ? imageWidth : 1;
    const scaleY = coordinateSpace === "normalized" ? imageHeight : 1;
    const normalized = {
        x: region.x * scaleX,
        y: region.y * scaleY,
        width: region.width * scaleX,
        height: region.height * scaleY,
        shape: region.shape || "rectangle",
        operation: region.operation || "paint",
        feather: region.feather || 0,
    };
    const epsilon = 0.001;
    if (
        normalized.x < 0 || normalized.y < 0 ||
        normalized.width <= 0 || normalized.height <= 0 ||
        normalized.x + normalized.width > imageWidth + epsilon ||
        normalized.y + normalized.height > imageHeight + epsilon
    ) {
        throw new Error(
            `Mask region (${region.x}, ${region.y}, ${region.width}, ${region.height}) ` +
            `falls outside the ${imageWidth}x${imageHeight} image`
        );
    }
    return normalized;
}

export function summarizeMaskPixels(imageData) {
    let weightedPixels = 0;
    let minX = imageData.width;
    let minY = imageData.height;
    let maxX = -1;
    let maxY = -1;
    for (let y = 0; y < imageData.height; y++) {
        for (let x = 0; x < imageData.width; x++) {
            const alpha = imageData.data[(y * imageData.width + x) * 4 + 3];
            weightedPixels += alpha / 255;
            if (alpha > 1) {
                minX = Math.min(minX, x);
                minY = Math.min(minY, y);
                maxX = Math.max(maxX, x);
                maxY = Math.max(maxY, y);
            }
        }
    }
    const totalPixels = imageData.width * imageData.height;
    return {
        coverage_percent: Number((weightedPixels / totalPixels * 100).toFixed(3)),
        bounds: maxX < 0 ? null : {
            x: minX,
            y: minY,
            width: maxX - minX + 1,
            height: maxY - minY + 1,
        },
    };
}
