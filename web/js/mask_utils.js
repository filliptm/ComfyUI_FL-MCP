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

export function formatImageWidgetRef(ref) {
    const image = parseImageWidgetRef(ref);
    if (!image) return null;
    const path = [image.subfolder, image.filename].filter(Boolean).join("/");
    return `${path} [${image.type}]`;
}

export function nestedImageRefForNode(node) {
    const imageWidgetIndex = node?.widgets?.findIndex(widget => widget.name === "image") ?? -1;
    if (imageWidgetIndex < 0) return null;

    const candidates = [
        node?.properties?.image,
        node?.widgets_values?.[imageWidgetIndex],
        node?.widgets?.[imageWidgetIndex]?.value,
        node?.images?.[0],
    ];
    for (const candidate of candidates) {
        const ref = parseImageWidgetRef(candidate);
        if (ref?.subfolder) return ref;
    }
    return null;
}

export const MASK_POLYGON_MIN_POINTS = 3;
export const MASK_POLYGON_MAX_POINTS = 64;
const MASK_SHA256_PATTERN = /^[0-9a-f]{64}$/;


export function buildExactMaskComposeFormData(sourceBlob, alphaBlob, attestation) {
    if (!(sourceBlob instanceof Blob) || sourceBlob.size <= 0) {
        throw new Error("An exact non-empty source Blob is required for mask composition");
    }
    if (!(alphaBlob instanceof Blob) || alphaBlob.size <= 0) {
        throw new Error("A non-empty alpha Blob is required for mask composition");
    }
    if (
        !attestation
        || !MASK_SHA256_PATTERN.test(String(attestation.sha256 || ""))
        || !Number.isSafeInteger(attestation.size_bytes)
        || attestation.size_bytes !== sourceBlob.size
        || !Number.isSafeInteger(attestation.width)
        || attestation.width <= 0
        || !Number.isSafeInteger(attestation.height)
        || attestation.height <= 0
    ) {
        throw new Error("The exact mask source attestation is invalid");
    }
    const formData = new FormData();
    formData.append("source", sourceBlob, "attested-mask-source");
    formData.append("alpha", alphaBlob, "mask-alpha.png");
    formData.append("expected_sha256", attestation.sha256);
    formData.append("expected_size_bytes", String(attestation.size_bytes));
    formData.append("expected_width", String(attestation.width));
    formData.append("expected_height", String(attestation.height));
    return formData;
}


function finiteMaskNumber(value, label) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        throw new Error(`${label} must be a finite number`);
    }
    return value;
}


function normalizePolygonMaskRegion(region, scaleX, scaleY, imageWidth, imageHeight) {
    if (
        !Array.isArray(region.points)
        || region.points.length < MASK_POLYGON_MIN_POINTS
        || region.points.length > MASK_POLYGON_MAX_POINTS
    ) {
        throw new Error(
            `Polygon mask regions require ${MASK_POLYGON_MIN_POINTS}-${MASK_POLYGON_MAX_POINTS} points`,
        );
    }
    const epsilon = 0.001;
    const points = region.points.map((point, index) => {
        if (!point || typeof point !== "object" || Array.isArray(point)) {
            throw new Error(`Polygon mask point ${index} must be an object`);
        }
        const x = finiteMaskNumber(point.x, `Polygon mask point ${index}.x`) * scaleX;
        const y = finiteMaskNumber(point.y, `Polygon mask point ${index}.y`) * scaleY;
        if (x < 0 || y < 0 || x > imageWidth + epsilon || y > imageHeight + epsilon) {
            throw new Error(
                `Polygon mask point ${index} (${point.x}, ${point.y}) falls outside `
                + `the ${imageWidth}x${imageHeight} image`,
            );
        }
        return { x, y };
    });
    const signedDoubleArea = points.reduce((sum, point, index) => {
        const next = points[(index + 1) % points.length];
        return sum + point.x * next.y - next.x * point.y;
    }, 0);
    if (Math.abs(signedDoubleArea) <= 1e-9) {
        throw new Error("Polygon mask regions must enclose a nonzero area");
    }
    const xs = points.map(point => point.x);
    const ys = points.map(point => point.y);
    const x = Math.min(...xs);
    const y = Math.min(...ys);
    return {
        x,
        y,
        width: Math.max(...xs) - x,
        height: Math.max(...ys) - y,
        points,
        shape: "polygon",
        operation: region.operation || "paint",
        feather: region.feather ?? 0,
    };
}


export function normalizeMaskRegion(region, coordinateSpace, imageWidth, imageHeight) {
    if (!region || typeof region !== "object" || Array.isArray(region)) {
        throw new Error("Mask region must be an object");
    }
    if (!["pixels", "normalized"].includes(coordinateSpace)) {
        throw new Error("Mask coordinate space must be pixels or normalized");
    }
    finiteMaskNumber(imageWidth, "Mask image width");
    finiteMaskNumber(imageHeight, "Mask image height");
    if (imageWidth <= 0 || imageHeight <= 0) {
        throw new Error("Mask image dimensions must be positive");
    }
    const shape = region.shape || "rectangle";
    if (!["rectangle", "ellipse", "polygon"].includes(shape)) {
        throw new Error(`Unsupported mask region shape: ${shape}`);
    }
    if (!["paint", "erase"].includes(region.operation || "paint")) {
        throw new Error(`Unsupported mask region operation: ${region.operation}`);
    }
    const feather = finiteMaskNumber(region.feather ?? 0, "Mask region feather");
    if (feather < 0 || feather > 512) {
        throw new Error("Mask region feather must be between 0 and 512 pixels");
    }
    const scaleX = coordinateSpace === "normalized" ? imageWidth : 1;
    const scaleY = coordinateSpace === "normalized" ? imageHeight : 1;
    if (shape === "polygon") {
        if ([region.x, region.y, region.width, region.height].some(value => value != null)) {
            throw new Error("Polygon mask regions use points instead of x/y/width/height");
        }
        return normalizePolygonMaskRegion(
            { ...region, operation: region.operation || "paint", feather },
            scaleX,
            scaleY,
            imageWidth,
            imageHeight,
        );
    }
    if (region.points != null) {
        throw new Error("Rectangle and ellipse mask regions cannot include polygon points");
    }
    const normalized = {
        x: finiteMaskNumber(region.x, "Mask region x") * scaleX,
        y: finiteMaskNumber(region.y, "Mask region y") * scaleY,
        width: finiteMaskNumber(region.width, "Mask region width") * scaleX,
        height: finiteMaskNumber(region.height, "Mask region height") * scaleY,
        shape,
        operation: region.operation || "paint",
        feather,
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


export function drawMaskRegionPath(context, region) {
    context.beginPath();
    if (region.shape === "polygon") {
        if (!Array.isArray(region.points) || region.points.length < MASK_POLYGON_MIN_POINTS) {
            throw new Error("A normalized polygon mask region requires at least three points");
        }
        context.moveTo(region.points[0].x, region.points[0].y);
        for (const point of region.points.slice(1)) {
            context.lineTo(point.x, point.y);
        }
        context.closePath();
    } else if (region.shape === "ellipse") {
        context.ellipse(
            region.x + region.width / 2,
            region.y + region.height / 2,
            region.width / 2,
            region.height / 2,
            0,
            0,
            Math.PI * 2,
        );
    } else if (region.shape === "rectangle") {
        context.rect(region.x, region.y, region.width, region.height);
    } else {
        throw new Error(`Unsupported normalized mask region shape: ${region.shape}`);
    }
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
