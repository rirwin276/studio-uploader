# app.py — Studio Uploader (FastAPI) — vNext FULL REPLACEMENT (END-TO-END + FASTER UI)
# Goals of this replacement:
# ✅ FIX end-to-end provisioning (uses --collection_handle, not --handle)
# ✅ Faster perceived load (no “blank minute”): upload returns immediately, UI polls until ready
# ✅ Better editor visibility (higher-contrast checkerboard + “erase tint” overlay toggle)
# ✅ Restore no longer brings back white background (restore uses BG-REMOVED baseline, not raw upload)
# ✅ Faster saves (PNG save optimized for speed, not max compression)
# ✅ Safer + memory guarded reads (keeps your OOM protection)

import os
import uuid
import json
import time
import threading
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, Any

from PIL import Image, ImageOps

from fastapi import FastAPI, UploadFile, File, Query, Request, Form
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware


# ----------------------------
# App
# ----------------------------
app = FastAPI(title="Studio Uploader", version="1.4.0")


# ----------------------------
# CORS (Railway / Shopify)
# ----------------------------
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").strip()
allow_origins_list = ["*"] if ALLOW_ORIGINS == "*" else [o.strip() for o in ALLOW_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------
# Storage + Config
# ----------------------------
ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(ROOT / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

TARGET_PX = int(os.getenv("TARGET_PX", "3000"))          # final normalized output
EDITOR_PX = int(os.getenv("EDITOR_PX", "1200"))          # browser/editor working size (lower = faster)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Guard against very large images (pixel bombs)
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(40_000_000)))  # 40MP default

# rembg model + max dim
REMBG_MODEL = os.getenv("REMBG_MODEL", "u2netp")
REMBG_MAX_DIM = int(os.getenv("REMBG_MAX_DIM", "1400"))  # smaller = faster, still good quality

# edge refine
EDGE_REFINE = os.getenv("EDGE_REFINE", "1").strip() not in ("0", "false", "False", "")
EDGE_SHRINK_PX = int(os.getenv("EDGE_SHRINK_PX", "2"))
EDGE_FEATHER_PX = int(os.getenv("EDGE_FEATHER_PX", "2"))

# provisioning script path
PROVISION_SCRIPT = Path(os.getenv("PROVISION_SCRIPT", str(ROOT / "shopify_provision.py")))

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))


# ----------------------------
# Job tracking (simple in-memory)
# ----------------------------
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

def _job_set(job_id: str, **kwargs):
    with _JOBS_LOCK:
        j = _JOBS.get(job_id, {})
        j.update(kwargs)
        _JOBS[job_id] = j

def _job_get(job_id: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        return dict(_JOBS.get(job_id, {}))


# ----------------------------
# Editor session tracking (in-memory)
# ----------------------------
_SESS: Dict[str, Dict[str, Any]] = {}
_SESS_LOCK = threading.Lock()

def _sess_set(session_id: str, **kwargs):
    with _SESS_LOCK:
        s = _SESS.get(session_id, {})
        s.update(kwargs)
        _SESS[session_id] = s

def _sess_get(session_id: str) -> Dict[str, Any]:
    with _SESS_LOCK:
        return dict(_SESS.get(session_id, {}))


# ----------------------------
# rembg session (lazy load)
# ----------------------------
_SESSION = None

def get_rembg_session():
    global _SESSION
    if _SESSION is None:
        print("🧠 Loading rembg model into memory:", REMBG_MODEL)
        from rembg import new_session
        _SESSION = new_session(REMBG_MODEL)
    return _SESSION


# ----------------------------
# Helpers: file read (stream + cap)
# ----------------------------
async def _read_upload_limited(file: UploadFile, limit_bytes: int) -> bytes:
    """
    Reads an UploadFile in chunks and hard-stops at limit_bytes.
    Prevents memory spikes / OOM from large unexpected uploads.
    """
    buf = bytearray()
    chunk_size = 1024 * 1024  # 1MB
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit_bytes:
            raise ValueError("too_large")
    return bytes(buf)


# ----------------------------
# Helpers: image IO + scaling
# ----------------------------
def _paths(session_id: str) -> Dict[str, Path]:
    # raw = uploaded image (as placed into square editor canvas)
    # base = baseline for RESTORE (bg removed version so restore won't re-add white background)
    # curr = current editable image
    return {
        "raw": UPLOAD_DIR / f"{session_id}_raw.png",
        "base": UPLOAD_DIR / f"{session_id}_base.png",
        "curr": UPLOAD_DIR / f"{session_id}_curr.png",
    }

def _save_png(img: Image.Image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Speed-first PNG write (optimize=True can be slow and make UI feel “stuck”)
    img.save(str(path), "PNG", optimize=False, compress_level=6)

def _scale_to_fit(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if w <= max_dim and h <= max_dim:
        return img
    scale = min(max_dim / w, max_dim / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

def _downscale_for_rembg(img: Image.Image, max_dim: int = REMBG_MAX_DIM) -> Image.Image:
    return _scale_to_fit(img, max_dim)

def _pil_open_safe(data: bytes) -> Image.Image:
    """
    Safe PIL open:
    - Blocks pixel bombs
    - Applies EXIF transpose (fixes iPhone rotation)
    """
    img = Image.open(BytesIO(data))
    w, h = img.size
    if (w * h) > MAX_IMAGE_PIXELS:
        raise ValueError("too_many_pixels")
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA")


# ----------------------------
# Better background removal
# ----------------------------
def _rembg_to_pil_better(img: Image.Image) -> Image.Image:
    """
    Higher quality than basic rembg:
    - alpha matting to reduce jagged edges
    - optional edge refine pass to reduce halos / fringe
    """
    from rembg import remove

    out = remove(
        img,
        session=get_rembg_session(),
        alpha_matting=True,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=10,
    )

    if isinstance(out, (bytes, bytearray)):
        out_img = Image.open(BytesIO(out)).convert("RGBA")
    else:
        out_img = out.convert("RGBA")

    if EDGE_REFINE:
        out_img = _refine_alpha_edges(out_img, shrink_px=EDGE_SHRINK_PX, feather_px=EDGE_FEATHER_PX)

    return out_img

def _refine_alpha_edges(img: Image.Image, shrink_px: int = 2, feather_px: int = 2) -> Image.Image:
    """
    Refines transparency edges to reduce halos/fringing.
    Uses OpenCV if available; gracefully falls back if not.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return img

    rgba = np.array(img, dtype=np.uint8)
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        return img

    a = rgba[:, :, 3]

    if shrink_px > 0:
        k = max(1, int(shrink_px))
        kernel = np.ones((k, k), np.uint8)
        a = cv2.erode(a, kernel, iterations=1)

    if feather_px > 0:
        k = max(1, int(feather_px) * 2 + 1)
        a = cv2.GaussianBlur(a, (k, k), 0)

    rgba[:, :, 3] = a.clip(0, 255).astype("uint8")
    return Image.fromarray(rgba, mode="RGBA")


# ----------------------------
# Padding trim + normalize to 3000x3000
# ----------------------------
def _trim_transparent_padding(img: Image.Image, alpha_threshold: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    a = img.split()[-1]
    bbox = a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    if not bbox:
        return img
    return img.crop(bbox)

def _normalize_logo(img: Image.Image, pad_ratio: float = 0.06, target_size: int = TARGET_PX) -> Image.Image:
    """
    Trim padding -> scale to fit within square -> center -> output exact target_size x target_size RGBA.
    """
    img = _trim_transparent_padding(img.convert("RGBA"))

    canvas = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    w, h = img.size
    if w <= 0 or h <= 0:
        return canvas

    max_dim = int(target_size * (1.0 - pad_ratio * 2.0))
    scale = min(max_dim / w, max_dim / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))

    img2 = img.resize((new_w, new_h), Image.LANCZOS)
    x = (target_size - new_w) // 2
    y = (target_size - new_h) // 2
    canvas.alpha_composite(img2, (x, y))
    return canvas


# ----------------------------
# Square-canvas placement
# ----------------------------
def _place_into_square(img: Image.Image, square: int) -> Image.Image:
    canvas = Image.new("RGBA", (square, square), (0, 0, 0, 0))
    x = (square - img.width) // 2
    y = (square - img.height) // 2
    canvas.alpha_composite(img, (x, y))
    return canvas


# ----------------------------
# Async bg-removal worker for faster UI
# ----------------------------
def _bg_remove_worker(session_id: str):
    """
    Runs rembg in background and writes:
      - base (bg-removed baseline used for RESTORE)
      - curr (starts as base)
    """
    try:
        p = _paths(session_id)
        raw_path = p["raw"]
        if not raw_path.exists():
            _sess_set(session_id, status="failed", error="raw_not_found")
            return

        _sess_set(session_id, status="processing")

        raw_img = Image.open(str(raw_path)).convert("RGBA")

        # downscale for model stability/speed
        img_for_model = _downscale_for_rembg(raw_img, max_dim=REMBG_MAX_DIM)
        removed_small = _rembg_to_pil_better(img_for_model)

        # scale back to raw canvas size so brush coords align
        removed = removed_small.resize(raw_img.size, Image.LANCZOS)

        _save_png(removed, p["base"])
        _save_png(removed, p["curr"])

        _sess_set(session_id, status="ready")
    except Exception as e:
        _sess_set(session_id, status="failed", error=str(e)[:500])


# ----------------------------
# Endpoints
# ----------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui")

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "version": app.version}


# --------
# Editor upload pipeline
# --------
@app.post("/upload")
async def upload_image(file: UploadFile = File(...), keep_original: bool = Query(False)):
    """
    Uploads an image and returns session_id immediately.
    If keep_original=false, bg removal happens in background for faster perceived UI.
    """
    # Stream read w/ hard cap
    try:
        data = await _read_upload_limited(file, MAX_UPLOAD_BYTES)
    except ValueError as e:
        if str(e) == "too_large":
            return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)
        return JSONResponse({"error": "Upload read failed"}, status_code=400)

    # Convert input to RGBA safely
    try:
        img = _pil_open_safe(data)
    except ValueError as e:
        if str(e) == "too_many_pixels":
            return JSONResponse({"error": "Image resolution too large. Please upload a smaller image."}, status_code=413)
        return JSONResponse({"error": "Unsupported image. Please upload a PNG/JPG/WebP."}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Unsupported image. Please upload a PNG/JPG/WebP."}, status_code=400)

    session_id = str(uuid.uuid4())
    p = _paths(session_id)

    # Scale uploaded image into editor working size
    img_scaled = _scale_to_fit(img, EDITOR_PX)
    raw_square = _place_into_square(img_scaled, EDITOR_PX)

    _save_png(raw_square, p["raw"])

    if keep_original:
        # baseline == raw (restore will restore raw)
        _save_png(raw_square, p["base"])
        _save_png(raw_square, p["curr"])
        _sess_set(session_id, status="ready")
    else:
        # placeholder curr/base while processing (shows something immediately)
        _save_png(raw_square, p["base"])
        _save_png(raw_square, p["curr"])
        _sess_set(session_id, status="queued")
        t = threading.Thread(target=_bg_remove_worker, args=(session_id,), daemon=True)
        t.start()

    return {
        "status": "ok",
        "session_id": session_id,
        "status_url": f"/status/{session_id}",
        "preview_url": f"/preview/{session_id}",
        "original_url": f"/original/{session_id}",  # NOTE: original = BASELINE (bg-removed) for restore safety
        "raw_url": f"/raw/{session_id}",
    }

@app.get("/status/{session_id}")
def status(session_id: str):
    s = _sess_get(session_id)
    if not s:
        # If files exist but no in-memory state (server restarted), infer readiness
        p = _paths(session_id)
        if p["curr"].exists():
            return {"status": "ready"}
        return JSONResponse({"error": "Not found"}, status_code=404)
    return s


# Images: add cache headers (we add ?t=Date.now() anyway, but this helps)
def _png_response(path: Path) -> Response:
    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )

@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    path = _paths(session_id)["curr"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _png_response(path)

@app.get("/original/{session_id}")
def get_original(session_id: str):
    # original = BASELINE used for RESTORE (bg removed)
    path = _paths(session_id)["base"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _png_response(path)

@app.get("/raw/{session_id}")
def get_raw(session_id: str):
    path = _paths(session_id)["raw"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _png_response(path)


@app.post("/save-edit/{session_id}")
async def save_edit(session_id: str, file: UploadFile = File(...)):
    """
    Receives browser canvas PNG (1000x1000) and saves normalized 3000x3000 in session_curr.
    """
    p = _paths(session_id)
    try:
        data = await _read_upload_limited(file, MAX_UPLOAD_BYTES)
    except ValueError:
        return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)

    try:
        browser_img = _pil_open_safe(data)
    except Exception:
        return JSONResponse({"error": "Bad image payload"}, status_code=400)

    final_img = _normalize_logo(browser_img, target_size=TARGET_PX)
    _save_png(final_img, p["curr"])
    return {"status": "ok"}


@app.post("/finalize/{session_id}")
def finalize(session_id: str):
    """
    Returns the final normalized PNG bytes for Shopify form consumption.
    """
    path = _paths(session_id)["curr"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ----------------------------
# Provisioning runner (subprocess)
# ----------------------------
def _run_shopify_provision_job(
    job_id: str,
    storefront_name: str,
    storefront_handle: str,
    owner_customer_id: str,
    type_of_store: Optional[str],
    main_session_id: str,
    secondary_session_id: Optional[str],
):
    """
    Runs shopify_provision.py as a subprocess.
    IMPORTANT: uses --collection_handle (matches your shopify_provision.py).
    """
    _job_set(job_id, status="running", started_at=time.time())

    if not PROVISION_SCRIPT.exists():
        _job_set(job_id, status="failed", error=f"Provision script not found: {PROVISION_SCRIPT}")
        return

    cmd = [
        "python",
        str(PROVISION_SCRIPT),
        "--name", storefront_name,
        "--collection_handle", storefront_handle,   # ✅ FIXED
        "--owner_customer_id", owner_customer_id,
        "--main_session_id", main_session_id,
        "--uploads_dir", str(UPLOAD_DIR),
    ]

    if secondary_session_id:
        cmd += ["--secondary_session_id", secondary_session_id]

    if type_of_store:
        cmd += ["--type_of_store", type_of_store]

    try:
        print("🚀 Provision cmd:", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=900,  # 15 min max
        )

        stdout = (proc.stdout or "")[-12000:]
        stderr = (proc.stderr or "")[-12000:]

        if proc.returncode != 0:
            _job_set(
                job_id,
                status="failed",
                finished_at=time.time(),
                error=f"Provision failed (exit {proc.returncode})",
                stdout=stdout,
                stderr=stderr,
            )
            return

        _job_set(
            job_id,
            status="succeeded",
            finished_at=time.time(),
            stdout=stdout,
            stderr=stderr,
        )

    except subprocess.TimeoutExpired:
        _job_set(job_id, status="failed", finished_at=time.time(), error="Provision timed out")
    except Exception as e:
        _job_set(job_id, status="failed", finished_at=time.time(), error=str(e))


# ----------------------------
# Shopify Provisioning Endpoint (called by your Shopify form)
# ----------------------------
@app.post("/api/storefront-request")
async def storefront_request(
    customer_id: str = Form(...),
    customer_email: str = Form(...),
    storefront_name: str = Form(...),
    storefront_handle: str = Form(...),
    org_type: str = Form(None),
    user_count: str = Form(None),
    duration: str = Form(None),
    military_branch: str = Form(None),
    sport_type: str = Form(None),
    storefront_logo_file: UploadFile = File(...),
    storefront_logo_secondary: Optional[UploadFile] = File(None),
):
    """
    - Produces normalized main/secondary logos in uploads/ as session_curr.png files.
    - Launches shopify_provision.py in a background thread and returns a job_id.
    """
    if not storefront_name.strip():
        return JSONResponse({"error": "storefront_name is required"}, status_code=400)
    if not storefront_handle.strip():
        return JSONResponse({"error": "storefront_handle is required"}, status_code=400)
    if not customer_id.strip():
        return JSONResponse({"error": "customer_id is required"}, status_code=400)

    owner_customer_id = customer_id.split("/")[-1].strip()

    # Read main logo
    try:
        main_bytes = await _read_upload_limited(storefront_logo_file, MAX_UPLOAD_BYTES)
    except ValueError:
        return JSONResponse({"error": f"Main logo too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)
    if not main_bytes:
        return JSONResponse({"error": "storefront_logo_file is required"}, status_code=400)

    sec_bytes = None
    if storefront_logo_secondary:
        try:
            sec_bytes = await _read_upload_limited(storefront_logo_secondary, MAX_UPLOAD_BYTES)
        except ValueError:
            return JSONResponse({"error": f"Secondary logo too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)

    # Process main logo -> session files
    main_session_id = str(uuid.uuid4())
    main_paths = _paths(main_session_id)

    try:
        img_main = _pil_open_safe(main_bytes)
    except ValueError as e:
        if str(e) == "too_many_pixels":
            return JSONResponse({"error": "Main logo resolution too large. Please upload a smaller image."}, status_code=413)
        return JSONResponse({"error": "Main logo is not a supported image."}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Main logo is not a supported image."}, status_code=400)

    img_main_scaled = _scale_to_fit(img_main, EDITOR_PX)
    img_main_removed = _rembg_to_pil_better(_downscale_for_rembg(img_main_scaled, REMBG_MAX_DIM))
    main_final = _normalize_logo(img_main_removed, target_size=TARGET_PX)

    # Save raw/base/curr (for consistency)
    _save_png(_place_into_square(img_main_scaled, EDITOR_PX), main_paths["raw"])
    _save_png(_place_into_square(img_main_removed.resize(img_main_scaled.size, Image.LANCZOS), EDITOR_PX), main_paths["base"])
    _save_png(main_final, main_paths["curr"])

    # Secondary
    secondary_session_id = None
    if sec_bytes:
        secondary_session_id = str(uuid.uuid4())
        sec_paths = _paths(secondary_session_id)

        try:
            img_sec = _pil_open_safe(sec_bytes)
            img_sec_scaled = _scale_to_fit(img_sec, EDITOR_PX)
            img_sec_removed = _rembg_to_pil_better(_downscale_for_rembg(img_sec_scaled, REMBG_MAX_DIM))
            sec_final = _normalize_logo(img_sec_removed, target_size=TARGET_PX)

            _save_png(_place_into_square(img_sec_scaled, EDITOR_PX), sec_paths["raw"])
            _save_png(_place_into_square(img_sec_removed.resize(img_sec_scaled.size, Image.LANCZOS), EDITOR_PX), sec_paths["base"])
            _save_png(sec_final, sec_paths["curr"])
        except ValueError as e:
            if str(e) == "too_many_pixels":
                return JSONResponse({"error": "Secondary logo resolution too large. Please upload a smaller image."}, status_code=413)
            return JSONResponse({"error": "Secondary logo is not a supported image."}, status_code=400)
        except Exception:
            return JSONResponse({"error": "Secondary logo is not a supported image."}, status_code=400)

    # Launch provisioning job
    job_id = str(uuid.uuid4())
    _job_set(
        job_id,
        status="queued",
        storefront_name=storefront_name,
        storefront_handle=storefront_handle,
        owner_customer_id=owner_customer_id,
        main_session_id=main_session_id,
        secondary_session_id=secondary_session_id,
        customer_email=customer_email,
    )

    type_of_store = (org_type or military_branch or sport_type or "").strip() or None

    t = threading.Thread(
        target=_run_shopify_provision_job,
        args=(job_id, storefront_name, storefront_handle, owner_customer_id, type_of_store, main_session_id, secondary_session_id),
        daemon=True,
    )
    t.start()

    return {
        "status": "ok",
        "job_id": job_id,
        "message": "Provisioning started.",
        "debug": {
            "storefront_name": storefront_name,
            "storefront_handle": storefront_handle,
            "owner_customer_id": owner_customer_id,
            "main_session_id": main_session_id,
            "secondary_session_id": secondary_session_id,
            "type_of_store": type_of_store,
        },
    }


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    j = _job_get(job_id)
    if not j:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return j


# ----------------------------
# Premium HTML5 UI
# ----------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui(
    request: Request,
    embed: int = Query(0),
    return_mode: str = Query("postmessage", alias="return"),
    slot: str = Query("main"),
    return_to: str = Query("", alias="return_to"),
):
    html = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=0" />
  <title>Logo Editor</title>
  <style>
    :root {
        --bg: #0b0f19;
        --surface: #1e293b;
        --surface-hover: #334155;
        --primary: #10b981;
        --primary-hover: #059669;
        --danger: #ef4444;
        --text: #f8fafc;
        --muted: #94a3b8;
        --radius: 16px;
    }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; display: flex; flex-direction: column; min-height: 100vh; overscroll-behavior: none; }

    .header { padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; background: rgba(30, 41, 59, 0.8); backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255,255,255,0.05); z-index: 10; position: relative; }
    .header h2 { margin: 0; font-size: 18px; font-weight: 800; letter-spacing: .2px; }
    .btn-done { background: var(--primary); color: white; border: none; padding: 8px 16px; border-radius: 99px; font-weight: 700; font-size: 14px; cursor: pointer; transition: background 0.2s; }
    .btn-done:hover { background: var(--primary-hover); }
    .btn-done:disabled { opacity: 0.5; cursor: not-allowed; }

    .main-container { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; position: relative; }

    .step-upload { text-align: center; width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: 16px; }
    .upload-box { border: 2px dashed rgba(255,255,255,0.22); border-radius: var(--radius); padding: 54px 20px; background: rgba(255,255,255,0.03); cursor: pointer; transition: all 0.2s; position: relative; }
    .upload-box:hover { background: rgba(255,255,255,0.06); border-color: var(--primary); }
    .upload-box input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
    .upload-icon { font-size: 40px; margin-bottom: 10px; display: block; }
    .upload-title { font-size: 18px; font-weight: 800; margin-bottom: 6px; }
    .upload-sub { color: var(--muted); font-size: 14px; }

    .step-loading { display: none; text-align: center; flex-direction: column; align-items: center; gap: 16px; }
    .spinner { width: 50px; height: 50px; border: 4px solid rgba(255,255,255,0.1); border-top-color: var(--primary); border-radius: 50%; animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .loading-sub { color: var(--muted); font-size: 14px; max-width: 360px; line-height: 1.4; }

    .step-editor { display: none; width: 100%; max-width: 520px; flex-direction: column; gap: 16px; }
    .top-actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 2px; }
    .btn-text { background: none; border: none; color: var(--muted); font-size: 13px; cursor: pointer; font-weight: 700; }
    .btn-text:hover { color: var(--text); }

    .canvas-wrap {
        border-radius: var(--radius);
        position: relative;
        width: 100%;
        aspect-ratio: 1/1;
        overflow: hidden;
        border: 1px solid rgba(255,255,255,0.12);
        box-shadow: 0 20px 40px rgba(0,0,0,0.35);
        background: #0f172a;
    }

    /* HIGH CONTRAST CHECKERBOARD */
    .checker {
        position:absolute; inset:0;
        background-color: #0b1220;
        background-image:
          linear-gradient(45deg, rgba(255,255,255,0.08) 25%, transparent 25%),
          linear-gradient(-45deg, rgba(255,255,255,0.08) 25%, transparent 25%),
          linear-gradient(45deg, transparent 75%, rgba(255,255,255,0.08) 75%),
          linear-gradient(-45deg, transparent 75%, rgba(255,255,255,0.08) 75%);
        background-size: 22px 22px;
        background-position: 0 0, 0 11px, 11px -11px, -11px 0px;
        z-index: 1;
    }

    /* ERASE VISIBILITY OVERLAY (tint) */
    .erase-tint {
        position:absolute; inset:0;
        background: rgba(239, 68, 68, 0.10);
        mix-blend-mode: multiply;
        opacity: 0;
        transition: opacity .15s ease;
        z-index: 1;
        pointer-events:none;
    }
    .erase-tint.on { opacity: 1; }

    canvas { width: 100%; height: 100%; display: block; position: relative; z-index: 2; touch-action: none; cursor: none; }

    #cursor { position: fixed; border: 2px solid rgba(255,255,255,0.92); box-shadow: 0 0 6px rgba(0,0,0,0.85); border-radius: 50%; pointer-events: none; transform: translate(-50%, -50%); z-index: 9999; display: none; }

    .toolbar { background: rgba(30, 41, 59, 0.95); border: 1px solid rgba(255,255,255,0.12); border-radius: 999px; padding: 6px; display: flex; gap: 6px; margin: 0 auto; box-shadow: 0 10px 25px rgba(0,0,0,0.55); flex-wrap: wrap; justify-content: center; }
    .tool-btn { background: transparent; color: var(--muted); border: none; padding: 10px 14px; border-radius: 999px; cursor: pointer; font-weight: 800; font-size: 14px; display: flex; align-items: center; gap: 6px; transition: all 0.2s; }
    .tool-btn:hover { color: var(--text); background: rgba(255,255,255,0.05); }
    .tool-btn.active { background: rgba(255,255,255,0.10); color: var(--primary); }

    .brush-controls { display: flex; align-items: center; gap: 12px; background: rgba(30, 41, 59, 0.95); padding: 12px 18px; border-radius: 999px; margin: 0 auto; border: 1px solid rgba(255,255,255,0.12); width: 100%; max-width: 520px; }
    .brush-controls span { font-size: 13px; color: var(--muted); font-weight: 800; }
    input[type=range] { flex: 1; accent-color: var(--primary); }

    .pill { display:flex; gap:8px; align-items:center; justify-content:center; margin-top: 2px; }
    .toggle {
      display:flex; align-items:center; gap:10px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      padding: 10px 12px;
      border-radius: 999px;
      font-size: 13px;
      color: var(--muted);
      font-weight: 800;
      cursor: pointer;
      user-select:none;
    }
    .toggle b { color: var(--text); }
  </style>
</head>
<body>
  <div id="cursor"></div>

  <div class="header">
      <h2>Logo Studio</h2>
      <button class="btn-done" id="btnDone" style="display:none;">Save & Done</button>
  </div>

  <div class="main-container">

    <div class="step-upload" id="step1">
      <div class="upload-box">
          <input id="file" type="file" accept="image/*" />
          <span class="upload-icon">🪄</span>
          <div class="upload-title">Tap to Upload Logo</div>
          <div class="upload-sub">We’ll auto-remove the background (fast)</div>
      </div>
    </div>

    <div class="step-loading" id="step2">
      <div class="spinner"></div>
      <div style="font-weight: 800; font-size: 18px;">Processing…</div>
      <div class="loading-sub" id="loadingSub">Uploading your image…</div>
    </div>

    <div class="step-editor" id="step3">
      <div class="top-actions">
          <button class="btn-text" onclick="location.reload()">Start Over</button>
          <button class="btn-text" id="btnUndo">↩️ Undo</button>
      </div>

      <div class="canvas-wrap" id="canvas-container">
        <div class="checker"></div>
        <div class="erase-tint" id="eraseTint"></div>
        <canvas id="cv" width="1000" height="1000"></canvas>
      </div>

      <div class="toolbar">
        <button class="tool-btn active" id="btnRestore">🖌️ Restore</button>
        <button class="tool-btn" id="btnErase">🧹 Erase</button>
        <button class="tool-btn" id="btnMagic">✨ Magic</button>
      </div>

      <div class="pill">
        <div class="toggle" id="toggleTint"><b>Visibility:</b> Show erase tint</div>
      </div>

      <div class="brush-controls" id="brush-controls">
          <span>Size</span>
          <input type="range" id="brushSize" min="10" max="160" value="55">
      </div>
    </div>

  </div>

<script>
  let sessionId = null;
  let mode = 'restore';
  let isDown = false;
  let lastX = 0, lastY = 0;
  let history = [];

  const fileEl = document.getElementById('file');
  const btnDone = document.getElementById('btnDone');

  const step1 = document.getElementById('step1');
  const step2 = document.getElementById('step2');
  const step3 = document.getElementById('step3');
  const loadingSub = document.getElementById('loadingSub');

  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d', { willReadFrequently: true });

  const offCanvas = document.createElement('canvas');
  offCanvas.width = 1000; offCanvas.height = 1000;
  const offCtx = offCanvas.getContext('2d');

  const origImg = new Image(); // NOTE: this now points to BASELINE (bg removed) so Restore won't re-add white bg
  const currImg = new Image();

  const btnErase = document.getElementById('btnErase');
  const btnRestore = document.getElementById('btnRestore');
  const btnMagic = document.getElementById('btnMagic');

  const brushSlider = document.getElementById('brushSize');
  const brushControls = document.getElementById('brush-controls');
  const cursor = document.getElementById('cursor');
  const canvasContainer = document.getElementById('canvas-container');
  const eraseTint = document.getElementById('eraseTint');

  const params = new URLSearchParams(window.location.search);
  const SLOT = params.get('slot') || 'main';
  const RETURN_TO = params.get('return_to') || '';

  function saveState() {
      if(history.length > 12) history.shift();
      history.push(ctx.getImageData(0, 0, cv.width, cv.height));
  }

  document.getElementById('btnUndo').addEventListener('click', () => {
      if(history.length > 0) ctx.putImageData(history.pop(), 0, 0);
  });

  function updateCursorSize() {
      if(mode === 'magic') {
          cursor.style.display = 'none';
          cv.style.cursor = 'crosshair';
      } else {
          const displayWidth = cv.getBoundingClientRect().width;
          const ratio = displayWidth / 1000;
          const visualSize = brushSlider.value * ratio;
          cursor.style.width = visualSize + 'px';
          cursor.style.height = visualSize + 'px';
          cv.style.cursor = 'none';
      }
      // Tint helps you see erased areas better when erasing
      if (mode === 'remove') eraseTint.classList.add('on');
      else eraseTint.classList.remove('on');
  }

  brushSlider.addEventListener('input', updateCursorSize);

  canvasContainer.addEventListener('mousemove', (e) => {
      if(mode !== 'magic') {
          cursor.style.display = 'block';
          cursor.style.left = e.clientX + 'px';
          cursor.style.top = e.clientY + 'px';
      }
  });
  canvasContainer.addEventListener('mouseleave', () => cursor.style.display = 'none');

  // Toggle erase tint visibility
  document.getElementById('toggleTint').addEventListener('click', () => {
      eraseTint.classList.toggle('on');
  });

  async function waitForReady(sessionId) {
      // Poll /status until ready (fast perceived UX; avoids 60s “dead wait” on /upload)
      const start = Date.now();
      const timeoutMs = 120000; // 2 min max
      while (true) {
          const r = await fetch(`/status/${sessionId}?t=${Date.now()}`);
          if (r.ok) {
              const j = await r.json();
              if (j.status === "ready") return;
              if (j.status === "failed") throw new Error("Background removal failed. Try Keep Original or re-upload.");
          }
          if (Date.now() - start > timeoutMs) throw new Error("Processing timed out. Try again.");
          await new Promise(res => setTimeout(res, 650));
      }
  }

  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;

    step1.style.display = 'none';
    step2.style.display = 'flex';
    loadingSub.innerText = "Uploading your image…";

    const fd = new FormData();
    fd.append('file', f);

    try {
        // returns quickly, processing continues in background
        const r = await fetch(`/upload?keep_original=false`, { method: 'POST', body: fd });
        const j = await r.json();
        if(!r.ok) throw new Error(j.error || "Upload failed");
        sessionId = j.session_id;

        loadingSub.innerText = "Removing background… (usually a few seconds)";
        await waitForReady(sessionId);

        // Load baseline + current
        await Promise.all([
            new Promise(res => { origImg.onload = res; origImg.src = `/original/${sessionId}?t=${Date.now()}`; }),
            new Promise(res => { currImg.onload = res; currImg.src = `/preview/${sessionId}?t=${Date.now()}`; })
        ]);

        ctx.clearRect(0, 0, cv.width, cv.height);
        ctx.drawImage(currImg, 0, 0, cv.width, cv.height);

        step2.style.display = 'none';
        step3.style.display = 'flex';
        btnDone.style.display = 'block';
        updateCursorSize();
    } catch(err) {
        alert(err.message || "Upload failed. Please try again.");
        location.reload();
    }
  });

  const setMode = (m, activeBtn) => {
      mode = m;
      btnErase.classList.remove('active');
      btnRestore.classList.remove('active');
      btnMagic.classList.remove('active');
      activeBtn.classList.add('active');
      brushControls.style.opacity = (m === 'magic') ? '0.3' : '1';
      brushControls.style.pointerEvents = (m === 'magic') ? 'none' : 'auto';
      updateCursorSize();
  };

  btnErase.addEventListener('click', () => setMode('remove', btnErase));
  btnRestore.addEventListener('click', () => setMode('restore', btnRestore));
  btnMagic.addEventListener('click', () => setMode('magic', btnMagic));

  function getCoords(evt) {
    const rect = cv.getBoundingClientRect();
    const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    const clientY = evt.touches ? evt.touches[0].clientY : evt.clientY;
    return {
      x: ((clientX - rect.left) / rect.width) * cv.width,
      y: ((clientY - rect.top) / rect.height) * cv.height
    };
  }

  function magicRemove(startX, startY) {
      startX = Math.floor(startX); startY = Math.floor(startY);
      const imgData = ctx.getImageData(0, 0, cv.width, cv.height);
      const data = imgData.data;
      const w = cv.width, h = cv.height;

      const startPos = (startY * w + startX) * 4;
      const sa = data[startPos+3];
      if (sa === 0) return;

      const sr = data[startPos], sg = data[startPos+1], sb = data[startPos+2];

      const stack = [startX, startY];
      const seen = new Uint8Array(w * h);
      seen[startY * w + startX] = 1;

      // slightly tighter tolerance = less accidental deletion
      const tolerance = 52;

      while (stack.length > 0) {
          const y = stack.pop();
          const x = stack.pop();
          const pos = (y * w + x) * 4;
          data[pos + 3] = 0;

          const neighbors = [[x-1, y], [x+1, y], [x, y-1], [x, y+1]];
          for (let i = 0; i < neighbors.length; i++) {
              const nx = neighbors[i][0], ny = neighbors[i][1];
              if (nx >= 0 && nx < w && ny >= 0 && ny < h) {
                  const idx = ny * w + nx;
                  if (seen[idx] === 0) {
                      seen[idx] = 1;
                      const nPos = idx * 4;
                      if (data[nPos+3] > 0) {
                          const dist = Math.abs(data[nPos] - sr) + Math.abs(data[nPos+1] - sg) + Math.abs(data[nPos+2] - sb);
                          if (dist <= tolerance) stack.push(nx, ny);
                      }
                  }
              }
          }
      }
      ctx.putImageData(imgData, 0, 0);
  }

  function drawBrush(x, y) {
    const bSize = parseInt(brushSlider.value);

    offCtx.globalCompositeOperation = 'source-over';
    offCtx.clearRect(0, 0, 1000, 1000);

    // Shadow helps users see brush edge on complex logos
    offCtx.shadowBlur = bSize / 5;
    offCtx.shadowColor = 'rgba(0,0,0,0.55)';
    offCtx.lineWidth = bSize;
    offCtx.lineCap = 'round';
    offCtx.lineJoin = 'round';
    offCtx.strokeStyle = 'black';

    offCtx.beginPath();
    offCtx.moveTo(lastX, lastY);
    offCtx.lineTo(x, y);
    offCtx.stroke();

    if(mode === 'remove') {
        ctx.globalCompositeOperation = 'destination-out';
        ctx.drawImage(offCanvas, 0, 0);
    } else if (mode === 'restore') {
        // Restore uses BASELINE image (bg removed), so it won't re-add white backgrounds.
        offCtx.globalCompositeOperation = 'source-in';
        offCtx.shadowBlur = 0;
        offCtx.drawImage(origImg, 0, 0, 1000, 1000);
        ctx.globalCompositeOperation = 'source-over';
        ctx.drawImage(offCanvas, 0, 0);
    }

    lastX = x; lastY = y;
  }

  const startDraw = (e) => {
      saveState();
      isDown = true;
      const c = getCoords(e);
      if(mode === 'magic') {
          magicRemove(c.x, c.y);
          isDown = false;
      } else {
          lastX = c.x; lastY = c.y;
          drawBrush(c.x, c.y);
      }
      if(e.cancelable) e.preventDefault();
  };

  const moveDraw = (e) => {
      if(!isDown || mode === 'magic') return;
      const c = getCoords(e);
      drawBrush(c.x, c.y);

      if(e.touches) {
          cursor.style.display = 'block';
          cursor.style.left = e.touches[0].clientX + 'px';
          cursor.style.top = e.touches[0].clientY + 'px';
      }
      if(e.cancelable) e.preventDefault();
  };

  const endDraw = () => { isDown = false; };

  cv.addEventListener('mousedown', startDraw);
  cv.addEventListener('mousemove', moveDraw);
  window.addEventListener('mouseup', endDraw);

  cv.addEventListener('touchstart', startDraw, {passive: false});
  cv.addEventListener('touchmove', moveDraw, {passive: false});
  window.addEventListener('touchend', endDraw);

  function notifyDone(payload) {
      // Prefer parent (iframe), then opener (popup), else redirect.
      try {
          if (window.parent && window.parent !== window) {
              window.parent.postMessage(payload, "*");
              return true;
          }
      } catch(e) {}

      try {
          if (window.opener && !window.opener.closed) {
              window.opener.postMessage(payload, "*");
              return true;
          }
      } catch(e) {}

      return false;
  }

  btnDone.addEventListener('click', () => {
    btnDone.innerText = "Saving…";
    btnDone.disabled = true;
    cv.style.opacity = '0.6';

    cv.toBlob(async (blob) => {
        try {
          const fd = new FormData();
          fd.append("file", blob, "edited_logo.png");
          const resp = await fetch(`/save-edit/${sessionId}`, { method: 'POST', body: fd });
          if (!resp.ok) {
            const j = await resp.json().catch(()=>({}));
            throw new Error(j.error || "Save failed");
          }

          const payload = {
              type: "studio-uploader:done",
              slot: SLOT,
              session_id: sessionId,
              finalize_url: `${window.location.origin}/finalize/${sessionId}`
          };

          const sent = notifyDone(payload);

          // If not embedded/popup, redirect back to form if provided.
          if (!sent) {
              if (RETURN_TO) {
                  const url = new URL(RETURN_TO, window.location.origin);
                  url.searchParams.set("slot", SLOT);
                  url.searchParams.set("session_id", sessionId);
                  url.searchParams.set("finalize_url", payload.finalize_url);
                  window.location.href = url.toString();
              } else {
                  btnDone.innerText = "Saved ✓";
              }
          } else {
              // Give postMessage a moment then show success state
              btnDone.innerText = "Saved ✓";
          }
        } catch (err) {
          alert(err.message || "Save failed");
          btnDone.innerText = "Save & Done";
          btnDone.disabled = false;
          cv.style.opacity = '1';
        }
    }, "image/png");
  });
</script>
</body>
</html>"""
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store, max-age=0"})


if __name__ == "__main__":
    # local dev only
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))