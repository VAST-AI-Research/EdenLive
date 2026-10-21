// The server's reference-image transform, reproduced in the browser so the panel
// can show exactly what the model starts from.
//
// Server side: the image is decoded with PIL to RGB (no EXIF rotation, no colour
// management), then "cover"-resized with float bilinear interpolation
// (torch interpolate, align_corners=False, no antialias) so both sides reach at
// least 1280x704, sizes rounded up, and centre-cropped to 1280x704. That crop
// goes straight to the VAE; no further resize happens to it.
export const MODEL_W = 1280;
export const MODEL_H = 704;
export const MAX_BYTES = 25 * 1024 * 1024;
export const MAX_PIXELS = 16_000_000;
const f = Math.fround;

/** Sizes the server computes for an image of w x h. */
export function coverPlan(w, h) {
  const scale = Math.max(MODEL_H / h, MODEL_W / w);
  const rw = Math.ceil(w * scale),
    rh = Math.ceil(h * scale);
  return {
    scale,
    rw,
    rh,
    top: Math.floor((rh - MODEL_H) / 2),
    left: Math.floor((rw - MODEL_W) / 2),
  };
}

/** Decode to raw RGB pixels as PIL would: ignore EXIF orientation and colour profiles. */
async function decode(blob) {
  const raw = { colorSpaceConversion: "none", premultiplyAlpha: "none" };
  let bitmap;
  try {
    bitmap = await createImageBitmap(blob, { ...raw, imageOrientation: "none" });
  } catch {
    bitmap = await createImageBitmap(blob, raw);
  }
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bitmap, 0, 0);
  return { bitmap, data: ctx.getImageData(0, 0, bitmap.width, bitmap.height).data };
}

// Source index and weight along one axis, as upsample_bilinear2d computes them
// (align_corners=False, size given: scale = in / out in float).
function axis(inSize, outSize, offset, count) {
  const scale = f(inSize / outSize);
  const i0 = new Int32Array(count),
    i1 = new Int32Array(count),
    w1 = new Float32Array(count);
  for (let d = 0; d < count; d++) {
    let src = f(f(scale * f(d + offset + 0.5)) - 0.5);
    if (src < 0) src = 0;
    const lo = Math.floor(src);
    i0[d] = lo;
    i1[d] = lo < inSize - 1 ? lo + 1 : lo;
    w1[d] = f(src - lo);
  }
  return { i0, i1, w1 };
}

/** Bilinear cover resize + centre crop, computed only over the kept 1280x704 window. */
function process(data, w, h, plan) {
  const ys = axis(h, plan.rh, plan.top, MODEL_H),
    xs = axis(w, plan.rw, plan.left, MODEL_W);
  const out = new ImageData(MODEL_W, MODEL_H);
  const o = out.data;
  for (let y = 0; y < MODEL_H; y++) {
    const r0 = ys.i0[y] * w,
      r1 = ys.i1[y] * w,
      b = ys.w1[y],
      a = 1 - b;
    for (let x = 0; x < MODEL_W; x++) {
      const c0 = xs.i0[x],
        c1 = xs.i1[x],
        q = xs.w1[x],
        p = 1 - q;
      const p00 = (r0 + c0) * 4,
        p01 = (r0 + c1) * 4,
        p10 = (r1 + c0) * 4,
        p11 = (r1 + c1) * 4,
        t = (y * MODEL_W + x) * 4;
      for (let c = 0; c < 3; c++)
        o[t + c] =
          a * (p * data[p00 + c] + q * data[p01 + c]) +
          b * (p * data[p10 + c] + q * data[p11 + c]);
      o[t + 3] = 255;
    }
  }
  return out;
}

const cache = new Map();
/** Original bitmap, the server's plan, and the 1280x704 model input, for a URL or File. */
export function prepareReference(source) {
  if (cache.has(source)) return cache.get(source);
  const job = (async () => {
    const blob =
      typeof source === "string" ? await (await fetch(source)).blob() : source;
    const { bitmap, data } = await decode(blob);
    const { width: w, height: h } = bitmap;
    if (w * h > MAX_PIXELS || Math.min(w, h) < 32)
      throw Error("Image dimensions must be at least 32 pixels and at most 16 megapixels");
    const plan = coverPlan(w, h);
    return { bitmap, w, h, plan, input: process(data, w, h, plan) };
  })();
  cache.set(source, job);
  job.catch(() => cache.delete(source));
  return job;
}

/** Client-side copy of the server's upload checks, so a bad file fails before upload. */
export function checkFile(file) {
  if (!/^image\/(png|jpeg|webp)$/.test(file.type)) return "Choose a PNG, JPEG or WebP image";
  if (!file.size || file.size > MAX_BYTES) return "Choose a non-empty image up to 25 MiB";
  return "";
}
