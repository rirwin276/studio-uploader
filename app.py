# app.py — Studio Uploader (FastAPI) — fixed finalize/save flow + stronger guidance
from __future__ import annotations

import os
import uuid
import time
import threading
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

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
app = FastAPI(title="Studio Uploader", version="4.1.0")


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
PREVIEW_PX = int(os.getenv("PREVIEW_PX", "1400"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(40_000_000)))  # 40MP

# PhotoRoom
PHOTOROOM_API_KEY = (os.getenv("PHOTOROOM_API_KEY") or "").strip()
PHOTOROOM_ENDPOINT = (os.getenv("PHOTOROOM_ENDPOINT") or "https://sdk.photoroom.com/v1/segment").strip()
PHOTOROOM_TIMEOUT = int(os.getenv("PHOTOROOM_TIMEOUT", "60"))
PHOTOROOM_SIZE = (os.getenv("PHOTOROOM_SIZE") or "hd").strip()  # preview | hd
PHOTOROOM_CROP = os.getenv("PHOTOROOM_CROP", "false").strip().lower() in ("1", "true", "yes", "y")
PHOTOROOM_FORMAT = (os.getenv("PHOTOROOM_FORMAT") or "png").strip()
PHOTOROOM_MAX_DIM = int(os.getenv("PHOTOROOM_MAX_DIM", "2000"))

# Cleanup
AI_ALPHA_CUTOFF = int(os.getenv("AI_ALPHA_CUTOFF", "8"))
AI_LOW_ALPHA_RATIO_WARN = float(os.getenv("AI_LOW_ALPHA_RATIO_WARN", "0.22"))
AI_TINY_SUBJECT_RATIO_WARN = float(os.getenv("AI_TINY_SUBJECT_RATIO_WARN", "0.08"))
DEHALO_ENABLED = os.getenv("DEHALO_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y")
DEHALO_SOLID_ALPHA = int(os.getenv("DEHALO_SOLID_ALPHA", "210"))

# True upscale
UPSCALE_ENABLED = os.getenv("UPSCALE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
UPSCALE_ENGINE = (os.getenv("UPSCALE_ENGINE") or "realesrgan").strip().lower()
UPSCALE_TARGET_PX = int(os.getenv("UPSCALE_TARGET_PX", str(TARGET_PX)))

# Real-ESRGAN external runner
REALESRGAN_BIN = (os.getenv("REALESRGAN_BIN") or "").strip()
REALESRGAN_MODEL = (os.getenv("REALESRGAN_MODEL") or "realesrgan-x4plus").strip()
REALESRGAN_SCALE = int(os.getenv("REALESRGAN_SCALE", "4"))
REALESRGAN_TILE = int(os.getenv("REALESRGAN_TILE", "0"))
REALESRGAN_TIMEOUT = int(os.getenv("REALESRGAN_TIMEOUT", "180"))

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
# Helpers: file read
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
# Helpers: paths
# ----------------------------
def _paths(session_id: str) -> Dict[str, Path]:
    return {
        "orig_master": UPLOAD_DIR / f"{session_id}_orig_master.png",
        "preview": UPLOAD_DIR / f"{session_id}_preview.png",
        "curr": UPLOAD_DIR / f"{session_id}_curr.png",
        "final": UPLOAD_DIR / f"{session_id}_final.png",
    }


def _session_exists(session_id: str) -> bool:
    p = _paths(session_id)
    needed = ["orig_master", "preview", "curr"]
    return all(p[k].exists() for k in needed)


# ----------------------------
# Helpers: image IO
# ----------------------------
def _save_png(img: Image.Image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG", optimize=True)


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


def _scale_to_fit(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if w <= max_dim and h <= max_dim:
        return img
    scale = min(max_dim / w, max_dim / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _trim_transparent_padding(img: Image.Image, alpha_threshold: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    a = img.split()[-1]
    bbox = a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    if not bbox:
        return img
    return img.crop(bbox)


def _center_preview(img: Image.Image, canvas_size: int = PREVIEW_PX, fill_ratio: float = 0.92) -> Image.Image:
    img = img.convert("RGBA")
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    w, h = img.size
    target = max(1, int(canvas_size * fill_ratio))
    scale = min(target / w, target / h)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    fitted = img.resize((nw, nh), Image.LANCZOS)
    x = (canvas_size - nw) // 2
    y = (canvas_size - nh) // 2
    canvas.alpha_composite(fitted, (x, y))
    return canvas


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
def _cleanup_cutout_light(img: Image.Image) -> Image.Image:
    rgba = np.array(img.convert("RGBA"))
    alpha = rgba[:, :, 3].copy()

    alpha[alpha < AI_ALPHA_CUTOFF] = 0

    mask = (alpha > 0).astype(np.uint8) * 255
    if mask.max() > 0:
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        alpha = np.where(mask > 0, alpha, 0).astype(np.uint8)

    rgba[:, :, 3] = alpha
    return Image.fromarray(rgba, "RGBA")


def _dehalo_cutout(img: Image.Image) -> Image.Image:
    arr = np.array(img.convert("RGBA")).astype(np.float32)
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]

    if alpha.max() <= 0:
        return Image.fromarray(arr.astype(np.uint8), "RGBA")

    solid = (alpha >= float(DEHALO_SOLID_ALPHA)).astype(np.float32)
    if solid.max() <= 0:
        return Image.fromarray(arr.astype(np.uint8), "RGBA")

    weighted = rgb * solid[:, :, None]
    blur_weighted = cv2.GaussianBlur(weighted, (0, 0), sigmaX=2.0, sigmaY=2.0)
    blur_solid = cv2.GaussianBlur(solid, (0, 0), sigmaX=2.0, sigmaY=2.0)
    blur_solid_3 = np.repeat(np.maximum(blur_solid[:, :, None], 1e-5), 3, axis=2)
    rebuilt_rgb = blur_weighted / blur_solid_3

    edge = (alpha > 0) & (alpha < float(DEHALO_SOLID_ALPHA))
    rgb[edge] = rebuilt_rgb[edge]

    out = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha, 0, 255)]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _apply_cutout_alpha_to_original(original_rgba: Image.Image, cutout_rgba: Image.Image) -> Image.Image:
    original_rgba = original_rgba.convert("RGBA")
    cutout_rgba = cutout_rgba.convert("RGBA")
    cutout_alpha = cutout_rgba.split()[-1].resize(original_rgba.size, Image.LANCZOS)
    out = original_rgba.copy()
    out.putalpha(cutout_alpha)
    return out


def _quality_flags(img: Image.Image) -> list[str]:
    flags: list[str] = []
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


# ----------------------------
# True upscale helpers
# ----------------------------
def _needs_upscale(img: Image.Image, target_px: int) -> bool:
    w, h = img.size
    return max(w, h) < target_px


def _resize_to_max_dim(img: Image.Image, target_px: int) -> Image.Image:
    w, h = img.size
    if max(w, h) == target_px:
        return img
    scale = target_px / float(max(w, h))
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return img.resize((nw, nh), Image.LANCZOS)


def _upscale_with_realesrgan(img: Image.Image, target_px: int) -> Image.Image:
    if not REALESRGAN_BIN:
        raise RuntimeError("REALESRGAN_BIN not set")

    temp_id = str(uuid.uuid4())
    in_path = UPLOAD_DIR / f"{temp_id}_sr_in.png"
    out_dir = UPLOAD_DIR / f"{temp_id}_sr_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    _save_png(img.convert("RGBA"), in_path)

    cmd = [
        REALESRGAN_BIN,
        "-i", str(in_path),
        "-o", str(out_dir),
        "-n", REALESRGAN_MODEL,
        "-s", str(REALESRGAN_SCALE),
    ]

    if REALESRGAN_TILE > 0:
        cmd += ["-t", str(REALESRGAN_TILE)]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=REALESRGAN_TIMEOUT,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"Real-ESRGAN failed: {(proc.stderr or proc.stdout or '').strip()[:500]}")

    candidates = list(out_dir.glob("*.png"))
    if not candidates:
        raise RuntimeError("Real-ESRGAN did not produce an output file")

    sr = Image.open(candidates[0]).convert("RGBA")

    if max(sr.size) > target_px:
        sr = _resize_to_max_dim(sr, target_px)

    try:
        in_path.unlink(missing_ok=True)
        for f in out_dir.glob("*"):
            f.unlink(missing_ok=True)
        out_dir.rmdir()
    except Exception:
        pass

    return sr


def _true_upscale_if_needed(img: Image.Image, target_px: int) -> Image.Image:
    img = img.convert("RGBA")

    if not _needs_upscale(img, target_px):
        return img

    if UPSCALE_ENABLED and UPSCALE_ENGINE == "realesrgan":
        try:
            return _upscale_with_realesrgan(img, target_px)
        except Exception as e:
            print("⚠️ True upscale failed, falling back to Lanczos:", e)

    return _resize_to_max_dim(img, target_px)


# ----------------------------
# Final save helper
# ----------------------------
def _finalize_session_image(session_id: str) -> Dict[str, Any]:
    p = _paths(session_id)
    curr = p["curr"]
    final = p["final"]

    if not curr.exists():
        raise FileNotFoundError("Final image not found for session")

    if not final.exists():
        final.write_bytes(curr.read_bytes())

    _sess_set(
        session_id,
        finalized=True,
        finalized_at=time.time(),
        final_path=str(final),
    )

    return {
        "status": "ok",
        "saved": True,
        "session_id": session_id,
        "finalize_url": f"/finalize/{session_id}",
        "final_image_url": f"/final-file/{session_id}",
    }


# ----------------------------
# Core processing
# ----------------------------
def _process_session(session_id: str, keep_original: bool):
    try:
        p = _paths(session_id)

        _sess_set(session_id, status="processing", stage="loading_image", started_at=time.time())

        if not p["orig_master"].exists():
            _sess_set(session_id, status="failed", stage="failed", error="orig_master missing")
            return

        master = Image.open(p["orig_master"]).convert("RGBA")

        if keep_original:
            _sess_set(session_id, stage="using_original")
            result = master.copy()
            flags = []
        else:
            _sess_set(session_id, stage="removing_background")
            removed = _photoroom_remove_bg(master)

            _sess_set(session_id, stage="cleaning_edges")
            result = _apply_cutout_alpha_to_original(master, removed)
            result = _cleanup_cutout_light(result)
            if DEHALO_ENABLED:
                result = _dehalo_cutout(result)

            _sess_set(session_id, stage="checking_quality")
            flags = _quality_flags(result)

        _sess_set(session_id, stage="trimming")
        result = _trim_transparent_padding(result, alpha_threshold=6)

        _sess_set(session_id, stage="upscaling")
        result = _true_upscale_if_needed(result, UPSCALE_TARGET_PX)

        _sess_set(session_id, stage="building_final")
        final_img = _normalize_logo(result, target_size=TARGET_PX)
        preview_img = _center_preview(final_img, canvas_size=PREVIEW_PX)

        _save_png(preview_img, p["preview"])
        _save_png(final_img, p["curr"])

        _sess_set(
            session_id,
            status="ready",
            stage="ready",
            quality_flags=flags,
            finished_at=time.time(),
        )

    except Exception as e:
        print("❌ process_session failed:", e)
        try:
            p = _paths(session_id)
            if p["orig_master"].exists():
                fallback = Image.open(p["orig_master"]).convert("RGBA")
                fallback = _trim_transparent_padding(fallback, alpha_threshold=6)
                fallback = _true_upscale_if_needed(fallback, UPSCALE_TARGET_PX)
                final_img = _normalize_logo(fallback, target_size=TARGET_PX)
                preview_img = _center_preview(final_img, canvas_size=PREVIEW_PX)
                _save_png(preview_img, p["preview"])
                _save_png(final_img, p["curr"])
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
        "finalize_url": f"/finalize/{session_id}",
        "final_image_url": f"/final-file/{session_id}",
        "status_url": f"/status/{session_id}",
        "finalized": bool(s.get("finalized")),
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

    _save_png(img, p["orig_master"])

    _sess_set(
        session_id,
        status="queued",
        stage="queued",
        created_at=time.time(),
        quality_flags=[],
        finalized=False,
    )

    t = threading.Thread(target=_process_session, args=(session_id, keep_original), daemon=True)
    t.start()

    return {
        "status": "ok",
        "session_id": session_id,
        "preview_url": f"/preview/{session_id}",
        "finalize_url": f"/finalize/{session_id}",
        "final_image_url": f"/final-file/{session_id}",
        "status_url": f"/status/{session_id}",
    }


@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    path = _paths(session_id)["preview"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png")


@app.get("/finalize/{session_id}")
def finalize_get(session_id: str):
    path = _paths(session_id)["curr"]
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png")


@app.post("/finalize/{session_id}")
def finalize_post(session_id: str):
    try:
        result = _finalize_session_image(session_id)
        return result
    except FileNotFoundError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/final-file/{session_id}")
def final_file(session_id: str):
    p = _paths(session_id)
    path = p["final"] if p["final"].exists() else p["curr"]
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
        "--name",
        storefront_name,
        "--handle",
        storefront_handle,
        "--owner_customer_id",
        owner_customer_id,
        "--main_session_id",
        main_session_id,
        "--uploads_dir",
        str(UPLOAD_DIR),
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

        main_session_id = str(uuid.uuid4())
        p = _paths(main_session_id)
        _save_png(img_main, p["orig_master"])

        _sess_set(
            main_session_id,
            status="ready",
            stage="ready",
            created_at=time.time(),
            quality_flags=[],
            finalized=False,
        )

        final_main = _trim_transparent_padding(img_main, alpha_threshold=6)
        final_main = _true_upscale_if_needed(final_main, UPSCALE_TARGET_PX)
        final_main = _normalize_logo(final_main, target_size=TARGET_PX)
        preview_main = _center_preview(final_main, canvas_size=PREVIEW_PX)

        _save_png(preview_main, p["preview"])
        _save_png(final_main, p["curr"])

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

                secondary_session_id = str(uuid.uuid4())
                sp = _paths(secondary_session_id)

                _save_png(img_sec, sp["orig_master"])

                _sess_set(
                    secondary_session_id,
                    status="ready",
                    stage="ready",
                    created_at=time.time(),
                    quality_flags=[],
                    finalized=False,
                )

                final_sec = _trim_transparent_padding(img_sec, alpha_threshold=6)
                final_sec = _true_upscale_if_needed(final_sec, UPSCALE_TARGET_PX)
                final_sec = _normalize_logo(final_sec, target_size=TARGET_PX)
                preview_sec = _center_preview(final_sec, canvas_size=PREVIEW_PX)

                _save_png(preview_sec, sp["preview"])
                _save_png(final_sec, sp["curr"])

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
  <title>Studio Uploader</title>
  <style>
    :root {
      --bg0: #f6f8fb;
      --bg1: #eef3f8;
      --panel-border: rgba(15,23,42,0.07);
      --text: #0f172a;
      --muted: #667085;
      --shadow: 0 26px 70px rgba(15,23,42,0.12);
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
      display: none;
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
      padding: 10px;
    }

    .shell {
      width: min(760px, 100%);
      background: rgba(255,255,255,0.78);
      backdrop-filter: blur(18px) saturate(1.12);
      border: 1px solid var(--panel-border);
      border-radius: 30px;
      box-shadow: var(--shadow);
      padding: 18px;
    }

    .upload-box {
      position: relative;
      border-radius: 24px;
      padding: 34px 18px;
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
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -0.02em;
      margin-top: 8px;
    }

    .muted {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .guidance {
      margin-top: 14px;
      display: grid;
      gap: 10px;
      text-align: left;
    }

    .guide-card {
      border-radius: 18px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(15,23,42,0.08);
      padding: 12px 14px;
    }

    .guide-title {
      font-size: 13px;
      font-weight: 800;
      color: #0f172a;
      margin-bottom: 4px;
    }

    .guide-text {
      font-size: 12px;
      line-height: 1.55;
      color: #475569;
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
      min-height: 42px;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(15,23,42,0.08);
      font-size: 13px;
      font-weight: 600;
      color: #334155;
    }

    .preview-wrap {
      display: none;
      width: min(980px, 100%);
      background: rgba(255,255,255,0.78);
      backdrop-filter: blur(18px) saturate(1.12);
      border: 1px solid var(--panel-border);
      border-radius: 30px;
      box-shadow: var(--shadow);
      padding: 18px;
    }

    .preview-stage {
      position: relative;
      border-radius: 24px;
      min-height: 420px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(15,23,42,0.08);
      background: #ffffff;
      overflow: hidden;
    }

    .preview-stage img {
      width: min(92vw, 84vh, 820px);
      height: min(92vw, 84vh, 820px);
      object-fit: contain;
      display: block;
    }

    .preview-stage.bg-checker {
      background-color: #fff;
      background-image:
        linear-gradient(45deg, #eceef2 25%, transparent 25%),
        linear-gradient(-45deg, #eceef2 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #eceef2 75%),
        linear-gradient(-45deg, transparent 75%, #eceef2 75%);
      background-size: 20px 20px;
      background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
    }

    .preview-stage.bg-white {
      background: #ffffff;
      background-image: none;
    }

    .preview-stage.bg-dark {
      background: #0f172a;
      background-image: none;
    }

    .preview-actions {
      margin-top: 14px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: center;
      align-items: center;
    }

    .ghost-btn {
      height: 40px;
      padding: 0 14px;
      border: 1px solid rgba(15,23,42,0.08);
      border-radius: 999px;
      background: rgba(255,255,255,0.82);
      color: #334155;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }

    .ghost-btn.active {
      background: #0f172a;
      color: white;
    }

    .status-row {
      min-height: 24px;
      margin-top: 12px;
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

    .processing-overlay {
      position: absolute;
      inset: 0;
      display: none;
      z-index: 5;
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
      max-width: 420px;
      font-size: 13px;
      color: #64748b;
      line-height: 1.45;
    }

    @media (max-width: 640px) {
      .main {
        padding: 6px;
      }

      .shell, .preview-wrap {
        border-radius: 22px;
        padding: 14px;
      }

      .upload-box {
        border-radius: 18px;
        padding: 28px 14px;
      }

      .preview-stage {
        min-height: 320px;
        border-radius: 18px;
      }

      .preview-stage img {
        width: min(95vw, 78vw, 520px);
        height: min(95vw, 78vw, 520px);
      }
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="brand">
      <h2>Studio Uploader</h2>
      <span class="brand-pill" id="slotPill">main</span>
    </div>
    <button class="btn-done" id="btnDone">Done</button>
  </div>

  <div class="main">
    <div class="shell" id="uploadShell">
      <div class="upload-box">
        <input id="file" type="file" accept="image/*" />
        <div style="font-size:30px;">✨</div>
        <div class="title">Upload logo</div>
        <div class="muted" style="margin-top:6px;">
          We can sharpen, upscale, and remove the background for most simple logos.
        </div>
      </div>

      <div class="guidance">
        <div class="guide-card">
          <div class="guide-title">Best results</div>
          <div class="guide-text">
            For the best results, use an image that is already prepped with the background removed and scaled up if possible.
          </div>
        </div>

        <div class="guide-card">
          <div class="guide-title">Built-in background removal</div>
          <div class="guide-text">
            If you need simple image sharpening, upscaling, or background removal, our built-in background removal works well for most images.
          </div>
        </div>

        <div class="guide-card">
          <div class="guide-title">If the removal looks wrong</div>
          <div class="guide-text">
            If your image does not look correct after background removal, please edit it elsewhere and come back and click <strong>Use original image</strong>. That will skip background removal and help preserve the result you want.
          </div>
        </div>

        <div class="guide-card">
          <div class="guide-title">Double-check before saving</div>
          <div class="guide-text">
            On the next screen, check your image on different background colors to make sure the background removal was aggressive enough, or not too aggressive. If everything looks good, click <strong>Done</strong> and then submit the form.
          </div>
        </div>
      </div>

      <div class="row">
        <label class="pill"><input type="checkbox" id="keepOriginal"> Use original image</label>
      </div>

      <div class="status-row" id="uploadStatus"></div>
    </div>

    <div class="preview-wrap" id="previewWrap">
      <div class="preview-stage bg-checker" id="previewStage">
        <img id="previewImg" alt="Processed preview" />
        <div class="processing-overlay" id="processingOverlay">
          <div class="spinner"></div>
          <div class="overlay-title" id="overlayTitle">Preparing your image…</div>
          <div class="overlay-sub" id="overlaySub">This usually takes a few seconds.</div>
        </div>
      </div>

      <div class="preview-actions">
        <button class="ghost-btn active" id="bgChecker">Grid</button>
        <button class="ghost-btn" id="bgWhite">White</button>
        <button class="ghost-btn" id="bgDark">Dark</button>
      </div>

      <div class="status-row" id="previewStatus"></div>
    </div>
  </div>

<script>
  const API_BASE = window.location.origin;

  let sessionId = null;
  let stageCycleTimer = null;
  let qualityFlags = [];

  const fileEl = document.getElementById('file');
  const keepOriginalEl = document.getElementById('keepOriginal');

  const uploadShell = document.getElementById('uploadShell');
  const previewWrap = document.getElementById('previewWrap');
  const previewStage = document.getElementById('previewStage');
  const previewImg = document.getElementById('previewImg');

  const btnDone = document.getElementById('btnDone');
  const uploadStatus = document.getElementById('uploadStatus');
  const previewStatus = document.getElementById('previewStatus');

  const processingOverlay = document.getElementById('processingOverlay');
  const overlayTitle = document.getElementById('overlayTitle');
  const overlaySub = document.getElementById('overlaySub');

  const bgChecker = document.getElementById('bgChecker');
  const bgWhite = document.getElementById('bgWhite');
  const bgDark = document.getElementById('bgDark');

  const params = new URLSearchParams(window.location.search);
  const SLOT = params.get('slot') || 'main';
  const RETURN_TO = params.get('return_to') || '';
  const EXISTING_SESSION_ID = (params.get('session_id') || '').trim();

  document.getElementById('slotPill').textContent = SLOT;

  const STAGE_LABELS = {
    queued: "Uploading image…",
    loading_image: "Loading image…",
    using_original: "Using original image…",
    removing_background: "Removing background…",
    cleaning_edges: "Cleaning edges…",
    checking_quality: "Checking quality…",
    trimming: "Trimming spacing…",
    upscaling: "Upscaling image…",
    building_final: "Building final image…",
    ready: "Ready ✓",
    failed: "Processing failed"
  };

  const CYCLE_MESSAGES = [
    "Uploading image…",
    "Removing background…",
    "Cleaning edges…",
    "Upscaling image…",
    "Building final image…"
  ];

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

  function setBgMode(mode) {
    previewStage.classList.remove('bg-checker', 'bg-white', 'bg-dark');
    bgChecker.classList.remove('active');
    bgWhite.classList.remove('active');
    bgDark.classList.remove('active');

    if (mode === 'checker') {
      previewStage.classList.add('bg-checker');
      bgChecker.classList.add('active');
    } else if (mode === 'white') {
      previewStage.classList.add('bg-white');
      bgWhite.classList.add('active');
    } else if (mode === 'dark') {
      previewStage.classList.add('bg-dark');
      bgDark.classList.add('active');
    }
  }

  bgChecker.addEventListener('click', () => setBgMode('checker'));
  bgWhite.addEventListener('click', () => setBgMode('white'));
  bgDark.addEventListener('click', () => setBgMode('dark'));

  function showOverlay(title, sub = "This usually takes a few seconds.") {
    processingOverlay.classList.add('show');
    overlayTitle.textContent = title || "Preparing your image…";
    overlaySub.textContent = sub;
  }

  function hideOverlay() {
    processingOverlay.classList.remove('show');
  }

  function startStageCycle() {
    stopStageCycle();
    let i = 0;
    showOverlay(CYCLE_MESSAGES[0], "Please wait while we prepare your file.");
    stageCycleTimer = setInterval(() => {
      i = (i + 1) % CYCLE_MESSAGES.length;
      overlayTitle.textContent = CYCLE_MESSAGES[i];
    }, 5000);
  }

  function stopStageCycle() {
    if (stageCycleTimer) {
      clearInterval(stageCycleTimer);
      stageCycleTimer = null;
    }
  }

  function renderQualityFlags(flags) {
    qualityFlags = Array.isArray(flags) ? flags : [];
    const warnings = [];

    if (qualityFlags.includes("possible_matte_box")) warnings.push("Possible edge artifact detected");
    if (qualityFlags.includes("heavy_soft_edges")) warnings.push("Soft edges detected");
    if (qualityFlags.includes("subject_too_small")) warnings.push("Logo looks small");

    if (warnings.length) {
      previewStatus.innerHTML = warnings.map(w => `<span class="warn-badge">⚠ ${w}</span>`).join(" ");
    } else {
      previewStatus.textContent = "Image ready ✓";
    }
  }

  function loadPreviewImage(src) {
    return new Promise((resolve, reject) => {
      previewImg.onload = () => resolve();
      previewImg.onerror = () => reject(new Error("Failed to load preview"));
      previewImg.src = src;
    });
  }

  async function loadExistingSession(existingId) {
    const r = await fetch(`${API_BASE}/session-info/${existingId}?t=${Date.now()}`, { cache: "no-store" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Saved session not found");

    sessionId = existingId;

    uploadShell.style.display = 'none';
    previewWrap.style.display = 'block';
    btnDone.style.display = 'inline-flex';

    await loadPreviewImage(`${API_BASE}/preview/${sessionId}?t=${Date.now()}`);
    renderQualityFlags(j.quality_flags || []);
    hideOverlay();
  }

  async function pollReady(statusUrl) {
    for (let i = 0; i < 180; i++) {
      try {
        const r = await fetch(statusUrl + '?t=' + Date.now(), { cache: "no-store" });
        const j = await r.json();

        const stage = j.stage || j.status || "processing";
        overlayTitle.textContent = STAGE_LABELS[stage] || "Preparing your image…";

        if (j.status === 'ready') {
          stopStageCycle();
          await loadPreviewImage(`${API_BASE}/preview/${sessionId}?t=${Date.now()}`);
          renderQualityFlags(j.quality_flags || []);
          hideOverlay();
          if (!previewStatus.textContent.trim()) {
            previewStatus.textContent = "Image ready ✓";
          }
          return;
        }

        if (j.status === 'failed') {
          stopStageCycle();
          try {
            await loadPreviewImage(`${API_BASE}/preview/${sessionId}?t=${Date.now()}`);
          } catch (e) {}
          hideOverlay();
          previewStatus.innerHTML = `<span class="warn-badge">⚠ Processing failed — fallback image ready</span>`;
          return;
        }
      } catch (e) {
        console.warn("Status poll error:", e);
      }

      await new Promise(res => setTimeout(res, 800));
    }

    stopStageCycle();
    overlayTitle.textContent = "Still processing…";
    overlaySub.textContent = "This is taking longer than usual. Please wait a little longer.";
  }

  async function handlePickedFile() {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;

    uploadStatus.textContent = "Uploading…";
    previewStatus.textContent = "";
    previewImg.removeAttribute("src");

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

      uploadShell.style.display = 'none';
      previewWrap.style.display = 'block';
      btnDone.style.display = 'inline-flex';

      startStageCycle();
      await pollReady(j.status_url);

    } catch (err) {
      console.error(err);
      alert(err.message || "Upload failed");
      location.reload();
    }
  }

  fileEl.addEventListener('click', () => { fileEl.value = ""; });
  fileEl.addEventListener('change', handlePickedFile);
  fileEl.addEventListener('input', handlePickedFile);

  btnDone.addEventListener('click', async () => {
    if (!sessionId) {
      alert("No image session found.");
      return;
    }

    btnDone.textContent = "Saving…";
    btnDone.disabled = true;

    try {
      const saveResp = await fetch(`${API_BASE}/finalize/${sessionId}`, {
        method: "POST",
        headers: { "Accept": "application/json" },
        cache: "no-store"
      });

      const saveJson = await saveResp.json().catch(() => ({}));
      if (!saveResp.ok) {
        throw new Error(saveJson.error || "Save failed");
      }

      const payload = {
        type: "studio-uploader:done",
        slot: SLOT,
        session_id: sessionId,
        finalize_url: `${API_BASE}/finalize/${sessionId}`,
        final_image_url: `${API_BASE}/final-file/${sessionId}`,
        saved: true
      };

      const sent = notifyDone(payload);
      if (!sent) {
        if (RETURN_TO) {
          const url = new URL(RETURN_TO, API_BASE);
          url.searchParams.set("slot", SLOT);
          url.searchParams.set("session_id", sessionId);
          url.searchParams.set("finalize_url", payload.finalize_url);
          url.searchParams.set("final_image_url", payload.final_image_url);
          url.searchParams.set("saved", "1");
          window.location.href = url.toString();
        } else {
          btnDone.textContent = "Saved ✓";
        }
      } else {
        btnDone.textContent = "Saved ✓";
      }
    } catch (e) {
      console.error(e);
      alert(e.message || "Done failed — try again.");
      btnDone.textContent = "Done";
      btnDone.disabled = false;
    }
  });

  setBgMode('checker');

  (async function boot() {
    try {
      if (EXISTING_SESSION_ID) {
        await loadExistingSession(EXISTING_SESSION_ID);
      }
    } catch (e) {
      console.warn("Failed to load existing session:", e);
    }
  })();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)