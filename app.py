# app.py — Studio Uploader (FastAPI) — async-rembg + visible provisioning logs
import os
import uuid
import time
import json
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

TARGET_PX = int(os.getenv("TARGET_PX", "3000"))
EDITOR_PX = int(os.getenv("EDITOR_PX", "1200"))  # smaller = faster UI
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(40_000_000)))  # 40MP

REMBG_MODEL = os.getenv("REMBG_MODEL", "u2netp")
REMBG_MAX_DIM = int(os.getenv("REMBG_MAX_DIM", "1024"))  # smaller = faster
REMBG_DETAIL_SAFE = os.getenv("REMBG_DETAIL_SAFE", "1").strip() not in ("0", "false", "False", "")
EDGE_REFINE = os.getenv("EDGE_REFINE", "0").strip() not in ("0", "false", "False", "")  # default OFF for speed
EDGE_SHRINK_PX = int(os.getenv("EDGE_SHRINK_PX", "1"))
EDGE_FEATHER_PX = int(os.getenv("EDGE_FEATHER_PX", "1"))

PROVISION_SCRIPT = Path(os.getenv("PROVISION_SCRIPT", str(ROOT / "shopify_provision.py")))

# ----------------------------
# Job + session status tracking
# ----------------------------
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

_SESS: Dict[str, Dict[str, Any]] = {}
_SESS_LOCK = threading.Lock()

def _job_set(job_id: str, **kwargs):
    with _JOBS_LOCK:
        j = _JOBS.get(job_id, {})
        j.update(kwargs)
        _JOBS[job_id] = j

def _job_get(job_id: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        return dict(_JOBS.get(job_id, {}))

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
_SESSION_LOCK = threading.Lock()

def get_rembg_session():
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            print("🧠 Loading rembg model into memory:", REMBG_MODEL)
            from rembg import new_session
            _SESSION = new_session(REMBG_MODEL)
    return _SESSION


# ----------------------------
# Helpers: file read (stream + cap)
# ----------------------------
async def _read_upload_limited(file: UploadFile, limit_bytes: int) -> bytes:
    buf = bytearray()
    chunk_size = 1024 * 1024
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
        "orig": UPLOAD_DIR / f"{session_id}_orig.png",   # always original (square canvas)
        "curr": UPLOAD_DIR / f"{session_id}_curr.png",   # current working (after AI or edits)
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
# Background removal + refine
# ----------------------------
def _refine_alpha_edges(img: Image.Image, shrink_px: int = 1, feather_px: int = 1) -> Image.Image:
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

def _rembg_remove(img: Image.Image, detail_safe: bool = True) -> Image.Image:
    from rembg import remove

    # detail_safe keeps tiny elements (stars, thin strokes) better by avoiding heavy matting
    if detail_safe:
        out = remove(img, session=get_rembg_session(), alpha_matting=False)
    else:
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
        out_img = _refine_alpha_edges(out_img, EDGE_SHRINK_PX, EDGE_FEATHER_PX)

    return out_img


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
# Async “upload then process” worker
# ----------------------------
def _bg_process_session(session_id: str, detail_safe: bool):
    try:
        _sess_set(session_id, status="processing", started_at=time.time())
        p = _paths(session_id)

        if not p["orig"].exists():
            _sess_set(session_id, status="failed", error="orig missing")
            return

        orig_sq = Image.open(p["orig"]).convert("RGBA")

        # Downscale for model speed
        work = _scale_to_fit(orig_sq, REMBG_MAX_DIM)

        removed = _rembg_remove(work, detail_safe=detail_safe)

        # Resize back to editor square to keep brush coords aligned
        removed_sq = removed.resize((EDITOR_PX, EDITOR_PX), Image.LANCZOS)

        _save_png(removed_sq, p["curr"])
        _sess_set(session_id, status="ready", finished_at=time.time())
    except Exception as e:
        _sess_set(session_id, status="failed", error=str(e), finished_at=time.time())


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
def session_status(session_id: str):
    s = _sess_get(session_id)
    if not s:
        return {"status": "unknown"}
    return s


# --------
# Editor upload pipeline (FAST response)
# --------
@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    keep_original: bool = Query(False),
    detail_safe: bool = Query(True),  # keep small stars/lines
):
    try:
        data = await _read_upload_limited(file, MAX_UPLOAD_BYTES)
    except ValueError as e:
        if str(e) == "too_large":
            return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)
        return JSONResponse({"error": "Upload read failed"}, status_code=400)

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

    # Make editor square canvas immediately (so UI is instant)
    img_fit = _scale_to_fit(img, EDITOR_PX)

    canvas_orig = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
    x = (EDITOR_PX - img_fit.width) // 2
    y = (EDITOR_PX - img_fit.height) // 2
    canvas_orig.alpha_composite(img_fit, (x, y))

    _save_png(canvas_orig, p["orig"])

    # Immediately set curr = orig for instant render
    _save_png(canvas_orig, p["curr"])
    _sess_set(session_id, status="ready" if keep_original else "queued", created_at=time.time())

    # If we want AI removal, do it in background (NOT blocking UI)
    if not keep_original:
        t = threading.Thread(target=_bg_process_session, args=(session_id, detail_safe), daemon=True)
        t.start()

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
        return JSONResponse({"error": "Not found"}, status_code=404)
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

    print("🚀 Provision cmd:", " ".join(cmd))

    try:
        # IMPORTANT: do NOT hide logs. Print them.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=900,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # Print to Railway logs so you can see failures immediately
        if stdout.strip():
            print("📄 Provision stdout:\n", stdout[-12000:])
        if stderr.strip():
            print("🧯 Provision stderr:\n", stderr[-12000:])

        if proc.returncode != 0:
            _job_set(
                job_id,
                status="failed",
                finished_at=time.time(),
                error=f"Provision failed (exit {proc.returncode})",
                stdout=stdout[-12000:],
                stderr=stderr[-12000:],
            )
            return

        _job_set(
            job_id,
            status="succeeded",
            finished_at=time.time(),
            stdout=stdout[-12000:],
            stderr=stderr[-12000:],
        )

    except subprocess.TimeoutExpired:
        _job_set(job_id, status="failed", finished_at=time.time(), error="Provision timed out")
    except Exception as e:
        _job_set(job_id, status="failed", finished_at=time.time(), error=str(e))


# ----------------------------
# Shopify Provisioning Endpoint
# ----------------------------
@app.post("/api/storefront-request")
async def storefront_request(
    customer_id: str = Form(...),
    customer_email: str = Form(...),
    storefront_name: str = Form(...),
    storefront_handle: str = Form(...),
    org_type: str = Form(None),
    military_branch: str = Form(None),
    sport_type: str = Form(None),

    # OPTION A: send session ids from editor (fastest, no re-upload)
    main_session_id: Optional[str] = Form(None),
    secondary_session_id: Optional[str] = Form(None),

    # OPTION B: raw file upload (fallback)
    storefront_logo_file: Optional[UploadFile] = File(None),
    storefront_logo_secondary: Optional[UploadFile] = File(None),
):
    if not storefront_name.strip():
        return JSONResponse({"error": "storefront_name is required"}, status_code=400)
    if not storefront_handle.strip():
        return JSONResponse({"error": "storefront_handle is required"}, status_code=400)
    if not customer_id.strip():
        return JSONResponse({"error": "customer_id is required"}, status_code=400)

    owner_customer_id = customer_id.split("/")[-1].strip()
    type_of_store = (org_type or military_branch or sport_type or "").strip() or None

    # If no session id provided, but file is provided -> create session(s) quickly without rembg
    if not main_session_id:
        if not storefront_logo_file:
            return JSONResponse({"error": "main_session_id or storefront_logo_file required"}, status_code=400)

        main_bytes = await _read_upload_limited(storefront_logo_file, MAX_UPLOAD_BYTES)
        img_main = _pil_open_safe(main_bytes)
        img_main_fit = _scale_to_fit(img_main, EDITOR_PX)

        main_session_id = str(uuid.uuid4())
        p = _paths(main_session_id)

        canvas = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
        x = (EDITOR_PX - img_main_fit.width) // 2
        y = (EDITOR_PX - img_main_fit.height) // 2
        canvas.alpha_composite(img_main_fit, (x, y))

        _save_png(canvas, p["orig"])
        _save_png(_normalize_logo(canvas, target_size=TARGET_PX), p["curr"])

        if storefront_logo_secondary:
            sec_bytes = await _read_upload_limited(storefront_logo_secondary, MAX_UPLOAD_BYTES)
            img_sec = _pil_open_safe(sec_bytes)
            img_sec_fit = _scale_to_fit(img_sec, EDITOR_PX)

            secondary_session_id = str(uuid.uuid4())
            sp = _paths(secondary_session_id)

            canvas2 = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
            x2 = (EDITOR_PX - img_sec_fit.width) // 2
            y2 = (EDITOR_PX - img_sec_fit.height) // 2
            canvas2.alpha_composite(img_sec_fit, (x2, y2))

            _save_png(canvas2, sp["orig"])
            _save_png(_normalize_logo(canvas2, target_size=TARGET_PX), sp["curr"])

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
        created_at=time.time(),
    )

    t = threading.Thread(
        target=_run_shopify_provision_job,
        args=(job_id, storefront_name, storefront_handle, owner_customer_id, type_of_store, main_session_id, secondary_session_id),
        daemon=True,
    )
    t.start()

    return {"status": "ok", "job_id": job_id}

@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    j = _job_get(job_id)
    if not j:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return j


# ----------------------------
# UI
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
  <title>Logo Studio</title>
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
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; display: flex; flex-direction: column; min-height: 100vh; }
    .header { padding: 14px 18px; display: flex; justify-content: space-between; align-items: center; background: rgba(30, 41, 59, 0.85); backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255,255,255,0.06); }
    .header h2 { margin: 0; font-size: 16px; font-weight: 800; letter-spacing: 0.2px; }
    .btn-done { background: var(--primary); color: white; border: none; padding: 9px 16px; border-radius: 999px; font-weight: 800; font-size: 13px; cursor: pointer; }
    .btn-done:hover { background: var(--primary-hover); }
    .btn-done:disabled { opacity: 0.6; cursor: not-allowed; }

    .main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 18px; gap: 14px; }
    .card { width: 100%; max-width: 520px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 18px; box-shadow: 0 24px 50px rgba(0,0,0,0.35); }
    .upload-box { border: 2px dashed rgba(255,255,255,0.20); border-radius: 18px; padding: 44px 16px; background: rgba(255,255,255,0.03); cursor: pointer; position: relative; text-align: center; }
    .upload-box:hover { border-color: var(--primary); }
    .upload-box input { position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }
    .muted { color: var(--muted); font-size: 13px; }

    .row { display:flex; gap:10px; flex-wrap: wrap; align-items:center; justify-content:center; }
    .pill { display:flex; gap:8px; align-items:center; padding:10px 12px; border-radius: 999px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10); font-size: 13px; color: var(--text); }
    .pill input { transform: scale(1.1); }

    .canvas-wrap {
      width: 100%;
      aspect-ratio: 1/1;
      border-radius: 18px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.12);
      background: #ffffff;
      position: relative;
    }
    .checker {
      position:absolute; inset:0;
      background-color:#fff;
      background-image:
        linear-gradient(45deg, #e8e8e8 25%, transparent 25%),
        linear-gradient(-45deg, #e8e8e8 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #e8e8e8 75%),
        linear-gradient(-45deg, transparent 75%, #e8e8e8 75%);
      background-size: 18px 18px;
      background-position: 0 0, 0 9px, 9px -9px, -9px 0px;
      opacity: 1;
      z-index: 1;
    }
    canvas { width:100%; height:100%; display:block; position:relative; z-index:2; touch-action:none; cursor:none; }

    #cursor { position: fixed; border: 2px solid rgba(0,0,0,0.85); background: rgba(255,255,255,0.10);
      box-shadow: 0 0 0 2px rgba(255,255,255,0.8); border-radius: 50%; pointer-events:none; transform: translate(-50%,-50%); z-index:9999; display:none; }

    .toolbar { display:flex; gap:8px; justify-content:center; }
    .tool-btn { background: rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.10); color: var(--text);
      padding: 10px 14px; border-radius: 999px; cursor:pointer; font-weight: 800; font-size: 13px; }
    .tool-btn.active { outline: 2px solid rgba(16,185,129,0.45); }
    .controls { display:flex; gap:10px; align-items:center; justify-content:center; flex-wrap: wrap; }
    input[type=range] { width: 220px; accent-color: var(--primary); }

    .statusline { text-align:center; font-size: 13px; color: var(--muted); min-height: 18px; }
  </style>
</head>
<body>
  <div id="cursor"></div>
  <div class="header">
    <h2>Studio Uploader</h2>
    <button class="btn-done" id="btnDone" style="display:none;">Done</button>
  </div>

  <div class="main">
    <div class="card" id="step1">
      <div class="upload-box">
        <input id="file" type="file" accept="image/*" />
        <div style="font-size:32px;">🪄</div>
        <div style="font-weight:900; margin-top:8px;">Upload logo</div>
        <div class="muted" style="margin-top:6px;">Opens instantly. AI cutout loads in the background.</div>
      </div>

      <div class="row" style="margin-top:12px;">
        <label class="pill"><input type="checkbox" id="keepOriginal"> Keep original (skip AI)</label>
        <label class="pill"><input type="checkbox" id="detailSafe" checked> Keep tiny details (stars/text)</label>
      </div>
    </div>

    <div class="card" id="step3" style="display:none;">
      <div class="canvas-wrap" id="canvasContainer">
        <div class="checker"></div>
        <canvas id="cv" width="1000" height="1000"></canvas>
      </div>

      <div class="toolbar" style="margin-top:14px;">
        <button class="tool-btn active" id="btnRestore">Restore</button>
        <button class="tool-btn" id="btnErase">Erase</button>
        <button class="tool-btn" id="btnMagic">Magic</button>
      </div>

      <div class="controls" style="margin-top:12px;">
        <span class="muted">Brush</span>
        <input type="range" id="brushSize" min="8" max="140" value="44">
        <button class="tool-btn" id="btnUndo">Undo</button>
        <button class="tool-btn" id="btnRestart">Start Over</button>
      </div>

      <div class="statusline" id="statusline"></div>
    </div>
  </div>

<script>
  let sessionId = null;
  let mode = 'restore';
  let isDown = false;
  let lastX = 0, lastY = 0;
  let history = [];

  const fileEl = document.getElementById('file');
  const keepOriginalEl = document.getElementById('keepOriginal');
  const detailSafeEl = document.getElementById('detailSafe');

  const step1 = document.getElementById('step1');
  const step3 = document.getElementById('step3');

  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d', { willReadFrequently: true });

  const offCanvas = document.createElement('canvas');
  offCanvas.width = 1000; offCanvas.height = 1000;
  const offCtx = offCanvas.getContext('2d');

  const origImg = new Image();
  const currImg = new Image();

  const btnDone = document.getElementById('btnDone');
  const btnErase = document.getElementById('btnErase');
  const btnRestore = document.getElementById('btnRestore');
  const btnMagic = document.getElementById('btnMagic');
  const brushSlider = document.getElementById('brushSize');
  const cursor = document.getElementById('cursor');
  const canvasContainer = document.getElementById('canvasContainer');
  const statusline = document.getElementById('statusline');

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
  document.getElementById('btnRestart').addEventListener('click', () => location.reload());

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
    const seen = new Uint8Array(w*h);
    seen[startY*w + startX] = 1;

    const tolerance = 55;

    while(stack.length) {
      const y = stack.pop();
      const x = stack.pop();
      const pos = (y*w + x)*4;
      data[pos+3] = 0;

      const nbs = [[x-1,y],[x+1,y],[x,y-1],[x,y+1]];
      for(let i=0;i<nbs.length;i++){
        const nx=nbs[i][0], ny=nbs[i][1];
        if(nx>=0 && nx<w && ny>=0 && ny<h){
          const idx = ny*w + nx;
          if(!seen[idx]){
            seen[idx]=1;
            const p2 = idx*4;
            if(data[p2+3]>0){
              const dist = Math.abs(data[p2]-sr)+Math.abs(data[p2+1]-sg)+Math.abs(data[p2+2]-sb);
              if(dist<=tolerance) stack.push(nx,ny);
            }
          }
        }
      }
    }
    ctx.putImageData(imgData,0,0);
  }

  function drawBrush(x, y) {
    const bSize = parseInt(brushSlider.value);
    offCtx.globalCompositeOperation = 'source-over';
    offCtx.clearRect(0, 0, 1000, 1000);

    offCtx.shadowBlur = 0;
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
      offCtx.globalCompositeOperation = 'source-in';
      offCtx.drawImage(origImg, 0, 0, 1000, 1000);
      ctx.globalCompositeOperation = 'source-over';
      ctx.drawImage(offCanvas, 0, 0);
    }

    lastX = x; lastY = y;
  }

  const setMode = (m, btn) => {
    mode = m;
    btnErase.classList.remove('active');
    btnRestore.classList.remove('active');
    btnMagic.classList.remove('active');
    btn.classList.add('active');
    updateCursorSize();
  };

  btnErase.addEventListener('click', () => setMode('remove', btnErase));
  btnRestore.addEventListener('click', () => setMode('restore', btnRestore));
  btnMagic.addEventListener('click', () => setMode('magic', btnMagic));

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

  cv.addEventListener('touchstart', startDraw, {passive:false});
  cv.addEventListener('touchmove', moveDraw, {passive:false});
  window.addEventListener('touchend', endDraw);

  function notifyDone(payload) {
    try {
      if(window.parent && window.parent !== window) {
        window.parent.postMessage(payload, "*");
        return true;
      }
    } catch(e){}
    try {
      if(window.opener && !window.opener.closed) {
        window.opener.postMessage(payload, "*");
        return true;
      }
    } catch(e){}
    return false;
  }

  async function pollAIReady(statusUrl) {
    // gentle polling so we don’t spam Railway
    for(let i=0;i<90;i++){
      try{
        const r = await fetch(statusUrl + '?t=' + Date.now());
        const j = await r.json();
        if(j.status === 'ready'){
          statusline.textContent = "AI cutout ready ✓";
          // swap in the cutout preview
          currImg.onload = () => {
            ctx.clearRect(0,0,cv.width,cv.height);
            ctx.drawImage(currImg,0,0,cv.width,cv.height);
          };
          currImg.src = `/preview/${sessionId}?t=${Date.now()}`;
          return;
        }
        if(j.status === 'failed'){
          statusline.textContent = "AI cutout failed — edit original instead.";
          return;
        }
        statusline.textContent = "AI cutout processing… (you can edit now)";
      }catch(e){
        // ignore
      }
      await new Promise(res => setTimeout(res, 800));
    }
    statusline.textContent = "AI cutout taking longer — you can still finish with original.";
  }

  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if(!f) return;

    step1.style.display='none';
    step3.style.display='block';
    btnDone.style.display='block';

    const fd = new FormData();
    fd.append('file', f);

    const keep = keepOriginalEl.checked ? 'true' : 'false';
    const detailSafe = detailSafeEl.checked ? 'true' : 'false';

    try{
      const r = await fetch(`/upload?keep_original=${keep}&detail_safe=${detailSafe}`, { method:'POST', body: fd });
      const j = await r.json();
      if(!r.ok) throw new Error(j.error || 'Upload failed');

      sessionId = j.session_id;

      // Load original immediately (fast)
      await new Promise(res => { origImg.onload=res; origImg.src=`/original/${sessionId}?t=${Date.now()}`; });
      // Start by showing orig (instant)
      ctx.clearRect(0,0,cv.width,cv.height);
      ctx.drawImage(origImg,0,0,cv.width,cv.height);

      // curr starts as orig; if AI is running, it will swap later
      currImg.onload = () => {};
      currImg.src = `/preview/${sessionId}?t=${Date.now()}`;

      updateCursorSize();

      if(!keepOriginalEl.checked){
        pollAIReady(j.status_url);
      } else {
        statusline.textContent = "Using original (AI skipped)";
      }
    }catch(err){
      alert(err.message || "Upload failed");
      location.reload();
    }
  });

  btnDone.addEventListener('click', async () => {
    btnDone.textContent="Saving…";
    btnDone.disabled=true;

    cv.toBlob(async (blob) => {
      try{
        const fd = new FormData();
        fd.append("file", blob, "edited_logo.png");
        await fetch(`/save-edit/${sessionId}`, { method:'POST', body: fd });

        const payload = {
          type: "studio-uploader:done",
          slot: SLOT,
          session_id: sessionId,
          finalize_url: `${window.location.origin}/finalize/${sessionId}`
        };

        const sent = notifyDone(payload);
        if(!sent){
          if(RETURN_TO){
            const url = new URL(RETURN_TO, window.location.origin);
            url.searchParams.set("slot", SLOT);
            url.searchParams.set("session_id", sessionId);
            url.searchParams.set("finalize_url", payload.finalize_url);
            window.location.href = url.toString();
          } else {
            btnDone.textContent="Saved ✓";
          }
        }
      }catch(e){
        alert("Save failed — try again.");
        btnDone.textContent="Done";
        btnDone.disabled=false;
      }
    }, "image/png");
  });
</script>
</body>
</html>"""
    return HTMLResponse(content=html)