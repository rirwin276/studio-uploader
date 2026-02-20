# app.py — Studio Uploader (FastAPI)
#
# Fixes:
# - OOM prevention: rembg u2netp + global session + pre-downscale + upload size guard
# - /ui embed + return=postmessage (NO download on Done, including mobile)
# - Brush edits work: fixes coordinate mismatch (1000px canvas vs 3000px image)
# - Normalize output to 3000x3000 PNG and trim transparent padding aggressively
#
# Railway start command (IMPORTANT: single worker on small plan):
#   uvicorn app:app --host 0.0.0.0 --port $PORT

from fastapi import FastAPI, UploadFile, File, Query, Request, Form
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from PIL import Image, ImageDraw
from io import BytesIO
import uuid
import os
import time
from typing import Optional, Dict, Any, Tuple

from rembg import remove, new_session

# ----------------------------
# App
# ----------------------------
app = FastAPI(title="Studio Uploader", version="0.4.0")

# ----------------------------
# CORS
# ----------------------------
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://127.0.0.1:8000,http://localhost:8000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Storage + Print Standard
# ----------------------------
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

TARGET_INCHES = 10
TARGET_DPI = 300
TARGET_PX = TARGET_INCHES * TARGET_DPI  # 3000

DEFAULT_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60)))  # 1 hour

# Upload size guard (bytes)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))  # adjust if needed
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Pre-downscale before rembg (reduces RAM spikes)
REMBG_MAX_DIM = int(os.getenv("REMBG_MAX_DIM", "1600"))

# Canvas coordinate system used by the UI
UI_CANVAS_PX = int(os.getenv("UI_CANVAS_PX", "1000"))

# ----------------------------
# rembg session (GLOBAL, SMALL MODEL)
# ----------------------------
# u2netp is smaller + lower RAM than u2net
REMBG_MODEL = os.getenv("REMBG_MODEL", "u2netp")
REMBG_SESSION = new_session(REMBG_MODEL)


def _paths(session_id: str) -> Dict[str, str]:
    return {
        "orig": os.path.join(UPLOAD_DIR, f"{session_id}_orig.png"),
        "base": os.path.join(UPLOAD_DIR, f"{session_id}_base.png"),
        "curr": os.path.join(UPLOAD_DIR, f"{session_id}_curr.png"),
    }


def _load_rgba(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")


def _save_png(img: Image.Image, path: str):
    img.save(path, "PNG", optimize=True)


def _cleanup_old_uploads(max_age_seconds: int = DEFAULT_TTL_SECONDS):
    now = time.time()
    try:
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                age = now - os.path.getmtime(path)
                if age > max_age_seconds:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass


def _delete_session_files(session_id: str):
    p = _paths(session_id)
    for k in ("orig", "base", "curr"):
        try:
            if os.path.exists(p[k]):
                os.remove(p[k])
        except Exception:
            pass


def _svg_bytes_to_png_bytes(svg_bytes: bytes) -> bytes:
    try:
        import cairosvg  # type: ignore
    except Exception as e:
        raise RuntimeError("SVG support requires 'cairosvg'. Install with: pip install cairosvg") from e

    png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=2048, output_height=2048)
    return png_bytes


def _badge_label(source_min_dim_px: int) -> str:
    if source_min_dim_px >= 3000:
        return "excellent"
    if source_min_dim_px >= 1800:
        return "ready"
    return "optimized"


def _downscale_for_rembg(img: Image.Image, max_dim: int = REMBG_MAX_DIM) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    m = max(w, h)
    if m <= max_dim:
        return img
    scale = max_dim / float(m)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _rembg_to_pil(img: Image.Image) -> Image.Image:
    # IMPORTANT: reuse global session to avoid re-loading model
    out = remove(img, session=REMBG_SESSION)
    if isinstance(out, (bytes, bytearray)):
        return Image.open(BytesIO(out)).convert("RGBA")
    return out.convert("RGBA")


def _trim_transparent_padding(img: Image.Image, alpha_threshold: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    a = img.split()[-1]
    bbox = a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    if not bbox:
        return img
    return img.crop(bbox)


def _normalize_logo(img: Image.Image, pad_ratio: float = 0.06) -> Image.Image:
    """
    Output: 3000x3000 RGBA
    - aggressively trims transparent padding
    - scales logo to fit inside canvas with a consistent margin
    - centers it
    """
    img = img.convert("RGBA")
    img = _trim_transparent_padding(img)

    canvas = Image.new("RGBA", (TARGET_PX, TARGET_PX), (0, 0, 0, 0))

    w, h = img.size
    if w <= 0 or h <= 0:
        return canvas

    max_dim = int(TARGET_PX * (1.0 - pad_ratio * 2.0))
    scale = min(max_dim / w, max_dim / h)

    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))

    img2 = img.resize((new_w, new_h), Image.LANCZOS)

    x = (TARGET_PX - new_w) // 2
    y = (TARGET_PX - new_h) // 2
    canvas.alpha_composite(img2, (x, y))
    return canvas


def _apply_circle_alpha(
    img: Image.Image,
    x: int,
    y: int,
    radius: int,
    make_transparent: bool,
    restore_from: Optional[Image.Image] = None,
) -> Image.Image:
    img = img.copy()

    if make_transparent:
        r, g, b, a = img.split()
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
        a = Image.composite(Image.new("L", img.size, 0), a, mask)
        return Image.merge("RGBA", (r, g, b, a))

    if restore_from is None:
        return img

    restore_from = restore_from.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return Image.composite(restore_from, img, mask)


def _scale_ui_coords_to_image(img: Image.Image, x_ui: int, y_ui: int, r_ui: int) -> Tuple[int, int, int]:
    """
    UI uses a 1000x1000 canvas; actual stored image is 3000x3000.
    Convert UI coordinates -> image coordinates so brush works correctly.
    """
    w, h = img.size
    sx = w / float(UI_CANVAS_PX)
    sy = h / float(UI_CANVAS_PX)

    x = int(round(x_ui * sx))
    y = int(round(y_ui * sy))

    # scale radius by average scale
    s = (sx + sy) / 2.0
    r = max(1, int(round(r_ui * s)))

    # clamp
    x = max(0, min(w - 1, x))
    y = max(0, min(h - 1, y))
    return x, y, r


# ----------------------------
# Basic endpoints
# ----------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": REMBG_MODEL, "target_px": TARGET_PX}


# ----------------------------
# Upload pipeline
# ----------------------------
@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    keep_original: bool = Query(False),
):
    """
    Upload PNG/JPG/WEBP/SVG
    - Guard: blocks extremely large uploads (or you can raise MAX_UPLOAD_MB)
    - If keep_original=False: bg remove (rembg u2netp)
    - Always: trim transparent padding + normalize to 3000x3000
    - Saves: orig/base/curr
    """
    _cleanup_old_uploads(DEFAULT_TTL_SECONDS)

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": "File too large", "detail": f"Max upload is {MAX_UPLOAD_MB}MB. Please choose a smaller file."},
            status_code=413
        )

    filename = (file.filename or "").lower()

    # Load image (support SVG -> PNG if cairosvg installed)
    try:
        if filename.endswith(".svg") or (file.content_type == "image/svg+xml"):
            png_bytes = _svg_bytes_to_png_bytes(data)
            img = Image.open(BytesIO(png_bytes)).convert("RGBA")
        else:
            img = Image.open(BytesIO(data)).convert("RGBA")
    except Exception as e:
        return JSONResponse({"error": "Could not read image", "detail": str(e)}, status_code=400)

    orig_w, orig_h = img.size
    min_dim = min(orig_w, orig_h)

    session_id = str(uuid.uuid4())
    p = _paths(session_id)

    # Save original (as uploaded)
    _save_png(img, p["orig"])

    try:
        if keep_original:
            base = img
        else:
            img_for_rembg = _downscale_for_rembg(img, max_dim=REMBG_MAX_DIM)
            base = _rembg_to_pil(img_for_rembg)

        base = _normalize_logo(base)
    except Exception as e:
        _delete_session_files(session_id)
        return JSONResponse({"error": "Failed to process image", "detail": str(e)}, status_code=500)

    curr = base.copy()
    _save_png(base, p["base"])
    _save_png(curr, p["curr"])

    badge = _badge_label(min_dim)

    return {
        "status": "ok",
        "session_id": session_id,
        "preview_url": f"/preview/{session_id}",
        "base_url": f"/base/{session_id}",
        "original_url": f"/original/{session_id}",
        "meta_url": f"/meta/{session_id}",
        "finalize_url": f"/finalize/{session_id}",
        "badge": badge,
        "meta": {
            "source_width": orig_w,
            "source_height": orig_h,
            "target_px": TARGET_PX,
            "rembg_model": REMBG_MODEL,
        },
    }


@app.get("/meta/{session_id}")
def get_meta(session_id: str):
    p = _paths(session_id)
    if not os.path.exists(p["orig"]) or not os.path.exists(p["curr"]):
        return JSONResponse({"error": "Not found"}, status_code=404)

    orig = _load_rgba(p["orig"])
    ow, oh = orig.size
    badge = _badge_label(min(ow, oh))

    return {
        "session_id": session_id,
        "badge": badge,
        "source_width": ow,
        "source_height": oh,
        "target_px": TARGET_PX,
    }


@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    p = _paths(session_id)
    if not os.path.exists(p["curr"]):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=open(p["curr"], "rb").read(), media_type="image/png")


@app.get("/base/{session_id}")
def get_base(session_id: str):
    p = _paths(session_id)
    if not os.path.exists(p["base"]):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=open(p["base"], "rb").read(), media_type="image/png")


@app.get("/original/{session_id}")
def get_original(session_id: str):
    p = _paths(session_id)
    if not os.path.exists(p["orig"]):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=open(p["orig"], "rb").read(), media_type="image/png")


# ----------------------------
# Tap tools (brush)
# ----------------------------
@app.post("/tap/remove")
def tap_remove(session_id: str, x: int, y: int, radius: int = 24):
    p = _paths(session_id)
    if not os.path.exists(p["curr"]):
        return JSONResponse({"error": "Not found"}, status_code=404)

    curr = _load_rgba(p["curr"])
    x2, y2, r2 = _scale_ui_coords_to_image(curr, x, y, radius)

    curr2 = _apply_circle_alpha(curr, x2, y2, r2, make_transparent=True)
    _save_png(curr2, p["curr"])
    return {"status": "ok"}


@app.post("/tap/restore")
def tap_restore(session_id: str, x: int, y: int, radius: int = 24):
    p = _paths(session_id)
    if not os.path.exists(p["curr"]) or not os.path.exists(p["base"]):
        return JSONResponse({"error": "Not found"}, status_code=404)

    curr = _load_rgba(p["curr"])
    base = _load_rgba(p["base"])

    x2, y2, r2 = _scale_ui_coords_to_image(curr, x, y, radius)

    curr2 = _apply_circle_alpha(curr, x2, y2, r2, make_transparent=False, restore_from=base)
    _save_png(curr2, p["curr"])
    return {"status": "ok"}


@app.post("/reset/{session_id}")
def reset_session(session_id: str):
    p = _paths(session_id)
    if not os.path.exists(p["base"]):
        return JSONResponse({"error": "Not found"}, status_code=404)

    base = _load_rgba(p["base"])
    _save_png(base, p["curr"])
    return {"status": "ok"}


@app.post("/finalize/{session_id}")
def finalize(session_id: str):
    """
    Returns final PNG bytes and deletes local session files.
    Parent Shopify form fetches this blob and injects into <input type="file">.
    """
    p = _paths(session_id)
    if not os.path.exists(p["curr"]):
        return JSONResponse({"error": "Not found"}, status_code=404)

    img_bytes = open(p["curr"], "rb").read()
    _delete_session_files(session_id)
    return Response(content=img_bytes, media_type="image/png")


# ----------------------------
# Storefront request endpoint (receives FormData)
# NOTE: Shopify provisioning hooks will go here next.
# For now, it validates + normalizes images (again) and returns OK.
# ----------------------------
@app.post("/api/storefront-request")
async def storefront_request(
    customer_id: str = Form(...),
    customer_email: str = Form(...),
    org_type: str = Form(...),
    user_count: str = Form(...),
    duration: str = Form(...),
    storefront_name: str = Form(...),
    storefront_handle: str = Form(...),
    military_branch: Optional[str] = Form(None),
    sport_type: Optional[str] = Form(None),
    storefront_logo_file: UploadFile = File(...),
    storefront_logo_secondary: Optional[UploadFile] = File(None),
):
    """
    Receives:
      - fields
      - primary logo file (required)
      - secondary logo file (optional)

    This endpoint is where you will:
      1) upload to Shopify Files
      2) upsert custom_shop metaobject
      3) create/update smart collection (template private-store)
      4) tag customer storefront-admin--{handle}
      5) trigger studio-automation build

    Right now: it validates + normalizes images and returns OK to prove the pipeline is stable.
    """

    # Guard handle
    if not storefront_handle or len(storefront_handle) < 3:
        return JSONResponse({"error": "Invalid handle"}, status_code=400)

    # Read + normalize primary
    pbytes = await storefront_logo_file.read()
    if len(pbytes) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "Primary logo too large", "detail": f"Max upload {MAX_UPLOAD_MB}MB"}, status_code=413)

    try:
        pimg = Image.open(BytesIO(pbytes)).convert("RGBA")
    except Exception:
        return JSONResponse({"error": "Primary logo unreadable"}, status_code=400)

    # aggressive trim + normalize to 3000x3000
    pimg_norm = _normalize_logo(pimg)

    # optional secondary
    simg_norm = None
    if storefront_logo_secondary is not None:
        sbytes = await storefront_logo_secondary.read()
        if len(sbytes) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "Secondary logo too large", "detail": f"Max upload {MAX_UPLOAD_MB}MB"}, status_code=413)
        try:
            simg = Image.open(BytesIO(sbytes)).convert("RGBA")
            simg_norm = _normalize_logo(simg)
        except Exception:
            return JSONResponse({"error": "Secondary logo unreadable"}, status_code=400)

    # TODO (next): Shopify Files upload + metaobject + collection + tags + trigger studio-automation
    # For now return a stable success response:
    return {
        "status": "ok",
        "storefront_handle": storefront_handle,
        "storefront_name": storefront_name,
        "customer_email": customer_email,
        "normalized_px": TARGET_PX,
        "notes": "Pipeline OK. Next: Shopify provisioning inside this endpoint.",
        "has_secondary": simg_norm is not None,
    }


# ----------------------------
# Premium UI (/ui)
# embed=1 -> tight layout
# return=postmessage -> Done posts message to parent (NO download)
# slot=main|secondary -> tells parent which file input to fill
# ----------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui(
    request: Request,
    embed: int = Query(0),
    return_mode: str = Query("download", alias="return"),
    slot: str = Query("main"),
):
    # Note: we keep the HTML inline for easy Railway deploy
    html = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Studio Uploader</title>
  <style>
    :root {
      --bg: #0b0c10;
      --card: rgba(255,255,255,0.06);
      --stroke: rgba(255,255,255,0.14);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.66);
      --muted2: rgba(255,255,255,0.52);
      --radius: 18px;
      --shadow: 0 24px 80px rgba(0,0,0,0.55);
      --accent: rgba(52,199,89,1);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background:
        radial-gradient(1200px 700px at 10% 10%, rgba(88,101,242,0.18), transparent 60%),
        radial-gradient(900px 600px at 90% 30%, rgba(52,199,89,0.14), transparent 55%),
        radial-gradient(900px 600px at 40% 90%, rgba(255,204,0,0.10), transparent 60%),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 14px;
    }
    .modal {
      width: min(1080px, 100%);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.07), rgba(255,255,255,0.04));
      border: 1px solid var(--stroke);
      box-shadow: var(--shadow);
      overflow: hidden;
      backdrop-filter: blur(14px);
    }
    .top {
      display:flex; align-items:center; justify-content:space-between;
      padding: 12px 14px;
      border-bottom: 1px solid rgba(255,255,255,0.10);
      background: rgba(0,0,0,0.18);
    }
    .brand { display:flex; align-items:center; gap:10px; font-weight:900; }
    .dot {
      width: 10px; height: 10px; border-radius: 99px;
      background: linear-gradient(180deg, #ffffff, rgba(255,255,255,0.3));
      opacity: .9;
    }
    .actions { display:flex; gap:10px; }
    .btn {
      appearance:none;
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(255,255,255,0.06);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 12px;
      cursor:pointer;
      font-weight:900;
      transition: transform .08s ease, background .2s ease, border-color .2s ease;
      user-select:none;
    }
    .btn:hover { background: rgba(255,255,255,0.10); border-color: rgba(255,255,255,0.22); }
    .btn:active { transform: scale(0.98); }
    .btn.primary {
      background: rgba(52,199,89,0.20);
      border-color: rgba(52,199,89,0.38);
    }
    .btn.primary:hover {
      background: rgba(52,199,89,0.28);
      border-color: rgba(52,199,89,0.48);
    }
    .content {
      display:grid;
      grid-template-columns: 1.5fr 1fr;
      gap: 12px;
      padding: 12px;
    }
    @media (max-width: 980px) { .content { grid-template-columns: 1fr; } }
    .panel {
      border-radius: var(--radius);
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.10);
      overflow:hidden;
    }
    .panelHeader {
      padding: 12px 14px;
      display:flex; align-items:center; justify-content:space-between;
      border-bottom: 1px solid rgba(255,255,255,0.10);
      background: rgba(0,0,0,0.14);
    }
    .title { font-weight:900; font-size: 14px; }
    .subtitle { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .badge {
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(255,255,255,0.06);
      font-weight:900;
      font-size: 12px;
      letter-spacing: 0.6px;
      display:flex; align-items:center; gap:8px;
    }
    .badge.excellent { border-color: rgba(52,199,89,0.50); background: rgba(52,199,89,0.16); }
    .badge.ready { border-color: rgba(52,199,89,0.35); background: rgba(52,199,89,0.10); }
    .badge.optimized { border-color: rgba(88,101,242,0.40); background: rgba(88,101,242,0.14); }
    .canvasWrap {
      position: relative;
      aspect-ratio: 1/1;
      width:100%;
      background: linear-gradient(180deg, rgba(255,255,255,0.06), rgba(255,255,255,0.03));
      display:flex; align-items:center; justify-content:center;
    }
    .checker {
      position:absolute; inset:0;
      background:
        linear-gradient(45deg, rgba(255,255,255,0.06) 25%, transparent 25%),
        linear-gradient(-45deg, rgba(255,255,255,0.06) 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, rgba(255,255,255,0.06) 75%),
        linear-gradient(-45deg, transparent 75%, rgba(255,255,255,0.06) 75%);
      background-size: 26px 26px;
      background-position: 0 0, 0 13px, 13px -13px, -13px 0px;
      opacity: .35;
    }
    canvas {
      width: min(720px, 100%);
      height:auto;
      border-radius: 18px;
      z-index: 2;
      touch-action: none; /* critical for mobile brush */
    }
    .right { display:flex; flex-direction:column; gap: 12px; }
    .card {
      padding: 14px;
      border-radius: var(--radius);
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.10);
    }
    .card h3 { margin:0; font-size: 14px; font-weight: 900; }
    .muted { color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 6px; }
    input[type="file"] {
      width: 100%;
      padding: 12px;
      border-radius: 14px;
      border: 1px dashed rgba(255,255,255,0.18);
      background: rgba(0,0,0,0.16);
      color: var(--muted);
      margin-top: 10px;
    }
    .toolsRow { display:flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
    .pill {
      padding: 10px 12px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.06);
      cursor: pointer;
      font-weight: 900;
      font-size: 12px;
      color: var(--text);
      user-select:none;
    }
    .pill.active { border-color: rgba(88,101,242,0.45); background: rgba(88,101,242,0.18); }
    .sliderRow { display:flex; align-items:center; gap: 12px; margin-top: 10px; }
    .tiny { font-size: 12px; color: var(--muted2); }
    input[type="range"] { width: 100%; }
    .toggleRow {
      display:flex; align-items:center; justify-content:space-between;
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.10);
      background: rgba(0,0,0,0.12);
    }
    .switch {
      width: 44px; height: 26px; border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(255,255,255,0.08);
      position: relative;
      cursor:pointer;
    }
    .knob {
      width: 22px; height: 22px; border-radius: 999px;
      background: rgba(255,255,255,0.92);
      position:absolute; top: 1px; left: 1px;
      transition: left .16s ease;
    }
    .switch.on { background: rgba(52,199,89,0.22); border-color: rgba(52,199,89,0.38); }
    .switch.on .knob { left: 20px; }
    .footer {
      padding: 10px 14px;
      display:flex; justify-content:space-between; align-items:center;
      border-top: 1px solid rgba(255,255,255,0.10);
      background: rgba(0,0,0,0.14);
    }
    .toast {
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(0,0,0,0.55);
      border: 1px solid rgba(255,255,255,0.12);
      color: rgba(255,255,255,0.88);
      font-weight: 900;
      display:none;
      z-index: 50;
      backdrop-filter: blur(10px);
    }
  </style>
</head>
<body>
  <div class="modal" role="dialog" aria-modal="true">
    <div class="top">
      <div class="brand"><span class="dot"></span> Studio Uploader</div>
      <div class="actions">
        <button class="btn" id="btnReset" title="Reset edits">Reset</button>
        <button class="btn primary" id="btnDone" title="Save and close">Done</button>
      </div>
    </div>

    <div class="content">
      <div class="panel">
        <div class="panelHeader">
          <div>
            <div class="title">Prepare Your Logo</div>
            <div class="subtitle">Upload • Quick touch-ups • Done</div>
          </div>
          <div id="badge" class="badge optimized">Optimized ✓</div>
        </div>
        <div class="canvasWrap">
          <div class="checker"></div>
          <canvas id="cv" width="1000" height="1000"></canvas>
        </div>
      </div>

      <div class="right">
        <div class="card">
          <h3>Upload</h3>
          <div class="muted">Upload an image. We’ll clean it up and get it ready for print.</div>

          <input id="file" type="file" accept="image/*,.svg" />
          <label class="muted" style="display:block;margin-top:8px;">
            <input type="checkbox" id="keepOriginal" /> Keep original
          </label>

          <div class="toolsRow">
            <div class="pill active" id="modeErase">Erase</div>
            <div class="pill" id="modeRestore">Restore</div>
          </div>

          <div class="sliderRow">
            <div class="tiny" style="min-width:72px;">Brush</div>
            <input id="radius" type="range" min="8" max="80" value="28" />
            <div class="tiny" id="radiusVal" style="min-width:36px;text-align:right;">28</div>
          </div>

          <div class="toggleRow">
            <div>
              <div style="font-weight:900;">Highlight edits</div>
              <div class="tiny">Helps you see what changed</div>
            </div>
            <div id="ghostSwitch" class="switch on" role="switch" aria-checked="true"><div class="knob"></div></div>
          </div>
        </div>

        <div class="card">
          <h3>Status</h3>
          <div class="muted" id="statusLine">Upload a logo to begin.</div>
        </div>
      </div>
    </div>

    <div class="footer">
      <div class="tiny">Simple • Fast • Done</div>
      <div class="tiny">No downloads in embed mode</div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

<script>
(() => {
  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d');

  const fileEl = document.getElementById('file');
  const keepOriginalEl = document.getElementById('keepOriginal');

  const badgeEl = document.getElementById('badge');
  const statusLine = document.getElementById('statusLine');

  const radiusEl = document.getElementById('radius');
  const radiusVal = document.getElementById('radiusVal');

  const btnDone = document.getElementById('btnDone');
  const btnReset = document.getElementById('btnReset');

  const modeErase = document.getElementById('modeErase');
  const modeRestore = document.getElementById('modeRestore');

  const ghostSwitch = document.getElementById('ghostSwitch');
  let highlightEdits = true;

  const toast = document.getElementById('toast');

  const params = new URLSearchParams(window.location.search);
  const RETURN_MODE = (params.get('return') || 'download').toLowerCase(); // postmessage | download
  const SLOT = (params.get('slot') || 'main').toLowerCase();

  let sessionId = null;
  let mode = 'remove';
  let isDown = false;

  function showToast(msg) {
    toast.textContent = msg;
    toast.style.display = 'block';
    setTimeout(() => toast.style.display = 'none', 1400);
  }

  function setBadge(kind) {
    const label = kind === 'excellent' ? 'Excellent ✓' : (kind === 'ready' ? 'Ready ✓' : 'Optimized ✓');
    badgeEl.className = `badge ${kind}`;
    badgeEl.textContent = label;
  }

  function setMode(next) {
    mode = next;
    modeErase.classList.toggle('active', mode === 'remove');
    modeRestore.classList.toggle('active', mode === 'restore');
  }

  modeErase.addEventListener('click', () => setMode('remove'));
  modeRestore.addEventListener('click', () => setMode('restore'));

  radiusEl.addEventListener('input', () => { radiusVal.textContent = radiusEl.value; });

  ghostSwitch.addEventListener('click', async () => {
    highlightEdits = !highlightEdits;
    ghostSwitch.classList.toggle('on', highlightEdits);
    ghostSwitch.setAttribute('aria-checked', highlightEdits ? 'true' : 'false');
    await drawComposite();
  });

  async function drawComposite() {
    ctx.clearRect(0,0,cv.width,cv.height);

    if (!sessionId) {
      ctx.save();
      ctx.fillStyle = 'rgba(255,255,255,0.16)';
      ctx.font = '900 26px system-ui';
      ctx.fillText('Upload a logo to begin', 40, 80);
      ctx.restore();
      return;
    }

    const baseImg = new Image();
    const currImg = new Image();

    const baseSrc = `/base/${sessionId}?t=${Date.now()}`;
    const currSrc = `/preview/${sessionId}?t=${Date.now()}`;

    await Promise.all([
      new Promise((res, rej) => { baseImg.onload = res; baseImg.onerror = rej; baseImg.src = baseSrc; }),
      new Promise((res, rej) => { currImg.onload = res; currImg.onerror = rej; currImg.src = currSrc; }),
    ]);

    if (highlightEdits) {
      ctx.save();
      ctx.globalAlpha = 0.28;
      ctx.drawImage(baseImg, 0, 0, cv.width, cv.height);
      ctx.restore();
    }

    ctx.drawImage(currImg, 0, 0, cv.width, cv.height);
  }

  async function loadMetaAndUpdateUI() {
    if (!sessionId) return;
    const r = await fetch(`/meta/${sessionId}?t=${Date.now()}`);
    if (!r.ok) return;
    const m = await r.json();
    setBadge(m.badge || 'optimized');
    statusLine.textContent = 'Ready';
  }

  async function upload(file) {
    statusLine.textContent = 'Preparing…';
    showToast('Preparing…');

    const fd = new FormData();
    fd.append('file', file);

    const keep = keepOriginalEl.checked ? 'true' : 'false';
    const r = await fetch(`/upload?keep_original=${keep}`, { method: 'POST', body: fd });

    if (r.status === 413) {
      const j = await r.json().catch(() => ({}));
      statusLine.textContent = j.detail || 'File too large. Try a smaller image.';
      showToast('Too large');
      return;
    }

    const j = await r.json().catch(() => ({}));

    if (!r.ok) {
      statusLine.textContent = 'Something went wrong. Try again.';
      showToast('Upload failed');
      return;
    }

    sessionId = j.session_id;
    await loadMetaAndUpdateUI();
    await drawComposite();
    showToast('Ready');
  }

  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    await upload(f);
  });

  function canvasToImageCoords(evt) {
    const rect = cv.getBoundingClientRect();
    const cx = (evt.clientX - rect.left);
    const cy = (evt.clientY - rect.top);
    const x = Math.round(cx * (cv.width / rect.width));
    const y = Math.round(cy * (cv.height / rect.height));
    return { x, y };
  }

  async function applyTap(x, y) {
    if (!sessionId) return;
    const radius = parseInt(radiusEl.value, 10);
    const endpoint = mode === 'remove' ? '/tap/remove' : '/tap/restore';
    const url = `${endpoint}?session_id=${encodeURIComponent(sessionId)}&x=${x}&y=${y}&radius=${radius}`;
    const r = await fetch(url, { method: 'POST' });
    if (!r.ok) { showToast('Edit failed'); return; }
    await drawComposite();
  }

  // Mouse
  cv.addEventListener('mousedown', async (e) => {
    isDown = true;
    const {x,y} = canvasToImageCoords(e);
    await applyTap(x,y);
  });
  cv.addEventListener('mousemove', async (e) => {
    if (!isDown) return;
    const {x,y} = canvasToImageCoords(e);
    await applyTap(x,y);
  });
  window.addEventListener('mouseup', () => isDown = false);

  // Touch
  cv.addEventListener('touchstart', async (e) => {
    isDown = true;
    const t = e.touches[0];
    const {x,y} = canvasToImageCoords(t);
    await applyTap(x,y);
    e.preventDefault();
  }, { passive: false });

  cv.addEventListener('touchmove', async (e) => {
    if (!isDown) return;
    const t = e.touches[0];
    const {x,y} = canvasToImageCoords(t);
    await applyTap(x,y);
    e.preventDefault();
  }, { passive: false });

  window.addEventListener('touchend', () => isDown = false);

  btnReset.addEventListener('click', async () => {
    if (!sessionId) return;
    const r = await fetch(`/reset/${sessionId}`, { method: 'POST' });
    if (!r.ok) { showToast('Reset failed'); return; }
    await drawComposite();
    showToast('Reset');
  });

  btnDone.addEventListener('click', async () => {
    if (!sessionId) { showToast('No file'); return; }

    // EMBED MODE: do NOT download. Tell parent to fetch finalize_url and inject blob.
    if (RETURN_MODE === 'postmessage') {
      const finalizeUrl = `${window.location.origin}/finalize/${sessionId}`;
      window.parent.postMessage({
        type: "studio-uploader:done",
        slot: (SLOT === "secondary" ? "secondary" : "main"),
        session_id: sessionId,
        finalize_url: finalizeUrl,
        filename: "logo_ready.png",
        mime: "image/png"
      }, "*");

      showToast('Sent ✓');
      // Parent will call finalize; we can reset our UI immediately
      sessionId = null;
      statusLine.textContent = 'Upload a logo to begin.';
      setBadge('optimized');
      await drawComposite();
      return;
    }

    // fallback (non-embed) download mode for local testing
    statusLine.textContent = 'Saving…';
    const r = await fetch(`/finalize/${sessionId}`, { method: 'POST' });
    if (!r.ok) {
      statusLine.textContent = 'Something went wrong. Try again.';
      showToast('Save failed');
      return;
    }

    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `logo_ready.png`;
    a.click();

    showToast('Logo ready ✓');

    sessionId = null;
    statusLine.textContent = 'Upload a logo to begin.';
    setBadge('optimized');
    await drawComposite();
  });

  drawComposite();
})();
</script>
</body>
</html>
"""
    return HTMLResponse(html)