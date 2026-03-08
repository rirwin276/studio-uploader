# app.py — Studio Uploader (FastAPI) — review-first editor UX
from __future__ import annotations

import os
import uuid
import time
import threading
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import cv2
import numpy as np
import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from fastapi import FastAPI, UploadFile, File, Query, Request, Form
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware


# ----------------------------
# App
# ----------------------------
app = FastAPI(title="Studio Uploader", version="2.3.0")


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
EDITOR_PX = int(os.getenv("EDITOR_PX", "1200"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(40_000_000)))  # 40MP

# PhotoRoom
PHOTOROOM_API_KEY = (os.getenv("PHOTOROOM_API_KEY") or "").strip()
PHOTOROOM_ENDPOINT = (os.getenv("PHOTOROOM_ENDPOINT") or "https://sdk.photoroom.com/v1/segment").strip()
PHOTOROOM_TIMEOUT = int(os.getenv("PHOTOROOM_TIMEOUT", "60"))
PHOTOROOM_SIZE = (os.getenv("PHOTOROOM_SIZE") or "preview").strip()  # preview | hd
PHOTOROOM_CROP = os.getenv("PHOTOROOM_CROP", "false").strip().lower() in ("1", "true", "yes", "y")
PHOTOROOM_FORMAT = (os.getenv("PHOTOROOM_FORMAT") or "png").strip()
PHOTOROOM_MAX_DIM = int(os.getenv("PHOTOROOM_MAX_DIM", "1024"))

# Cleanup tuning
AI_ALPHA_CUTOFF = int(os.getenv("AI_ALPHA_CUTOFF", "14"))
AI_KEEP_COMPONENT_MIN_AREA = int(os.getenv("AI_KEEP_COMPONENT_MIN_AREA", "36"))
AI_LOW_ALPHA_RATIO_WARN = float(os.getenv("AI_LOW_ALPHA_RATIO_WARN", "0.22"))
AI_TINY_SUBJECT_RATIO_WARN = float(os.getenv("AI_TINY_SUBJECT_RATIO_WARN", "0.08"))

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
        "orig": UPLOAD_DIR / f"{session_id}_orig.png",
        "curr": UPLOAD_DIR / f"{session_id}_curr.png",
    }


def _session_exists(session_id: str) -> bool:
    p = _paths(session_id)
    return p["orig"].exists() and p["curr"].exists()


def _save_png(img: Image.Image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG", optimize=True)


def _scale_to_fit(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if w <= max_dim and h <= max_dim:
        return img
    scale = min(max_dim / w, max_dim / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _scale_to_editor_bounds(img: Image.Image, max_dim: int, fill_ratio: float = 0.92) -> Image.Image:
    w, h = img.size
    target = max(1, int(max_dim * fill_ratio))
    scale = min(target / w, target / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _pil_open_safe(data: bytes) -> Image.Image:
    try:
        img = Image.open(BytesIO(data))
        w, h = img.size
        if (w * h) > MAX_IMAGE_PIXELS:
            raise ValueError("too_many_pixels")
        img = ImageOps.exif_transpose(img)
        return img.convert("RGBA")
    except UnidentifiedImageError:
        raise ValueError("bad_image")
    except OSError:
        raise ValueError("bad_image")


def _pil_to_png_bytes(img: Image.Image) -> bytes:
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ----------------------------
# PhotoRoom BG removal
# ----------------------------
def _photoroom_remove_bg(img: Image.Image) -> Image.Image:
    if not PHOTOROOM_API_KEY:
        raise RuntimeError("PHOTOROOM_API_KEY not set")

    work = _scale_to_fit(img.convert("RGBA"), PHOTOROOM_MAX_DIM)
    png_bytes = _pil_to_png_bytes(work)

    headers = {"x-api-key": PHOTOROOM_API_KEY}
    files = {"image_file": ("image.png", png_bytes, "image/png")}
    data = {
        "crop": "true" if PHOTOROOM_CROP else "false",
        "format": PHOTOROOM_FORMAT,
        "size": PHOTOROOM_SIZE,
    }

    r = requests.post(
        PHOTOROOM_ENDPOINT,
        headers=headers,
        files=files,
        data=data,
        timeout=PHOTOROOM_TIMEOUT,
    )

    if r.status_code != 200:
        snippet = (r.text or "")[:500]
        raise RuntimeError(f"PhotoRoom failed ({r.status_code}): {snippet}")

    return Image.open(BytesIO(r.content)).convert("RGBA")


# ----------------------------
# Cleanup / QC
# ----------------------------
def _trim_transparent_padding(img: Image.Image, alpha_threshold: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    a = img.split()[-1]
    bbox = a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    if not bbox:
        return img
    return img.crop(bbox)


def _nontransparent_bbox(img: Image.Image, alpha_threshold: int = 6) -> Optional[Tuple[int, int, int, int]]:
    img = img.convert("RGBA")
    a = img.split()[-1]
    return a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()


def _cleanup_cutout(img: Image.Image) -> Image.Image:
    rgba = np.array(img.convert("RGBA"))
    alpha = rgba[:, :, 3].copy()

    alpha[alpha < AI_ALPHA_CUTOFF] = 0

    mask = (alpha > 0).astype(np.uint8) * 255
    if mask.max() == 0:
        rgba[:, :, 3] = alpha
        return Image.fromarray(rgba, "RGBA")

    kernel3 = np.ones((3, 3), np.uint8)
    kernel5 = np.ones((5, 5), np.uint8)

    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel3, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel3, iterations=1)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    keep = np.zeros_like(mask)
    component_areas: List[int] = []

    for i in range(1, num_labels):
        area = int(stats[i, cv2.CC_STAT_AREA])
        component_areas.append(area)

    largest_area = max(component_areas) if component_areas else 0

    for i in range(1, num_labels):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area >= AI_KEEP_COMPONENT_MIN_AREA or (largest_area > 0 and area >= int(largest_area * 0.015)):
            keep[labels == i] = 255

    if keep.max() == 0:
        keep = mask

    keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, kernel5, iterations=1)
    alpha = np.where(keep > 0, alpha, 0).astype(np.uint8)
    alpha[alpha < AI_ALPHA_CUTOFF] = 0

    rgba[:, :, 3] = alpha
    return Image.fromarray(rgba, "RGBA")


def _quality_flags(img: Image.Image) -> List[str]:
    flags: List[str] = []
    rgba = np.array(img.convert("RGBA"))
    alpha = rgba[:, :, 3]

    mask = (alpha > 0).astype(np.uint8)
    total_px = mask.shape[0] * mask.shape[1]
    subject_px = int(mask.sum())

    if total_px > 0:
        subject_ratio = subject_px / total_px
        if subject_ratio < AI_TINY_SUBJECT_RATIO_WARN:
            flags.append("subject_too_small")

    nonzero = alpha[alpha > 0]
    if nonzero.size > 0:
        low_alpha_ratio = float((nonzero < 40).sum()) / float(nonzero.size)
        if low_alpha_ratio > AI_LOW_ALPHA_RATIO_WARN:
            flags.append("heavy_soft_edges")

    ys, xs = np.where(mask > 0)
    if xs.size > 0 and ys.size > 0:
        x1, x2 = xs.min(), xs.max()
        y1, y2 = ys.min(), ys.max()
        bw = max(1, x2 - x1 + 1)
        bh = max(1, y2 - y1 + 1)
        box_ratio = (bw * bh) / float(total_px)
        if box_ratio > 0.75 and subject_px / max(1, (bw * bh)) < 0.18:
            flags.append("possible_matte_box")

    return flags


def _fit_cutout_into_reference_box(
    cutout: Image.Image,
    reference_canvas: Image.Image,
    canvas_size: int,
) -> Image.Image:
    out = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))

    ref_box = _nontransparent_bbox(reference_canvas, alpha_threshold=6)
    trimmed = _trim_transparent_padding(cutout.convert("RGBA"), alpha_threshold=6)

    if not ref_box:
        tw, th = trimmed.size
        if tw <= 0 or th <= 0:
            return out
        scale = min(canvas_size / tw, canvas_size / th)
        nw = max(1, int(tw * scale))
        nh = max(1, int(th * scale))
        fitted = trimmed.resize((nw, nh), Image.LANCZOS)
        x = (canvas_size - nw) // 2
        y = (canvas_size - nh) // 2
        out.alpha_composite(fitted, (x, y))
        return out

    rx1, ry1, rx2, ry2 = ref_box
    rw = max(1, rx2 - rx1)
    rh = max(1, ry2 - ry1)

    tw, th = trimmed.size
    if tw <= 0 or th <= 0:
        return out

    scale = min(rw / tw, rh / th)
    nw = max(1, int(tw * scale))
    nh = max(1, int(th * scale))
    fitted = trimmed.resize((nw, nh), Image.LANCZOS)

    x = rx1 + (rw - nw) // 2
    y = ry1 + (rh - nh) // 2
    out.alpha_composite(fitted, (x, y))
    return out


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
# Async worker
# ----------------------------
def _bg_process_session(session_id: str):
    try:
        _sess_set(session_id, status="processing", stage="removing_background", started_at=time.time())

        p = _paths(session_id)
        if not p["orig"].exists():
            _sess_set(session_id, status="failed", stage="failed", error="orig missing")
            return

        orig_sq = Image.open(p["orig"]).convert("RGBA")

        _sess_set(session_id, stage="removing_background")
        removed = _photoroom_remove_bg(orig_sq)

        _sess_set(session_id, stage="cleaning_edges")
        cleaned = _cleanup_cutout(removed)

        _sess_set(session_id, stage="checking_quality")
        flags = _quality_flags(cleaned)

        _sess_set(session_id, stage="finishing_details")
        editor_ready = _fit_cutout_into_reference_box(cleaned, orig_sq, EDITOR_PX)

        _save_png(editor_ready, p["curr"])
        _sess_set(
            session_id,
            status="ready",
            stage="ready",
            quality_flags=flags,
            finished_at=time.time(),
        )

    except Exception as e:
        try:
            p = _paths(session_id)
            if p["orig"].exists():
                _save_png(Image.open(p["orig"]).convert("RGBA"), p["curr"])
        except Exception:
            pass

        _sess_set(
            session_id,
            status="failed",
            stage="failed",
            error=str(e),
            finished_at=time.time(),
        )


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
        if _session_exists(session_id):
            return {"status": "ready", "stage": "ready", "quality_flags": []}
        return {"status": "unknown"}
    return s


@app.get("/session-info/{session_id}")
def session_info(session_id: str):
    if not _session_exists(session_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    s = _sess_get(session_id)
    return {
        "status": s.get("status", "ready"),
        "stage": s.get("stage", "ready"),
        "quality_flags": s.get("quality_flags", []),
        "session_id": session_id,
        "preview_url": f"/preview/{session_id}",
        "original_url": f"/original/{session_id}",
        "status_url": f"/status/{session_id}",
    }


@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    keep_original: bool = Query(False),
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

    img_fit = _scale_to_editor_bounds(img, EDITOR_PX)

    canvas_orig = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
    x = (EDITOR_PX - img_fit.width) // 2
    y = (EDITOR_PX - img_fit.height) // 2
    canvas_orig.alpha_composite(img_fit, (x, y))

    _save_png(canvas_orig, p["orig"])
    _save_png(canvas_orig, p["curr"])

    _sess_set(
        session_id,
        status="ready" if keep_original else "queued",
        stage="ready" if keep_original else "queued",
        created_at=time.time(),
        quality_flags=[],
    )

    if not keep_original:
        t = threading.Thread(target=_bg_process_session, args=(session_id,), daemon=True)
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
    except ValueError:
        return JSONResponse({"error": "Bad image payload"}, status_code=400)

    final_img = _normalize_logo(browser_img, target_size=TARGET_PX)
    _save_png(final_img, p["curr"])
    _sess_set(session_id, status="ready", stage="ready", saved_at=time.time())
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
    primary_color: Optional[str],
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
    if primary_color:
        cmd += ["--primary_color", primary_color]

    print("🚀 Provision cmd:", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=900,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

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
    primary_color: Optional[str] = Form(None),
    main_session_id: Optional[str] = Form(None),
    secondary_session_id: Optional[str] = Form(None),
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
    primary_color_norm = (primary_color or "").strip() or "No preference"
    print("🎨 primary_color received:", repr(primary_color), "-> normalized:", repr(primary_color_norm))

    if not main_session_id:
        if not storefront_logo_file:
            return JSONResponse({"error": "main_session_id or storefront_logo_file required"}, status_code=400)

        try:
            main_bytes = await _read_upload_limited(storefront_logo_file, MAX_UPLOAD_BYTES)
            if not main_bytes:
                return JSONResponse({"error": "Main logo upload was empty"}, status_code=400)
            img_main = _pil_open_safe(main_bytes)
        except ValueError as e:
            if str(e) == "too_large":
                return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)
            if str(e) == "too_many_pixels":
                return JSONResponse({"error": "Image resolution too large. Please upload a smaller image."}, status_code=413)
            return JSONResponse({"error": "Main logo is not a valid image"}, status_code=400)

        img_main_fit = _scale_to_editor_bounds(img_main, EDITOR_PX)

        main_session_id = str(uuid.uuid4())
        p = _paths(main_session_id)

        canvas = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
        x = (EDITOR_PX - img_main_fit.width) // 2
        y = (EDITOR_PX - img_main_fit.height) // 2
        canvas.alpha_composite(img_main_fit, (x, y))

        _save_png(canvas, p["orig"])
        _save_png(_normalize_logo(canvas, target_size=TARGET_PX), p["curr"])

        if storefront_logo_secondary:
            try:
                if not (storefront_logo_secondary.filename or "").strip():
                    raise ValueError("empty_part")

                ct = (storefront_logo_secondary.content_type or "").lower()
                if ct and not ct.startswith("image/"):
                    raise ValueError("not_image_content_type")

                sec_bytes = await _read_upload_limited(storefront_logo_secondary, MAX_UPLOAD_BYTES)
                if not sec_bytes:
                    raise ValueError("empty_bytes")

                img_sec = _pil_open_safe(sec_bytes)
                img_sec_fit = _scale_to_editor_bounds(img_sec, EDITOR_PX)

                secondary_session_id = str(uuid.uuid4())
                sp = _paths(secondary_session_id)

                canvas2 = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
                x2 = (EDITOR_PX - img_sec_fit.width) // 2
                y2 = (EDITOR_PX - img_sec_fit.height) // 2
                canvas2.alpha_composite(img_sec_fit, (x2, y2))

                _save_png(canvas2, sp["orig"])
                _save_png(_normalize_logo(canvas2, target_size=TARGET_PX), sp["curr"])

            except Exception as e:
                print("⚠️ Secondary logo skipped:", str(e))
                secondary_session_id = None

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
        primary_color=primary_color_norm,
        created_at=time.time(),
    )

    t = threading.Thread(
        target=_run_shopify_provision_job,
        args=(
            job_id,
            storefront_name,
            storefront_handle,
            owner_customer_id,
            type_of_store,
            primary_color_norm,
            main_session_id,
            secondary_session_id,
        ),
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
    session_id: str = Query("", alias="session_id"),
):
    html = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=0" />
  <title>Logo Studio</title>
  <style>
    :root {
      --bg0: #f6f8fb;
      --bg1: #eef3f8;
      --panel: rgba(255,255,255,0.78);
      --panel-border: rgba(15,23,42,0.07);
      --text: #0f172a;
      --muted: #667085;
      --shadow: 0 26px 70px rgba(15,23,42,0.12);
      --shadow-soft: 0 12px 30px rgba(15,23,42,0.07);
    }

    * { box-sizing: border-box; }
    html, body { height: 100%; overflow: hidden; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 480px at 50% -120px, rgba(255,255,255,0.95), rgba(255,255,255,0) 60%),
        linear-gradient(180deg, var(--bg0) 0%, var(--bg1) 100%);
      display: flex;
      flex-direction: column;
    }

    .header {
      flex: 0 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 14px;
      background: rgba(255,255,255,0.70);
      backdrop-filter: blur(18px) saturate(1.12);
      border-bottom: 1px solid rgba(15,23,42,0.06);
      position: relative;
      z-index: 20;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .brand h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .brand-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 28px;
      min-width: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: rgba(15,23,42,0.05);
      color: #475569;
      border: 1px solid rgba(15,23,42,0.06);
      font-size: 12px;
      font-weight: 700;
    }

    .btn-done {
      border: none;
      border-radius: 999px;
      padding: 10px 16px;
      min-width: 82px;
      background: linear-gradient(180deg, #34d399, #10b981);
      color: white;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: -0.01em;
      box-shadow: 0 10px 22px rgba(16,185,129,0.20);
      cursor: pointer;
    }

    .btn-done:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      box-shadow: none;
    }

    .main {
      flex: 1 1 auto;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 8px;
    }

    .upload-card {
      width: min(560px, 100%);
      background: rgba(255,255,255,0.78);
      backdrop-filter: blur(18px) saturate(1.12);
      border: 1px solid var(--panel-border);
      border-radius: 30px;
      box-shadow: var(--shadow);
      padding: 16px;
    }

    .upload-box {
      position: relative;
      border-radius: 24px;
      padding: 42px 18px;
      text-align: center;
      border: 1.5px dashed rgba(15,23,42,0.12);
      background: rgba(255,255,255,0.58);
      overflow: hidden;
    }

    .upload-box input {
      position: absolute;
      inset: 0;
      opacity: 0;
      cursor: pointer;
      width: 100%;
      height: 100%;
    }

    .title {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.02em;
      margin-top: 8px;
    }

    .muted {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }

    .row {
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: center;
      align-items: center;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      height: 40px;
      padding: 0 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(15,23,42,0.08);
      font-size: 13px;
      font-weight: 600;
      color: #334155;
      box-shadow: 0 6px 18px rgba(15,23,42,0.05);
    }

    .editor-shell {
      width: min(1400px, 100%);
      height: calc(100vh - 68px);
      min-height: 0;
      display: grid;
      grid-template-rows: minmax(0,1fr) auto;
      gap: 10px;
      padding: 10px;
      background: rgba(255,255,255,0.78);
      backdrop-filter: blur(20px) saturate(1.14);
      border: 1px solid var(--panel-border);
      border-radius: 28px;
      box-shadow: 0 28px 70px rgba(15,23,42,0.14);
      overflow: hidden;
    }

    .editor-top {
      min-height: 0;
      display: grid;
      gap: 10px;
      align-items: stretch;
    }

    .editor-shell.review-mode .editor-top {
      grid-template-columns: minmax(0,1fr) 72px;
    }

    .editor-shell.edit-mode .editor-top {
      grid-template-columns: 72px minmax(0,1fr) 72px;
    }

    .canvas-panel {
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(255,255,255,0.78), rgba(255,255,255,0.58));
      border: 1px solid rgba(15,23,42,0.08);
      overflow: hidden;
      padding: 10px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.85);
    }

    .canvas-stage {
      width: min(70vw, 68vh, 840px);
      height: min(70vw, 68vh, 840px);
      max-width: 100%;
      max-height: 100%;
      aspect-ratio: 1 / 1;
      border-radius: 24px;
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(15,23,42,0.08);
      background: #ffffff;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.85);
      flex: 0 0 auto;
    }

    .canvas-stage.bg-checker {
      background-color: #fff;
      background-image:
        linear-gradient(45deg, #eceef2 25%, transparent 25%),
        linear-gradient(-45deg, #eceef2 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #eceef2 75%),
        linear-gradient(-45deg, transparent 75%, #eceef2 75%);
      background-size: 20px 20px;
      background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
    }

    .canvas-stage.bg-white {
      background: #ffffff;
      background-image: none;
    }

    .canvas-stage.bg-dark {
      background: #101828;
      background-image: none;
    }

    .canvas-viewport {
      position: absolute;
      inset: 0;
      overflow: hidden;
      touch-action: none;
      cursor: default;
    }

    .canvas-inner {
      position: absolute;
      inset: 0;
      transform-origin: center center;
      will-change: transform;
      cursor: default;
    }

    canvas {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      display: block;
      touch-action: none;
      cursor: default;
      z-index: 2;
    }

    .processing-overlay {
      position: absolute;
      inset: 0;
      display: none;
      z-index: 6;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 12px;
      text-align: center;
      background: linear-gradient(180deg, rgba(255,255,255,0.72), rgba(255,255,255,0.86));
      backdrop-filter: blur(6px);
      padding: 22px;
    }

    .processing-overlay.show { display: flex; }

    .spinner {
      width: 44px;
      height: 44px;
      border-radius: 999px;
      border: 3px solid rgba(15,23,42,0.10);
      border-top-color: rgba(15,23,42,0.80);
      animation: spin 0.9s linear infinite;
    }

    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }

    .overlay-title {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #0f172a;
    }

    .overlay-sub {
      max-width: 320px;
      font-size: 13px;
      color: #64748b;
      line-height: 1.45;
    }

    .tool-rail,
    .swatch-rail {
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      gap: 10px;
      align-self: stretch;
      padding: 10px 0;
      border-radius: 22px;
      background: rgba(255,255,255,0.60);
      border: 1px solid rgba(15,23,42,0.08);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.84);
    }

    .editor-shell.review-mode .tool-rail {
      display: none;
    }

    .rail-tool-btn {
      width: 50px;
      min-height: 38px;
      padding: 7px 4px;
      border: none;
      border-radius: 14px;
      background: transparent;
      color: #475569;
      font-size: 10px;
      line-height: 1.05;
      font-weight: 800;
      letter-spacing: -0.01em;
      text-align: center;
      cursor: pointer;
      transition: background 0.16s ease, color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
    }

    .rail-tool-btn:hover {
      background: rgba(15,23,42,0.05);
      color: #0f172a;
      transform: translateY(-1px);
    }

    .rail-tool-btn.active {
      background: #0f172a;
      color: white;
      box-shadow: 0 10px 24px rgba(15,23,42,0.16);
    }

    .mini-rail {
      display: none;
      flex-direction: column;
      gap: 6px;
      margin-top: 4px;
    }

    .mini-rail.show {
      display: flex;
    }

    .mini-rail-btn {
      width: 34px;
      height: 34px;
      border: none;
      border-radius: 999px;
      background: rgba(15,23,42,0.08);
      color: #334155;
      font-size: 15px;
      font-weight: 800;
      cursor: pointer;
    }

    .mini-rail-btn.active {
      background: rgba(15,23,42,0.92);
      color: white;
    }

    .swatch-btn {
      width: 40px;
      height: 40px;
      border-radius: 999px;
      border: 1px solid rgba(15,23,42,0.10);
      cursor: pointer;
      position: relative;
      box-shadow: 0 8px 18px rgba(15,23,42,0.08);
      transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
      background: #fff;
    }

    .swatch-btn.active {
      box-shadow: 0 0 0 3px rgba(15,23,42,0.10), 0 14px 30px rgba(15,23,42,0.12);
      border-color: rgba(15,23,42,0.24);
    }

    .swatch-btn.checker {
      background-color: #fff;
      background-image:
        linear-gradient(45deg, #e5e7eb 25%, transparent 25%),
        linear-gradient(-45deg, #e5e7eb 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #e5e7eb 75%),
        linear-gradient(-45deg, transparent 75%, #e5e7eb 75%);
      background-size: 12px 12px;
      background-position: 0 0, 0 6px, 6px -6px, -6px 0px;
    }

    .swatch-btn.white { background: #ffffff; }
    .swatch-btn.dark { background: #111827; }

    .swatch-label {
      font-size: 10px;
      color: #64748b;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin-top: -4px;
    }

    .controls-shell {
      display: grid;
      gap: 10px;
      justify-items: center;
      padding: 0 4px;
    }

    .review-controls {
      display: grid;
      gap: 10px;
      width: 100%;
      justify-items: center;
    }

    .review-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: center;
      width: 100%;
    }

    .review-btn {
      height: 40px;
      min-width: 120px;
      padding: 0 18px;
      border: none;
      border-radius: 999px;
      background: rgba(255,255,255,0.82);
      color: #334155;
      font-size: 13px;
      font-weight: 800;
      cursor: pointer;
      border: 1px solid rgba(15,23,42,0.08);
      box-shadow: var(--shadow-soft);
    }

    .review-btn.primary {
      background: #0f172a;
      color: white;
    }

    .editor-controls {
      display: none;
      gap: 10px;
      width: 100%;
      justify-items: center;
    }

    .editor-controls.show {
      display: grid;
    }

    .tool-row {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      max-width: 100%;
    }

    .tool-segment {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(15,23,42,0.08);
      box-shadow: var(--shadow-soft);
      flex-wrap: wrap;
      justify-content: center;
      max-width: 100%;
    }

    .tool-btn {
      min-width: 0;
      height: 34px;
      border: none;
      border-radius: 999px;
      background: transparent;
      color: #475569;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: -0.01em;
      cursor: pointer;
      transition: background 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
      padding: 0 10px;
    }

    .tool-btn.active {
      background: #0f172a;
      color: white;
      box-shadow: 0 10px 24px rgba(15,23,42,0.16);
    }

    .utility-row {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      max-width: 100%;
    }

    .utility-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      height: 38px;
      padding: 0 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(15,23,42,0.08);
      box-shadow: var(--shadow-soft);
      color: #334155;
      font-size: 12px;
      font-weight: 600;
    }

    .ghost-btn {
      height: 38px;
      padding: 0 12px;
      border: 1px solid rgba(15,23,42,0.08);
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      color: #334155;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: var(--shadow-soft);
    }

    .zoom-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 38px;
      padding: 0 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(15,23,42,0.08);
      box-shadow: var(--shadow-soft);
      color: #334155;
      font-size: 12px;
      font-weight: 700;
    }

    .zoom-btn {
      width: 28px;
      height: 28px;
      border: none;
      border-radius: 999px;
      background: rgba(15,23,42,0.06);
      color: #0f172a;
      font-weight: 800;
      cursor: pointer;
    }

    .zoom-readout {
      min-width: 44px;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }

    input[type=range] {
      width: min(180px, 34vw);
      accent-color: #0f172a;
    }

    .zoom-range {
      width: min(120px, 22vw);
    }

    .status-row {
      min-height: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      color: #64748b;
      font-size: 13px;
      font-weight: 600;
      text-align: center;
    }

    .warn-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(245,158,11,0.12);
      border: 1px solid rgba(245,158,11,0.24);
      color: #b45309;
      font-size: 12px;
      font-weight: 700;
    }

    .hint-banner {
      position: fixed;
      left: 50%;
      bottom: 16px;
      transform: translateX(-50%);
      z-index: 12000;
      max-width: min(92vw, 520px);
      padding: 12px 16px;
      border-radius: 18px;
      background: rgba(15,23,42,0.92);
      color: white;
      box-shadow: 0 24px 50px rgba(15,23,42,0.24);
      font-size: 13px;
      line-height: 1.4;
      display: none;
      text-align: center;
    }

    .hint-banner.show { display: block; }

    .hint-pop {
      position: fixed;
      z-index: 12001;
      max-width: 240px;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(15,23,42,0.94);
      color: white;
      box-shadow: 0 16px 32px rgba(15,23,42,0.24);
      font-size: 12px;
      line-height: 1.4;
      display: none;
      pointer-events: none;
    }

    .hint-pop.show { display: block; }

    #cursor {
      position: fixed;
      width: 32px;
      height: 32px;
      border-radius: 999px;
      border: 2px solid rgba(15,23,42,0.84);
      box-shadow: 0 0 0 2px rgba(255,255,255,0.96);
      background: rgba(255,255,255,0.08);
      pointer-events: none;
      transform: translate(-50%, -50%);
      z-index: 9999;
      display: none;
    }

    .cursor-dot {
      position: fixed;
      width: 4px;
      height: 4px;
      border-radius: 999px;
      background: rgba(15,23,42,0.92);
      box-shadow: 0 0 0 1px rgba(255,255,255,0.92);
      pointer-events: none;
      transform: translate(-50%, -50%);
      z-index: 10000;
      display: none;
    }

    .mobile-tools {
      display: none;
    }

    .mobile-magic-sheet {
      display: none;
    }

    @media (max-width: 980px) {
      .main {
        align-items: stretch;
        padding: 8px;
      }

      .editor-shell {
        width: 100%;
        height: calc(100vh - 64px);
        border-radius: 24px;
        padding: 10px;
        gap: 10px;
      }

      .editor-shell.review-mode .editor-top,
      .editor-shell.edit-mode .editor-top {
        grid-template-columns: 1fr;
      }

      .tool-rail {
        display: none !important;
      }

      .mobile-tools {
        display: flex;
        width: 100%;
      }

      .canvas-panel {
        padding: 8px;
      }

      .canvas-stage {
        width: min(92vw, 62vw, 540px);
        height: min(92vw, 62vw, 540px);
      }

      .swatch-rail {
        flex-direction: row;
        justify-content: center;
        padding: 8px 10px;
        gap: 10px;
      }

      .swatch-label {
        display: none;
      }

      .zoom-range {
        width: min(130px, 24vw);
      }

      input[type=range] {
        width: min(180px, 36vw);
      }
    }

    @media (max-width: 640px) {
      .header {
        padding: 8px 10px;
      }

      .brand h2 {
        font-size: 14px;
      }

      .btn-done {
        padding: 9px 14px;
        min-width: 74px;
      }

      .main {
        padding: 4px;
        align-items: stretch;
      }

      .upload-card {
        border-radius: 22px;
        padding: 14px;
      }

      .upload-box {
        border-radius: 20px;
        padding: 36px 16px;
      }

      .editor-shell {
        height: calc(100vh - 56px);
        gap: 6px;
        padding: 6px;
      }

      .canvas-panel {
        padding: 6px;
        border-radius: 20px;
      }

      .canvas-stage {
        width: min(94vw, 78vw, 430px);
        height: min(94vw, 78vw, 430px);
        border-radius: 18px;
      }

      .swatch-rail {
        width: 100%;
        padding: 8px;
        gap: 14px;
        border-radius: 18px;
      }

      .swatch-btn {
        width: 42px;
        height: 42px;
      }

      .controls-shell {
        gap: 6px;
        padding: 0;
      }

      .review-actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px;
        width: 100%;
      }

      .review-btn {
        width: 100%;
        min-width: 0;
        height: 38px;
        font-size: 12px;
        border-radius: 16px;
      }

      .tool-row,
      .utility-row {
        gap: 6px;
      }

      .tool-segment {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px;
        padding: 6px;
        border-radius: 18px;
        width: 100%;
      }

      .tool-btn {
        width: 100%;
        height: 38px;
        padding: 0 8px;
        font-size: 12px;
        border-radius: 14px;
      }

      .utility-row {
        display: grid;
        grid-template-columns: 1fr;
        width: 100%;
      }

      .utility-pill,
      .zoom-pill {
        width: 100%;
        justify-content: center;
        height: 36px;
        font-size: 11px;
        border-radius: 18px;
      }

      .ghost-btn {
        height: 36px;
        padding: 0 10px;
        font-size: 11px;
        border-radius: 18px;
      }

      .utility-actions {
        display: grid;
        grid-template-columns: 1fr 1fr 1fr;
        gap: 6px;
        width: 100%;
      }

      input[type=range] {
        width: min(150px, 38vw);
      }

      .zoom-range {
        width: min(118px, 28vw);
      }

      .zoom-readout {
        min-width: 38px;
        font-size: 11px;
      }

      .zoom-btn {
        width: 26px;
        height: 26px;
      }

      .status-row {
        min-height: 18px;
        font-size: 11px;
      }

      .hint-banner {
        bottom: 12px;
        padding: 10px 14px;
      }

      .mobile-magic-sheet {
        position: fixed;
        left: 12px;
        right: 12px;
        bottom: 22px;
        z-index: 13000;
        display: none;
        padding: 10px;
        border-radius: 22px;
        background: rgba(255,255,255,0.96);
        border: 1px solid rgba(15,23,42,0.08);
        box-shadow: 0 24px 50px rgba(15,23,42,0.18);
        backdrop-filter: blur(18px) saturate(1.15);
      }

      .mobile-magic-sheet.show {
        display: block;
      }

      .mobile-magic-title {
        font-size: 12px;
        font-weight: 800;
        color: #475569;
        text-align: center;
        margin-bottom: 8px;
      }

      .mobile-magic-actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }

      .mobile-magic-btn {
        height: 42px;
        border: none;
        border-radius: 16px;
        background: rgba(15,23,42,0.06);
        color: #334155;
        font-size: 13px;
        font-weight: 800;
        cursor: pointer;
      }

      .mobile-magic-btn.active {
        background: #0f172a;
        color: white;
        box-shadow: 0 10px 24px rgba(15,23,42,0.16);
      }
    }
  </style>
</head>
<body>
  <div id="cursor"></div>
  <div class="cursor-dot" id="cursorDot"></div>
  <div class="hint-banner" id="hintBanner"></div>
  <div class="hint-pop" id="hintPop"></div>

  <div class="mobile-magic-sheet" id="mobileMagicSheet">
    <div class="mobile-magic-title">Magic mode</div>
    <div class="mobile-magic-actions">
      <button class="mobile-magic-btn active" id="magicRemoveModeMobileSheet">Remove</button>
      <button class="mobile-magic-btn" id="magicRestoreModeMobileSheet">Restore</button>
    </div>
  </div>

  <div class="header">
    <div class="brand">
      <h2>Studio Uploader</h2>
      <span class="brand-pill" id="slotPill">main</span>
    </div>
    <button class="btn-done" id="btnDone" style="display:none;">Done</button>
  </div>

  <div class="main">
    <div class="upload-card" id="step1">
      <div class="upload-box">
        <input id="file" type="file" accept="image/*" />
        <div style="font-size:30px;">✨</div>
        <div class="title">Upload logo</div>
        <div class="muted" style="margin-top:6px;">We’ll remove the background, clean the edges, and let you fine-tune anything that needs a quick touch-up.</div>
      </div>

      <div class="row">
        <label class="pill"><input type="checkbox" id="keepOriginal"> Keep original</label>
        <label class="pill" style="opacity:.7;"><input type="checkbox" checked disabled> Detail-safe auto mode</label>
      </div>

      <div class="status-row" id="statusline1"></div>
    </div>

    <div class="editor-shell review-mode" id="step3" style="display:none;">
      <div class="editor-top">
        <div class="tool-rail" id="desktopToolRail">
          <button class="rail-tool-btn active tip-target" id="btnRestore" data-tip="Restore paints the original image back in.">Restore</button>
          <button class="rail-tool-btn tip-target" id="btnErase" data-tip="Erase removes parts manually.">Erase</button>
          <button class="rail-tool-btn tip-target" id="btnMagic" data-tip="Magic removes or restores one connected area.">Magic</button>
          <button class="rail-tool-btn tip-target" id="btnPan" data-tip="Pan lets you move around while zoomed in.">Pan</button>

          <div class="mini-rail" id="magicModeWrapDesktop">
            <button class="mini-rail-btn active tip-target" id="magicRemoveModeDesktop" data-tip="Magic Remove removes one connected region based on color.">−</button>
            <button class="mini-rail-btn tip-target" id="magicRestoreModeDesktop" data-tip="Magic Restore brings back one connected region from the original image.">+</button>
          </div>
        </div>

        <div class="canvas-panel">
          <div class="canvas-stage bg-checker" id="canvasContainer">
            <div class="canvas-viewport" id="canvasViewport">
              <div class="canvas-inner" id="canvasInner">
                <canvas id="cv" width="1000" height="1000"></canvas>
              </div>
            </div>

            <div class="processing-overlay" id="processingOverlay">
              <div class="spinner"></div>
              <div class="overlay-title" id="overlayTitle">Preparing your logo…</div>
              <div class="overlay-sub" id="overlaySub">This usually takes 5–10 seconds.</div>
            </div>
          </div>
        </div>

        <div class="swatch-rail">
          <div style="display:flex; flex-direction:column; align-items:center; gap:6px;">
            <button class="swatch-btn checker active tip-target" id="bgChecker" data-tip="Grid shows transparency best."></button>
            <div class="swatch-label">Grid</div>
          </div>
          <div style="display:flex; flex-direction:column; align-items:center; gap:6px;">
            <button class="swatch-btn white tip-target" id="bgWhite" data-tip="White helps you spot missing white details."></button>
            <div class="swatch-label">White</div>
          </div>
          <div style="display:flex; flex-direction:column; align-items:center; gap:6px;">
            <button class="swatch-btn dark tip-target" id="bgDark" data-tip="Dark helps you spot halos and haze."></button>
            <div class="swatch-label">Dark</div>
          </div>
        </div>
      </div>

      <div class="controls-shell">
        <div class="review-controls" id="reviewControls">
          <div class="review-actions">
            <button class="review-btn primary" id="btnApprove">Done</button>
            <button class="review-btn" id="btnEnterEdit">Edit</button>
          </div>
        </div>

        <div class="editor-controls" id="editorControls">
          <div class="tool-row mobile-tools">
            <div class="tool-segment">
              <button class="tool-btn active tip-target" id="btnRestoreMobile" data-tip="Restore paints the original image back in.">Restore</button>
              <button class="tool-btn tip-target" id="btnEraseMobile" data-tip="Erase removes parts manually.">Erase</button>
              <button class="tool-btn tip-target" id="btnMagicMobile" data-tip="Magic removes or restores one connected area.">Magic</button>
              <button class="tool-btn tip-target" id="btnPanMobile" data-tip="Pan lets you move around while zoomed in.">Pan</button>
            </div>
          </div>

          <div class="utility-row">
            <div class="utility-pill">
              <span>Brush</span>
              <input type="range" id="brushSize" min="8" max="140" value="44">
            </div>

            <div class="zoom-pill">
              <button class="zoom-btn" id="zoomOut" type="button">−</button>
              <span class="zoom-readout" id="zoomReadout">100%</span>
              <input class="zoom-range" type="range" id="zoomSlider" min="100" max="600" step="10" value="100">
              <button class="zoom-btn" id="zoomIn" type="button">+</button>
            </div>

            <div class="utility-actions">
              <button class="ghost-btn tip-target" id="btnFit" data-tip="Fit puts the logo back to centered view.">Fit</button>
              <button class="ghost-btn tip-target" id="btnUndo" data-tip="Undo your last edit.">Undo</button>
              <button class="ghost-btn tip-target" id="btnRestart" data-tip="Reset starts over with the current uploaded image or reloads the saved session.">Reset</button>
            </div>
          </div>
        </div>

        <div class="status-row" id="statusline"></div>
      </div>
    </div>
  </div>

<script>
  const API_BASE = window.location.origin;

  let sessionId = null;
  let mode = 'restore';
  let magicMode = 'remove';
  let isDown = false;
  let lastX = 0;
  let lastY = 0;
  let history = [];
  let aiReady = false;
  let stageCycleTimer = null;
  let qualityFlags = [];
  let hintTimer = null;
  let introBannerTimer = null;
  let editorUnlocked = false;

  let viewScale = 1;
  let panX = 0;
  let panY = 0;
  let isPanning = false;
  let panStartX = 0;
  let panStartY = 0;
  let startPanX = 0;
  let startPanY = 0;

  const fileEl = document.getElementById('file');
  const keepOriginalEl = document.getElementById('keepOriginal');

  const step1 = document.getElementById('step1');
  const step3 = document.getElementById('step3');

  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d', { willReadFrequently: true });

  const offCanvas = document.createElement('canvas');
  offCanvas.width = 1000;
  offCanvas.height = 1000;
  const offCtx = offCanvas.getContext('2d');

  const origBaseCanvas = document.createElement('canvas');
  origBaseCanvas.width = 1000;
  origBaseCanvas.height = 1000;
  const origBaseCtx = origBaseCanvas.getContext('2d', { willReadFrequently: true });

  const origImg = new Image();
  const currImg = new Image();

  const btnDone = document.getElementById('btnDone');
  const btnApprove = document.getElementById('btnApprove');
  const btnEnterEdit = document.getElementById('btnEnterEdit');

  const reviewControls = document.getElementById('reviewControls');
  const editorControls = document.getElementById('editorControls');
  const desktopToolRail = document.getElementById('desktopToolRail');

  const btnErase = document.getElementById('btnErase');
  const btnRestore = document.getElementById('btnRestore');
  const btnMagic = document.getElementById('btnMagic');
  const btnPan = document.getElementById('btnPan');

  const btnEraseMobile = document.getElementById('btnEraseMobile');
  const btnRestoreMobile = document.getElementById('btnRestoreMobile');
  const btnMagicMobile = document.getElementById('btnMagicMobile');
  const btnPanMobile = document.getElementById('btnPanMobile');

  const brushSlider = document.getElementById('brushSize');

  const magicModeWrapDesktop = document.getElementById('magicModeWrapDesktop');
  const magicRemoveModeDesktop = document.getElementById('magicRemoveModeDesktop');
  const magicRestoreModeDesktop = document.getElementById('magicRestoreModeDesktop');

  const mobileMagicSheet = document.getElementById('mobileMagicSheet');
  const magicRemoveModeMobileSheet = document.getElementById('magicRemoveModeMobileSheet');
  const magicRestoreModeMobileSheet = document.getElementById('magicRestoreModeMobileSheet');

  const cursor = document.getElementById('cursor');
  const cursorDot = document.getElementById('cursorDot');

  const canvasContainer = document.getElementById('canvasContainer');
  const canvasViewport = document.getElementById('canvasViewport');
  const canvasInner = document.getElementById('canvasInner');
  const processingOverlay = document.getElementById('processingOverlay');
  const overlayTitle = document.getElementById('overlayTitle');
  const overlaySub = document.getElementById('overlaySub');

  const statusline = document.getElementById('statusline');
  const statusline1 = document.getElementById('statusline1');

  const hintBanner = document.getElementById('hintBanner');
  const hintPop = document.getElementById('hintPop');

  const bgChecker = document.getElementById('bgChecker');
  const bgWhite = document.getElementById('bgWhite');
  const bgDark = document.getElementById('bgDark');

  const zoomSlider = document.getElementById('zoomSlider');
  const zoomReadout = document.getElementById('zoomReadout');
  const zoomIn = document.getElementById('zoomIn');
  const zoomOut = document.getElementById('zoomOut');
  const btnFit = document.getElementById('btnFit');
  const slotPill = document.getElementById('slotPill');

  const params = new URLSearchParams(window.location.search);
  const SLOT = params.get('slot') || 'main';
  const RETURN_TO = params.get('return_to') || '';
  const EXISTING_SESSION_ID = (params.get('session_id') || '').trim();

  slotPill.textContent = SLOT;

  const STAGE_LABELS = {
    queued: "Uploading image…",
    processing: "Preparing your logo…",
    removing_background: "Removing background…",
    cleaning_edges: "Cleaning edges…",
    checking_quality: "Checking quality…",
    finishing_details: "Finishing details…",
    ready: "AI cutout ready ✓",
    failed: "AI cutout failed — edit original instead."
  };

  const CYCLE_MESSAGES = [
    "Uploading image…",
    "Removing background…",
    "Cleaning edges…",
    "Checking quality…",
    "Finishing details…"
  ];

  fileEl.addEventListener('click', () => { fileEl.value = ""; });

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function isMobile() {
    return window.innerWidth <= 980;
  }

  function setCanvasCursor(cursorValue) {
    cv.style.cursor = cursorValue;
    canvasViewport.style.cursor = cursorValue;
    canvasInner.style.cursor = cursorValue;
  }

  function syncTopDoneButton() {
    btnDone.style.display = 'inline-flex';
    btnDone.textContent = 'Done';
  }

  function enterReviewMode() {
    editorUnlocked = false;
    step3.classList.remove('edit-mode');
    step3.classList.add('review-mode');
    reviewControls.style.display = 'grid';
    editorControls.classList.remove('show');
    desktopToolRail.style.display = 'none';
    mobileMagicSheet.classList.remove('show');
    fitView();
    setCanvasCursor('default');
    showBanner("Review your image first. If it looks good, hit Done. If not, tap Edit.", 4);
  }

  function enterEditMode() {
    editorUnlocked = true;
    step3.classList.remove('review-mode');
    step3.classList.add('edit-mode');
    reviewControls.style.display = 'none';
    editorControls.classList.add('show');

    if (!isMobile()) {
      desktopToolRail.style.display = 'flex';
    } else {
      desktopToolRail.style.display = 'none';
    }

    updateMagicSheetVisibility();
    updateCursorSize();
    showBanner("Editor unlocked. Make any fixes, then hit Done.", 4);
  }

  function updateViewportTransform() {
    canvasInner.style.transform = `translate(${panX}px, ${panY}px) scale(${viewScale})`;
    zoomSlider.value = Math.round(viewScale * 100);
    zoomReadout.textContent = `${Math.round(viewScale * 100)}%`;
    updateCursorSize();
  }

  function fitView() {
    viewScale = 1;
    panX = 0;
    panY = 0;
    updateViewportTransform();
  }

  function setZoom(nextScale) {
    const oldScale = viewScale;
    viewScale = clamp(nextScale, 1, 6);

    if (oldScale === viewScale) {
      updateViewportTransform();
      return;
    }

    const stageRect = canvasViewport.getBoundingClientRect();
    const maxPanX = ((viewScale - 1) * stageRect.width) / 2;
    const maxPanY = ((viewScale - 1) * stageRect.height) / 2;

    panX = clamp(panX, -maxPanX, maxPanX);
    panY = clamp(panY, -maxPanY, maxPanY);

    updateViewportTransform();
  }

  zoomIn.addEventListener('click', () => {
    if (!editorUnlocked) return;
    setZoom(viewScale + 0.25);
  });

  zoomOut.addEventListener('click', () => {
    if (!editorUnlocked) return;
    setZoom(viewScale - 0.25);
  });

  zoomSlider.addEventListener('input', () => {
    if (!editorUnlocked) {
      fitView();
      return;
    }
    setZoom(parseInt(zoomSlider.value, 10) / 100);
  });

  btnFit.addEventListener('click', () => {
    if (!editorUnlocked) return;
    fitView();
  });

  canvasViewport.addEventListener('wheel', (e) => {
    if (!editorUnlocked) return;
    e.preventDefault();
    const delta = e.deltaY < 0 ? 0.15 : -0.15;
    setZoom(viewScale + delta);
  }, { passive: false });

  function setBgMode(mode) {
    canvasContainer.classList.remove('bg-checker', 'bg-white', 'bg-dark');
    bgChecker.classList.remove('active');
    bgWhite.classList.remove('active');
    bgDark.classList.remove('active');

    if (mode === 'checker') {
      canvasContainer.classList.add('bg-checker');
      bgChecker.classList.add('active');
    } else if (mode === 'white') {
      canvasContainer.classList.add('bg-white');
      bgWhite.classList.add('active');
    } else if (mode === 'dark') {
      canvasContainer.classList.add('bg-dark');
      bgDark.classList.add('active');
    }
  }

  bgChecker.addEventListener('click', () => setBgMode('checker'));
  bgWhite.addEventListener('click', () => setBgMode('white'));
  bgDark.addEventListener('click', () => setBgMode('dark'));

  function showOverlay(title, sub = "This usually takes 5–10 seconds.") {
    processingOverlay.classList.add('show');
    overlayTitle.textContent = title || "Preparing your logo…";
    overlaySub.textContent = sub;
  }

  function hideOverlay() {
    processingOverlay.classList.remove('show');
  }

  function startStageCycle() {
    stopStageCycle();
    let i = 0;
    showOverlay(CYCLE_MESSAGES[0]);
    stageCycleTimer = setInterval(() => {
      if (aiReady) return;
      i = (i + 1) % CYCLE_MESSAGES.length;
      overlayTitle.textContent = CYCLE_MESSAGES[i];
    }, 1400);
  }

  function stopStageCycle() {
    if (stageCycleTimer) {
      clearInterval(stageCycleTimer);
      stageCycleTimer = null;
    }
  }

  function saveState() {
    if (history.length > 12) history.shift();
    history.push(ctx.getImageData(0, 0, cv.width, cv.height));
  }

  document.getElementById('btnUndo').addEventListener('click', () => {
    if (!editorUnlocked) return;
    if (history.length > 0) ctx.putImageData(history.pop(), 0, 0);
  });

  document.getElementById('btnRestart').addEventListener('click', async () => {
    if (!editorUnlocked) return;
    if (sessionId) {
      await loadExistingSession(sessionId, false);
      enterEditMode();
    } else {
      location.reload();
    }
  });

  function updateCursorSize() {
    if (!editorUnlocked) {
      cursor.style.display = 'none';
      cursorDot.style.display = 'none';
      setCanvasCursor('default');
      return;
    }

    if (mode === 'magic') {
      cursor.style.display = 'none';
      cursorDot.style.display = 'none';
      setCanvasCursor('crosshair');
      return;
    }

    if (mode === 'pan') {
      cursor.style.display = 'none';
      cursorDot.style.display = 'none';
      setCanvasCursor('grab');
      return;
    }

    const rect = cv.getBoundingClientRect();
    const ratio = rect.width / 1000;
    const visualSize = brushSlider.value * ratio;
    cursor.style.width = visualSize + 'px';
    cursor.style.height = visualSize + 'px';
    setCanvasCursor('none');
  }

  brushSlider.addEventListener('input', updateCursorSize);

  canvasContainer.addEventListener('mousemove', (e) => {
    if (!editorUnlocked) return;
    if (mode !== 'magic' && mode !== 'pan') {
      cursor.style.display = 'block';
      cursorDot.style.display = 'block';
      cursor.style.left = e.clientX + 'px';
      cursor.style.top = e.clientY + 'px';
      cursorDot.style.left = e.clientX + 'px';
      cursorDot.style.top = e.clientY + 'px';
    }
  });

  canvasContainer.addEventListener('mouseleave', () => {
    cursor.style.display = 'none';
    cursorDot.style.display = 'none';
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

  function colorDistance(data, pos, r, g, b) {
    return Math.abs(data[pos] - r) + Math.abs(data[pos + 1] - g) + Math.abs(data[pos + 2] - b);
  }

  function magicRemove(startX, startY) {
    startX = Math.floor(startX);
    startY = Math.floor(startY);

    const imgData = ctx.getImageData(0, 0, cv.width, cv.height);
    const data = imgData.data;
    const w = cv.width;
    const h = cv.height;

    const startPos = (startY * w + startX) * 4;
    const sa = data[startPos + 3];
    if (sa === 0) return;

    const sr = data[startPos];
    const sg = data[startPos + 1];
    const sb = data[startPos + 2];

    const stack = [startX, startY];
    const seen = new Uint8Array(w * h);
    seen[startY * w + startX] = 1;

    const tolerance = 55;

    while (stack.length) {
      const y = stack.pop();
      const x = stack.pop();
      const pos = (y * w + x) * 4;
      data[pos + 3] = 0;

      const nbs = [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]];
      for (let i = 0; i < nbs.length; i++) {
        const nx = nbs[i][0];
        const ny = nbs[i][1];
        if (nx >= 0 && nx < w && ny >= 0 && ny < h) {
          const idx = ny * w + nx;
          if (!seen[idx]) {
            seen[idx] = 1;
            const p2 = idx * 4;
            if (data[p2 + 3] > 0) {
              const dist = colorDistance(data, p2, sr, sg, sb);
              if (dist <= tolerance) stack.push(nx, ny);
            }
          }
        }
      }
    }

    ctx.putImageData(imgData, 0, 0);
  }

  function magicRestore(startX, startY) {
    startX = Math.floor(startX);
    startY = Math.floor(startY);

    const curr = ctx.getImageData(0, 0, cv.width, cv.height);
    const currData = curr.data;

    const orig = origBaseCtx.getImageData(0, 0, cv.width, cv.height);
    const origData = orig.data;

    const w = cv.width;
    const h = cv.height;
    const startPos = (startY * w + startX) * 4;

    if (origData[startPos + 3] === 0) return;

    const sr = origData[startPos];
    const sg = origData[startPos + 1];
    const sb = origData[startPos + 2];

    const stack = [startX, startY];
    const seen = new Uint8Array(w * h);
    seen[startY * w + startX] = 1;

    const tolerance = 55;

    while (stack.length) {
      const y = stack.pop();
      const x = stack.pop();
      const pos = (y * w + x) * 4;

      currData[pos] = origData[pos];
      currData[pos + 1] = origData[pos + 1];
      currData[pos + 2] = origData[pos + 2];
      currData[pos + 3] = origData[pos + 3];

      const nbs = [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]];
      for (let i = 0; i < nbs.length; i++) {
        const nx = nbs[i][0];
        const ny = nbs[i][1];
        if (nx >= 0 && nx < w && ny >= 0 && ny < h) {
          const idx = ny * w + nx;
          if (!seen[idx]) {
            seen[idx] = 1;
            const p2 = idx * 4;
            if (origData[p2 + 3] > 0) {
              const dist = colorDistance(origData, p2, sr, sg, sb);
              if (dist <= tolerance) stack.push(nx, ny);
            }
          }
        }
      }
    }

    ctx.putImageData(curr, 0, 0);
  }

  function drawBrush(x, y) {
    const bSize = parseInt(brushSlider.value, 10);

    offCtx.globalCompositeOperation = 'source-over';
    offCtx.clearRect(0, 0, 1000, 1000);
    offCtx.lineWidth = bSize;
    offCtx.lineCap = 'round';
    offCtx.lineJoin = 'round';
    offCtx.strokeStyle = 'black';

    offCtx.beginPath();
    offCtx.moveTo(lastX, lastY);
    offCtx.lineTo(x, y);
    offCtx.stroke();

    if (mode === 'remove') {
      ctx.globalCompositeOperation = 'destination-out';
      ctx.drawImage(offCanvas, 0, 0);
    } else if (mode === 'restore') {
      offCtx.globalCompositeOperation = 'source-in';
      offCtx.drawImage(origBaseCanvas, 0, 0, 1000, 1000);
      ctx.globalCompositeOperation = 'source-over';
      ctx.drawImage(offCanvas, 0, 0);
    }

    lastX = x;
    lastY = y;
  }

  function updateMagicModeUI() {
    magicRemoveModeDesktop.classList.toggle('active', magicMode === 'remove');
    magicRestoreModeDesktop.classList.toggle('active', magicMode === 'restore');
    magicRemoveModeMobileSheet.classList.toggle('active', magicMode === 'remove');
    magicRestoreModeMobileSheet.classList.toggle('active', magicMode === 'restore');
  }

  function updateMagicSheetVisibility() {
    if (window.innerWidth <= 640 && editorUnlocked && mode === 'magic') {
      mobileMagicSheet.classList.add('show');
    } else {
      mobileMagicSheet.classList.remove('show');
    }
  }

  function setMode(m) {
    if (!editorUnlocked) return;

    mode = m;

    [
      btnErase, btnRestore, btnMagic, btnPan,
      btnEraseMobile, btnRestoreMobile, btnMagicMobile, btnPanMobile
    ].forEach(btn => btn.classList.remove('active'));

    if (mode === 'remove') {
      btnErase.classList.add('active');
      btnEraseMobile.classList.add('active');
    } else if (mode === 'restore') {
      btnRestore.classList.add('active');
      btnRestoreMobile.classList.add('active');
    } else if (mode === 'magic') {
      btnMagic.classList.add('active');
      btnMagicMobile.classList.add('active');
    } else if (mode === 'pan') {
      btnPan.classList.add('active');
      btnPanMobile.classList.add('active');
    }

    if (mode === 'magic') {
      magicModeWrapDesktop.classList.add('show');
    } else {
      magicModeWrapDesktop.classList.remove('show');
    }

    updateMagicSheetVisibility();
    updateCursorSize();
  }

  magicRemoveModeDesktop.addEventListener('click', () => {
    magicMode = 'remove';
    updateMagicModeUI();
  });

  magicRestoreModeDesktop.addEventListener('click', () => {
    magicMode = 'restore';
    updateMagicModeUI();
  });

  magicRemoveModeMobileSheet.addEventListener('click', () => {
    magicMode = 'remove';
    updateMagicModeUI();
  });

  magicRestoreModeMobileSheet.addEventListener('click', () => {
    magicMode = 'restore';
    updateMagicModeUI();
  });

  btnErase.addEventListener('click', () => setMode('remove'));
  btnRestore.addEventListener('click', () => setMode('restore'));
  btnMagic.addEventListener('click', () => setMode('magic'));
  btnPan.addEventListener('click', () => setMode('pan'));

  btnEraseMobile.addEventListener('click', () => setMode('remove'));
  btnRestoreMobile.addEventListener('click', () => setMode('restore'));
  btnMagicMobile.addEventListener('click', () => setMode('magic'));
  btnPanMobile.addEventListener('click', () => setMode('pan'));

  btnEnterEdit.addEventListener('click', () => {
    enterEditMode();
  });

  btnApprove.addEventListener('click', async () => {
    btnDone.click();
  });

  function beginPan(evt) {
    isPanning = true;
    const pointX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    const pointY = evt.touches ? evt.touches[0].clientY : evt.clientY;
    panStartX = pointX;
    panStartY = pointY;
    startPanX = panX;
    startPanY = panY;
    setCanvasCursor('grabbing');
  }

  function movePan(evt) {
    if (!isPanning) return;

    const pointX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    const pointY = evt.touches ? evt.touches[0].clientY : evt.clientY;
    const dx = pointX - panStartX;
    const dy = pointY - panStartY;

    const stageRect = canvasViewport.getBoundingClientRect();
    const maxPanX = ((viewScale - 1) * stageRect.width) / 2;
    const maxPanY = ((viewScale - 1) * stageRect.height) / 2;

    panX = clamp(startPanX + dx, -maxPanX, maxPanX);
    panY = clamp(startPanY + dy, -maxPanY, maxPanY);
    updateViewportTransform();

    if (evt.cancelable) evt.preventDefault();
  }

  function endPan() {
    isPanning = false;
    if (mode === 'pan') setCanvasCursor('grab');
  }

  function startDraw(e) {
    if (!editorUnlocked) return;
    if (!aiReady && !keepOriginalEl.checked) return;

    if (mode === 'pan') {
      beginPan(e);
      if (e.cancelable) e.preventDefault();
      return;
    }

    saveState();
    isDown = true;
    const c = getCoords(e);

    if (mode === 'magic') {
      if (magicMode === 'remove') {
        magicRemove(c.x, c.y);
      } else {
        magicRestore(c.x, c.y);
      }
      isDown = false;
    } else {
      lastX = c.x;
      lastY = c.y;
      drawBrush(c.x, c.y);
    }

    if (e.cancelable) e.preventDefault();
  }

  function moveDraw(e) {
    if (!editorUnlocked) return;

    if (mode === 'pan') {
      movePan(e);
      return;
    }

    if (!isDown || mode === 'magic') return;

    const c = getCoords(e);
    drawBrush(c.x, c.y);

    if (e.touches) {
      cursor.style.display = 'block';
      cursorDot.style.display = 'block';
      cursor.style.left = e.touches[0].clientX + 'px';
      cursor.style.top = e.touches[0].clientY + 'px';
      cursorDot.style.left = e.touches[0].clientX + 'px';
      cursorDot.style.top = e.touches[0].clientY + 'px';
    }

    if (e.cancelable) e.preventDefault();
  }

  function endDraw() {
    isDown = false;
    endPan();
  }

  cv.addEventListener('mousedown', startDraw);
  cv.addEventListener('mousemove', moveDraw);
  window.addEventListener('mouseup', endDraw);

  cv.addEventListener('touchstart', startDraw, { passive: false });
  cv.addEventListener('touchmove', moveDraw, { passive: false });
  window.addEventListener('touchend', endDraw);

  function notifyDone(payload) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(payload, "*");
        return true;
      }
    } catch (e) {}
    try {
      if (window.opener && !window.opener.closed) {
        window.opener.postMessage(payload, "*");
        return true;
      }
    } catch (e) {}
    return false;
  }

  function renderQualityFlags(flags) {
    qualityFlags = Array.isArray(flags) ? flags : [];
    const warnings = [];

    if (qualityFlags.includes("possible_matte_box")) warnings.push("Possible edge artifact detected");
    if (qualityFlags.includes("heavy_soft_edges")) warnings.push("Soft edges detected — review if needed");
    if (qualityFlags.includes("subject_too_small")) warnings.push("Logo looks small — review spacing");

    if (warnings.length) {
      statusline.innerHTML = warnings.map(w => `<span class="warn-badge">⚠ ${w}</span>`).join(" ");
    } else {
      statusline.textContent = "AI cutout ready ✓";
    }
  }

  function showBanner(message, seconds = 5) {
    hintBanner.textContent = message;
    hintBanner.classList.add('show');
    if (introBannerTimer) clearTimeout(introBannerTimer);
    introBannerTimer = setTimeout(() => {
      hintBanner.classList.remove('show');
    }, seconds * 1000);
  }

  function hideHintPop() {
    hintPop.classList.remove('show');
  }

  function showHintPop(target, text, seconds = 5) {
    if (!text) return;
    hintPop.textContent = text;
    const rect = target.getBoundingClientRect();
    const popW = 230;
    const x = Math.max(10, Math.min(window.innerWidth - popW - 10, rect.left + (rect.width / 2) - (popW / 2)));
    const y = rect.top - 62 > 10 ? rect.top - 62 : rect.bottom + 10;

    hintPop.style.left = x + 'px';
    hintPop.style.top = y + 'px';
    hintPop.classList.add('show');

    if (hintTimer) clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      hideHintPop();
    }, seconds * 1000);
  }

  function bindHints() {
    document.querySelectorAll('.tip-target').forEach(el => {
      const tip = el.getAttribute('data-tip') || '';
      el.addEventListener('mouseenter', () => showHintPop(el, tip, 5));
      el.addEventListener('focus', () => showHintPop(el, tip, 5));
      el.addEventListener('click', () => showHintPop(el, tip, 4));
      el.addEventListener('touchstart', () => showHintPop(el, tip, 4), { passive: true });
    });
  }

  async function pollAIReady(statusUrl) {
    for (let i = 0; i < 120; i++) {
      try {
        const r = await fetch(statusUrl + '?t=' + Date.now(), { cache: "no-store" });
        const j = await r.json();

        const stage = j.stage || j.status || "processing";
        if (!aiReady) {
          overlayTitle.textContent = STAGE_LABELS[stage] || "Preparing your logo…";
        }

        if (j.status === 'ready') {
          aiReady = true;
          stopStageCycle();

          currImg.onload = () => {
            ctx.clearRect(0, 0, cv.width, cv.height);
            ctx.drawImage(currImg, 0, 0, cv.width, cv.height);
            hideOverlay();
            renderQualityFlags(j.quality_flags || []);
            fitView();
            enterReviewMode();
          };

          currImg.src = `${API_BASE}/preview/${sessionId}?t=${Date.now()}`;
          return;
        }

        if (j.status === 'failed') {
          aiReady = true;
          stopStageCycle();
          overlayTitle.textContent = "AI cutout failed";
          overlaySub.textContent = "You can still use the original image and make manual edits.";
          setTimeout(() => hideOverlay(), 1400);
          statusline.textContent = "AI cutout failed — edit original instead.";
          enterReviewMode();
          return;
        }
      } catch (e) {}

      await new Promise(res => setTimeout(res, 800));
    }

    stopStageCycle();
    overlayTitle.textContent = "Still processing…";
    overlaySub.textContent = "This is taking longer than usual. You can wait a bit longer or restart.";
    statusline.textContent = "AI cutout taking longer than usual.";
  }

  async function loadExistingSession(existingId, showSavedBanner = true) {
    const r = await fetch(`${API_BASE}/session-info/${existingId}?t=${Date.now()}`, { cache: "no-store" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Saved session not found");

    sessionId = existingId;
    aiReady = true;
    qualityFlags = Array.isArray(j.quality_flags) ? j.quality_flags : [];

    step1.style.display = 'none';
    step3.style.display = 'grid';
    btnDone.style.display = 'inline-flex';
    hideOverlay();

    await new Promise(res => {
      origImg.onload = res;
      origImg.src = `${API_BASE}/original/${sessionId}?t=${Date.now()}`;
    });

    origBaseCtx.clearRect(0, 0, 1000, 1000);
    origBaseCtx.drawImage(origImg, 0, 0, 1000, 1000);

    await new Promise(res => {
      currImg.onload = res;
      currImg.src = `${API_BASE}/preview/${sessionId}?t=${Date.now()}`;
    });

    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.drawImage(currImg, 0, 0, cv.width, cv.height);

    renderQualityFlags(qualityFlags);
    fitView();
    enterReviewMode();

    if (showSavedBanner) {
      showBanner("Loaded your saved image. Review it, or tap Edit if you want to make changes.", 5);
    }
  }

  async function handlePickedFile() {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;

    statusline1.textContent = "Uploading…";

    step1.style.display = 'none';
    step3.style.display = 'grid';
    btnDone.style.display = 'inline-flex';

    const fd = new FormData();
    fd.append('file', f);

    const keep = keepOriginalEl.checked ? 'true' : 'false';

    try {
      const r = await fetch(`${API_BASE}/upload?keep_original=${keep}`, {
        method: 'POST',
        body: fd,
        cache: "no-store"
      });

      const ct = (r.headers.get("content-type") || "").toLowerCase();
      if (!ct.includes("application/json")) {
        const text = await r.text();
        throw new Error("Upload failed (non-JSON response): " + text.slice(0, 200));
      }

      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'Upload failed');

      sessionId = j.session_id;
      aiReady = keepOriginalEl.checked;
      qualityFlags = [];

      await new Promise(res => {
        origImg.onload = res;
        origImg.src = `${API_BASE}/original/${sessionId}?t=${Date.now()}`;
      });

      origBaseCtx.clearRect(0, 0, 1000, 1000);
      origBaseCtx.drawImage(origImg, 0, 0, 1000, 1000);

      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.drawImage(origImg, 0, 0, cv.width, cv.height);

      fitView();

      if (!keepOriginalEl.checked) {
        showOverlay("Uploading image…", "Please wait while we prepare your logo.");
        startStageCycle();
        statusline.textContent = "Preparing your logo…";
        pollAIReady(j.status_url);
      } else {
        hideOverlay();
        statusline.textContent = "Using original";
        enterReviewMode();
      }

      updateCursorSize();
    } catch (err) {
      alert(err.message || "Upload failed");
      location.reload();
    }
  }

  fileEl.addEventListener('change', handlePickedFile);
  fileEl.addEventListener('input', handlePickedFile);

  btnDone.addEventListener('click', async () => {
    btnDone.textContent = "Saving…";
    btnDone.disabled = true;

    cv.toBlob(async (blob) => {
      try {
        const fd = new FormData();
        fd.append("file", blob, "edited_logo.png");
        await fetch(`${API_BASE}/save-edit/${sessionId}`, {
          method: 'POST',
          body: fd,
          cache: "no-store"
        });

        const payload = {
          type: "studio-uploader:done",
          slot: SLOT,
          session_id: sessionId,
          finalize_url: `${API_BASE}/finalize/${sessionId}`
        };

        const sent = notifyDone(payload);
        if (!sent) {
          if (RETURN_TO) {
            const url = new URL(RETURN_TO, API_BASE);
            url.searchParams.set("slot", SLOT);
            url.searchParams.set("session_id", sessionId);
            url.searchParams.set("finalize_url", payload.finalize_url);
            window.location.href = url.toString();
          } else {
            btnDone.textContent = "Saved ✓";
          }
        }
      } catch (e) {
        alert("Save failed — try again.");
        btnDone.textContent = "Done";
        btnDone.disabled = false;
      }
    }, "image/png");
  });

  btnApprove.addEventListener('click', () => btnDone.click());
  btnEnterEdit.addEventListener('click', () => enterEditMode());

  setBgMode('checker');
  fitView();
  updateMagicModeUI();
  bindHints();
  syncTopDoneButton();
  enterReviewMode();

  window.addEventListener('resize', () => {
    updateViewportTransform();
    updateMagicSheetVisibility();
    if (!editorUnlocked) {
      step3.classList.add('review-mode');
      step3.classList.remove('edit-mode');
    } else {
      step3.classList.add('edit-mode');
      step3.classList.remove('review-mode');
      if (!isMobile()) {
        desktopToolRail.style.display = 'flex';
      } else {
        desktopToolRail.style.display = 'none';
      }
    }
  });

  (async function boot() {
    try {
      if (EXISTING_SESSION_ID) {
        await loadExistingSession(EXISTING_SESSION_ID, true);
      }
    } catch (e) {
      console.warn("Failed to load existing session:", e);
    }
  })();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)