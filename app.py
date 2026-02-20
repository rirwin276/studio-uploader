# app.py — Studio Uploader (FastAPI)

import os
import uuid
import threading
from io import BytesIO
from typing import Optional, Dict, Tuple

import requests
from PIL import Image, ImageDraw
from fastapi import FastAPI, UploadFile, File, Query, Request, Form
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

# Import the provisioning script we built
from shopify_provision import run_provisioning

app = FastAPI(title="Studio Uploader", version="0.8.0")

# ----------------------------
# CORS
# ----------------------------
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Storage + Config
# ----------------------------
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

TARGET_PX = 3000
DEFAULT_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60)))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
REMBG_MAX_DIM = int(os.getenv("REMBG_MAX_DIM", "1600"))
UI_CANVAS_PX = int(os.getenv("UI_CANVAS_PX", "1000"))

# ----------------------------
# MEMORY FIX: Lazy Load AI
# ----------------------------
REMBG_MODEL = os.getenv("REMBG_MODEL", "u2netp")
_SESSION = None

def get_rembg_session():
    global _SESSION
    if _SESSION is None:
        print("🧠 Loading AI Model into Memory...")
        from rembg import new_session
        _SESSION = new_session(REMBG_MODEL)
    return _SESSION

# ----------------------------
# Image Processing Logic
# ----------------------------
def _paths(session_id: str) -> Dict[str, str]:
    return {
        "orig": os.path.join(UPLOAD_DIR, f"{session_id}_orig.png"),
        "base": os.path.join(UPLOAD_DIR, f"{session_id}_base.png"),
        "curr": os.path.join(UPLOAD_DIR, f"{session_id}_curr.png"),
    }

def _save_png(img: Image.Image, path: str):
    img.save(path, "PNG", optimize=True)

def _scale_to_fit(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if w <= max_dim and h <= max_dim: return img
    scale = min(max_dim / w, max_dim / h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

def _downscale_for_rembg(img: Image.Image, max_dim: int = REMBG_MAX_DIM) -> Image.Image:
    return _scale_to_fit(img, max_dim)

def _rembg_to_pil(img: Image.Image) -> Image.Image:
    from rembg import remove
    out = remove(img, session=get_rembg_session())
    if isinstance(out, (bytes, bytearray)):
        return Image.open(BytesIO(out)).convert("RGBA")
    return out.convert("RGBA")

def _trim_transparent_padding(img: Image.Image, alpha_threshold: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    a = img.split()[-1]
    bbox = a.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    if not bbox: return img
    return img.crop(bbox)

def _normalize_logo(img: Image.Image, pad_ratio: float = 0.06) -> Image.Image:
    img = _trim_transparent_padding(img.convert("RGBA"))
    canvas = Image.new("RGBA", (TARGET_PX, TARGET_PX), (0, 0, 0, 0))
    w, h = img.size
    if w <= 0 or h <= 0: return canvas
    max_dim = int(TARGET_PX * (1.0 - pad_ratio * 2.0))
    scale = min(max_dim / w, max_dim / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    img2 = img.resize((new_w, new_h), Image.LANCZOS)
    x = (TARGET_PX - new_w) // 2
    y = (TARGET_PX - new_h) // 2
    canvas.alpha_composite(img2, (x, y))
    return canvas

def _apply_circle_alpha(img: Image.Image, x: int, y: int, radius: int, make_transparent: bool, restore_from: Optional[Image.Image] = None) -> Image.Image:
    img = img.copy()
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)

    if make_transparent:
        r, g, b, a = img.split()
        a = Image.composite(Image.new("L", img.size, 0), a, mask)
        return Image.merge("RGBA", (r, g, b, a))

    if restore_from is None: return img
    restore_from = restore_from.convert("RGBA")
    return Image.composite(restore_from, img, mask)

def _scale_ui_coords_to_image(img: Image.Image, x_ui: int, y_ui: int, r_ui: int) -> Tuple[int, int, int]:
    w, h = img.size
    sx = w / float(UI_CANVAS_PX)
    sy = h / float(UI_CANVAS_PX)
    x = int(round(x_ui * sx))
    y = int(round(y_ui * sy))
    s = (sx + sy) / 2.0
    r = max(1, int(round(r_ui * s)))
    return max(0, min(w - 1, x)), max(0, min(h - 1, y)), r

# ----------------------------
# Image Endpoints
# ----------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui")

@app.post("/upload")
async def upload_image(file: UploadFile = File(...), keep_original: bool = Query(False)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File too large"}, status_code=413)

    img = Image.open(BytesIO(data)).convert("RGBA")
    session_id = str(uuid.uuid4())
    p = _paths(session_id)

    img_scaled = _scale_to_fit(img, TARGET_PX)

    if keep_original:
        curr = img_scaled
    else:
        img_for_rembg = _downscale_for_rembg(img_scaled, max_dim=REMBG_MAX_DIM)
        curr = _rembg_to_pil(img_for_rembg)
        curr = curr.resize(img_scaled.size, Image.LANCZOS)

    canvas_orig = Image.new("RGBA", (TARGET_PX, TARGET_PX), (0, 0, 0, 0))
    canvas_curr = Image.new("RGBA", (TARGET_PX, TARGET_PX), (0, 0, 0, 0))
    
    x = (TARGET_PX - img_scaled.width) // 2
    y = (TARGET_PX - img_scaled.height) // 2
    
    canvas_orig.alpha_composite(img_scaled, (x, y))
    canvas_curr.alpha_composite(curr, (x, y))

    _save_png(canvas_orig, p["orig"])
    _save_png(canvas_curr, p["base"])
    _save_png(canvas_curr, p["curr"])

    return {"status": "ok", "session_id": session_id, "preview_url": f"/preview/{session_id}", "base_url": f"/base/{session_id}"}

@app.get("/meta/{session_id}")
def get_meta(session_id: str):
    return {"session_id": session_id, "badge": "optimized", "target_px": TARGET_PX}

@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    return Response(content=open(_paths(session_id)["curr"], "rb").read(), media_type="image/png")

@app.get("/base/{session_id}")
def get_base(session_id: str):
    return Response(content=open(_paths(session_id)["base"], "rb").read(), media_type="image/png")

@app.get("/original/{session_id}")
def get_original(session_id: str):
    return Response(content=open(_paths(session_id)["orig"], "rb").read(), media_type="image/png")

# ----------------------------
# Tap tools (Brush)
# ----------------------------
@app.post("/tap/remove")
def tap_remove(session_id: str, x: int, y: int, radius: int = 24):
    p = _paths(session_id)
    curr = Image.open(p["curr"]).convert("RGBA")
    x2, y2, r2 = _scale_ui_coords_to_image(curr, x, y, radius)
    curr2 = _apply_circle_alpha(curr, x2, y2, r2, make_transparent=True)
    _save_png(curr2, p["curr"])
    return {"status": "ok"}

@app.post("/tap/restore")
def tap_restore(session_id: str, x: int, y: int, radius: int = 24):
    p = _paths(session_id)
    curr = Image.open(p["curr"]).convert("RGBA")
    orig = Image.open(p["orig"]).convert("RGBA") 
    x2, y2, r2 = _scale_ui_coords_to_image(curr, x, y, radius)
    curr2 = _apply_circle_alpha(curr, x2, y2, r2, make_transparent=False, restore_from=orig)
    _save_png(curr2, p["curr"])
    return {"status": "ok"}

@app.post("/reset/{session_id}")
def reset_session(session_id: str):
    p = _paths(session_id)
    base = Image.open(p["base"])
    _save_png(base, p["curr"])
    return {"status": "ok"}

@app.post("/finalize/{session_id}")
def finalize(session_id: str):
    p = _paths(session_id)
    curr = Image.open(p["curr"]).convert("RGBA")
    final_img = _normalize_logo(curr) 
    _save_png(final_img, p["curr"])
    img_bytes = open(p["curr"], "rb").read()
    return Response(content=img_bytes, media_type="image/png")

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
    user_count: str = Form(None),
    duration: str = Form(None),
    military_branch: str = Form(None),
    sport_type: str = Form(None),
    storefront_logo_file: UploadFile = File(...),
    storefront_logo_secondary: Optional[UploadFile] = File(None)
):
    # 1. TAG CUSTOMER INSTANTLY
    SHOP_URL = os.environ.get("SHOPIFY_SHOP") or os.environ.get("SHOP")
    SHOP_TOKEN = os.environ.get("SHOPIFY_TOKEN") or os.environ.get("CLIENT_SECRET")

    if SHOP_URL and SHOP_TOKEN:
        try:
            clean_id = customer_id.split("/")[-1] 
            tag_data = {"customer": {"id": clean_id, "tags": f"storefront-admin--{storefront_handle}"}}
            headers = {"X-Shopify-Access-Token": SHOP_TOKEN, "Content-Type": "application/json"}
            requests.put(f"https://{SHOP_URL}/admin/api/2024-01/customers/{clean_id}.json", json=tag_data, headers=headers)
            print(f"✅ Tagged customer: {clean_id}")
        except Exception as e:
            print(f"⚠️ Error tagging customer: {e}")

    # 2. READ FILES
    main_bytes = await storefront_logo_file.read()
    sec_bytes = None
    if storefront_logo_secondary:
        sec_bytes = await storefront_logo_secondary.read()

    # 3. RUN PROVISIONING IN BACKGROUND
    thread = threading.Thread(
        target=run_provisioning, 
        args=(storefront_name, storefront_handle, customer_id, main_bytes, sec_bytes)
    )
    thread.start()

    # 4. INSTANT REPLY TO SHOPIFY
    return {"status": "ok", "message": "Provisioning started in background."}

# ----------------------------
# Customer UI
# ----------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request, embed: int = Query(0), return_mode: str = Query("download", alias="return"), slot: str = Query("main")):
    html = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Logo Prep</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0b0c10; color: white; margin: 0; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .modal { width: 100%; max-width: 600px; background: #1f232a; border-radius: 16px; padding: 24px; text-align: center; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
    h2 { margin: 0 0 8px; font-size: 24px; }
    p { color: #aaa; font-size: 14px; margin-bottom: 20px; }
    input[type="file"] { padding: 40px 20px; border: 2px dashed #444; border-radius: 12px; width: calc(100% - 44px); margin-bottom: 20px; cursor: pointer; background: #111; color: #888; }
    .canvas-wrap { background: #111; border-radius: 12px; margin: 0 auto 20px; position: relative; max-width: 400px; overflow: hidden; border: 1px solid #333; }
    
    /* Checkerboard background so they can see what is transparent */
    .checker { position:absolute; inset:0; background: linear-gradient(45deg, rgba(255,255,255,0.06) 25%, transparent 25%), linear-gradient(-45deg, rgba(255,255,255,0.06) 25%, transparent 25%), linear-gradient(45deg, transparent 75%, rgba(255,255,255,0.06) 75%), linear-gradient(-45deg, transparent 75%, rgba(255,255,255,0.06) 75%); background-size: 20px 20px; background-position: 0 0, 0 10px, 10px -10px, -10px 0px; opacity: 0.5; z-index: 1; }
    
    canvas { width: 100%; display: block; touch-action: none; cursor: crosshair; position: relative; z-index: 2; }
    .tools { display: flex; justify-content: center; gap: 10px; margin-bottom: 20px; padding: 12px; background: #111; border-radius: 12px; border: 1px solid #333; }
    .tool-btn { background: #333; color: white; border: none; padding: 10px 16px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; }
    .tool-btn.active { background: #34c759; color: black; }
    .btn-done { background: #34c759; color: black; border: none; padding: 14px 24px; font-weight: bold; border-radius: 12px; cursor: pointer; font-size: 16px; width: 100%; margin-bottom: 10px; }
    .btn-done:hover { background: #2fae4e; }
    .advanced-toggle { background: none; border: none; color: #888; cursor: pointer; font-size: 13px; text-decoration: underline; padding: 5px; }
    .advanced-toggle:hover { color: #fff; }
  </style>
</head>
<body>
  <div class="modal">
    <h2>Prepare your Logo</h2>
    <p id="status-text">Upload a file. We'll automatically remove the background and size it perfectly.</p>
    
    <input id="file" type="file" accept="image/*,.svg" />
    
    <div id="editor-section" style="display:none;">
      <h3 style="color: #34c759; margin-top:0;">✨ Background Removed!</h3>
      <p style="margin-bottom: 15px;">If it looks perfect, click Done.</p>
      
      <div class="canvas-wrap">
        <div class="checker"></div>
        <canvas id="cv" width="1000" height="1000"></canvas>
      </div>

      <div class="tools" id="tools-section" style="display:none;">
        <button class="tool-btn active" id="btnRestore">🖌️ Bring Back (Restore)</button>
        <button class="tool-btn" id="btnErase">🪄 Erase Extra</button>
        <button class="tool-btn" id="btnReset" style="background:#555;">↺ Undo All</button>
      </div>

      <button class="btn-done" id="btnDone">Looks Good — Done</button>
      <button class="advanced-toggle" id="btnToggleTools">Wait, it deleted part of my logo! (Fix a mistake)</button>
    </div>
  </div>

<script>
  let sessionId = null;
  let mode = 'restore'; 
  let isDown = false;
  
  const fileEl = document.getElementById('file');
  const btnDone = document.getElementById('btnDone');
  const editor = document.getElementById('editor-section');
  const status = document.getElementById('status-text');
  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d');

  const btnToggleTools = document.getElementById('btnToggleTools');
  const toolsSection = document.getElementById('tools-section');
  const btnErase = document.getElementById('btnErase');
  const btnRestore = document.getElementById('btnRestore');
  const btnReset = document.getElementById('btnReset');

  const params = new URLSearchParams(window.location.search);
  const RETURN_MODE = params.get('return') || 'download';
  const SLOT = params.get('slot') || 'main';

  function refreshCanvas() {
    const img = new Image();
    img.onload = () => {
      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, 0, 0, cv.width, cv.height);
    };
    img.src = `/preview/${sessionId}?t=${Date.now()}`;
  }

  // File Upload
  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    status.innerText = "Removing background... please wait.";
    fileEl.style.display = 'none';
    
    const fd = new FormData();
    fd.append('file', f);
    
    const r = await fetch(`/upload?keep_original=false`, { method: 'POST', body: fd });
    const j = await r.json();
    sessionId = j.session_id;

    editor.style.display = 'block';
    status.style.display = 'none';
    refreshCanvas();
  });

  // Tool Selection
  btnToggleTools.addEventListener('click', () => {
    toolsSection.style.display = 'flex';
    btnToggleTools.style.display = 'none';
    document.querySelector('#editor-section p').innerText = "Swipe over the image to restore missing pieces or erase extra background.";
  });

  btnErase.addEventListener('click', () => { mode = 'remove'; btnErase.classList.add('active'); btnRestore.classList.remove('active'); });
  btnRestore.addEventListener('click', () => { mode = 'restore'; btnRestore.classList.add('active'); btnErase.classList.remove('active'); });
  
  btnReset.addEventListener('click', async () => {
      await fetch(`/reset/${sessionId}`, { method: 'POST' });
      refreshCanvas();
  });

  // Brush Logic
  function getCoords(evt) {
    const rect = cv.getBoundingClientRect();
    const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    const clientY = evt.touches ? evt.touches[0].clientY : evt.clientY;
    return {
      x: Math.round((clientX - rect.left) * (cv.width / rect.width)),
      y: Math.round((clientY - rect.top) * (cv.height / rect.height))
    };
  }

  async function applyTap(x, y) {
    if (!sessionId) return;
    const endpoint = mode === 'remove' ? '/tap/remove' : '/tap/restore';
    await fetch(`${endpoint}?session_id=${sessionId}&x=${x}&y=${y}&radius=45`, { method: 'POST' });
    refreshCanvas();
  }

  cv.addEventListener('mousedown', (e) => { isDown = true; const c = getCoords(e); applyTap(c.x, c.y); });
  cv.addEventListener('mousemove', (e) => { if(!isDown) return; const c = getCoords(e); applyTap(c.x, c.y); });
  window.addEventListener('mouseup', () => isDown = false);
  
  cv.addEventListener('touchstart', (e) => { isDown = true; const c = getCoords(e); applyTap(c.x, c.y); e.preventDefault(); }, {passive: false});
  cv.addEventListener('touchmove', (e) => { if(!isDown) return; const c = getCoords(e); applyTap(c.x, c.y); e.preventDefault(); }, {passive: false});
  window.addEventListener('touchend', () => isDown = false);

  // Finish
  btnDone.addEventListener('click', () => {
    if (RETURN_MODE === 'postmessage') {
      btnDone.innerText = "Saving...";
      window.parent.postMessage({
        type: "studio-uploader:done", slot: SLOT, session_id: sessionId, finalize_url: `${window.location.origin}/finalize/${sessionId}`
      }, "*");
    }
  });
</script>
</body>
</html>"""
    return HTMLResponse(content=html)