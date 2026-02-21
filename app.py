# app.py — Studio Uploader (FastAPI) — FAST + ASYNC + UI IMPROVEMENTS
# Goals:
# - /upload returns immediately (no 2-minute blocking request)
# - rembg runs in background; UI polls /status/{session_id}
# - Skip rembg if input already has real transparency (editor output)
# - Faster default rembg settings (alpha matting off)
# - Clearer editor checkerboard + optional "erase highlight" overlay
# - Provision endpoint avoids re-rembg when logo already transparent

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
app = FastAPI(title="Studio Uploader", version="1.4.0-fast-async")


# ----------------------------
# CORS
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

# Output sizes
TARGET_PX = int(os.getenv("TARGET_PX", "3000"))      # final normalized output used for print
EDITOR_PX = int(os.getenv("EDITOR_PX", "1000"))      # editor canvas size (smaller = faster in browser)

# Upload limits
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Guard against pixel bombs
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(40_000_000)))  # 40MP

# rembg
# NOTE: for SPEED, keep dims smaller. 768-1024 is a sweet spot on CPU.
REMBG_MODEL = os.getenv("REMBG_MODEL", "u2netp")
REMBG_MAX_DIM = int(os.getenv("REMBG_MAX_DIM", "1024"))

# If you want higher quality (slower), set REMBG_QUALITY=high
REMBG_QUALITY = os.getenv("REMBG_QUALITY", "fast").strip().lower()  # "fast" | "high"

# edge refine (fast-ish, but optional)
EDGE_REFINE = os.getenv("EDGE_REFINE", "1").strip() not in ("0", "false", "False", "")
EDGE_SHRINK_PX = int(os.getenv("EDGE_SHRINK_PX", "2"))
EDGE_FEATHER_PX = int(os.getenv("EDGE_FEATHER_PX", "2"))

# provisioning script path
PROVISION_SCRIPT = Path(os.getenv("PROVISION_SCRIPT", str(ROOT / "shopify_provision.py")))

# Put rembg model cache in /app so it survives within container runtime
# (won't persist across cold starts unless Railway keeps the instance alive)
os.environ.setdefault("U2NET_HOME", str(ROOT / ".u2net"))
Path(os.environ["U2NET_HOME"]).mkdir(parents=True, exist_ok=True)


# ----------------------------
# Session Status Tracking
# ----------------------------
_STATUS: Dict[str, Dict[str, Any]] = {}
_STATUS_LOCK = threading.Lock()

def _status_set(session_id: str, **kwargs):
    with _STATUS_LOCK:
        s = _STATUS.get(session_id, {})
        s.update(kwargs)
        _STATUS[session_id] = s

def _status_get(session_id: str) -> Dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_STATUS.get(session_id, {}))


# ----------------------------
# Jobs (provisioning)
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
# rembg session (lazy load)
# ----------------------------
_SESSION = None
_SESSION_LOCK = threading.Lock()

def get_rembg_session():
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                print("🧠 Loading rembg model into memory:", REMBG_MODEL)
                from rembg import new_session
                _SESSION = new_session(REMBG_MODEL)
    return _SESSION


@app.on_event("startup")
def _warm_start():
    # Warm model in background so first user upload isn't waiting on model init
    def _warm():
        try:
            get_rembg_session()
            print("✅ rembg session warmed")
        except Exception as e:
            print("⚠️ rembg warm failed:", str(e))
    threading.Thread(target=_warm, daemon=True).start()


# ----------------------------
# Helpers: file read (stream + cap)
# ----------------------------
async def _read_upload_limited(file: UploadFile, limit_bytes: int) -> bytes:
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
    return {
        "orig": UPLOAD_DIR / f"{session_id}_orig.png",
        "curr": UPLOAD_DIR / f"{session_id}_curr.png",
    }

def _save_png(img: Image.Image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG", optimize=True)

def _scale_to_fit(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if w <= max_dim and h <= max_dim:
        return img
    scale = min(max_dim / w, max_dim / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

def _pil_open_safe(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    w, h = img.size
    if (w * h) > MAX_IMAGE_PIXELS:
        raise ValueError("too_many_pixels")
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA")


# ----------------------------
# Transparency heuristics (skip rembg if already transparent)
# ----------------------------
def _has_real_transparency(img: Image.Image, sample_step: int = 8, alpha_cut: int = 245) -> bool:
    """
    Detect if image already has meaningful transparency.
    We sample pixels; if enough have alpha < alpha_cut, treat as already-processed.
    """
    if img.mode != "RGBA":
        return False
    a = img.split()[-1]
    w, h = a.size
    # Sample grid
    count = 0
    trans = 0
    px = a.load()
    for y in range(0, h, sample_step):
        for x in range(0, w, sample_step):
            count += 1
            if px[x, y] < alpha_cut:
                trans += 1
    if count == 0:
        return False
    return (trans / count) > 0.03  # >3% sampled pixels transparent-ish


# ----------------------------
# rembg pipeline (fast vs high)
# ----------------------------
def _rembg_remove(img: Image.Image) -> Image.Image:
    from rembg import remove

    # Downscale for speed/stability
    img_small = _scale_to_fit(img, REMBG_MAX_DIM)

    if REMBG_QUALITY == "high":
        out = remove(
            img_small,
            session=get_rembg_session(),
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=10,
        )
    else:
        # FAST path: no alpha matting
        out = remove(img_small, session=get_rembg_session())

    if isinstance(out, (bytes, bytearray)):
        out_img = Image.open(BytesIO(out)).convert("RGBA")
    else:
        out_img = out.convert("RGBA")

    if EDGE_REFINE:
        out_img = _refine_alpha_edges(out_img, shrink_px=EDGE_SHRINK_PX, feather_px=EDGE_FEATHER_PX)

    return out_img


def _refine_alpha_edges(img: Image.Image, shrink_px: int = 2, feather_px: int = 2) -> Image.Image:
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

    rgba[:, :, 3] = np.clip(a, 0, 255).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


# ----------------------------
# Trim + normalize (print)
# ----------------------------
def _trim_transparent_padding(img: Image.Image, alpha_threshold: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    a = img.split()[-1]
    bbox = a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    if not bbox:
        return img
    return img.crop(bbox)

def _normalize_logo(img: Image.Image, pad_ratio: float = 0.06, target_size: int = TARGET_PX) -> Image.Image:
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
# Async worker for /upload
# ----------------------------
def _process_upload_background(session_id: str, keep_original: bool):
    start = time.time()
    try:
        _status_set(session_id, status="processing", stage="remove_bg", started_at=start)

        p = _paths(session_id)
        orig = Image.open(p["orig"]).convert("RGBA")

        # If keep_original, no bg removal
        if keep_original or _has_real_transparency(orig):
            curr = orig.copy()
        else:
            curr = _rembg_remove(orig)

        # Resize to editor size for the UI (exact square canvas)
        orig_fit = _scale_to_fit(orig, EDITOR_PX)
        curr_fit = _scale_to_fit(curr, EDITOR_PX)

        canvas_orig = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
        canvas_curr = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))

        x = (EDITOR_PX - orig_fit.width) // 2
        y = (EDITOR_PX - orig_fit.height) // 2
        canvas_orig.alpha_composite(orig_fit, (x, y))

        x2 = (EDITOR_PX - curr_fit.width) // 2
        y2 = (EDITOR_PX - curr_fit.height) // 2
        canvas_curr.alpha_composite(curr_fit, (x2, y2))

        _save_png(canvas_orig, p["orig"])
        _save_png(canvas_curr, p["curr"])

        _status_set(session_id, status="ready", stage="done", finished_at=time.time(), elapsed=time.time() - start)
    except Exception as e:
        _status_set(session_id, status="failed", stage="error", error=str(e), finished_at=time.time(), elapsed=time.time() - start)


# ----------------------------
# Endpoints
# ----------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui")

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "version": app.version}

@app.get("/status/{session_id}")
def status(session_id: str):
    s = _status_get(session_id)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return s


# --------
# Upload: immediate response + background processing
# --------
@app.post("/upload")
async def upload_image(file: UploadFile = File(...), keep_original: bool = Query(False)):
    try:
        data = await _read_upload_limited(file, MAX_UPLOAD_BYTES)
    except ValueError:
        return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)

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

    # Save original immediately (fast)
    _save_png(img, p["orig"])

    # Mark status and process in background
    _status_set(session_id, status="queued", stage="queued", created_at=time.time())
    threading.Thread(target=_process_upload_background, args=(session_id, keep_original), daemon=True).start()

    return {
        "status": "ok",
        "session_id": session_id,
        "preview_url": f"/preview/{session_id}",
        "original_url": f"/original/{session_id}",
        "status_url": f"/status/{session_id}",
    }

@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    path = _paths(session_id)["curr"]
    if not path.exists():
        return JSONResponse({"error": "Not ready"}, status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png")

@app.get("/original/{session_id}")
def get_original(session_id: str):
    path = _paths(session_id)["orig"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png")

@app.post("/save-edit/{session_id}")
async def save_edit(session_id: str, file: UploadFile = File(...)):
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
    path = _paths(session_id)["curr"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png")


# ----------------------------
# Provisioning runner
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
    _job_set(job_id, status="running", started_at=time.time())

    if not PROVISION_SCRIPT.exists():
        _job_set(job_id, status="failed", error=f"Provision script not found: {PROVISION_SCRIPT}")
        return

    cmd = [
        "python",
        str(PROVISION_SCRIPT),
        "--name", storefront_name,
        "--handle", storefront_handle,
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
            _job_set(job_id, status="failed", finished_at=time.time(), error=f"Provision failed (exit {proc.returncode})", stdout=stdout, stderr=stderr)
            return

        _job_set(job_id, status="succeeded", finished_at=time.time(), stdout=stdout, stderr=stderr)

    except subprocess.TimeoutExpired:
        _job_set(job_id, status="failed", finished_at=time.time(), error="Provision timed out")
    except Exception as e:
        _job_set(job_id, status="failed", finished_at=time.time(), error=str(e))


# ----------------------------
# Shopify Provisioning Endpoint
# IMPORTANT SPEED CHANGE:
# - If logo file already has transparency => skip rembg and just normalize
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
    if not storefront_name.strip():
        return JSONResponse({"error": "storefront_name is required"}, status_code=400)
    if not storefront_handle.strip():
        return JSONResponse({"error": "storefront_handle is required"}, status_code=400)
    if not customer_id.strip():
        return JSONResponse({"error": "customer_id is required"}, status_code=400)

    owner_customer_id = customer_id.split("/")[-1].strip()

    try:
        main_bytes = await _read_upload_limited(storefront_logo_file, MAX_UPLOAD_BYTES)
    except ValueError:
        return JSONResponse({"error": f"Main logo too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)

    sec_bytes = None
    if storefront_logo_secondary:
        try:
            sec_bytes = await _read_upload_limited(storefront_logo_secondary, MAX_UPLOAD_BYTES)
        except ValueError:
            return JSONResponse({"error": f"Secondary logo too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)

    # ---- MAIN ----
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

    # If already transparent, skip rembg (FAST)
    if _has_real_transparency(img_main):
        main_processed = img_main
    else:
        main_processed = _rembg_remove(img_main)

    main_final = _normalize_logo(main_processed, target_size=TARGET_PX)
    _save_png(_scale_to_fit(img_main, EDITOR_PX), main_paths["orig"])
    _save_png(main_final, main_paths["curr"])

    # ---- SECONDARY ----
    secondary_session_id = None
    if sec_bytes:
        secondary_session_id = str(uuid.uuid4())
        sec_paths = _paths(secondary_session_id)

        try:
            img_sec = _pil_open_safe(sec_bytes)
        except ValueError as e:
            if str(e) == "too_many_pixels":
                return JSONResponse({"error": "Secondary logo resolution too large. Please upload a smaller image."}, status_code=413)
            return JSONResponse({"error": "Secondary logo is not a supported image."}, status_code=400)
        except Exception:
            return JSONResponse({"error": "Secondary logo is not a supported image."}, status_code=400)

        if _has_real_transparency(img_sec):
            sec_processed = img_sec
        else:
            sec_processed = _rembg_remove(img_sec)

        sec_final = _normalize_logo(sec_processed, target_size=TARGET_PX)
        _save_png(_scale_to_fit(img_sec, EDITOR_PX), sec_paths["orig"])
        _save_png(sec_final, sec_paths["curr"])

    # Launch provisioning job (background)
    job_id = str(uuid.uuid4())
    type_of_store = (org_type or military_branch or sport_type or "").strip() or None

    _job_set(
        job_id,
        status="queued",
        storefront_name=storefront_name,
        storefront_handle=storefront_handle,
        owner_customer_id=owner_customer_id,
        main_session_id=main_session_id,
        secondary_session_id=secondary_session_id,
        customer_email=customer_email,
        type_of_store=type_of_store,
        created_at=time.time(),
    )

    threading.Thread(
        target=_run_shopify_provision_job,
        args=(job_id, storefront_name, storefront_handle, owner_customer_id, type_of_store, main_session_id, secondary_session_id),
        daemon=True,
    ).start()

    # IMPORTANT: return fast so Shopify doesn't spin forever
    return {
        "status": "ok",
        "job_id": job_id,
        "message": "Provisioning started.",
    }

@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    j = _job_get(job_id)
    if not j:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return j


# ----------------------------
# UI (Editor) — faster load + better visibility
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
        --primary: #10b981;
        --primary-hover: #059669;
        --text: #f8fafc;
        --muted: #94a3b8;
        --radius: 16px;
    }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; display: flex; flex-direction: column; min-height: 100vh; overscroll-behavior: none; }
    .header { padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; background: rgba(30, 41, 59, 0.85); backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255,255,255,0.06); z-index: 10; position: relative; }
    .header h2 { margin: 0; font-size: 18px; font-weight: 800; letter-spacing: 0.2px; }
    .btn-done { background: var(--primary); color: white; border: none; padding: 8px 16px; border-radius: 99px; font-weight: 700; font-size: 14px; cursor: pointer; transition: background 0.2s; }
    .btn-done:hover { background: var(--primary-hover); }
    .btn-done:disabled { opacity: 0.5; cursor: not-allowed; }

    .main-container { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; position: relative; }

    .step-upload { text-align: center; width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: 14px; }
    .upload-box { border: 2px dashed rgba(255,255,255,0.22); border-radius: var(--radius); padding: 56px 20px; background: rgba(255,255,255,0.03); cursor: pointer; transition: all 0.2s; position: relative; }
    .upload-box:hover { background: rgba(255,255,255,0.06); border-color: var(--primary); }
    .upload-box input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
    .upload-icon { font-size: 40px; margin-bottom: 12px; display: block; }
    .upload-title { font-size: 18px; font-weight: 800; margin-bottom: 6px; }
    .upload-sub { color: var(--muted); font-size: 14px; line-height: 1.35; }

    .step-loading { display: none; text-align: center; flex-direction: column; align-items: center; gap: 16px; }
    .spinner { width: 46px; height: 46px; border: 4px solid rgba(255,255,255,0.12); border-top-color: var(--primary); border-radius: 50%; animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .progress { width: 260px; height: 10px; border-radius: 99px; background: rgba(255,255,255,0.08); overflow: hidden; }
    .bar { height: 100%; width: 20%; background: var(--primary); border-radius: 99px; animation: pulse 1.1s ease-in-out infinite; }
    @keyframes pulse { 0%{ transform: translateX(-40%);} 100%{ transform: translateX(360%);} }

    .step-editor { display: none; width: 100%; max-width: 540px; flex-direction: column; gap: 18px; }

    .canvas-wrap {
        background: #111827; border-radius: var(--radius); position: relative; width: 100%; aspect-ratio: 1/1;
        overflow: hidden; border: 1px solid rgba(255,255,255,0.10); touch-action: none; box-shadow: 0 20px 40px rgba(0,0,0,0.35);
    }

    /* DARK checkerboard so white/halo areas pop */
    .checker {
        position:absolute; inset:0;
        background-color: #0f172a;
        background-image:
          linear-gradient(45deg, rgba(255,255,255,0.08) 25%, transparent 25%),
          linear-gradient(-45deg, rgba(255,255,255,0.08) 25%, transparent 25%),
          linear-gradient(45deg, transparent 75%, rgba(255,255,255,0.08) 75%),
          linear-gradient(-45deg, transparent 75%, rgba(255,255,255,0.08) 75%);
        background-size: 22px 22px;
        background-position: 0 0, 0 11px, 11px -11px, -11px 0px;
        z-index: 1;
    }

    /* Optional overlay to highlight erased (transparent) pixels */
    .erase-overlay {
        position:absolute; inset:0; z-index: 3; pointer-events: none; opacity: 0; mix-blend-mode: screen;
        background: radial-gradient(circle at center, rgba(255,0,0,0.0) 0%, rgba(255,0,0,0.0) 55%, rgba(255,0,0,0.08) 100%);
    }

    canvas { width: 100%; height: 100%; display: block; position: relative; z-index: 2; touch-action: none; cursor: none; }

    #cursor { position: fixed; border: 2px solid rgba(255,255,255,0.95); box-shadow: 0 0 6px rgba(0,0,0,0.8); border-radius: 50%; pointer-events: none; transform: translate(-50%, -50%); z-index: 9999; display: none; }

    .toolbar { background: rgba(30,41,59,0.95); border: 1px solid rgba(255,255,255,0.10); border-radius: 999px; padding: 6px; display: flex; gap: 6px; margin: 0 auto; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
    .tool-btn { background: transparent; color: var(--muted); border: none; padding: 10px 14px; border-radius: 999px; cursor: pointer; font-weight: 800; font-size: 13px; display: flex; align-items: center; gap: 6px; transition: all 0.2s; }
    .tool-btn:hover { color: var(--text); background: rgba(255,255,255,0.06); }
    .tool-btn.active { background: rgba(255,255,255,0.10); color: var(--primary); }

    .brush-controls { display: flex; align-items: center; gap: 12px; background: rgba(30,41,59,0.95); padding: 12px 18px; border-radius: 999px; margin-top: 8px; border: 1px solid rgba(255,255,255,0.10); }
    .brush-controls span { font-size: 13px; color: var(--muted); font-weight: 800; }
    input[type=range] { flex: 1; accent-color: var(--primary); }

    .top-actions { display: flex; justify-content: space-between; margin-bottom: 6px; }
    .btn-text { background: none; border: none; color: var(--muted); font-size: 13px; cursor: pointer; font-weight: 800; }
    .btn-text:hover { color: var(--text); }

    .hint { color: rgba(255,255,255,0.75); font-size: 12px; text-align: center; margin-top: 4px; }
  </style>
</head>
<body>
  <div id="cursor"></div>

  <div class="header">
      <h2>Studio Uploader</h2>
      <button class="btn-done" id="btnDone" style="display:none;">Save & Done</button>
  </div>

  <div class="main-container">
    <div class="step-upload" id="step1">
      <div class="upload-box">
          <input id="file" type="file" accept="image/*" />
          <span class="upload-icon">🪄</span>
          <div class="upload-title">Tap to Upload</div>
          <div class="upload-sub">We’ll prep your logo fast. You can erase/restore if needed.</div>
      </div>
      <div class="hint">Tip: If your logo already has a transparent background, it loads almost instantly.</div>
    </div>

    <div class="step-loading" id="step2">
      <div class="spinner"></div>
      <div style="font-weight: 800; font-size: 18px;">Processing…</div>
      <div style="color: var(--muted); font-size: 14px;" id="statusLine">Preparing image</div>
      <div class="progress"><div class="bar"></div></div>
      <div class="hint" id="timeHint"></div>
    </div>

    <div class="step-editor" id="step3">
      <div class="top-actions">
          <button class="btn-text" onclick="location.reload()">Start Over</button>
          <button class="btn-text" id="btnUndo">↩️ Undo</button>
      </div>

      <div class="canvas-wrap" id="canvas-container">
        <div class="checker"></div>
        <div class="erase-overlay" id="eraseOverlay"></div>
        <canvas id="cv" width="1000" height="1000"></canvas>
      </div>

      <div class="toolbar">
        <button class="tool-btn active" id="btnRestore">🖌️ Restore</button>
        <button class="tool-btn" id="btnErase">🧹 Erase</button>
        <button class="tool-btn" id="btnMagic">✨ Magic</button>
        <button class="tool-btn" id="btnShowErase">👁️ Show Erased</button>
      </div>

      <div class="brush-controls" id="brush-controls">
          <span>Size</span>
          <input type="range" id="brushSize" min="10" max="150" value="50">
      </div>

      <div class="hint">Magic removes similar colors where you click (great for leftover white halos).</div>
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

  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d', { willReadFrequently: true });

  const offCanvas = document.createElement('canvas');
  offCanvas.width = 1000; offCanvas.height = 1000;
  const offCtx = offCanvas.getContext('2d');

  const origImg = new Image();
  const currImg = new Image();

  const btnErase = document.getElementById('btnErase');
  const btnRestore = document.getElementById('btnRestore');
  const btnMagic = document.getElementById('btnMagic');
  const btnShowErase = document.getElementById('btnShowErase');
  const brushSlider = document.getElementById('brushSize');
  const brushControls = document.getElementById('brush-controls');
  const cursor = document.getElementById('cursor');
  const canvasContainer = document.getElementById('canvas-container');
  const statusLine = document.getElementById('statusLine');
  const timeHint = document.getElementById('timeHint');
  const eraseOverlay = document.getElementById('eraseOverlay');

  const params = new URLSearchParams(window.location.search);
  const SLOT = params.get('slot') || 'main';
  const RETURN_TO = params.get('return_to') || '';

  function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

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

  async function waitUntilReady(sessionId) {
      const start = Date.now();
      while(true){
          const r = await fetch(`/status/${sessionId}?t=${Date.now()}`);
          const j = await r.json();
          if(r.ok){
              if(j.status === "ready"){
                  return { ok:true, elapsed: j.elapsed || ((Date.now()-start)/1000) };
              }
              if(j.status === "failed"){
                  return { ok:false, error: j.error || "processing failed" };
              }
              statusLine.textContent = (j.stage === "remove_bg") ? "Removing background…" : "Preparing…";
              const sec = Math.floor((Date.now()-start)/1000);
              timeHint.textContent = sec > 2 ? `Time: ${sec}s` : "";
          }
          await sleep(450);
      }
  }

  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;

    step1.style.display = 'none';
    step2.style.display = 'flex';
    statusLine.textContent = "Uploading…";
    timeHint.textContent = "";

    const fd = new FormData();
    fd.append('file', f);

    try {
        // keep_original=false means "try background removal"
        const r = await fetch(`/upload?keep_original=false`, { method: 'POST', body: fd });
        const j = await r.json();
        if(!r.ok) throw new Error(j.error || "Upload failed");
        sessionId = j.session_id;

        // Poll until /preview exists
        const ready = await waitUntilReady(sessionId);
        if(!ready.ok) throw new Error(ready.error || "Processing failed");

        statusLine.textContent = "Loading editor…";

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
      brushControls.style.opacity = (m === 'magic') ? '0.35' : '1';
      brushControls.style.pointerEvents = (m === 'magic') ? 'none' : 'auto';
      updateCursorSize();
  };

  btnErase.addEventListener('click', () => setMode('remove', btnErase));
  btnRestore.addEventListener('click', () => setMode('restore', btnRestore));
  btnMagic.addEventListener('click', () => setMode('magic', btnMagic));

  let showErase = false;
  btnShowErase.addEventListener('click', () => {
      showErase = !showErase;
      eraseOverlay.style.opacity = showErase ? '1' : '0';
      btnShowErase.classList.toggle('active', showErase);
  });

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

      // Lower tolerance prevents deleting nearby art; adjust if needed
      const tolerance = 50;

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

    offCtx.shadowBlur = bSize / 4;
    offCtx.shadowColor = 'rgba(0,0,0,0.65)';
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
        ctx.globalCompositeOperation = 'source-over';
    } else if (mode === 'restore') {
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
    cv.style.opacity = '0.65';

    cv.toBlob(async (blob) => {
        const fd = new FormData();
        fd.append("file", blob, "edited_logo.png");
        await fetch(`/save-edit/${sessionId}`, { method: 'POST', body: fd });

        const payload = {
            type: "studio-uploader:done",
            slot: SLOT,
            session_id: sessionId,
            finalize_url: `${window.location.origin}/finalize/${sessionId}`
        };

        const sent = notifyDone(payload);
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
        }
    }, "image/png");
  });
</script>
</body>
</html>"""
    return HTMLResponse(content=html)