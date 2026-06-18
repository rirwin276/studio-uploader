# app.py — Studio Uploader (FastAPI) — neutral initial selector + required selection warning + faster transition to review screen
from __future__ import annotations

import os
import json
import uuid
import time
import secrets
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, Any

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
app = FastAPI(title="Studio Uploader", version="5.2.0")


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
    allow_headers=["*", "X-Admin-Secret"],
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

# Printful Automation proxy (for Shopify file uploads)
PRINTFUL_AUTOMATION_URL = (os.getenv("PRINTFUL_AUTOMATION_URL") or "https://printfulautomation-production.up.railway.app").strip().rstrip("/")
EDITOR_SECRET = (os.getenv("EDITOR_SECRET") or "stellasage-god-mode-2026-xK9mP").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "ryan.irwin@stellaandsagecompany.com")

# PhotoRoom
PHOTOROOM_API_KEY = (os.getenv("PHOTOROOM_API_KEY") or "").strip()
PHOTOROOM_ENDPOINT = (os.getenv("PHOTOROOM_ENDPOINT") or "https://sdk.photoroom.com/v1/segment").strip()
PHOTOROOM_TIMEOUT = int(os.getenv("PHOTOROOM_TIMEOUT", "60"))
PHOTOROOM_SIZE = (os.getenv("PHOTOROOM_SIZE") or "hd").strip()
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
DEPROVISION_SCRIPT = Path(os.getenv("DEPROVISION_SCRIPT", str(ROOT / "shopify_deprovision.py")))
DEPROVISION_TIMEOUT = int(os.getenv("DEPROVISION_TIMEOUT", "900"))  # 15 min: nuke can touch many customers

# Shopify Admin API (used by leave/nuke endpoints directly)
_SHOPIFY_SHOP = os.getenv("SHOP", "").strip()
_SHOPIFY_API_VERSION = os.getenv("API_VERSION", "2026-01").strip()
_SHOPIFY_ACCESS_TOKEN = os.getenv("CLIENT_SECRET", "").strip()


def _send_new_store_email(handle: str, store_name: str) -> None:
    """Send a notification email when a new store finishes building. Never raises."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        print(f"[email] SMTP not configured — skipping notification for {handle}")
        return
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        store_url = f"https://stellasageco.com/collections/{handle}"
        god_mode_url = "https://stellasageco.com/pages/super-admin"
        subject = f"🆕 New store built: {store_name}"
        body = (
            f"New store finished building on Stella & Sage.\n\n"
            f"Store: {store_name}\n"
            f"Handle: {handle}\n\n"
            f"View storefront: {store_url}\n"
            f"God Mode: {god_mode_url}\n\n"
            f"This is an automated notification from Studio Uploader."
        )
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"[email] Notification sent for store {handle}")
    except Exception as exc:
        print(f"[email] Failed to send notification for {handle}: {exc}")


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
# Shopify Admin API helpers
# ----------------------------
def _shopify_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Direct Shopify Admin GraphQL call (used by leave/nuke endpoints)."""
    if not _SHOPIFY_SHOP or not _SHOPIFY_ACCESS_TOKEN:
        raise RuntimeError("Shopify Admin API credentials not configured (SHOP / CLIENT_SECRET)")
    url = f"https://{_SHOPIFY_SHOP}/admin/api/{_SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": _SHOPIFY_ACCESS_TOKEN,
    }
    r = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=60)
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(payload['errors'])}")
    data = payload.get("data")
    if data is None:
        raise RuntimeError(f"Shopify GraphQL returned no data: {json.dumps(payload)}")
    return data


def _get_customer_tags(customer_gid: str) -> Optional[list]:
    """Return the tag list for a customer, or None if not found."""
    q = """
    query getCustomerTags($id: ID!) {
      customer(id: $id) {
        id
        tags
      }
    }
    """
    data = _shopify_graphql(q, {"id": customer_gid})
    cust = data.get("customer")
    if not cust:
        return None
    return cust.get("tags") or []


def _ensure_gid_customer(customer_id: str) -> str:
    cid = (customer_id or "").strip()
    if cid.startswith("gid://"):
        return cid
    cid_num = cid.split("/")[-1]
    return f"gid://shopify/Customer/{cid_num}"


def _customer_remove_tag(customer_gid: str, tag: str) -> None:
    """Remove a single tag from a customer (merge-safe)."""
    existing = _get_customer_tags(customer_gid)
    if existing is None:
        raise RuntimeError(f"Customer not found: {customer_gid}")
    if tag not in existing:
        return
    new_tags = [t for t in existing if t != tag]
    q = """
    mutation customerUpdate($input: CustomerInput!) {
      customerUpdate(input: $input) {
        customer { id tags }
        userErrors { field message }
      }
    }
    """
    res = _shopify_graphql(q, {"input": {"id": customer_gid, "tags": new_tags}})
    errs = (res.get("customerUpdate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs)}")


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
        "orig_preview": UPLOAD_DIR / f"{session_id}_orig_preview.png",
        "orig_curr": UPLOAD_DIR / f"{session_id}_orig_curr.png",
        "ai_preview": UPLOAD_DIR / f"{session_id}_ai_preview.png",
        "ai_curr": UPLOAD_DIR / f"{session_id}_ai_curr.png",
        "curr": UPLOAD_DIR / f"{session_id}_curr.png",
        "final": UPLOAD_DIR / f"{session_id}_final.png",
    }


def _session_exists(session_id: str) -> bool:
    p = _paths(session_id)
    return p["orig_master"].exists() and p["orig_preview"].exists() and p["orig_curr"].exists()


def _processed_exists(session_id: str) -> bool:
    p = _paths(session_id)
    return p["ai_preview"].exists() and p["ai_curr"].exists()


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


def _normalize_preserve_original(img: Image.Image, pad_ratio: float = 0.06, target_size: int = TARGET_PX) -> Image.Image:
    img = img.convert("RGBA")
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
# Build original / AI assets
# ----------------------------
def _build_original_assets(session_id: str):
    p = _paths(session_id)
    master = Image.open(p["orig_master"]).convert("RGBA")
    original_final = _normalize_preserve_original(master, target_size=TARGET_PX)
    original_preview = _center_preview(original_final, canvas_size=PREVIEW_PX)

    _save_png(original_preview, p["orig_preview"])
    _save_png(original_final, p["orig_curr"])


def _write_active_files(session_id: str, version: str):
    p = _paths(session_id)
    if version == "original":
        p["curr"].write_bytes(p["orig_curr"].read_bytes())
    elif version == "processed":
        p["curr"].write_bytes(p["ai_curr"].read_bytes())
    else:
        raise ValueError("Invalid version")


# ----------------------------
# Final save helper
# ----------------------------
def _finalize_session_image(session_id: str) -> Dict[str, Any]:
    p = _paths(session_id)
    s = _sess_get(session_id)
    active_version = s.get("active_version", "")

    if active_version not in ("original", "processed"):
        raise RuntimeError("Choose original or processed image before saving")

    _write_active_files(session_id, active_version)

    curr = p["curr"]
    final = p["final"]

    if not curr.exists():
        raise FileNotFoundError("Final image not found for session")

    final.write_bytes(curr.read_bytes())

    _sess_set(
        session_id,
        finalized=True,
        finalized_at=time.time(),
        final_path=str(final),
        final_version=active_version,
    )

    return {
        "status": "ok",
        "saved": True,
        "session_id": session_id,
        "finalize_url": f"/finalize/{session_id}",
        "final_image_url": f"/final-file/{session_id}",
        "active_version": active_version,
    }


# ----------------------------
# Core AI processing
# ----------------------------
def _process_session_ai(session_id: str):
    try:
        p = _paths(session_id)
        s = _sess_get(session_id)

        if not p["orig_master"].exists():
            _sess_set(session_id, status="failed", stage="failed", error="orig_master missing")
            return

        if s.get("processing"):
            return

        _sess_set(
            session_id,
            processing=True,
            status="processing",
            stage="loading_image",
            started_at=time.time(),
            error="",
        )

        master = Image.open(p["orig_master"]).convert("RGBA")

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

        _save_png(preview_img, p["ai_preview"])
        _save_png(final_img, p["ai_curr"])

        _write_active_files(session_id, "processed")

        _sess_set(
            session_id,
            processing=False,
            processed_available=True,
            status="ready",
            stage="ready",
            quality_flags=flags,
            active_version="processed",
            selected=True,
            finished_at=time.time(),
        )

    except Exception as e:
        print("❌ process_session_ai failed:", e)
        _sess_set(
            session_id,
            processing=False,
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
            return {
                "status": "uploaded",
                "stage": "uploaded",
                "session_id": session_id,
                "processed_available": _processed_exists(session_id),
                "active_version": "processed" if _processed_exists(session_id) else "",
                "selected": False,
                "quality_flags": [],
            }
        return {"status": "unknown"}
    return s


@app.get("/session-info/{session_id}")
def session_info(session_id: str):
    if not _session_exists(session_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    s = _sess_get(session_id)
    active_version = s.get("active_version", "")
    selected = bool(s.get("selected", False))
    processed_available = _processed_exists(session_id) or bool(s.get("processed_available", False))

    return {
        "status": s.get("status", "uploaded"),
        "stage": s.get("stage", "uploaded"),
        "quality_flags": s.get("quality_flags", []),
        "session_id": session_id,
        "preview_url": f"/preview/{session_id}",
        "preview_original_url": f"/preview/{session_id}?version=original",
        "preview_processed_url": f"/preview/{session_id}?version=processed",
        "finalize_url": f"/finalize/{session_id}",
        "final_image_url": f"/final-file/{session_id}",
        "status_url": f"/status/{session_id}",
        "finalized": bool(s.get("finalized")),
        "processed_available": processed_available,
        "active_version": active_version,
        "selected": selected,
        "processing": bool(s.get("processing", False)),
        "error": s.get("error", ""),
    }


@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
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
    _build_original_assets(session_id)

    _sess_set(
        session_id,
        status="uploaded",
        stage="uploaded",
        created_at=time.time(),
        quality_flags=[],
        finalized=False,
        processing=False,
        processed_available=False,
        active_version="",
        selected=False,
        error="",
    )

    return {
        "status": "ok",
        "session_id": session_id,
        "preview_url": f"/preview/{session_id}?version=original",
        "preview_original_url": f"/preview/{session_id}?version=original",
        "preview_processed_url": f"/preview/{session_id}?version=processed",
        "finalize_url": f"/finalize/{session_id}",
        "final_image_url": f"/final-file/{session_id}",
        "status_url": f"/status/{session_id}",
        "processed_available": False,
        "active_version": "",
        "selected": False,
    }


@app.post("/process/{session_id}")
def process_session(session_id: str):
    if not _session_exists(session_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    s = _sess_get(session_id)
    if s.get("processing"):
        return {"status": "ok", "session_id": session_id, "processing": True, "cached": False}

    if _processed_exists(session_id):
        _write_active_files(session_id, "processed")
        _sess_set(
            session_id,
            status="ready",
            stage="ready",
            processing=False,
            processed_available=True,
            active_version="processed",
            selected=True,
            error="",
        )
        return {
            "status": "ok",
            "session_id": session_id,
            "processing": False,
            "cached": True,
            "active_version": "processed",
            "processed_available": True,
        }

    t = threading.Thread(target=_process_session_ai, args=(session_id,), daemon=True)
    t.start()

    return {
        "status": "ok",
        "session_id": session_id,
        "processing": True,
        "cached": False,
    }


@app.post("/set-active/{session_id}")
def set_active_version(session_id: str, version: str = Query(...)):
    if not _session_exists(session_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    version = (version or "").strip().lower()
    if version not in ("original", "processed"):
        return JSONResponse({"error": "Invalid version"}, status_code=400)

    if version == "processed" and not _processed_exists(session_id):
        return JSONResponse({"error": "Processed image not available yet"}, status_code=409)

    try:
        _write_active_files(session_id, version)
        _sess_set(
            session_id,
            active_version=version,
            selected=True,
            status="ready" if version == "processed" else "uploaded",
            stage="ready" if version == "processed" else "uploaded",
            error="",
        )
        return {
            "status": "ok",
            "session_id": session_id,
            "active_version": version,
            "selected": True,
            "processed_available": _processed_exists(session_id),
            "preview_url": f"/preview/{session_id}?version={version}",
        }
    except FileNotFoundError:
        return JSONResponse({"error": "Version not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/preview/{session_id}")
def get_preview(session_id: str, version: str = Query("active")):
    if not _session_exists(session_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    version = (version or "active").strip().lower()
    p = _paths(session_id)

    try:
        if version == "original":
            path = p["orig_preview"]
            if not path.exists():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return Response(content=path.read_bytes(), media_type="image/png")

        if version == "processed":
            path = p["ai_preview"]
            if not path.exists():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return Response(content=path.read_bytes(), media_type="image/png")

        s = _sess_get(session_id)
        active_version = s.get("active_version", "")
        if active_version == "processed" and p["ai_preview"].exists():
            return Response(content=p["ai_preview"].read_bytes(), media_type="image/png")
        return Response(content=p["orig_preview"].read_bytes(), media_type="image/png")

    except Exception:
        return JSONResponse({"error": "Not found"}, status_code=404)


@app.get("/finalize/{session_id}")
def finalize_get(session_id: str):
    p = _paths(session_id)
    s = _sess_get(session_id)
    version = s.get("active_version", "")

    if version == "processed" and p["ai_curr"].exists():
        return Response(content=p["ai_curr"].read_bytes(), media_type="image/png")
    if p["orig_curr"].exists():
        return Response(content=p["orig_curr"].read_bytes(), media_type="image/png")
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.post("/finalize/{session_id}")
def finalize_post(session_id: str):
    try:
        result = _finalize_session_image(session_id)
        return result
    except FileNotFoundError:
        return JSONResponse({"error": "Not found"}, status_code=404)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/upload-to-files/{session_id}")
async def upload_to_files(session_id: str):
    """
    Upload the finalized PNG for this session to Shopify Files via
    Printful_Automation's /editor/pro-shirt/bc3413/upload-logo proxy endpoint.
    Returns {"cdn_url": "https://cdn.shopify.com/..."}.

    Requires env var PRINTFUL_AUTOMATION_URL (e.g. https://printful-automation.up.railway.app)
    and EDITOR_SECRET to be set.
    """
    if not _session_exists(session_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    s = _sess_get(session_id)
    if not s.get("finalized"):
        return JSONResponse({"error": "Session not finalized — call /finalize first"}, status_code=409)

    p = _paths(session_id)
    if p["final"].exists():
        final_file_path = p["final"]
    elif p["curr"].exists():
        final_file_path = p["curr"]
    else:
        final_file_path = None

    if not final_file_path:
        return JSONResponse({"error": "Final PNG not found on disk"}, status_code=404)

    try:
        png_bytes = final_file_path.read_bytes()
        filename = f"logo_{session_id[:8]}.png"

        resp = requests.post(
            f"{PRINTFUL_AUTOMATION_URL}/editor/pro-shirt/bc3413/upload-logo",
            params={"secret": EDITOR_SECRET},
            files={"file": (filename, png_bytes, "image/png")},
            timeout=120,
        )

        if resp.status_code != 200:
            return JSONResponse(
                {"error": f"upload-logo proxy failed: HTTP {resp.status_code}: {resp.text[:300]}"},
                status_code=502,
            )

        data = resp.json()
        cdn_url = data.get("cdn_url") or ""
        if not cdn_url:
            return JSONResponse({"error": "No cdn_url in upload-logo response"}, status_code=502)

        _sess_set(session_id, cdn_url=cdn_url)

        return {"status": "ok", "cdn_url": cdn_url}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/final-file/{session_id}")
def final_file(session_id: str):
    p = _paths(session_id)
    if p["final"].exists():
        return Response(content=p["final"].read_bytes(), media_type="image/png")

    s = _sess_get(session_id)
    active_version = s.get("active_version", "")
    if active_version == "processed" and p["ai_curr"].exists():
        return Response(content=p["ai_curr"].read_bytes(), media_type="image/png")
    if p["orig_curr"].exists():
        return Response(content=p["orig_curr"].read_bytes(), media_type="image/png")

    return JSONResponse({"error": "Not found"}, status_code=404)


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
    type_of_store_direct: Optional[str] = Form(None),
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
    # type_of_store_direct is the pre-computed slug from the hidden field added by the Shopify form.
    # Use it when present; otherwise derive the slug from org_type + sub-field.
    if type_of_store_direct and type_of_store_direct.strip():
        type_of_store = type_of_store_direct.strip()
    elif (org_type or "").strip() == "Sports Team":
        type_of_store = (sport_type or "").strip() or None
    elif (org_type or "").strip() == "Military Unit":
        type_of_store = (military_branch or "").strip() or None
    else:
        type_of_store = (org_type or "").strip() or None
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
        mp = _paths(main_session_id)
        _save_png(img_main, mp["orig_master"])
        _build_original_assets(main_session_id)
        _write_active_files(main_session_id, "original")

        _sess_set(
            main_session_id,
            status="uploaded",
            stage="uploaded",
            created_at=time.time(),
            quality_flags=[],
            finalized=False,
            processing=False,
            processed_available=False,
            active_version="original",
            selected=True,
            error="",
        )

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
                _build_original_assets(secondary_session_id)
                _write_active_files(secondary_session_id, "original")

                _sess_set(
                    secondary_session_id,
                    status="uploaded",
                    stage="uploaded",
                    created_at=time.time(),
                    quality_flags=[],
                    finalized=False,
                    processing=False,
                    processed_available=False,
                    active_version="original",
                    selected=True,
                    error="",
                )

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


@app.get("/admin/job/{job_id}")
def admin_job_status(job_id: str):
    """Admin-accessible job status endpoint. Returns status, log, and error fields."""
    j = _job_get(job_id)
    if not j:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {"status": j.get("status"), "log": j.get("log", []), "error": j.get("error")}


# ----------------------------
# Deprovision (nuke) runner
# ----------------------------
def _run_shopify_deprovision_job(job_id: str, handle: str):
    _job_set(job_id, status="running", started_at=time.time())

    if not DEPROVISION_SCRIPT.exists():
        _job_set(job_id, status="error", error=f"Deprovision script not found: {DEPROVISION_SCRIPT}")
        return

    cmd = ["python", str(DEPROVISION_SCRIPT), "--handle", handle]
    print("💣 Deprovision cmd:", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=DEPROVISION_TIMEOUT,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # Parse log lines from stdout
        log_lines = [ln for ln in stdout.splitlines() if ln.strip()]

        if stdout.strip():
            print("📄 Deprovision stdout:\n", stdout[-12000:])
        if stderr.strip():
            print("🧯 Deprovision stderr:\n", stderr[-12000:])

        if proc.returncode != 0:
            _job_set(
                job_id,
                status="error",
                finished_at=time.time(),
                error=f"Deprovision failed (exit {proc.returncode})",
                log=log_lines,
                stdout=stdout[-12000:],
                stderr=stderr[-12000:],
            )
            return

        _job_set(
            job_id,
            status="done",
            finished_at=time.time(),
            log=log_lines,
            stdout=stdout[-12000:],
            stderr=stderr[-12000:],
        )

    except subprocess.TimeoutExpired:
        _job_set(job_id, status="error", finished_at=time.time(), error="Deprovision timed out", log=[])
    except Exception as e:
        _job_set(job_id, status="error", finished_at=time.time(), error=str(e), log=[])


# ----------------------------
# Storefront: Leave + Nuke
# ----------------------------
@app.post("/api/storefront/{handle}/leave")
async def storefront_leave(handle: str, request: Request):
    """
    Member self-removal from a store.
    Body JSON: {"customer_id": "<id>"}
    Returns 403 if the customer is also an admin (must nuke instead).
    Returns 200 {"ok": true} on success.

    Auth: requires X-Admin-Secret. Reached only through the Printful_Automation
    relay, which verifies the App Proxy signature and injects the VERIFIED
    logged-in customer id — so the customer_id here is trusted and a caller can
    only remove themselves, not an arbitrary member by guessing their id.
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    body = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)

    customer_id = (body.get("customer_id") or "").strip()
    if not customer_id:
        return JSONResponse({"error": "customer_id is required"}, status_code=400)

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    customer_gid = _ensure_gid_customer(customer_id)
    admin_tag = f"storefront-admin--{handle}"
    member_tag = f"storefront-member--{handle}"

    try:
        tags = _get_customer_tags(customer_gid)
    except Exception as e:
        return JSONResponse({"error": f"Failed to fetch customer tags: {e}"}, status_code=502)

    if tags is None:
        return JSONResponse({"error": "Customer not found"}, status_code=404)

    if admin_tag in tags:
        return JSONResponse(
            {"error": "Admins cannot leave a store — use the nuke endpoint instead"},
            status_code=403,
        )

    if member_tag not in tags:
        return JSONResponse({"error": "Customer is not a member of this store"}, status_code=404)

    try:
        _customer_remove_tag(customer_gid, member_tag)
    except Exception as e:
        return JSONResponse({"error": f"Failed to remove member tag: {e}"}, status_code=502)

    return {"ok": True}


@app.post("/api/storefront/{handle}/nuke")
async def storefront_nuke(handle: str, request: Request):
    """
    Admin-only store nuke (destructive). Runs as a background job.

    Authorization (required):
      - Send header  X-Admin-Secret: <ADMIN_SECRET>.
      A request without it is rejected with 401.

    Legitimate storefront-admin / super-admin nukes arrive through the
    Printful_Automation App Proxy relay, which verifies the Shopify proxy
    signature + the customer's admin tag server-side, then forwards here with
    the private ADMIN_SECRET. The relay is the only authorized initiator.

    Returns {"job_id": "<uuid>"}; poll GET /api/job/{job_id} for status + log lines.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}  # Empty body is fine — the relay sends no body

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    customer_id = (body.get("customer_id") or "").strip()

    # Authorization — require a valid X-Admin-Secret header.
    #
    # The previous "customer_id in body holds the admin tag" path was removed: it
    # trusted an unverified customer_id, so anyone who knew a store-admin's numeric
    # customer id could nuke that store with a direct call, bypassing the App Proxy
    # signature. Store-admin nukes now go through the relay (which proves identity
    # via the proxy signature) and reach here with the private admin secret.
    if _require_admin_secret(request) is not None:
        return JSONResponse(
            {"error": "Unauthorized: a valid admin secret is required to nuke a store"},
            status_code=401,
        )

    job_id = str(uuid.uuid4())
    _job_set(
        job_id,
        status="running",
        handle=handle,
        customer_id=customer_id,
        created_at=time.time(),
        log=[],
    )

    t = threading.Thread(
        target=_run_shopify_deprovision_job,
        args=(job_id, handle),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id}


# ----------------------------
# Fundraising — metaobject persistence (type: store_fundraising)
# ----------------------------
# One singleton metaobject per store, keyed by the store handle. The full
# wizard/launch state is stored as a single JSON blob in the `data` field. This
# is deliberately schemaless (like global_pricing's `prices` field) so partial
# drafts — empty goal, empty end date, Stripe connected but not launched — all
# round-trip without per-field type validation headaches.
#
# Reached only through the Printful_Automation relay, which verifies the App
# Proxy signature + admin tag and injects X-Admin-Secret. We re-check the secret
# here so a direct call without it is rejected.
_FR_METAOBJECT_TYPE = "store_fundraising"

# Platform fee added on top of the fundraising amount on launch.
# Defined once here so both the POST handler and _fr_sync_pricing stay in sync.
_FR_PLATFORM_FEE = 1

# Fields the client is allowed to set. Anything else in the body is ignored so a
# caller can't write arbitrary keys into the metaobject.
_FR_ALLOWED_FIELDS = (
    "cause_name", "amount", "goal", "end_date", "show_bar",
    "stripe_account_id", "stripe_connected", "setup_step",
)

# Fields returned to the browser by fundraising_get(). Internal tracking data
# (base_prices, ledger, markup_add, total_paid_out) is never sent to the client.
_FR_PUBLIC_FIELDS = frozenset({
    "ok", "enabled", "cause_name", "amount", "goal", "end_date", "show_bar",
    "total_raised", "stripe_account_id", "stripe_connected", "setup_step",
    "pricing_status", "pricing_error", "pricing_updated_at",
    "created_at", "updated_at",
})


def _fr_desired_markup(state: Dict[str, Any]) -> int:
    """Dollar markup that should be applied given the current fundraiser state."""
    if not bool(state.get("enabled")):
        return 0
    return int(state.get("amount") or 0) + _FR_PLATFORM_FEE


def _fr_applied_markup(state: Dict[str, Any]) -> int:
    """Dollar markup currently recorded as applied in the state."""
    return int(state.get("markup_add") or 0)


def _ensure_fundraising_definition() -> None:
    """Idempotently create the store_fundraising metaobject definition."""
    check = """
    query CheckFundraisingDef($type: String!) {
      metaobjectDefinitionByType(type: $type) { id }
    }
    """
    data = _shopify_graphql(check, {"type": _FR_METAOBJECT_TYPE})
    existing = data.get("metaobjectDefinitionByType") or {}
    if existing.get("id"):
        return

    create = """
    mutation CreateFundraisingDef($definition: MetaobjectDefinitionCreateInput!) {
      metaobjectDefinitionCreate(definition: $definition) {
        metaobjectDefinition { id }
        userErrors { field message code }
      }
    }
    """
    variables = {
        "definition": {
            "type": _FR_METAOBJECT_TYPE,
            "name": "Store Fundraising",
            # Storefront PUBLIC_READ so the public progress bar can read it later
            # via Liquid / the Storefront API without going through the relay.
            "access": {"storefront": "PUBLIC_READ"},
            "fieldDefinitions": [
                {"key": "data", "name": "Data", "type": "json", "required": False},
            ],
        }
    }
    res = _shopify_graphql(create, variables)
    errs = ((res.get("metaobjectDefinitionCreate") or {}).get("userErrors")) or []
    if errs:
        raise RuntimeError(f"metaobjectDefinitionCreate userErrors: {json.dumps(errs)}")


def _fr_get_state(handle: str) -> Dict[str, Any]:
    """Return the stored fundraising state dict for a store, or {} if none."""
    q = """
    query GetFundraising($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        fields { key value }
      }
    }
    """
    data = _shopify_graphql(q, {"handle": {"type": _FR_METAOBJECT_TYPE, "handle": handle}})
    node = data.get("metaobjectByHandle")
    if not node:
        return {}
    fields = {f["key"]: f["value"] for f in (node.get("fields") or [])}
    raw = fields.get("data")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _fr_set_state(handle: str, state: Dict[str, Any]) -> None:
    """Upsert the fundraising metaobject for a store with the given state dict."""
    m = """
    mutation UpsertFundraising($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id handle }
        userErrors { field message code }
      }
    }
    """
    variables = {
        "handle": {"type": _FR_METAOBJECT_TYPE, "handle": handle},
        "metaobject": {"fields": [{"key": "data", "value": json.dumps(state)}]},
    }
    res = _shopify_graphql(m, variables)
    errs = ((res.get("metaobjectUpsert") or {}).get("userErrors")) or []
    if errs:
        raise RuntimeError(f"metaobjectUpsert userErrors: {json.dumps(errs)}")


@app.get("/api/fundraising/{handle}")
async def fundraising_get(handle: str, request: Request):
    """Load a store's fundraising state. Requires X-Admin-Secret (via relay)."""
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = (handle or "").strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        _ensure_fundraising_definition()
        state = _fr_get_state(handle)
    except Exception as e:
        print(f"[fundraising/get] state load error for {handle}: {e}")
        return JSONResponse({"ok": False, "error": f"Failed to load fundraising state: {e}"})

    if not state:
        return JSONResponse({"ok": True, "enabled": False})

    out = {k: v for k, v in state.items() if k in _FR_PUBLIC_FIELDS}
    out["ok"] = True
    return JSONResponse(out)


@app.post("/api/fundraising/{handle}")
async def fundraising_post(handle: str, request: Request):
    """
    Save / launch / stop a store's fundraiser. Requires X-Admin-Secret (via relay).

    Body is merged onto the stored state (so a partial draft save doesn't wipe
    fields). `enabled` drives launch (true) vs stop (false). Whitelisted fields
    only — see _FR_ALLOWED_FIELDS.
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = (handle or "").strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    try:
        _ensure_fundraising_definition()
        current = _fr_get_state(handle)
    except Exception as e:
        return JSONResponse({"error": f"Failed to read current state: {e}"}, status_code=502)

    now_iso = datetime.now(timezone.utc).isoformat()

    state = dict(current)
    for k in _FR_ALLOWED_FIELDS:
        if k in body:
            state[k] = body[k]

    enabled = bool(body.get("enabled"))
    state["enabled"] = enabled
    # total_raised is owned by the order webhook (phase 2); never clobber it here.
    state["total_raised"] = current.get("total_raised", 0)
    if enabled and not current.get("created_at"):
        state["created_at"] = now_iso
    state["updated_at"] = now_iso

    # Determine whether pricing will actually need to change so we can set the
    # right initial pricing_status before the background thread runs.
    desired_add = _fr_desired_markup(state)
    applied_add = _fr_applied_markup(current)
    pricing_will_run = desired_add != applied_add
    state["pricing_status"] = "pending" if pricing_will_run else "skipped"
    state["pricing_error"] = ""
    state["pricing_updated_at"] = now_iso

    try:
        _fr_set_state(handle, state)
    except Exception as e:
        print(f"[fundraising/post] state save error for {handle}: {e}")
        return JSONResponse({"ok": False, "error": f"Failed to save fundraising state: {e}"})

    # Reprice products to match the new state (raise on launch, restore on stop).
    # Runs in the background so the launch/stop response stays fast; the metaobject
    # already reflects the committed state above. Idempotent (see _fr_sync_pricing).
    if pricing_will_run:
        threading.Thread(target=_fr_sync_pricing, args=(handle,), daemon=True).start()

    out = {k: v for k, v in state.items() if k in _FR_PUBLIC_FIELDS}
    out["ok"] = True
    return JSONResponse(out)


# ----------------------------
# Fundraising — Stripe Connect (Express) onboarding + payouts
# ----------------------------
# The platform (Stella & Sage) collects on every sale via Shopify Payments, then
# routes each cause's accumulated share to a connected Express account with a
# Stripe Transfer. Recipients onboard their own bank through Stripe-hosted
# onboarding — we never see their banking details.
#
# STRIPE_SECRET_KEY (sk_test_… / sk_live_…) lives ONLY on this service.
_STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
# Days a contribution is held before it's eligible for payout.
_FR_HOLD_DAYS = int(os.getenv("FUNDRAISING_HOLD_DAYS", "7"))


def _stripe():
    """Return the configured stripe module, or raise if not set up."""
    if not _STRIPE_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured on this service")
    import stripe  # lazy import so the app boots even before the dep is installed
    stripe.api_key = _STRIPE_KEY
    return stripe


@app.get("/api/fundraising/{handle}/public")
async def fundraising_public(handle: str):
    """
    Public, unauthenticated read for the storefront progress bar. Returns only
    display-safe fields, and only when the fundraiser is enabled AND show_bar is
    on. No secret required — this is the same info shown to shoppers.
    """
    handle = (handle or "").strip()
    if not handle:
        return JSONResponse({"ok": True, "enabled": False})
    try:
        state = _fr_get_state(handle)
    except Exception:
        return JSONResponse({"ok": True, "enabled": False})

    if not state.get("enabled") or not state.get("show_bar"):
        return JSONResponse({"ok": True, "enabled": False})

    return JSONResponse({
        "ok": True,
        "enabled": True,
        "cause_name": state.get("cause_name") or "",
        "goal": float(state.get("goal") or 0),
        "total_raised": float(state.get("total_raised") or 0),
        "end_date": state.get("end_date") or "",
    })


@app.post("/api/fundraising/{handle}/stripe/connect")
async def fundraising_stripe_connect(handle: str, request: Request):
    """
    Create (or reuse) the recipient's Stripe Express account and return a hosted
    onboarding URL. Body may include {"return_url": "...", "refresh_url": "..."}.
    Requires X-Admin-Secret (via relay).
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = (handle or "").strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    return_url = (body.get("return_url") or "").strip()
    refresh_url = (body.get("refresh_url") or return_url).strip()
    if not return_url:
        return JSONResponse({"error": "return_url is required"}, status_code=400)

    try:
        stripe = _stripe()
        _ensure_fundraising_definition()
        state = _fr_get_state(handle)

        acct_id = state.get("stripe_account_id")
        if not acct_id:
            acct = stripe.Account.create(
                type="express",
                capabilities={"transfers": {"requested": True}},
                business_profile={"name": state.get("cause_name") or handle},
                metadata={"store_handle": handle},
            )
            acct_id = acct.id
            state["stripe_account_id"] = acct_id
            state["stripe_connected"] = False
            _fr_set_state(handle, state)

        link = stripe.AccountLink.create(
            account=acct_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
    except Exception as e:
        # Log the full error so it appears in Railway logs — the App Proxy
        # swallows non-200 response bodies before they reach the browser.
        print(f"[stripe/connect] error for {handle}: {type(e).__name__}: {e}")
        # Return 200 so the App Proxy forwards the JSON body to the browser.
        # The frontend checks j.ok rather than HTTP status.
        return JSONResponse({"ok": False, "error": f"Stripe connect failed: {e}"})

    return JSONResponse({"ok": True, "url": link.url, "account_id": acct_id})


@app.get("/api/fundraising/{handle}/stripe/status")
async def fundraising_stripe_status(handle: str, request: Request):
    """
    Re-check the connected account and persist stripe_connected. 'connected' means
    the account can receive payouts (details submitted + payouts enabled).
    Requires X-Admin-Secret (via relay).
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = (handle or "").strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        _ensure_fundraising_definition()
        state = _fr_get_state(handle)
    except Exception as e:
        return JSONResponse({"error": f"Failed to read state: {e}"}, status_code=502)

    acct_id = state.get("stripe_account_id")
    if not acct_id:
        return JSONResponse({"ok": True, "connected": False})

    try:
        stripe = _stripe()
        acct = stripe.Account.retrieve(acct_id)
        connected = bool(getattr(acct, "details_submitted", False) and getattr(acct, "payouts_enabled", False))
        if connected != bool(state.get("stripe_connected")):
            state["stripe_connected"] = connected
            _fr_set_state(handle, state)
    except Exception as e:
        print(f"[stripe/status] error for {handle}: {type(e).__name__}: {e}")
        return JSONResponse({"ok": False, "connected": False, "error": f"Stripe status failed: {e}"})

    return JSONResponse({
        "ok": True,
        "connected": connected,
        "account_id": acct_id,
        "details_submitted": bool(getattr(acct, "details_submitted", False)),
        "payouts_enabled": bool(getattr(acct, "payouts_enabled", False)),
    })


@app.post("/api/fundraising/payouts/run")
async def fundraising_payouts_run(request: Request):
    """
    Weekly batched payout job (cron-triggered; requires X-Cron-Secret).

    For every store fundraiser, sum unpaid ledger contributions whose 7-day hold
    has elapsed and issue ONE Stripe Transfer per connected account (never per
    order). Marks those ledger rows paid and bumps total_paid_out.

    The ledger lives in the metaobject `data.ledger` array, appended by the
    order-paid webhook. Pass {"handle": "..."} to run a single store, or omit to
    run all stores that have a fundraiser metaobject.
    """
    denied = _require_cron_secret(request)
    if denied is not None:
        return denied

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    handles = []
    one = (body.get("handle") or "").strip()
    if one:
        handles = [one]
    else:
        try:
            handles = _fr_all_handles()
        except Exception as e:
            print(f"[payouts/run] failed to enumerate fundraisers: {e}")
            return JSONResponse({"ok": False, "error": f"Failed to enumerate fundraisers: {e}"})

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_FR_HOLD_DAYS)

    results = []
    for h in handles:
        try:
            state = _fr_get_state(h)
            acct_id = state.get("stripe_account_id")
            if not acct_id or not state.get("stripe_connected"):
                results.append({"handle": h, "skipped": "no connected stripe account"})
                continue

            ledger = state.get("ledger") or []
            due_cents = 0
            due_idx = []
            for i, row in enumerate(ledger):
                if row.get("paid"):
                    continue
                created = row.get("created_at") or ""
                try:
                    when = datetime.fromisoformat(created)
                except Exception:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when <= cutoff:
                    due_cents += int(round(float(row.get("amount", 0)) * 100))
                    due_idx.append(i)

            if due_cents <= 0:
                results.append({"handle": h, "transferred": 0})
                continue

            stripe = _stripe()

            # Guard: Stripe Transfers require available balance in the platform
            # account. Storefront payments go through Shopify Payments, so the
            # platform Stripe balance may not be funded. Skip rather than fail
            # with a Stripe error and leave ledger rows unpaid.
            balance = stripe.Balance.retrieve()
            available_usd = next(
                (b["amount"] for b in (balance.get("available") or []) if b.get("currency") == "usd"),
                0,
            )
            if available_usd < due_cents:
                results.append({
                    "handle": h,
                    "skipped": "insufficient_stripe_balance",
                    "due_cents": due_cents,
                    "available_cents": available_usd,
                })
                continue

            transfer = stripe.Transfer.create(
                amount=due_cents,
                currency="usd",
                destination=acct_id,
                metadata={"store_handle": h, "rows": str(len(due_idx))},
            )
            for i in due_idx:
                ledger[i]["paid"] = True
                ledger[i]["transfer_id"] = transfer.id
            state["ledger"] = ledger
            state["total_paid_out"] = float(state.get("total_paid_out", 0)) + (due_cents / 100.0)
            _fr_set_state(h, state)
            results.append({"handle": h, "transferred": due_cents / 100.0, "transfer_id": transfer.id})
        except Exception as e:
            results.append({"handle": h, "error": str(e)})

    return JSONResponse({"ok": True, "results": results})


def _fr_all_handles() -> list:
    """Return all store handles that have a store_fundraising metaobject."""
    q = """
    query AllFundraisers($type: String!, $cursor: String) {
      metaobjects(type: $type, first: 100, after: $cursor) {
        edges { node { handle } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    handles = []
    cursor = None
    while True:
        data = _shopify_graphql(q, {"type": _FR_METAOBJECT_TYPE, "cursor": cursor})
        mo = data.get("metaobjects") or {}
        for edge in (mo.get("edges") or []):
            h = (edge.get("node") or {}).get("handle")
            if h:
                handles.append(h)
        page = mo.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
        else:
            break
    return handles


# ----------------------------
# Fundraising — product repricing (launch raises prices, stop restores them)
# ----------------------------
# A store's products are scoped by the bare tag == handle (same filter the
# storefront product list and sleep mode use). On launch we snapshot each
# active variant's price into the metaobject's `base_prices` map, then raise it
# by (amount + $1 fee). On stop we restore from the snapshot exactly. The whole
# thing is idempotent and keyed on `markup_add` so editing the amount re-syncs.

def _shopify_rest_put(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not _SHOPIFY_SHOP or not _SHOPIFY_ACCESS_TOKEN:
        raise RuntimeError("Shopify Admin API credentials not configured (SHOP / CLIENT_SECRET)")
    url = f"https://{_SHOPIFY_SHOP}/admin/api/{_SHOPIFY_API_VERSION}/{path.lstrip('/')}"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": _SHOPIFY_ACCESS_TOKEN}
    for attempt in range(5):
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 429:  # rate limited — back off and retry
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Shopify REST PUT rate-limited after retries: {path}")


def _variant_set_price(variant_gid: str, price: str) -> None:
    vid = str(variant_gid).split("/")[-1]
    _shopify_rest_put(f"variants/{vid}.json", {"variant": {"id": int(vid), "price": str(price)}})


def _fr_list_variants(handle: str) -> list:
    """All active variants for a store: [{'gid':..., 'price':'20.00'}]."""
    q = """
    query StoreVariants($query: String!, $cursor: String) {
      products(first: 50, query: $query, after: $cursor) {
        edges { node { id variants(first: 100) { edges { node { id price } } } } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    out = []
    cursor = None
    while True:
        data = _shopify_graphql(q, {"query": f"tag:{handle} status:active", "cursor": cursor})
        prods = data.get("products") or {}
        for edge in (prods.get("edges") or []):
            node = edge.get("node") or {}
            for ve in ((node.get("variants") or {}).get("edges") or []):
                vn = ve.get("node") or {}
                if vn.get("id") and vn.get("price") is not None:
                    out.append({"gid": vn["id"], "price": str(vn["price"])})
        page = prods.get("pageInfo") or {}
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
        else:
            break
    return out


def _fr_restore_prices(state: Dict[str, Any]) -> None:
    """
    Restore every variant to its snapshotted base price (mutates state).
    Only clears base_prices/markup_add after ALL restores succeed so the
    snapshot is preserved for retry if any individual restore fails.
    """
    base = state.get("base_prices") or {}
    errors = []
    for vgid, price in base.items():
        try:
            _variant_set_price(vgid, str(price))
        except Exception as e:
            errors.append(f"{vgid}: {e}")
        time.sleep(0.3)
    if errors:
        raise RuntimeError("; ".join(errors[:10]))
    # Only clear after full success — partial failure leaves snapshot intact for retry.
    state["base_prices"] = {}
    state["markup_add"] = 0


def _fr_apply_markup(handle: str, add: int, state: Dict[str, Any]) -> None:
    """
    Snapshot all base prices, persist them to the metaobject, then raise
    every variant price by `add` (mutates state).

    The snapshot is persisted BEFORE any Shopify price writes so that a
    mid-loop failure still leaves base_prices in the metaobject, allowing
    a subsequent Stop to restore prices correctly.
    """
    variants = _fr_list_variants(handle)
    base = {}
    for v in variants:
        try:
            orig = float(v["price"])
        except Exception:
            continue
        base[v["gid"]] = f"{orig:.2f}"

    # Persist snapshot before touching any live prices.
    state["base_prices"] = base
    state["markup_add"] = add
    _fr_set_state(handle, state)

    errors = []
    for gid, orig_str in base.items():
        try:
            _variant_set_price(gid, f"{float(orig_str) + add:.2f}")
        except Exception as e:
            errors.append(f"{gid}: {e}")
        time.sleep(0.3)
    if errors:
        raise RuntimeError("; ".join(errors[:10]))


def _fr_sync_pricing(handle: str) -> None:
    """
    Bring a store's product prices in line with current fundraiser state.
    Idempotent: compares desired markup against the markup already applied.
    Runs in a background thread off launch/stop.
    Writes pricing_status / pricing_error / pricing_updated_at when done.
    """
    def _save_pricing_status(st: Dict[str, Any], status: str, error: str = "") -> None:
        st["pricing_status"] = status
        st["pricing_error"] = error
        st["pricing_updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _fr_set_state(handle, st)
        except Exception as ex:
            print(f"[fundraising] failed to persist pricing_status for {handle}: {ex}")

    state: Optional[Dict[str, Any]] = None
    try:
        state = _fr_get_state(handle)
        desired_add = _fr_desired_markup(state)
        applied_add = _fr_applied_markup(state)
        if desired_add == applied_add:
            _save_pricing_status(state, "skipped")
            return
        if applied_add > 0:
            _fr_restore_prices(state)
        if desired_add > 0:
            _fr_apply_markup(handle, desired_add, state)
        _save_pricing_status(state, "succeeded")
        print(f"[fundraising] reprice synced for {handle}: add={desired_add}")
    except Exception as e:
        print(f"[fundraising] reprice sync failed for {handle}: {e}")
        try:
            # Use the in-memory state if available — it may contain a freshly
            # computed base_prices snapshot that was not yet persisted. Only
            # fall back to a fresh metaobject read if state was never fetched.
            if state is None:
                state = _fr_get_state(handle)
            _save_pricing_status(state, "failed", str(e)[:500])
        except Exception as ex:
            print(f"[fundraising] could not persist pricing failure for {handle}: {ex}")


# ----------------------------
# Fundraising — order-paid webhook (grows total_raised + escrow ledger)
# ----------------------------
_SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "").strip()


def _verify_shopify_webhook(raw: bytes, header_hmac: str) -> bool:
    if not _SHOPIFY_WEBHOOK_SECRET:
        return False
    import hmac as _hmac, hashlib, base64
    digest = _hmac.new(_SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return _hmac.compare_digest(computed, header_hmac or "")


def _product_tags(product_id) -> list:
    pid = str(product_id)
    gid = pid if pid.startswith("gid://") else f"gid://shopify/Product/{pid}"
    q = "query($id: ID!) { product(id: $id) { tags } }"
    try:
        data = _shopify_graphql(q, {"id": gid})
    except Exception:
        return []
    return (data.get("product") or {}).get("tags") or []


def _fr_send_goal_email(handle: str, state: Dict[str, Any]) -> None:
    """Notify the admin when a fundraiser hits its goal. Never raises."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        print(f"[email] SMTP not configured — skipping goal-met email for {handle}")
        return
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        cause = state.get("cause_name") or handle
        raised = float(state.get("total_raised", 0))
        goal = float(state.get("goal", 0))
        subject = f"🎉 Goal met: {cause} raised ${raised:.0f}"
        body = (
            f"The fundraiser for {cause} ({handle}) just hit its goal.\n\n"
            f"Raised: ${raised:.2f}\n"
            f"Goal:   ${goal:.2f}\n\n"
            f"You can close it or set a new goal from Admin Powers.\n\n"
            f"This is an automated notification from Studio Uploader."
        )
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [NOTIFY_EMAIL], msg.as_string())
    except Exception as e:
        print(f"[email] goal-met email failed for {handle}: {e}")


@app.post("/webhooks/fundraising/order-paid")
async def fundraising_order_paid(request: Request):
    """
    Shopify orders/paid webhook. Verifies HMAC (SHOPIFY_WEBHOOK_SECRET), then for
    each line item whose product carries a fundraiser-active store tag, adds
    amount × qty to that store's total_raised and appends an escrow ledger row.
    Idempotent per order id.
    """
    raw = await request.body()
    if not _verify_shopify_webhook(raw, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        order = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    order_id = str(order.get("id") or "")
    prod_qty: Dict[str, int] = {}
    for li in (order.get("line_items") or []):
        pid = li.get("product_id")
        if pid is None:
            continue
        prod_qty[str(pid)] = prod_qty.get(str(pid), 0) + int(li.get("quantity") or 0)
    if not prod_qty:
        return JSONResponse({"ok": True, "skipped": "no products"})

    # Which fundraiser-active store(s) do this order's products belong to?
    handle_qty: Dict[str, int] = {}
    for pid, qty in prod_qty.items():
        for tag in _product_tags(pid):
            st = _fr_get_state(tag)
            if st.get("enabled"):
                handle_qty[tag] = handle_qty.get(tag, 0) + qty

    if not handle_qty:
        return JSONResponse({"ok": True, "updated": []})

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = []
    for handle, qty in handle_qty.items():
        try:
            state = _fr_get_state(handle)
            if not state.get("enabled"):
                continue
            ledger = state.get("ledger") or []
            if order_id and any(r.get("order_id") == order_id for r in ledger):
                continue  # already counted this order
            amount = int(state.get("amount") or 0)
            contribution = amount * qty
            if contribution <= 0:
                continue
            ledger.append({
                "order_id": order_id, "amount": contribution, "qty": qty,
                "created_at": now_iso, "paid": False,
            })
            state["ledger"] = ledger
            state["total_raised"] = float(state.get("total_raised", 0)) + contribution

            goal = float(state.get("goal") or 0)
            hit_goal = goal > 0 and state["total_raised"] >= goal and not state.get("goal_met_notified")
            if hit_goal:
                state["goal_met_notified"] = True

            _fr_set_state(handle, state)
            if hit_goal:
                _fr_send_goal_email(handle, state)
            updated.append(handle)
        except Exception as e:
            print(f"[fundraising] order-paid update failed for {handle}: {e}")

    return JSONResponse({"ok": True, "updated": updated})


# ----------------------------
# Admin secret + cron secret helpers
# ----------------------------
# Fail closed: if ADMIN_SECRET is not explicitly configured, fall back to an
# unguessable random value (NOT the public editor secret) so the X-Admin-Secret
# path can never authenticate until the env var is set. Store-admin auth via the
# customer's storefront-admin--{handle} tag still works independently. This
# removes the previous footgun where ADMIN_SECRET defaulted to the editor secret,
# which is published in the storefront page source.
_ADMIN_SECRET = (os.getenv("ADMIN_SECRET") or "").strip()
if not _ADMIN_SECRET:
    print(
        "⚠️ ADMIN_SECRET not set — admin-secret auth disabled (fail closed). "
        "Set ADMIN_SECRET to enable the Admin Powers / relay path."
    )
    _ADMIN_SECRET = "unset-" + secrets.token_urlsafe(32)
_CRON_SECRET = os.getenv("CRON_SECRET", "").strip()


def _require_admin_secret(request: Request) -> Optional[JSONResponse]:
    """Return a 401 JSONResponse if X-Admin-Secret header is missing or wrong."""
    secret = request.headers.get("X-Admin-Secret", "").strip()
    if not secrets.compare_digest(secret, _ADMIN_SECRET):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def _require_cron_secret(request: Request) -> Optional[JSONResponse]:
    """Return a 401 JSONResponse if X-Cron-Secret header is missing or wrong."""
    secret = request.headers.get("X-Cron-Secret", "").strip()
    if not _CRON_SECRET:
        return JSONResponse({"error": "CRON_SECRET not configured on server"}, status_code=401)
    if not secrets.compare_digest(secret, _CRON_SECRET):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


# ----------------------------
# Sleep-mode background runner
# ----------------------------
def _run_sleep_check_job(job_id: str) -> None:
    from shopify_sleep import run_sleep_check  # imported here to keep top-level imports clean

    _job_set(job_id, status="running", started_at=time.time())
    log: list = []
    try:
        run_sleep_check(log)
        _job_set(job_id, status="done", finished_at=time.time(), log=log)
    except Exception as e:
        _job_set(job_id, status="error", finished_at=time.time(), error=str(e), log=log)


def _run_store_sleep_job(job_id: str, handle: str) -> None:
    """Sleep a single store in the background."""
    from shopify_sleep import sleep_store
    from shopify_wakeup import _get_metaobject_id_by_handle

    _job_set(job_id, status="running", started_at=time.time())
    log: list = []

    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    try:
        mo_id = _get_metaobject_id_by_handle(handle)
        if not mo_id:
            raise RuntimeError(f"Metaobject not found for handle {handle!r}")
        sleep_store(handle, mo_id, log)
        _job_set(job_id, status="done", finished_at=time.time(), log=log)
    except Exception as e:
        _job_set(job_id, status="error", finished_at=time.time(), error=str(e), log=log)


def _run_store_wakeup_job(job_id: str, handle: str) -> None:
    from shopify_wakeup import wakeup

    _job_set(job_id, status="running", started_at=time.time())
    log: list = []
    try:
        printful_job_id = wakeup(handle, log)
        _job_set(
            job_id,
            status="done",
            finished_at=time.time(),
            log=log,
            printful_job_id=printful_job_id,
        )
    except Exception as e:
        _job_set(job_id, status="failed", finished_at=time.time(), error=str(e), log=log)


def _delete_placement_overrides_for_store(handle: str, log: list) -> int:
    """
    Delete all placement_override metaobjects for a given store handle.
    Returns the count of deleted overrides.
    Called before a logo-change rebuild so the new image uses Titan auto-placement.
    """
    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    list_query = """
    query ListPlacementOverrides($after: String) {
      metaobjects(type: "placement_override", first: 50, after: $after) {
        edges {
          node {
            id
            handle
            fields { key value }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    delete_mutation = """
    mutation DeleteMetaobject($id: ID!) {
      metaobjectDelete(id: $id) {
        deletedId
        userErrors { field message }
      }
    }
    """

    to_delete = []
    cursor = None

    try:
        while True:
            variables: Dict[str, Any] = {}
            if cursor:
                variables["after"] = cursor
            data = _shopify_graphql(list_query, variables)
            metaobjects = (data.get("metaobjects") or {})

            for edge in (metaobjects.get("edges") or []):
                node = edge["node"]
                fields = {f["key"]: f["value"] for f in (node.get("fields") or [])}
                product_handle = fields.get("product_handle", "")
                if product_handle == handle or product_handle.startswith(handle + "-"):
                    to_delete.append({"id": node["id"], "handle": node["handle"]})

            page_info = metaobjects.get("pageInfo") or {}
            if page_info.get("hasNextPage"):
                cursor = page_info.get("endCursor")
            else:
                break
    except Exception as e:
        _log(f"⚠️ Failed to list placement overrides: {e}")
        return 0

    if not to_delete:
        _log(f"ℹ️ No placement overrides found for store {handle!r} — nothing to clear")
        return 0

    _log(f"🗑️ Clearing {len(to_delete)} placement override(s) for {handle!r} (logo changed)")

    deleted = 0
    for item in to_delete:
        try:
            result = _shopify_graphql(delete_mutation, {"id": item["id"]})
            errs = (result.get("metaobjectDelete") or {}).get("userErrors") or []
            if errs:
                _log(f"  ⚠️ Delete {item['handle']}: userErrors {errs}")
            else:
                _log(f"  ✅ Deleted override: {item['handle']}")
                deleted += 1
        except Exception as e:
            _log(f"  ⚠️ Failed to delete {item['handle']}: {e}")

    _log(f"✅ Cleared {deleted}/{len(to_delete)} placement overrides for {handle!r}")
    return deleted


def _run_store_update_settings_job(
    job_id: str,
    handle: str,
    store_name: Optional[str],
    primary_color: Optional[str],
    main_session_id: Optional[str],
    needs_rebuild: bool,
) -> None:
    """Update store display name, primary color, and/or main logo; optionally trigger sleep+rebuild."""
    from shopify_provision import upload_png_to_shopify_files, read_session_png

    _job_set(job_id, status="running", started_at=time.time())
    log: list = []

    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    try:
        metaobject_type = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()

        # 1. Look up the metaobject ID by handle
        find_q = """
        query getMetaobject($handle: MetaobjectHandleInput!) {
          metaobjectByHandle(handle: $handle) { id }
        }
        """
        data = _shopify_graphql(find_q, {"handle": {"type": metaobject_type, "handle": handle}})
        mo = data.get("metaobjectByHandle")
        if not mo:
            raise RuntimeError(f"Store not found: {handle!r}")
        mo_id = mo["id"]
        _log(f"✅ Found metaobject: {mo_id}")

        # 2. Upload new logo if a session was provided
        logo_file_gid: Optional[str] = None
        if main_session_id:
            _log(f"📸 Reading session PNG for session {main_session_id}")
            png_bytes = read_session_png(UPLOAD_DIR, main_session_id)
            _log("⬆️  Uploading logo to Shopify Files…")
            logo_file_gid, logo_file_url = upload_png_to_shopify_files(
                png_bytes,
                filename=f"{handle}_logo.png",
                alt=f"{handle} logo",
            )
            _log(f"✅ Logo uploaded: {logo_file_gid} — {logo_file_url}")

        # 3. Build mutation fields list — only include fields that are changing
        fields_to_update = []
        if store_name:
            fields_to_update.append({"key": "name", "value": store_name})
        if primary_color:
            fields_to_update.append({"key": "primary_color", "value": primary_color})
        if logo_file_gid:
            fields_to_update.append({"key": "logo", "value": logo_file_gid})

        # 4. Apply metaobject update if any fields changed
        if fields_to_update:
            update_q = """
            mutation metaobjectUpdate($id: ID!, $metaobject: MetaobjectUpdateInput!) {
              metaobjectUpdate(id: $id, metaobject: $metaobject) {
                metaobject { id }
                userErrors { field message }
              }
            }
            """
            upd = _shopify_graphql(update_q, {"id": mo_id, "metaobject": {"fields": fields_to_update}})
            errs = (upd.get("metaobjectUpdate") or {}).get("userErrors") or []
            if errs:
                raise RuntimeError(f"metaobjectUpdate userErrors: {json.dumps(errs)}")
            _log(f"✅ Metaobject updated: {[f['key'] for f in fields_to_update]}")
        else:
            _log("ℹ️  No metaobject fields to update")

        # 5. Trigger sleep → wakeup rebuild if logo or color changed
        if needs_rebuild:
            from shopify_sleep import sleep_store
            from shopify_wakeup import wakeup

            # If logo changed, clear placement overrides so new image uses Titan auto-placement
            if main_session_id:
                _delete_placement_overrides_for_store(handle, log)

            _log("😴 Starting sleep (deleting products)…")
            sleep_store(handle, mo_id, log)
            _log("✅ Store slept — triggering wakeup/rebuild…")
            printful_job_id = wakeup(handle, log)
            _log(f"✅ Wakeup triggered (Printful job: {printful_job_id})")

        _job_set(job_id, status="done", finished_at=time.time(), log=log)

    except Exception as e:
        _log(f"❌ Error: {e}")
        _job_set(job_id, status="error", finished_at=time.time(), error=str(e), log=log)

# ----------------------------
# Color-selection helpers
# ----------------------------
_COLOR_SELECTION_TYPE = "store_color_selection"
_VALID_SHIRT_VARIANTS = {"bc3413", "bc3001y", "nl6733", "mc1790", "cc1467y", "m2580"}


def _ensure_color_selection_definition() -> None:
    """
    Ensure the store_color_selection metaobject definition exists in Shopify.
    Creates it with the required fields if it does not exist yet.
    """
    check_q = """
    query GetMetaobjectDefinition($type: String!) {
      metaobjectDefinitionByType(type: $type) { id type }
    }
    """
    data = _shopify_graphql(check_q, {"type": _COLOR_SELECTION_TYPE})
    if (data.get("metaobjectDefinitionByType") or {}).get("id"):
        return  # already exists

    create_q = """
    mutation CreateMetaobjectDefinition($definition: MetaobjectDefinitionCreateInput!) {
      metaobjectDefinitionCreate(definition: $definition) {
        metaobjectDefinition { id type }
        userErrors { field message }
      }
    }
    """
    field_defs = [
        {"key": "store_handle", "type": "single_line_text_field"},
        {"key": "shirt_variant", "type": "single_line_text_field"},
        {"key": "color_1", "type": "single_line_text_field"},
        {"key": "color_2", "type": "single_line_text_field"},
        {"key": "color_3", "type": "single_line_text_field"},
        {"key": "updated_at", "type": "single_line_text_field"},
    ]
    result = _shopify_graphql(
        create_q,
        {
            "definition": {
                "type": _COLOR_SELECTION_TYPE,
                "name": "Store Color Selection",
                "fieldDefinitions": field_defs,
            }
        },
    )
    errs = (result.get("metaobjectDefinitionCreate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"metaobjectDefinitionCreate userErrors: {json.dumps(errs)}")


def _upsert_color_selection(store_handle: str, shirt_variant: str, colors: list) -> None:
    """
    Upsert a store_color_selection metaobject for the given store + shirt variant.
    handle format: {store_handle}--{shirt_variant}
    """
    mo_handle = f"{store_handle}--{shirt_variant}"
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    fields = [
        {"key": "store_handle", "value": store_handle},
        {"key": "shirt_variant", "value": shirt_variant},
        {"key": "color_1", "value": colors[0] if len(colors) > 0 else ""},
        {"key": "color_2", "value": colors[1] if len(colors) > 1 else ""},
        {"key": "color_3", "value": colors[2] if len(colors) > 2 else ""},
        {"key": "updated_at", "value": now_str},
    ]

    # Check if it already exists
    find_q = """
    query GetColorSelection($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) { id }
    }
    """
    data = _shopify_graphql(find_q, {"handle": {"type": _COLOR_SELECTION_TYPE, "handle": mo_handle}})
    existing = (data.get("metaobjectByHandle") or {}).get("id")

    if existing:
        update_q = """
        mutation UpdateColorSelection($id: ID!, $metaobject: MetaobjectUpdateInput!) {
          metaobjectUpdate(id: $id, metaobject: $metaobject) {
            metaobject { id }
            userErrors { field message }
          }
        }
        """
        result = _shopify_graphql(update_q, {"id": existing, "metaobject": {"fields": fields}})
        errs = (result.get("metaobjectUpdate") or {}).get("userErrors") or []
        if errs:
            raise RuntimeError(f"metaobjectUpdate userErrors: {json.dumps(errs)}")
    else:
        create_q = """
        mutation CreateColorSelection($metaobject: MetaobjectCreateInput!) {
          metaobjectCreate(metaobject: $metaobject) {
            metaobject { id }
            userErrors { field message }
          }
        }
        """
        result = _shopify_graphql(
            create_q,
            {
                "metaobject": {
                    "type": _COLOR_SELECTION_TYPE,
                    "handle": mo_handle,
                    "fields": fields,
                }
            },
        )
        errs = (result.get("metaobjectCreate") or {}).get("userErrors") or []
        if errs:
            raise RuntimeError(f"metaobjectCreate userErrors: {json.dumps(errs)}")


def _run_store_color_rebuild_job(job_id: str, handle: str) -> None:
    """Sleep then wake up a store to apply new color selections."""
    from shopify_sleep import sleep_store
    from shopify_wakeup import wakeup, _get_metaobject_id_by_handle

    _job_set(job_id, status="running", started_at=time.time())
    log: list = []

    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    try:
        mo_id = _get_metaobject_id_by_handle(handle)
        if not mo_id:
            raise RuntimeError(f"Metaobject not found for handle {handle!r}")

        _log("😴 Starting sleep (deleting products)…")
        sleep_store(handle, mo_id, log)
        _log("✅ Store slept — triggering wakeup/rebuild…")
        printful_job_id = wakeup(handle, log)
        _log(f"✅ Wakeup triggered (Printful job: {printful_job_id})")

        _job_set(job_id, status="done", finished_at=time.time(), log=log)
    except Exception as e:
        _log(f"❌ Error: {e}")
        _job_set(job_id, status="error", finished_at=time.time(), error=str(e), log=log)


# ----------------------------
# Sleep-mode endpoints
# ----------------------------
@app.post("/admin/sleep-check")
async def admin_sleep_check(request: Request):
    """
    Admin-only. Run the full sleep check across all stores in the background.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"status": "ok", "job_id": "..."}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    job_id = str(uuid.uuid4())
    _job_set(job_id, status="queued", created_at=time.time(), log=[])
    t = threading.Thread(target=_run_sleep_check_job, args=(job_id,), daemon=True)
    t.start()
    return {"status": "ok", "job_id": job_id}


@app.post("/admin/sleep-check/cron")
async def admin_sleep_check_cron(request: Request):
    """
    Cron-triggered sleep check. Validate with X-Cron-Secret header.
    Called by Railway cron or an external scheduler once per day.
    Returns: {"status": "ok", "job_id": "..."}
    """
    denied = _require_cron_secret(request)
    if denied is not None:
        return denied

    job_id = str(uuid.uuid4())
    _job_set(job_id, status="queued", created_at=time.time(), log=[])
    t = threading.Thread(target=_run_sleep_check_job, args=(job_id,), daemon=True)
    t.start()
    return {"status": "ok", "job_id": job_id}


@app.post("/admin/store/{handle}/sleep")
async def admin_store_sleep(handle: str, request: Request):
    """
    Admin-only. Manually put a specific store to sleep immediately.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"status": "ok", "job_id": "..."}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    job_id = str(uuid.uuid4())
    _job_set(job_id, status="queued", handle=handle, created_at=time.time(), log=[])
    t = threading.Thread(target=_run_store_sleep_job, args=(job_id, handle), daemon=True)
    t.start()
    return {"status": "ok", "job_id": job_id}


@app.post("/admin/store/{handle}/wakeup")
@app.post("/api/store/{handle}/wakeup")
async def admin_store_wakeup(handle: str, request: Request):
    """
    Admin-only. Wake up a sleeping store by re-running Printful Automation.
    Also accessible at POST /api/store/{handle}/wakeup (alias).
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"status": "ok", "job_id": "..."}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    job_id = str(uuid.uuid4())
    _job_set(job_id, status="queued", handle=handle, created_at=time.time(), log=[])
    t = threading.Thread(target=_run_store_wakeup_job, args=(job_id, handle), daemon=True)
    t.start()
    return {"status": "ok", "job_id": job_id}


@app.post("/admin/store/{handle}/update-settings")
async def admin_store_update_settings(
    handle: str,
    request: Request,
    store_name: Optional[str] = Form(None),
    primary_color: Optional[str] = Form(None),
    main_session_id: Optional[str] = Form(None),
    storefront_logo_file: Optional[UploadFile] = File(None),
):
    """
    Admin-only. Update a store's display name, primary color, and/or main logo image.
    If logo or color changes, triggers a sleep+rebuild (delete all products → Printful rebuild).
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body: multipart/form-data with optional fields:
      store_name, primary_color, main_session_id, storefront_logo_file
    Returns: {"status": "ok", "job_id": "...", "needs_rebuild": bool}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    store_name = (store_name or "").strip() or None
    primary_color = (primary_color or "").strip() or None
    main_session_id = (main_session_id or "").strip() or None

    has_file = storefront_logo_file is not None and (storefront_logo_file.filename or "").strip()
    if not any([store_name, primary_color, main_session_id, has_file]):
        return JSONResponse(
            {"error": "At least one of store_name, primary_color, main_session_id, or storefront_logo_file is required"},
            status_code=400,
        )

    # If a direct file upload was provided (and no session_id), create a session for it
    if has_file and not main_session_id:
        try:
            ct = (storefront_logo_file.content_type or "").lower()
            if ct and not ct.startswith("image/"):
                return JSONResponse({"error": "storefront_logo_file is not a valid image"}, status_code=400)

            file_bytes = await _read_upload_limited(storefront_logo_file, MAX_UPLOAD_BYTES)
            if not file_bytes:
                return JSONResponse({"error": "storefront_logo_file upload was empty"}, status_code=400)
            img = _pil_open_safe(file_bytes)
        except ValueError as e:
            if str(e) == "too_large":
                return JSONResponse({"error": f"File too large (max {MAX_UPLOAD_MB}MB)"}, status_code=413)
            if str(e) == "too_many_pixels":
                return JSONResponse({"error": "Image resolution too large. Please upload a smaller image."}, status_code=413)
            return JSONResponse({"error": "storefront_logo_file is not a valid image"}, status_code=400)

        main_session_id = str(uuid.uuid4())
        mp = _paths(main_session_id)
        _save_png(img, mp["orig_master"])
        _build_original_assets(main_session_id)
        _write_active_files(main_session_id, "original")

        _sess_set(
            main_session_id,
            status="uploaded",
            stage="uploaded",
            created_at=time.time(),
            quality_flags=[],
            finalized=False,
            processing=False,
            processed_available=False,
            active_version="original",
            selected=True,
            error="",
        )

    # needs_rebuild is True when logo or color changes; note that when a direct file upload
    # is provided, main_session_id is assigned above before this check, so it is included.
    needs_rebuild = bool(primary_color or main_session_id)

    job_id = str(uuid.uuid4())
    _job_set(job_id, status="queued", handle=handle, created_at=time.time(), log=[])
    t = threading.Thread(
        target=_run_store_update_settings_job,
        args=(job_id, handle, store_name, primary_color, main_session_id, needs_rebuild),
        daemon=True,
    )
    t.start()
    return {"status": "ok", "job_id": job_id, "needs_rebuild": needs_rebuild}


@app.post("/admin/store/{handle}/color-selections")
async def admin_store_color_selections(handle: str, request: Request):
    """
    Admin-only. Save up to 3 shirt color selections for a store to a Shopify metaobject,
    then optionally trigger a sleep+rebuild.
    Auth: X-Admin-Secret header OR "secret" key in JSON body.
    Body JSON:
      {
        "shirt_variant": "bc3413",   (optional — defaults to "bc3413")
        "colors": ["Solid Black Triblend", "Navy Triblend", "Grey Triblend"],
        "rebuild": true,
        "secret": "<ADMIN_SECRET>"   (optional — alternative to X-Admin-Secret header)
      }
    Returns: {"status": "ok", "saved_colors": [...], "job_id": "..." or null}
    """
    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # Auth: accept X-Admin-Secret header OR "secret" field in the JSON body
    header_secret = request.headers.get("X-Admin-Secret", "").strip()
    body_secret = (body.get("secret") or "").strip()
    if not secrets.compare_digest(header_secret, _ADMIN_SECRET) and not secrets.compare_digest(body_secret, _ADMIN_SECRET):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    shirt_variant = (body.get("shirt_variant") or "").strip()
    if not shirt_variant:
        shirt_variant = "bc3413"
    if shirt_variant not in _VALID_SHIRT_VARIANTS:
        return JSONResponse(
            {"error": f"shirt_variant must be one of: {sorted(_VALID_SHIRT_VARIANTS)}"},
            status_code=400,
        )

    colors = body.get("colors")
    if not isinstance(colors, list) or not (1 <= len(colors) <= 3):
        return JSONResponse({"error": "colors must be a list of 1–3 items"}, status_code=400)
    colors = [str(c).strip() for c in colors]
    if any(not c for c in colors):
        return JSONResponse({"error": "colors must not contain empty strings"}, status_code=400)

    rebuild = body.get("rebuild", True)
    if not isinstance(rebuild, bool):
        rebuild = bool(rebuild)

    try:
        _ensure_color_selection_definition()
        _upsert_color_selection(store_handle=handle, shirt_variant=shirt_variant, colors=colors)
    except Exception as e:
        print(f"[color-selections] Failed to save: {e}")
        return JSONResponse({"error": "Failed to save color selections"}, status_code=500)

    job_id = None
    if rebuild:
        job_id = str(uuid.uuid4())
        _job_set(job_id, status="queued", handle=handle, created_at=time.time(), log=[])
        t = threading.Thread(
            target=_run_store_color_rebuild_job,
            args=(job_id, handle),
            daemon=True,
        )
        t.start()

    return {"status": "ok", "saved_colors": colors, "job_id": job_id}


@app.get("/admin/store/{handle}/color-selections")
async def admin_store_get_color_selections(handle: str, request: Request, shirt_variant: str = Query(...)):
    """
    Admin-only. Return the current saved color selections for a store + shirt variant.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Query param: shirt_variant (e.g. bc3413)
    Returns: {"colors": ["...", "..."] or null}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    shirt_variant = (shirt_variant or "").strip()
    if shirt_variant not in _VALID_SHIRT_VARIANTS:
        return JSONResponse(
            {"error": f"shirt_variant must be one of: {sorted(_VALID_SHIRT_VARIANTS)}"},
            status_code=400,
        )

    mo_handle = f"{handle}--{shirt_variant}"
    find_q = """
    query GetColorSelection($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        fields { key value }
      }
    }
    """
    try:
        data = _shopify_graphql(find_q, {"handle": {"type": _COLOR_SELECTION_TYPE, "handle": mo_handle}})
    except Exception as e:
        print(f"[color-selections] Shopify request failed: {e}")
        return JSONResponse({"error": "Shopify request failed"}, status_code=502)

    mo = data.get("metaobjectByHandle")
    if not mo:
        return {"colors": None}

    fields = {f["key"]: f["value"] for f in (mo.get("fields") or [])}
    colors = [fields[k] for k in ("color_1", "color_2", "color_3") if fields.get(k)]
    return {"colors": colors if colors else None}


@app.get("/admin/store/{handle}/color-rebuild-status")
async def admin_store_color_rebuild_status(
    handle: str,
    request: Request,
    job_id: str = Query(...),
    secret: str = Query(...),
):
    """
    Poll the status of a color-rebuild job launched by POST /admin/store/{handle}/color-selections.
    Auth: secret query param validated against ADMIN_SECRET (no custom header needed — browser-safe).
    Query params:
      job_id  — the job UUID returned by the color-selections POST
      secret  — ADMIN_SECRET value
    Returns:
      {"status": "queued"|"running"|"done"|"error", "log": [...], "error": null|"..."}
      {"status": "not_found"} when job_id is unknown
    """
    if not secrets.compare_digest((secret or "").strip(), _ADMIN_SECRET):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    job = _job_get((job_id or "").strip())
    if not job:
        return {"status": "not_found"}

    return {
        "status": job.get("status", "unknown"),
        "log": job.get("log", []),
        "error": job.get("error", None),
    }


@app.get("/admin/store/{handle}/status")
async def admin_store_status(handle: str, request: Request):
    """
    Admin-only. Returns metaobject status fields and last 20 log lines from wakeup jobs.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"handle", "status", "slept_at", "last_active", "printful_automation_url", "recent_log"}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    shop = os.getenv("SHOP", "").strip()
    api_version = os.getenv("API_VERSION", "2026-01").strip()
    access_token = os.getenv("CLIENT_SECRET", "").strip()
    metaobject_type = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()
    printful_automation_url = os.getenv(
        "PRINTFUL_AUTOMATION_URL",
        "https://printfulautomation-production.up.railway.app",
    ).strip()

    if not shop or not access_token:
        return JSONResponse({"error": "Shopify not configured"}, status_code=503)

    q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        handle
        fields { key value }
      }
    }
    """
    gql_url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    gql_headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    try:
        r = requests.post(
            gql_url,
            headers=gql_headers,
            json={"query": q, "variables": {"handle": {"type": metaobject_type, "handle": handle}}},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        return JSONResponse({"error": f"Shopify request failed: {e}"}, status_code=502)

    mo = (payload.get("data") or {}).get("metaobjectByHandle")
    if not mo:
        return JSONResponse({"error": "Store not found"}, status_code=404)

    def _field(key: str) -> Optional[str]:
        for f in (mo.get("fields") or []):
            if f.get("key") == key:
                return f.get("value") or None
        return None

    # Collect last 20 log lines from any wakeup jobs for this handle
    recent_log: list = []
    with _JOBS_LOCK:
        jobs_snapshot = list(_JOBS.values())
    for j in jobs_snapshot:
        if j.get("handle") == handle:
            recent_log.extend(j.get("log") or [])
    recent_log = recent_log[-20:]

    return {
        "handle": handle,
        "status": _field("status"),
        "slept_at": _field("slept_at"),
        "last_active": _field("last_active"),
        "printful_automation_url": printful_automation_url,
        "recent_log": recent_log,
    }


@app.post("/admin/store/{handle}/reset-status")
async def admin_store_reset_status(handle: str, request: Request):
    """
    Admin-only. Reset a store's metaobject status back to 'sleeping'.
    Useful for stores stuck in 'waking' due to a failed wakeup attempt.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body JSON (optional): {"status": "sleeping"}  -- defaults to "sleeping"
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        body = {}

    new_status = (body.get("status") or "sleeping").strip()
    allowed = {"sleeping", "active", "waking"}
    if new_status not in allowed:
        return JSONResponse({"error": f"status must be one of: {allowed}"}, status_code=400)

    shop = os.getenv("SHOP", "").strip()
    api_version = os.getenv("API_VERSION", "2026-01").strip()
    access_token = os.getenv("CLIENT_SECRET", "").strip()
    metaobject_type = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()

    find_q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) { id }
    }
    """
    update_q = """
    mutation metaobjectUpdate($id: ID!, $metaobject: MetaobjectUpdateInput!) {
      metaobjectUpdate(id: $id, metaobject: $metaobject) {
        metaobject { id }
        userErrors { field message }
      }
    }
    """

    url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    headers_gql = {"Content-Type": "application/json", "X-Shopify-Access-Token": access_token}

    try:
        r = requests.post(
            url,
            headers=headers_gql,
            json={"query": find_q, "variables": {"handle": {"type": metaobject_type, "handle": handle}}},
            timeout=30,
        )
        r.raise_for_status()
        mo = (r.json().get("data") or {}).get("metaobjectByHandle")
        if not mo:
            return JSONResponse({"error": "Store not found"}, status_code=404)
        mo_id = mo["id"]

        r2 = requests.post(
            url,
            headers=headers_gql,
            json={"query": update_q, "variables": {"id": mo_id, "metaobject": {"fields": [{"key": "status", "value": new_status}]}}},
            timeout=30,
        )
        r2.raise_for_status()
        errs = ((r2.json().get("data") or {}).get("metaobjectUpdate") or {}).get("userErrors") or []
        if errs:
            return JSONResponse({"error": f"userErrors: {errs}"}, status_code=500)

        return {"status": "ok", "handle": handle, "new_status": new_status}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/admin/store/{handle}/add-member")
async def admin_store_add_member(handle: str, request: Request):
    """
    Admin-only. Add a customer to a store by email (God Mode).
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body JSON: {"email": "user@example.com"}
    Returns: {"ok": true, "customer_id": "...", "email": "...", "tag_added": "storefront-member--{handle}"}
         or: {"ok": true, "already_member": true}
         or: {"error": "..."} with status 502
    Returns 404 with a helpful message if the customer does not exist in Shopify.
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    email = (body.get("email") or "").strip()
    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)

    member_tag = f"storefront-member--{handle}"

    try:
        # Look up customer by email
        find_q = """
        query findCustomerByEmail($query: String!) {
          customers(first: 1, query: $query) {
            edges {
              node {
                id
                email
                tags
              }
            }
          }
        }
        """
        data = _shopify_graphql(find_q, {"query": f"email:{email}"})
        edges = (data.get("customers") or {}).get("edges") or []

        if not edges:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "No account found. Ask them to create an account at stellasageco.com then try again.",
                    "not_found": True,
                },
                status_code=404,
            )

        customer = edges[0]["node"]
        customer_gid = customer["id"]
        customer_email = customer["email"]
        existing_tags = customer.get("tags") or []

        if member_tag in existing_tags:
            return JSONResponse({"ok": True, "already_member": True})

        # Add the tag (merge-safe)
        new_tags = existing_tags + [member_tag]
        update_q = """
        mutation customerUpdate($input: CustomerInput!) {
          customerUpdate(input: $input) {
            customer { id tags }
            userErrors { field message }
          }
        }
        """
        res = _shopify_graphql(update_q, {"input": {"id": customer_gid, "tags": new_tags}})
        errs = (res.get("customerUpdate") or {}).get("userErrors") or []
        if errs:
            raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs)}")

        return {"ok": True, "customer_id": customer_gid, "email": customer_email, "tag_added": member_tag}

    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/admin/store/{handle}/add-admin")
async def admin_store_add_admin(handle: str, request: Request):
    """
    Admin-only. Add a customer as an admin of a store by email.
    Adds BOTH storefront-admin--{handle} AND storefront-member--{handle} tags.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body JSON: {"email": "user@example.com"}
    Returns 404 with a helpful message if the customer does not exist in Shopify.
    Returns: {"ok": true, "email": "...", "tags_added": [...]}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    email = (body.get("email") or "").strip()
    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)

    member_tag = f"storefront-member--{handle}"
    admin_tag = f"storefront-admin--{handle}"
    desired_tags = [member_tag, admin_tag]

    try:
        find_q = """
        query findCustomerByEmail($query: String!) {
          customers(first: 1, query: $query) {
            edges {
              node {
                id
                email
                tags
              }
            }
          }
        }
        """
        data = _shopify_graphql(find_q, {"query": f"email:{email}"})
        edges = (data.get("customers") or {}).get("edges") or []

        if not edges:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "No account found. Ask them to create an account at stellasageco.com then try again.",
                    "not_found": True,
                },
                status_code=404,
            )

        customer = edges[0]["node"]
        customer_gid = customer["id"]
        customer_email = customer["email"]
        existing_tags = customer.get("tags") or []

        tags_added = [t for t in desired_tags if t not in existing_tags]
        if not tags_added:
            return {"ok": True, "email": customer_email, "tags_added": []}

        new_tags = existing_tags + tags_added
        update_q = """
        mutation customerUpdate($input: CustomerInput!) {
          customerUpdate(input: $input) {
            customer { id tags }
            userErrors { field message }
          }
        }
        """
        res = _shopify_graphql(update_q, {"input": {"id": customer_gid, "tags": new_tags}})
        errs = (res.get("customerUpdate") or {}).get("userErrors") or []
        if errs:
            raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs)}")

        return {"ok": True, "email": customer_email, "tags_added": tags_added}

    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/admin/store/{handle}/remove-member")
async def admin_store_remove_member(handle: str, request: Request):
    """
    Admin-only. Fully remove a customer from a store (removes both member and admin tags).
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body JSON: {"email": "user@example.com"}
    Returns 404 if customer not found.
    Returns: {"ok": true, "email": "..."}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    email = (body.get("email") or "").strip()
    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)

    member_tag = f"storefront-member--{handle}"
    admin_tag = f"storefront-admin--{handle}"

    try:
        find_q = """
        query findCustomerByEmail($query: String!) {
          customers(first: 1, query: $query) {
            edges {
              node {
                id
                email
                tags
              }
            }
          }
        }
        """
        data = _shopify_graphql(find_q, {"query": f"email:{email}"})
        edges = (data.get("customers") or {}).get("edges") or []
        if not edges:
            return JSONResponse({"error": "Customer not found"}, status_code=404)

        customer = edges[0]["node"]
        customer_gid = customer["id"]
        customer_email = customer["email"]
        existing_tags = customer.get("tags") or []

        tags_to_remove = {member_tag, admin_tag}
        new_tags = [t for t in existing_tags if t not in tags_to_remove]

        if len(new_tags) < len(existing_tags):
            update_q = """
            mutation customerUpdate($input: CustomerInput!) {
              customerUpdate(input: $input) {
                customer { id tags }
                userErrors { field message }
              }
            }
            """
            res = _shopify_graphql(update_q, {"input": {"id": customer_gid, "tags": new_tags}})
            errs = (res.get("customerUpdate") or {}).get("userErrors") or []
            if errs:
                raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs)}")

        return {"ok": True, "email": customer_email}

    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/admin/store/{handle}/remove-admin")
async def admin_store_remove_admin(handle: str, request: Request):
    """
    Admin-only. Demote an admin to a regular member (removes admin tag, keeps member tag).
    Blocked if the customer is the last admin of the store.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body JSON: {"email": "user@example.com"}
    Returns: {"ok": true, "email": "..."}
         or: {"error": "Cannot remove the last admin of a store"} with status 400
         or: {"error": "Customer not found"} with status 404
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    email = (body.get("email") or "").strip()
    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)

    admin_tag = f"storefront-admin--{handle}"

    try:
        find_q = """
        query findCustomerByEmail($query: String!) {
          customers(first: 1, query: $query) {
            edges {
              node {
                id
                email
                tags
              }
            }
          }
        }
        """
        data = _shopify_graphql(find_q, {"query": f"email:{email}"})
        edges = (data.get("customers") or {}).get("edges") or []
        if not edges:
            return JSONResponse({"error": "Customer not found"}, status_code=404)

        customer = edges[0]["node"]
        customer_gid = customer["id"]
        customer_email = customer["email"]
        existing_tags = customer.get("tags") or []

        admins_q = f"""
        query {{
          customers(first: 10, query: "tag:{admin_tag}") {{
            edges {{
              node {{
                id
                email
              }}
            }}
          }}
        }}
        """
        admins_data = _shopify_graphql(admins_q, {})
        all_admin_edges = (admins_data.get("customers") or {}).get("edges") or []
        if len(all_admin_edges) == 1 and all_admin_edges[0]["node"]["id"] == customer_gid:
            return JSONResponse({"error": "Cannot remove the last admin of a store"}, status_code=400)

        if admin_tag not in existing_tags:
            return {"ok": True, "email": customer_email}

        # Remove only the admin tag
        new_tags = [t for t in existing_tags if t != admin_tag]
        update_q = """
        mutation customerUpdate($input: CustomerInput!) {
          customerUpdate(input: $input) {
            customer { id tags }
            userErrors { field message }
          }
        }
        """
        res = _shopify_graphql(update_q, {"input": {"id": customer_gid, "tags": new_tags}})
        errs = (res.get("customerUpdate") or {}).get("userErrors") or []
        if errs:
            raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs)}")

        return {"ok": True, "email": customer_email}

    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/admin/store/{handle}/members")
async def admin_store_list_members(handle: str, request: Request):
    """
    Admin-only. List all members and admins for a store.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"ok": true, "members": [{"email": "...", "is_admin": bool}], "total": N}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    member_tag = f"storefront-member--{handle}"
    admin_tag = f"storefront-admin--{handle}"

    try:
        search_q = """
        query findCustomersByTag($query: String!) {
          customers(first: 250, query: $query) {
            edges {
              node {
                id
                email
                tags
              }
            }
          }
        }
        """

        # Fetch members and admins in parallel via separate queries
        members_data = _shopify_graphql(search_q, {"query": f"tag:{member_tag}"})
        admins_data = _shopify_graphql(search_q, {"query": f"tag:{admin_tag}"})

        member_edges = (members_data.get("customers") or {}).get("edges") or []
        admin_edges = (admins_data.get("customers") or {}).get("edges") or []

        # Build a combined deduplicated map keyed by customer GID
        customer_map: Dict[str, Dict[str, Any]] = {}
        for edge in member_edges:
            node = edge["node"]
            customer_map[node["id"]] = {"email": node["email"], "is_admin": False}
        for edge in admin_edges:
            node = edge["node"]
            if node["id"] in customer_map:
                customer_map[node["id"]]["is_admin"] = True
            else:
                customer_map[node["id"]] = {"email": node["email"], "is_admin": True}

        members_list = sorted(customer_map.values(), key=lambda x: x["email"])
        return {"ok": True, "members": members_list, "total": len(members_list)}

    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/admin/store/{handle}/products")
async def admin_store_list_products(handle: str, request: Request):
    """
    Admin-only. List all products tagged with the given store handle.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"handle": "...", "products": [{"id", "title", "status", "hidden", "featured_image"}, ...]}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    shop = os.getenv("SHOP", "").strip()
    api_version = os.getenv("API_VERSION", "2026-01").strip()
    access_token = os.getenv("CLIENT_SECRET", "").strip()

    if not shop or not access_token:
        return JSONResponse({"error": "Shopify not configured"}, status_code=503)

    gql_url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    gql_headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }

    q = """
    query getProductsByTag($query: String!, $after: String) {
      products(first: 50, query: $query, after: $after) {
        edges {
          node {
            id
            handle
            title
            status
            featuredImage { url }
            tags
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    products = []
    cursor = None
    try:
        while True:
            variables: Dict[str, Any] = {"query": f"tag:{handle}"}
            if cursor:
                variables["after"] = cursor
            r = requests.post(
                gql_url,
                headers=gql_headers,
                json={"query": q, "variables": variables},
                timeout=30,
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("errors"):
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(payload['errors'])}")
            data = (payload.get("data") or {}).get("products") or {}
            for edge in data.get("edges") or []:
                node = edge["node"]
                products.append({
                    "id": node["id"],
                    "handle": node.get("handle") or "",
                    "title": node["title"],
                    "status": node["status"],
                    "hidden": node["status"] != "ACTIVE",
                    "featured_image": (node.get("featuredImage") or {}).get("url"),
                    "tags": node.get("tags") or [],
                })
            page_info = data.get("pageInfo") or {}
            if page_info.get("hasNextPage"):
                cursor = page_info.get("endCursor")
            else:
                break
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return {"handle": handle, "products": products}


@app.post("/admin/store/{handle}/products/{product_id:path}/hide")
async def admin_store_hide_product(handle: str, product_id: str, request: Request):
    """
    Admin-only. Toggle a product between hidden (DRAFT) and visible (ACTIVE).
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Body JSON: {"hidden": true|false}
    Returns: {"ok": true, "id": "...", "status": "DRAFT"|"ACTIVE", "hidden": true|false}
    """
    from urllib.parse import unquote

    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    product_gid = unquote(product_id).strip()
    if not product_gid:
        return JSONResponse({"error": "product_id is required"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    hidden = body.get("hidden")
    if hidden is None:
        return JSONResponse({"error": "hidden field is required"}, status_code=400)

    new_status = "DRAFT" if hidden else "ACTIVE"

    mutation = """
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id status }
        userErrors { field message }
      }
    }
    """

    try:
        data = _shopify_graphql(mutation, {"input": {"id": product_gid, "status": new_status}})
        result = (data.get("productUpdate") or {})
        errs = result.get("userErrors") or []
        if errs:
            raise RuntimeError(f"productUpdate userErrors: {json.dumps(errs)}")
        product = result.get("product") or {}
        final_status = product.get("status", new_status)
        return {"ok": True, "id": product.get("id", product_gid), "status": final_status, "hidden": final_status != "ACTIVE"}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.delete("/admin/store/{handle}/products/{product_id:path}")
async def admin_store_delete_product(handle: str, product_id: str, request: Request):
    """
    Admin-only. Permanently delete a product.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"ok": true, "deleted_id": "gid://shopify/Product/123"}
    """
    from urllib.parse import unquote

    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    product_gid = unquote(product_id).strip()
    if not product_gid:
        return JSONResponse({"error": "product_id is required"}, status_code=400)

    mutation = """
    mutation productDelete($id: ID!) {
      productDelete(input: { id: $id }) {
        deletedProductId
        userErrors { field message }
      }
    }
    """

    try:
        data = _shopify_graphql(mutation, {"id": product_gid})
        result = (data.get("productDelete") or {})
        errs = result.get("userErrors") or []
        if errs:
            raise RuntimeError(f"productDelete userErrors: {json.dumps(errs)}")
        deleted_id = result.get("deletedProductId", product_gid)
        return {"ok": True, "deleted_id": deleted_id}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/store/{handle}/status")
async def store_status(handle: str):
    """
    Public. Returns the current sleep status of a store from its metaobject.
    Response: {"handle": "...", "status": "active"|"sleeping", "slept_at": "...|null", "last_active": "...|null"}
    """
    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    # Inline metaobject lookup using the same env vars as shopify_sleep/deprovision
    shop = os.getenv("SHOP", "").strip()
    api_version = os.getenv("API_VERSION", "2026-01").strip()
    access_token = os.getenv("CLIENT_SECRET", "").strip()
    metaobject_type = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()

    if not shop or not access_token:
        return JSONResponse({"error": "Shopify not configured"}, status_code=503)

    q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        handle
        fields { key value }
      }
    }
    """
    url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    try:
        r = requests.post(
            url,
            headers=headers,
            json={"query": q, "variables": {"handle": {"type": metaobject_type, "handle": handle}}},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        return JSONResponse({"error": f"Shopify request failed: {e}"}, status_code=502)

    mo = (payload.get("data") or {}).get("metaobjectByHandle")
    if not mo:
        return JSONResponse({"error": "Store not found"}, status_code=404)

    def _field(key: str) -> Optional[str]:
        for f in (mo.get("fields") or []):
            if f.get("key") == key:
                return f.get("value") or None
        return None

    status_val = _field("status") or "active"
    slept_at = _field("slept_at")
    last_active = _field("last_active")

    return {
        "handle": handle,
        "status": status_val,
        "slept_at": slept_at,
        "last_active": last_active,
    }


@app.get("/store/{handle}/ready-status")
async def store_ready_status(handle: str):
    """
    Public. Polling endpoint for the Shopify dashboard frontend.
    Returns {"ready": true, "handle": "..."} if is_fully_ready == "true",
    or {"ready": false, "handle": "...", "status": "building"} otherwise.
    Also returns can_show_card=true as soon as the store metaobject exists so
    UI handoff can end without waiting for full readiness.
    No authentication required.
    """
    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    shop = os.getenv("SHOP", "").strip()
    api_version = os.getenv("API_VERSION", "2026-01").strip()
    access_token = os.getenv("CLIENT_SECRET", "").strip()
    metaobject_type = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()

    if not shop or not access_token:
        return JSONResponse({"error": "Shopify not configured"}, status_code=503)

    q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        handle
        fields { key value }
      }
    }
    """
    url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    try:
        r = requests.post(
            url,
            headers=headers,
            json={"query": q, "variables": {"handle": {"type": metaobject_type, "handle": handle}}},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return JSONResponse({"error": "Shopify request failed"}, status_code=502)

    mo = (payload.get("data") or {}).get("metaobjectByHandle")
    if not mo:
        return JSONResponse({"error": "Store not found"}, status_code=404)

    def _field(key: str) -> Optional[str]:
        for f in (mo.get("fields") or []):
            if f.get("key") == key:
                return f.get("value") or None
        return None

    is_ready = (_field("is_fully_ready") or "").lower() == "true"
    status_val = (_field("status") or "").strip().lower() or ("active" if is_ready else "building")

    if is_ready:
        return {
            "ready": True,
            "is_fully_ready": True,
            "can_show_card": True,
            "status": status_val,
            "handle": handle,
        }
    return {
        "ready": False,
        "is_fully_ready": False,
        "can_show_card": True,
        "status": "building" if status_val == "building" else status_val,
        "handle": handle,
    }


@app.post("/store/{handle}/store-ready")
async def store_mark_ready(handle: str, request: Request):
    """
    Called by Printful Automation when products are fully built.
    Sets is_fully_ready = true on the metaobject for the given handle.
    Header: X-Admin-Secret: <ADMIN_SECRET>
    Returns: {"status": "ok", "handle": "...", "is_fully_ready": true}
    """
    denied = _require_admin_secret(request)
    if denied is not None:
        return denied

    handle = handle.strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)

    shop = os.getenv("SHOP", "").strip()
    api_version = os.getenv("API_VERSION", "2026-01").strip()
    access_token = os.getenv("CLIENT_SECRET", "").strip()
    metaobject_type = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()

    if not shop or not access_token:
        return JSONResponse({"error": "Shopify not configured"}, status_code=503)

    # Look up the metaobject ID by handle
    lookup_q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        fields { key value }
      }
    }
    """
    gql_url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    gql_headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    try:
        r = requests.post(
            gql_url,
            headers=gql_headers,
            json={"query": lookup_q, "variables": {"handle": {"type": metaobject_type, "handle": handle}}},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return JSONResponse({"error": "Shopify request failed"}, status_code=502)

    mo = (payload.get("data") or {}).get("metaobjectByHandle")
    if not mo:
        return JSONResponse({"error": "Store not found"}, status_code=404)

    store_name = handle
    for f in (mo.get("fields") or []):
        if f.get("key") in ("name", "title"):
            maybe_name = (f.get("value") or "").strip()
            if maybe_name:
                store_name = maybe_name
                break

    mo_id = mo.get("id")

    # Update is_fully_ready and status to active
    update_q = """
    mutation metaobjectUpdate($id: ID!, $metaobject: MetaobjectUpdateInput!) {
      metaobjectUpdate(id: $id, metaobject: $metaobject) {
        metaobject { id }
        userErrors { field message }
      }
    }
    """
    try:
        r = requests.post(
            gql_url,
            headers=gql_headers,
            json={
                "query": update_q,
                "variables": {
                    "id": mo_id,
                    "metaobject": {
                        "fields": [
                            {"key": "is_fully_ready", "value": "true"},
                            {"key": "status", "value": "active"},
                        ]
                    },
                },
            },
            timeout=30,
        )
        r.raise_for_status()
        update_payload = r.json()
    except Exception:
        return JSONResponse({"error": "Shopify update failed"}, status_code=502)

    errs = ((update_payload.get("data") or {}).get("metaobjectUpdate") or {}).get("userErrors") or []
    if errs:
        return JSONResponse({"error": "Metaobject update errors", "details": errs}, status_code=500)

    threading.Thread(
        target=_send_new_store_email,
        args=(handle, store_name),
        daemon=True,
    ).start()

    return {"status": "ok", "handle": handle, "is_fully_ready": True}


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
      --bg0: #fbf8f1;
      --bg1: #f6efdf;
      --card: rgba(255,255,255,0.84);
      --card-2: rgba(255,255,255,0.72);
      --border: rgba(183,163,106,0.26);
      --text: #1c1710;
      --muted: #75654a;
      --muted-2: #6d5b3c;
      --shadow-lg: 0 28px 80px rgba(17,16,14,0.10);
      --gold: #b7a36a;
      --gold-soft: rgba(183,163,106,0.22);
      --green1: #34d399;
      --green2: #10b981;
      --blue1: #0f172a;
      --blue2: #1f2937;
      --blue3: #334155;
      --warn-bg: rgba(245,158,11,0.10);
      --warn-border: rgba(245,158,11,0.20);
      --warn-text: #a16207;
      --danger: #dc2626;
      --danger-soft: rgba(220,38,38,0.08);
      --danger-border: rgba(220,38,38,0.18);
    }

    * { box-sizing: border-box; }

    html, body {
      height: 100%;
      margin: 0;
    }

    body {
      min-height: 100vh;
      overflow: auto;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 8% -6%, rgba(183,163,106,0.20), rgba(183,163,106,0) 36%),
        radial-gradient(circle at 92% 0%, rgba(255,255,255,0.95), rgba(255,255,255,0) 28%),
        radial-gradient(circle at 50% 105%, rgba(183,163,106,0.12), rgba(183,163,106,0) 34%),
        linear-gradient(180deg, var(--bg0) 0%, #fffdfa 44%, var(--bg1) 100%);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    .app {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 10px;
      gap: 10px;
    }

    .topbar {
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 2px 4px 2px;
    }

    .topbar-left {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }

    .step-label {
      font-size: 13px;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: #334155;
    }

    .slot-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: rgba(15,23,42,0.05);
      color: #475569;
      border: 1px solid rgba(15,23,42,0.06);
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
      white-space: nowrap;
    }

    .btn-done {
      display: none;
      align-items: center;
      justify-content: center;
      border: none;
      border-radius: 999px;
      min-height: 42px;
      height: 42px;
      min-width: 92px;
      padding: 0 18px;
      background: linear-gradient(180deg, var(--green1), var(--green2));
      color: white;
      font-size: 13px;
      font-weight: 800;
      line-height: 1;
      letter-spacing: -0.01em;
      box-shadow: 0 12px 26px rgba(16,185,129,0.18);
      cursor: pointer;
      white-space: nowrap;
      transition: transform 0.12s ease, opacity 0.15s ease, box-shadow 0.15s ease;
    }

    .btn-done:hover { transform: translateY(-1px); }
    .btn-done:disabled {
      opacity: 0.56;
      cursor: not-allowed;
      box-shadow: none;
      transform: none;
    }

    .main {
      flex: 1 1 auto;
      min-height: 0;
      display: flex;
      align-items: stretch;
      justify-content: center;
    }

    .shell,
    .review-wrap {
      width: min(980px, 100%);
      background: var(--card);
      backdrop-filter: blur(22px) saturate(1.05);
      border: 1px solid var(--border);
      border-radius: 30px;
      box-shadow: var(--shadow-lg);
      padding: 16px;
    }

    .shell {
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 12px;
    }

    .upload-box {
      position: relative;
      border-radius: 24px;
      padding: 28px 18px;
      text-align: center;
      border: 1.5px dashed rgba(183,163,106,0.34);
      background: var(--card-2);
      overflow: hidden;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.85);
    }

    .upload-box input {
      position: absolute;
      inset: 0;
      opacity: 0;
      cursor: pointer;
      width: 100%;
      height: 100%;
    }

    .upload-emoji {
      font-size: 28px;
      line-height: 1;
    }

    .title {
      font-size: 19px;
      font-weight: 800;
      letter-spacing: -0.03em;
      margin-top: 8px;
    }

    .muted {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .guidance {
      display: grid;
      gap: 10px;
    }

    .guide-card {
      border-radius: 20px;
      background: linear-gradient(145deg, rgba(255,255,255,0.88), rgba(249,245,236,0.82));
      border: 1px solid var(--border);
      padding: 11px 13px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
    }

    .guide-title {
      font-size: 13px;
      font-weight: 800;
      color: #2a1f10;
      margin-bottom: 4px;
      letter-spacing: -0.01em;
    }

    .guide-text {
      font-size: 12px;
      line-height: 1.52;
      color: #475569;
    }

    .status-row {
      min-height: 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted-2);
      font-size: 13px;
      font-weight: 700;
      text-align: center;
    }

    .review-wrap {
      display: none;
      flex-direction: column;
      gap: 12px;
      min-height: 0;
    }

    .warning-banner {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      border-radius: 20px;
      padding: 12px 14px;
      background: linear-gradient(145deg, rgba(255,255,255,0.75), var(--warn-bg));
      border: 1px solid var(--warn-border);
      color: var(--warn-text);
      font-size: 12px;
      line-height: 1.5;
      font-weight: 700;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.55);
    }

    .preview-stage {
      position: relative;
      border-radius: 26px;
      min-height: 380px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--border);
      background: #ffffff;
      overflow: hidden;
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,0.95),
        0 10px 24px rgba(15,23,42,0.05);
    }

    .preview-stage img {
      width: min(88vw, 62vh, 760px);
      height: min(88vw, 62vh, 760px);
      object-fit: contain;
      display: block;
      filter: drop-shadow(0 10px 22px rgba(15,23,42,0.07));
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

    .toolbar-card {
      border-radius: 22px;
      background: rgba(255,255,255,0.74);
      border: 1px solid var(--border);
      padding: 12px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
      transition: border-color 0.15s ease, background 0.15s ease;
    }

    .toolbar-card.needs-attention {
      border-color: var(--danger-border);
      background: linear-gradient(180deg, rgba(255,255,255,0.76), var(--danger-soft));
    }

    .toolbar-label {
      text-align: center;
      color: #475569;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }

    .toolbar-label .req-star {
      color: var(--danger);
      margin-left: 4px;
      display: none;
    }

    .toolbar-card.needs-attention .toolbar-label .req-star {
      display: inline;
    }

    .segmented {
      display: flex;
      gap: 8px;
      width: 100%;
      justify-content: center;
      align-items: center;
      flex-wrap: wrap;
    }

    .seg-btn,
    .ghost-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-align: center;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 800;
      line-height: 1;
      white-space: nowrap;
      cursor: pointer;
      transition: transform 0.10s ease, opacity 0.15s ease, box-shadow 0.15s ease, background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
      user-select: none;
    }

    .seg-btn:hover,
    .ghost-btn:hover {
      transform: translateY(-1px);
    }

    .seg-btn:disabled,
    .ghost-btn:disabled {
      opacity: 0.56;
      cursor: not-allowed;
      transform: none;
    }

    .seg-btn {
      min-height: 44px;
      height: 44px;
      padding: 0 16px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.96);
      color: #334155;
      box-shadow: 0 8px 18px rgba(15,23,42,0.05);
      min-width: 220px;
    }

    .seg-btn.active {
      background: linear-gradient(180deg, var(--blue2), var(--blue1));
      color: white;
      border-color: rgba(15,23,42,0.16);
      box-shadow: 0 12px 24px rgba(15,23,42,0.16);
    }

    .seg-btn.process {
      background: rgba(255,255,255,0.96);
      color: #334155;
      border-color: var(--border);
      box-shadow: 0 8px 18px rgba(15,23,42,0.05);
    }

    .bg-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: center;
      align-items: center;
    }

    .ghost-btn {
      min-height: 40px;
      height: 40px;
      padding: 0 14px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.88);
      color: #334155;
      box-shadow: 0 6px 16px rgba(15,23,42,0.04);
    }

    .ghost-btn.active {
      background: #0f172a;
      color: white;
      border-color: rgba(15,23,42,0.16);
      box-shadow: 0 10px 18px rgba(15,23,42,0.12);
    }

    .small-note {
      text-align: center;
      color: var(--muted-2);
      font-size: 12px;
      line-height: 1.5;
      font-weight: 650;
      max-width: 680px;
      margin: 0 auto;
    }

    .warn-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(245,158,11,0.12);
      border: 1px solid rgba(245,158,11,0.22);
      color: #b45309;
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
      white-space: nowrap;
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
      background:
        radial-gradient(circle at 50% 6%, rgba(183,163,106,0.24), rgba(183,163,106,0) 42%),
        linear-gradient(180deg, rgba(255,255,255,0.80), rgba(255,253,248,0.94));
      backdrop-filter: blur(8px);
      padding: 22px;
    }

    .processing-overlay.show { display: flex; }

    .spinner {
      width: 46px;
      height: 46px;
      border-radius: 999px;
      border: 3px solid var(--gold-soft);
      border-top-color: var(--gold);
      animation: spin 0.9s linear infinite;
    }

    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }

    .overlay-title {
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: #2a1f10;
    }

    .overlay-sub {
      max-width: 430px;
      font-size: 13px;
      color: var(--muted-2);
      line-height: 1.45;
      font-weight: 650;
    }

    @media (max-width: 640px) {
      .app {
        padding: 6px;
        gap: 8px;
      }

      .shell,
      .review-wrap {
        border-radius: 22px;
        padding: 12px;
      }

      .upload-box {
        border-radius: 18px;
        padding: 18px 12px;
      }

      .preview-stage {
        min-height: 250px;
        border-radius: 20px;
      }

      .preview-stage img {
        width: min(84vw, 54vh, 420px);
        height: min(84vw, 54vh, 420px);
      }

      .warning-banner {
        padding: 10px 11px;
        font-size: 11px;
      }

      .seg-btn {
        min-width: 100%;
        width: 100%;
        height: 42px;
        min-height: 42px;
        font-size: 12px;
      }

      .ghost-btn,
      .btn-done {
        min-height: 40px;
        height: 40px;
        font-size: 12px;
      }

      .guide-title {
        font-size: 12px;
      }

      .guide-text,
      .small-note,
      .muted {
        font-size: 11px;
      }

      .slot-pill {
        display: none;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="topbar">
      <div class="topbar-left">
        <div class="step-label" id="stepLabel">Upload logo</div>
        <div class="slot-pill" id="slotPill">main</div>
      </div>
      <button class="btn-done" id="btnDone" disabled>Done</button>
    </div>

    <div class="main">
      <div class="shell" id="uploadShell">
        <div class="upload-box">
          <input id="file" type="file" accept="image/*" />
          <div class="upload-emoji">✨</div>
          <div class="title">Upload your logo</div>
          <div class="muted" style="margin-top:6px;">
            Upload first. Then choose either <strong>Use original image</strong> or <strong>Send for AI cleanup</strong>.
          </div>
        </div>

        <div class="guidance">
          <div class="guide-card">
            <div class="guide-title">Best results</div>
            <div class="guide-text">
              Use a clean image when possible. The better the upload, the better the final result.
            </div>
          </div>

          <div class="guide-card">
            <div class="guide-title">AI is optional</div>
            <div class="guide-text">
              After upload, you can keep the original image or send it for background removal and upscale.
            </div>
          </div>

          <div class="guide-card">
            <div class="guide-title">Check carefully</div>
            <div class="guide-text">
              If it looks wrong here, it will look wrong on the final product. Review it before you click <strong>Done</strong>.
            </div>
          </div>
        </div>

        <div class="status-row" id="uploadStatus"></div>
      </div>

      <div class="review-wrap" id="reviewWrap">
        <div class="warning-banner">
          <div>⚠️</div>
          <div>
            Choose one version before saving. If the cleanup or edges look wrong here, they will look wrong on your products.
          </div>
        </div>

        <div class="preview-stage bg-checker" id="previewStage">
          <img id="previewImg" alt="Logo preview" />
          <div class="processing-overlay" id="processingOverlay">
            <div class="spinner"></div>
            <div class="overlay-title" id="overlayTitle">Preparing your image…</div>
            <div class="overlay-sub" id="overlaySub">Please wait while we prepare your preview.</div>
          </div>
        </div>

        <div class="toolbar-card" id="toolbarCard">
          <div class="toolbar-label" id="versionToolbarLabel">
            Choose image version <span class="req-star" id="reqStar">*</span>
          </div>
          <div class="segmented" id="versionSelector"></div>
        </div>

        <div class="bg-row">
          <button class="ghost-btn active" id="bgChecker">Grid</button>
          <button class="ghost-btn" id="bgWhite">White</button>
          <button class="ghost-btn" id="bgDark">Dark</button>
        </div>

        <div class="status-row" id="previewStatus"></div>
        <div class="small-note" id="bottomNote">
          Choose original or AI before saving. Done stays locked until you pick one.
        </div>
      </div>
    </div>
  </div>

<script>
  const API_BASE = window.location.origin;

  let sessionId = null;
  let stageCycleTimer = null;
  let qualityFlags = [];
  let processedAvailable = false;
  let selectedVersion = "";
  let activePreviewVersion = "original";
  let isProcessing = false;

  const fileEl = document.getElementById('file');

  const uploadShell = document.getElementById('uploadShell');
  const reviewWrap = document.getElementById('reviewWrap');
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

  const versionSelector = document.getElementById('versionSelector');
  const versionToolbarLabel = document.getElementById('versionToolbarLabel');
  const toolbarCard = document.getElementById('toolbarCard');

  const params = new URLSearchParams(window.location.search);
  const SLOT = params.get('slot') || 'main';
  const RETURN_TO = params.get('return_to') || '';
  const EXISTING_SESSION_ID = (params.get('session_id') || '').trim();

  document.getElementById('slotPill').textContent = SLOT;

  const STAGE_LABELS = {
    uploaded: "Preparing preview…",
    loading_image: "Loading image…",
    removing_background: "Removing background…",
    cleaning_edges: "Cleaning edges…",
    checking_quality: "Checking quality…",
    trimming: "Trimming spacing…",
    upscaling: "Upscaling image…",
    building_final: "Building final image…",
    ready: "Processed image ready ✓",
    failed: "Processing failed"
  };

  const CYCLE_MESSAGES = [
    "Preparing preview…",
    "Sending to AI cleanup…",
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

  function setStepLabel(text) {
    document.getElementById('stepLabel').textContent = text;
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

  function showOverlay(title, sub = "Please wait while we prepare your preview.") {
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
    showOverlay(CYCLE_MESSAGES[0], "Please wait while we prepare your preview.");
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

  function setSelectionError(show) {
    toolbarCard.classList.toggle('needs-attention', !!show);
  }

  function renderQualityFlags(flags) {
    qualityFlags = Array.isArray(flags) ? flags : [];
    const warnings = [];

    if (qualityFlags.includes("possible_matte_box")) warnings.push("Possible edge artifact detected");
    if (qualityFlags.includes("heavy_soft_edges")) warnings.push("Soft edges detected");
    if (qualityFlags.includes("subject_too_small")) warnings.push("Logo looks small");

    if (warnings.length && selectedVersion === "processed") {
      previewStatus.innerHTML = warnings.map(w => `<span class="warn-badge">⚠ ${w}</span>`).join(" ");
    } else if (selectedVersion === "processed") {
      previewStatus.textContent = "AI processed image selected ✓";
    } else if (selectedVersion === "original") {
      previewStatus.textContent = "Original image selected ✓";
    } else {
      previewStatus.textContent = "Choose a version before saving.";
    }
  }

  function setDoneState() {
    const ready = !!selectedVersion && !isProcessing;
    btnDone.disabled = !ready;
    btnDone.style.display = reviewWrap.style.display === 'flex' ? 'inline-flex' : 'none';
  }

  function setBottomNote(text) {
    document.getElementById('bottomNote').textContent = text;
  }

  function renderVersionSelector() {
    versionSelector.innerHTML = "";

    const makeBtn = ({ label, version = "", mode = "select", active = false }) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "seg-btn";
      if (active) btn.classList.add("active");
      if (mode === "process") btn.classList.add("process");
      btn.textContent = label;

      if (mode === "process") {
        btn.addEventListener("click", runAiProcessing);
        btn.disabled = isProcessing;
      } else {
        btn.addEventListener("click", async () => {
          if (isProcessing) return;
          try {
            setSelectionError(false);
            await switchPreview(version, true);
          } catch (e) {
            console.error(e);
            alert(e.message || "Could not switch image version.");
          }
        });
        btn.disabled = isProcessing;
      }

      versionSelector.appendChild(btn);
    };

    if (!processedAvailable) {
      versionToolbarLabel.childNodes[0].nodeValue = "Choose image version ";
      makeBtn({
        label: "Use original image",
        version: "original",
        mode: "select",
        active: selectedVersion === "original"
      });
      makeBtn({
        label: "Send for AI cleanup",
        mode: "process",
        active: false
      });
      return;
    }

    versionToolbarLabel.childNodes[0].nodeValue = "Choose image version ";
    makeBtn({
      label: "Use original image",
      version: "original",
      mode: "select",
      active: selectedVersion === "original"
    });
    makeBtn({
      label: "Use AI processed image",
      version: "processed",
      mode: "select",
      active: selectedVersion === "processed"
    });
  }

  async function loadPreviewImage(src) {
    return new Promise((resolve, reject) => {
      previewImg.onload = () => resolve();
      previewImg.onerror = () => reject(new Error("Failed to load preview"));
      previewImg.src = src;
    });
  }

  async function switchPreview(version, chooseIt = false) {
    if (!sessionId) return;

    const src = `${API_BASE}/preview/${sessionId}?version=${encodeURIComponent(version)}&t=${Date.now()}`;
    await loadPreviewImage(src);

    activePreviewVersion = version;

    if (chooseIt) {
      const r = await fetch(`${API_BASE}/set-active/${sessionId}?version=${encodeURIComponent(version)}`, {
        method: 'POST',
        cache: 'no-store'
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "Failed to select image version");
      selectedVersion = version;
      setSelectionError(false);
    }

    if (version === 'original') {
      setBottomNote(processedAvailable
        ? "Original image selected. You can switch back to the AI processed version instantly."
        : "Original image selected. You can hit Done now, or send it for AI cleanup first.");
    } else {
      setBottomNote("AI processed image selected. Review it carefully on different backgrounds before clicking Done.");
    }

    renderQualityFlags(version === 'processed' ? qualityFlags : []);
    renderVersionSelector();
    setDoneState();
  }

  async function moveToReviewScreen(showLoader = false, loaderText = "Preparing preview…") {
    uploadShell.style.display = 'none';
    reviewWrap.style.display = 'flex';
    setStepLabel('Review logo');
    renderVersionSelector();
    setDoneState();
    if (showLoader) {
      showOverlay(loaderText, "Please wait while we load your image.");
    }
  }

  async function loadExistingSession(existingId) {
    const r = await fetch(`${API_BASE}/session-info/${existingId}?t=${Date.now()}`, { cache: "no-store" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Saved session not found");

    sessionId = existingId;
    processedAvailable = !!j.processed_available;
    selectedVersion = j.selected ? (j.active_version || "") : "";
    qualityFlags = Array.isArray(j.quality_flags) ? j.quality_flags : [];
    isProcessing = !!j.processing;

    await moveToReviewScreen(true, "Loading saved image…");

    if (selectedVersion === 'processed' && processedAvailable) {
      await switchPreview('processed', false);
    } else {
      await switchPreview('original', false);
    }

    if (isProcessing) {
      startStageCycle();
      await pollReady(j.status_url);
    } else {
      hideOverlay();
      renderQualityFlags(selectedVersion === 'processed' ? qualityFlags : []);
      renderVersionSelector();
      setDoneState();
    }
  }

  async function pollReady(statusUrl) {
    for (let i = 0; i < 240; i++) {
      try {
        const r = await fetch(statusUrl + '?t=' + Date.now(), { cache: "no-store" });
        const j = await r.json();

        const stage = j.stage || j.status || "processing";
        overlayTitle.textContent = STAGE_LABELS[stage] || "Preparing your image…";

        if (j.status === 'ready' && j.processed_available) {
          stopStageCycle();
          processedAvailable = true;
          isProcessing = false;
          qualityFlags = Array.isArray(j.quality_flags) ? j.quality_flags : [];
          await switchPreview('processed', true);
          hideOverlay();
          return;
        }

        if (j.status === 'failed') {
          stopStageCycle();
          isProcessing = false;
          hideOverlay();
          previewStatus.innerHTML = `<span class="warn-badge">⚠ Processing failed — use original image or try again later</span>`;
          selectedVersion = "";
          renderVersionSelector();
          setDoneState();
          setSelectionError(true);
          return;
        }
      } catch (e) {
        console.warn("Status poll error:", e);
      }

      await new Promise(res => setTimeout(res, 800));
    }

    stopStageCycle();
    isProcessing = false;
    overlayTitle.textContent = "Still processing…";
    overlaySub.textContent = "This is taking longer than usual. Please wait a little longer.";
    renderVersionSelector();
    setDoneState();
  }

  async function handlePickedFile() {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;

    uploadStatus.textContent = "Uploading image…";

    const fd = new FormData();
    fd.append('file', f);

    try {
      const r = await fetch(`${API_BASE}/upload`, {
        method: 'POST',
        body: fd,
        cache: 'no-store'
      });

      const ct = (r.headers.get("content-type") || "").toLowerCase();
      if (!ct.includes("application/json")) {
        const text = await r.text();
        throw new Error("Upload failed (non-JSON response): " + text.slice(0, 200));
      }

      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'Upload failed');

      sessionId = j.session_id;
      processedAvailable = false;
      selectedVersion = "";
      qualityFlags = [];
      isProcessing = false;

      await moveToReviewScreen(true, "Loading preview…");
      activePreviewVersion = 'original';
      await loadPreviewImage(`${API_BASE}/preview/${sessionId}?version=original&t=${Date.now()}`);
      hideOverlay();

      renderVersionSelector();
      renderQualityFlags([]);
      setDoneState();
      setSelectionError(false);
      setBottomNote("Choose original or AI before saving. If it looks wrong here, it will look wrong on products.");

    } catch (err) {
      console.error(err);
      alert(err.message || "Upload failed");
      location.reload();
    }
  }

  async function runAiProcessing() {
    if (!sessionId || isProcessing || processedAvailable) return;

    try {
      isProcessing = true;
      setSelectionError(false);
      renderVersionSelector();
      setDoneState();

      const resp = await fetch(`${API_BASE}/process/${sessionId}`, {
        method: 'POST',
        cache: 'no-store'
      });
      const json = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(json.error || "Processing failed to start");

      if (json.cached) {
        processedAvailable = true;
        isProcessing = false;
        await switchPreview('processed', true);
        hideOverlay();
        return;
      }

      startStageCycle();
      await pollReady(`${API_BASE}/status/${sessionId}`);
    } catch (e) {
      console.error(e);
      isProcessing = false;
      stopStageCycle();
      hideOverlay();
      renderVersionSelector();
      setDoneState();
      alert(e.message || "AI processing failed.");
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

    if (!selectedVersion) {
      setSelectionError(true);
      alert("Please choose either the original image or the AI processed image before continuing.");
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
        saved: true,
        active_version: selectedVersion
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
          url.searchParams.set("active_version", selectedVersion);
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
      setDoneState();
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
