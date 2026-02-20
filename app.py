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

app = FastAPI(title="Studio Uploader", version="1.0.0")

# ----------------------------
# CORS
# ----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Storage + Config
# ----------------------------
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

TARGET_PX = 3000
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
REMBG_MAX_DIM = int(os.getenv("REMBG_MAX_DIM", "1600"))

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

    # 1. Scale raw image
    img_scaled = _scale_to_fit(img, 1500) # Smaller resolution for the browser editor

    # 2. Process background
    if keep_original:
        curr = img_scaled
    else:
        img_for_rembg = _downscale_for_rembg(img_scaled, max_dim=REMBG_MAX_DIM)
        curr = _rembg_to_pil(img_for_rembg)
        curr = curr.resize(img_scaled.size, Image.LANCZOS)

    _save_png(img_scaled, p["orig"]) # Used by the browser for restoring
    _save_png(curr, p["curr"])       # Used by the browser for initial canvas

    return {"status": "ok", "session_id": session_id, "preview_url": f"/preview/{session_id}", "original_url": f"/original/{session_id}"}

@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    return Response(content=open(_paths(session_id)["curr"], "rb").read(), media_type="image/png")

@app.get("/original/{session_id}")
def get_original(session_id: str):
    return Response(content=open(_paths(session_id)["orig"], "rb").read(), media_type="image/png")

# --- THE NEW SAVE ENDPOINT (Receives the edited browser canvas) ---
@app.post("/save-edit/{session_id}")
async def save_edit(session_id: str, file: UploadFile = File(...)):
    p = _paths(session_id)
    data = await file.read()
    
    # Take the browser's final edit, trim the padding, and scale to 3000x3000
    browser_img = Image.open(BytesIO(data)).convert("RGBA")
    final_img = _normalize_logo(browser_img)
    
    _save_png(final_img, p["curr"])
    return {"status": "ok"}

@app.get("/finalize/{session_id}")
def finalize(session_id: str):
    # Just returns the final saved image to Shopify
    p = _paths(session_id)
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

    main_bytes = await storefront_logo_file.read()
    sec_bytes = None
    if storefront_logo_secondary:
        sec_bytes = await storefront_logo_secondary.read()

    thread = threading.Thread(
        target=run_provisioning, 
        args=(storefront_name, storefront_handle, customer_id, main_bytes, sec_bytes)
    )
    thread.start()

    return {"status": "ok", "message": "Provisioning started in background."}

# ----------------------------
# The New Fast Client-Side UI
# ----------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request, embed: int = Query(0), return_mode: str = Query("download", alias="return"), slot: str = Query("main")):
    html = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=0" />
  <title>Logo Prep</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0b0c10; color: white; margin: 0; display: flex; align-items: center; justify-content: center; min-height: 100vh; overscroll-behavior: none; }
    .modal { width: 100%; max-width: 600px; background: #1f232a; border-radius: 16px; padding: 20px; text-align: center; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
    h2 { margin: 0 0 8px; font-size: 24px; }
    p { color: #aaa; font-size: 14px; margin-bottom: 20px; }
    input[type="file"] { padding: 40px 20px; border: 2px dashed #444; border-radius: 12px; width: calc(100% - 44px); margin-bottom: 20px; cursor: pointer; background: #111; color: #888; }
    
    .canvas-wrap { 
        background: #fff; border-radius: 12px; margin: 0 auto 15px; position: relative; max-width: 400px; 
        overflow: hidden; border: 1px solid #333; touch-action: none; 
    }
    .checker { 
        position:absolute; inset:0; background-color: #e5e5e5;
        background-image: linear-gradient(45deg, #f0f0f0 25%, transparent 25%), linear-gradient(-45deg, #f0f0f0 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #f0f0f0 75%), linear-gradient(-45deg, transparent 75%, #f0f0f0 75%); 
        background-size: 20px 20px; background-position: 0 0, 0 10px, 10px -10px, -10px 0px; z-index: 1; 
    }
    
    /* The main visible canvas */
    canvas { width: 100%; display: block; position: relative; z-index: 2; cursor: crosshair; touch-action: none; }
    
    .tools-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .btn-undo { background: #333; color: white; border: none; padding: 8px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: bold; }
    .btn-undo:active { background: #555; }
    
    .tools { display: flex; justify-content: center; gap: 8px; margin-bottom: 20px; padding: 10px; background: #111; border-radius: 12px; border: 1px solid #333; }
    .tool-btn { background: #333; color: white; border: none; padding: 12px 16px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; flex: 1; }
    .tool-btn.active { background: #34c759; color: black; }
    
    .slider-container { display: flex; align-items: center; gap: 10px; margin-bottom: 15px; color: #888; font-size: 13px; }
    input[type=range] { flex: 1; }

    .btn-done { background: #34c759; color: black; border: none; padding: 14px 24px; font-weight: bold; border-radius: 12px; cursor: pointer; font-size: 16px; width: 100%; margin-bottom: 10px; }
    .btn-done:hover { background: #2fae4e; }
    .btn-done:disabled { background: #222; color: #555; cursor: not-allowed; }
    
    .advanced-toggle { background: none; border: none; color: #888; cursor: pointer; font-size: 13px; text-decoration: underline; padding: 5px; }
    .advanced-toggle:hover { color: #fff; }
  </style>
</head>
<body>
  <div class="modal">
    <h2>Prepare your Logo</h2>
    <p id="status-text">Upload a file. We'll automatically remove the background.</p>
    
    <input id="file" type="file" accept="image/*,.svg" />
    
    <div id="editor-section" style="display:none;">
      <div class="tools-top" id="tools-top" style="display:none;">
          <span style="font-size: 13px; color: #aaa;">Swipe to edit</span>
          <button class="btn-undo" id="btnUndo">↩️ Undo</button>
      </div>

      <div class="canvas-wrap">
        <div class="checker"></div>
        <canvas id="cv" width="1000" height="1000"></canvas>
      </div>

      <div id="tools-section" style="display:none;">
        <div class="tools">
          <button class="tool-btn active" id="btnRestore">🖌️ Restore Part</button>
          <button class="tool-btn" id="btnErase">🪄 Erase Part</button>
        </div>
        <div class="slider-container">
            <span>Brush Size:</span>
            <input type="range" id="brushSize" min="10" max="100" value="40">
        </div>
      </div>

      <button class="btn-done" id="btnDone">Looks Good — Done</button>
      <button class="advanced-toggle" id="btnToggleTools">Wait, it deleted part of my logo! (Fix a mistake)</button>
    </div>
  </div>

<script>
  let sessionId = null;
  let mode = 'restore'; 
  let isDown = false;
  let lastX = 0, lastY = 0;
  
  // History array for the Undo function
  let history = [];

  const fileEl = document.getElementById('file');
  const btnDone = document.getElementById('btnDone');
  const editor = document.getElementById('editor-section');
  const status = document.getElementById('status-text');
  
  // Main Canvas
  const cv = document.getElementById('cv');
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  
  // Invisible canvas for drawing buttery smooth brush strokes
  const offCanvas = document.createElement('canvas');
  offCanvas.width = 1000; offCanvas.height = 1000;
  const offCtx = offCanvas.getContext('2d');

  const origImg = new Image();
  const currImg = new Image();

  const btnToggleTools = document.getElementById('btnToggleTools');
  const toolsSection = document.getElementById('tools-section');
  const toolsTop = document.getElementById('tools-top');
  const btnErase = document.getElementById('btnErase');
  const btnRestore = document.getElementById('btnRestore');
  const btnUndo = document.getElementById('btnUndo');
  const brushSlider = document.getElementById('brushSize');

  const params = new URLSearchParams(window.location.search);
  const RETURN_MODE = params.get('return') || 'download';
  const SLOT = params.get('slot') || 'main';

  // Save state for undo
  function saveState() {
      if(history.length > 15) history.shift(); // keep last 15 edits
      history.push(ctx.getImageData(0, 0, cv.width, cv.height));
  }

  btnUndo.addEventListener('click', () => {
      if(history.length > 0) {
          ctx.putImageData(history.pop(), 0, 0);
      }
  });

  // Upload Logic
  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    status.innerText = "AI removing background... please wait.";
    fileEl.style.display = 'none';
    
    const fd = new FormData();
    fd.append('file', f);
    
    const r = await fetch(`/upload?keep_original=false`, { method: 'POST', body: fd });
    const j = await r.json();
    sessionId = j.session_id;

    // Load both images into browser memory
    await Promise.all([
        new Promise(res => { origImg.onload = res; origImg.src = `/original/${sessionId}?t=${Date.now()}`; }),
        new Promise(res => { currImg.onload = res; currImg.src = `/preview/${sessionId}?t=${Date.now()}`; })
    ]);

    // Draw the initial AI result
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.drawImage(currImg, 0, 0, cv.width, cv.height);

    editor.style.display = 'block';
    status.style.display = 'none';
  });

  // Tool UI logic
  btnToggleTools.addEventListener('click', () => {
    toolsSection.style.display = 'block';
    toolsTop.style.display = 'flex';
    btnToggleTools.style.display = 'none';
    document.querySelector('#editor-section h3').style.display = 'none';
  });

  btnErase.addEventListener('click', () => { mode = 'remove'; btnErase.classList.add('active'); btnRestore.classList.remove('active'); });
  btnRestore.addEventListener('click', () => { mode = 'restore'; btnRestore.classList.add('active'); btnErase.classList.remove('active'); });

  // Get coordinates that match the actual 1000x1000 canvas size regardless of screen size
  function getCoords(evt) {
    const rect = cv.getBoundingClientRect();
    const clientX = evt.touches ? evt.touches[0].clientX : evt.clientX;
    const clientY = evt.touches ? evt.touches[0].clientY : evt.clientY;
    return {
      x: ((clientX - rect.left) / rect.width) * cv.width,
      y: ((clientY - rect.top) / rect.height) * cv.height
    };
  }

  // Draw Smooth Line Logic (Client-Side Only)
  function drawBrush(x, y) {
    const bSize = parseInt(brushSlider.value);
    
    // Draw the thick stroke on the invisible off-screen canvas
    offCtx.clearRect(0, 0, 1000, 1000);
    offCtx.lineWidth = bSize * 2;
    offCtx.lineCap = 'round';
    offCtx.lineJoin = 'round';
    offCtx.beginPath();
    offCtx.moveTo(lastX, lastY);
    offCtx.lineTo(x, y);
    offCtx.stroke();

    if(mode === 'remove') {
        // Erase pixels on the main canvas
        ctx.globalCompositeOperation = 'destination-out';
        ctx.drawImage(offCanvas, 0, 0);
    } else {
        // Mask the original image to the stroke, then draw onto main canvas
        offCtx.globalCompositeOperation = 'source-in';
        offCtx.drawImage(origImg, 0, 0, 1000, 1000);
        
        ctx.globalCompositeOperation = 'source-over';
        ctx.drawImage(offCanvas, 0, 0);
    }
    
    lastX = x; lastY = y;
  }

  // Mouse & Touch Events
  const startDraw = (e) => {
      saveState(); // Save before we change anything
      isDown = true;
      const c = getCoords(e);
      lastX = c.x; lastY = c.y;
      drawBrush(c.x, c.y); // Draw single dot if just tapped
      if(e.cancelable) e.preventDefault(); // Stop mobile scroll
  };
  
  const moveDraw = (e) => {
      if(!isDown) return;
      const c = getCoords(e);
      drawBrush(c.x, c.y);
      if(e.cancelable) e.preventDefault(); // Stop mobile scroll
  };
  
  const endDraw = () => { isDown = false; };

  cv.addEventListener('mousedown', startDraw);
  cv.addEventListener('mousemove', moveDraw);
  window.addEventListener('mouseup', endDraw);
  
  cv.addEventListener('touchstart', startDraw, {passive: false});
  cv.addEventListener('touchmove', moveDraw, {passive: false});
  window.addEventListener('touchend', endDraw);

  // Done Button -> Sends the final edited canvas back to Python
  btnDone.addEventListener('click', () => {
    btnDone.innerText = "Saving... please wait";
    btnDone.disabled = true;

    // Convert the browser canvas to a real image file (Blob)
    cv.toBlob(async (blob) => {
        const fd = new FormData();
        fd.append("file", blob, "edited_logo.png");
        
        // Send to server to be scaled perfectly to 3000x3000 for Printify
        await fetch(`/save-edit/${sessionId}`, { method: 'POST', body: fd });
        
        if (RETURN_MODE === 'postmessage') {
            window.parent.postMessage({
                type: "studio-uploader:done", 
                slot: SLOT, 
                session_id: sessionId, 
                finalize_url: `${window.location.origin}/finalize/${sessionId}`
            }, "*");
        }
    }, "image/png");
  });
</script>
</body>
</html>"""
    return HTMLResponse(content=html)