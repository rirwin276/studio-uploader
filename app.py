# app.py — Studio Uploader (FastAPI)

import os
import uuid
import threading
from io import BytesIO
from typing import Optional, Dict, Tuple

import requests
from PIL import Image, ImageDraw
from fastapi import FastAPI, UploadFile, File, Query, Request, Form
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# Import your provisioning script
from shopify_provision import run_provisioning

app = FastAPI(title="Studio Uploader", version="1.2.0")

# ----------------------------
# CORS FIX
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
EDITOR_PX = 1500 # Size used for the browser editor to keep it fast
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

def _normalize_logo(img: Image.Image, pad_ratio: float = 0.06, target_size: int = TARGET_PX) -> Image.Image:
    img = _trim_transparent_padding(img.convert("RGBA"))
    canvas = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    w, h = img.size
    if w <= 0 or h <= 0: return canvas
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
# Endpoints
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

    # Scale the uploaded image to fit inside the editor parameters
    img_scaled = _scale_to_fit(img, EDITOR_PX)

    if keep_original:
        curr = img_scaled
    else:
        # Remove background
        img_for_rembg = _downscale_for_rembg(img_scaled, max_dim=REMBG_MAX_DIM)
        curr = _rembg_to_pil(img_for_rembg)
        curr = curr.resize(img_scaled.size, Image.LANCZOS)

    # ALIGNMENT FIX: Paste both images onto an identical transparent square canvas.
    # This guarantees the browser canvas (which is square) maps 1:1 to the image pixels, fixing the Restore brush!
    canvas_orig = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
    canvas_curr = Image.new("RGBA", (EDITOR_PX, EDITOR_PX), (0, 0, 0, 0))
    
    x = (EDITOR_PX - img_scaled.width) // 2
    y = (EDITOR_PX - img_scaled.height) // 2
    
    canvas_orig.alpha_composite(img_scaled, (x, y))
    canvas_curr.alpha_composite(curr, (x, y))

    _save_png(canvas_orig, p["orig"]) 
    _save_png(canvas_curr, p["curr"])       

    return {"status": "ok", "session_id": session_id, "preview_url": f"/preview/{session_id}", "original_url": f"/original/{session_id}"}

@app.get("/preview/{session_id}")
def get_preview(session_id: str):
    return Response(content=open(_paths(session_id)["curr"], "rb").read(), media_type="image/png")

@app.get("/original/{session_id}")
def get_original(session_id: str):
    return Response(content=open(_paths(session_id)["orig"], "rb").read(), media_type="image/png")

# --- SAVES THE BROWSER EDIT ---
@app.post("/save-edit/{session_id}")
async def save_edit(session_id: str, file: UploadFile = File(...)):
    p = _paths(session_id)
    data = await file.read()
    
    # Take the browser's final edit, trim the padding, and scale to 3000x3000 for Printify
    browser_img = Image.open(BytesIO(data)).convert("RGBA")
    final_img = _normalize_logo(browser_img, target_size=TARGET_PX)
    
    _save_png(final_img, p["curr"])
    return {"status": "ok"}

# --- FIX: THIS MUST BE A POST ROUTE FOR SHOPIFY ---
@app.post("/finalize/{session_id}")
def finalize(session_id: str):
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
        except Exception as e:
            print(f"⚠️ Error tagging customer: {e}")

    main_bytes = await storefront_logo_file.read()
    sec_bytes = await storefront_logo_secondary.read() if storefront_logo_secondary else None

    # Run the heavy lifting in the background
    thread = threading.Thread(
        target=run_provisioning, 
        args=(storefront_name, storefront_handle, customer_id, main_bytes, sec_bytes)
    )
    thread.start()

    return {"status": "ok", "message": "Provisioning started in background."}

# ----------------------------
# Premium HTML5 UI (Smoother & Prettier)
# ----------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request, embed: int = Query(0), return_mode: str = Query("download", alias="return"), slot: str = Query("main")):
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
        --text: #f8fafc;
        --muted: #94a3b8;
        --radius: 16px;
    }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; display: flex; flex-direction: column; min-height: 100vh; overscroll-behavior: none; }
    
    .header { padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; background: rgba(30, 41, 59, 0.8); backdrop-filter: blur(12px); border-bottom: 1px solid rgba(255,255,255,0.05); z-index: 10; position: relative; }
    .header h2 { margin: 0; font-size: 18px; font-weight: 700; }
    .btn-done { background: var(--primary); color: white; border: none; padding: 8px 16px; border-radius: 99px; font-weight: 600; font-size: 14px; cursor: pointer; transition: background 0.2s; }
    .btn-done:hover { background: var(--primary-hover); }
    .btn-done:disabled { opacity: 0.5; cursor: not-allowed; }

    .main-container { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; position: relative; }

    /* STEP 1: UPLOAD */
    .step-upload { text-align: center; width: 100%; max-width: 400px; display: flex; flex-direction: column; gap: 16px; transition: opacity 0.3s ease; }
    .upload-box { border: 2px dashed rgba(255,255,255,0.2); border-radius: var(--radius); padding: 60px 20px; background: rgba(255,255,255,0.02); cursor: pointer; transition: all 0.2s; position: relative; }
    .upload-box:hover { background: rgba(255,255,255,0.05); border-color: var(--primary); }
    .upload-box input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
    .upload-icon { font-size: 40px; margin-bottom: 12px; display: block; }
    .upload-title { font-size: 18px; font-weight: 600; margin-bottom: 6px; }
    .upload-sub { color: var(--muted); font-size: 14px; }

    /* STEP 2: LOADING */
    .step-loading { display: none; text-align: center; flex-direction: column; align-items: center; gap: 20px; transition: opacity 0.3s ease; }
    .spinner { width: 50px; height: 50px; border: 4px solid rgba(255,255,255,0.1); border-top-color: var(--primary); border-radius: 50%; animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* STEP 3: EDITOR */
    .step-editor { display: none; width: 100%; max-width: 500px; flex-direction: column; gap: 20px; transition: opacity 0.3s ease; }
    .canvas-wrap { 
        background: #fff; border-radius: var(--radius); position: relative; width: 100%; aspect-ratio: 1/1;
        overflow: hidden; border: 1px solid rgba(255,255,255,0.1); touch-action: none; box-shadow: 0 20px 40px rgba(0,0,0,0.3);
    }
    .checker { 
        position:absolute; inset:0; background-color: #e5e5e5;
        background-image: linear-gradient(45deg, #f0f0f0 25%, transparent 25%), linear-gradient(-45deg, #f0f0f0 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #f0f0f0 75%), linear-gradient(-45deg, transparent 75%, #f0f0f0 75%); 
        background-size: 20px 20px; background-position: 0 0, 0 10px, 10px -10px, -10px 0px; z-index: 1; 
    }
    canvas { width: 100%; height: 100%; display: block; position: relative; z-index: 2; touch-action: none; cursor: none; }
    
    #cursor { position: fixed; border: 2px solid white; box-shadow: 0 0 4px rgba(0,0,0,0.8); border-radius: 50%; pointer-events: none; transform: translate(-50%, -50%); z-index: 9999; display: none; }

    /* FLOATING TOOLBAR */
    .toolbar { background: var(--surface); border: 1px solid rgba(255,255,255,0.1); border-radius: 99px; padding: 6px; display: flex; gap: 6px; margin: 0 auto; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
    .tool-btn { background: transparent; color: var(--muted); border: none; padding: 10px 16px; border-radius: 99px; cursor: pointer; font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 6px; transition: all 0.2s; }
    .tool-btn:hover { color: var(--text); background: rgba(255,255,255,0.05); }
    .tool-btn.active { background: rgba(255,255,255,0.1); color: var(--primary); }
    
    .brush-controls { display: flex; align-items: center; gap: 12px; background: var(--surface); padding: 12px 20px; border-radius: 99px; margin-top: 10px; border: 1px solid rgba(255,255,255,0.1); }
    .brush-controls span { font-size: 13px; color: var(--muted); font-weight: 600; }
    input[type=range] { flex: 1; accent-color: var(--primary); }

    .top-actions { display: flex; justify-content: space-between; margin-bottom: 8px; }
    .btn-text { background: none; border: none; color: var(--muted); font-size: 13px; cursor: pointer; font-weight: 600; }
    .btn-text:hover { color: var(--text); }
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
          <input id="file" type="file" accept="image/*,.svg" />
          <span class="upload-icon">🪄</span>
          <div class="upload-title">Tap to Upload Logo</div>
          <div class="upload-sub">We'll auto-remove the background</div>
      </div>
    </div>

    <div class="step-loading" id="step2">
      <div class="spinner"></div>
      <div style="font-weight: 600; font-size: 18px;">AI Processing...</div>
      <div style="color: var(--muted); font-size: 14px;">Removing background and preparing canvas</div>
    </div>

    <div class="step-editor" id="step3">
      <div class="top-actions">
          <button class="btn-text" onclick="location.reload()">Start Over</button>
          <button class="btn-text" id="btnUndo">↩️ Undo Edit</button>
      </div>

      <div class="canvas-wrap" id="canvas-container">
        <div class="checker"></div>
        <canvas id="cv" width="1000" height="1000"></canvas>
      </div>

      <div class="toolbar">
        <button class="tool-btn active" id="btnRestore">🖌️ Restore</button>
        <button class="tool-btn" id="btnErase">🧹 Erase</button>
        <button class="tool-btn" id="btnMagic">✨ Magic Fill</button>
      </div>

      <div class="brush-controls" id="brush-controls">
          <span>Size</span>
          <input type="range" id="brushSize" min="10" max="150" value="50">
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
  const brushSlider = document.getElementById('brushSize');
  const brushControls = document.getElementById('brush-controls');
  const cursor = document.getElementById('cursor');
  const canvasContainer = document.getElementById('canvas-container');

  const params = new URLSearchParams(window.location.search);
  const RETURN_MODE = params.get('return') || 'download';
  const SLOT = params.get('slot') || 'main';

  // --- STATE & CURSOR ---
  function saveState() {
      if(history.length > 10) history.shift();
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

  // --- UPLOAD PIPELINE ---
  fileEl.addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    
    // UI Transition
    step1.style.display = 'none';
    step2.style.display = 'flex';
    
    const fd = new FormData();
    fd.append('file', f);
    
    try {
        const r = await fetch(`/upload?keep_original=false`, { method: 'POST', body: fd });
        const j = await r.json();
        sessionId = j.session_id;

        await Promise.all([
            new Promise(res => { origImg.onload = res; origImg.src = `/original/${sessionId}?t=${Date.now()}`; }),
            new Promise(res => { currImg.onload = res; currImg.src = `/preview/${sessionId}?t=${Date.now()}`; })
        ]);

        ctx.clearRect(0, 0, cv.width, cv.height);
        ctx.drawImage(currImg, 0, 0, cv.width, cv.height);

        // UI Transition
        step2.style.display = 'none';
        step3.style.display = 'flex';
        btnDone.style.display = 'block';
        updateCursorSize();
    } catch(err) {
        alert("Upload failed. Please try again.");
        location.reload();
    }
  });

  // --- TOOL SWITCHING ---
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

  // --- MAGIC WAND (FLOOD FILL) ---
  function magicRemove(startX, startY) {
      startX = Math.floor(startX); startY = Math.floor(startY);
      const imgData = ctx.getImageData(0, 0, cv.width, cv.height);
      const data = imgData.data;
      const w = cv.width, h = cv.height;

      const startPos = (startY * w + startX) * 4;
      const sr = data[startPos], sg = data[startPos+1], sb = data[startPos+2], sa = data[startPos+3];
      if (sa === 0) return; // Ignore already transparent areas

      const stack = [startX, startY];
      const seen = new Uint8Array(w * h);
      seen[startY * w + startX] = 1;
      const tolerance = 60; 

      while (stack.length > 0) {
          const y = stack.pop();
          const x = stack.pop();
          const pos = (y * w + x) * 4;
          data[pos + 3] = 0; // Erase alpha

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

  // --- DRAWING LOGIC ---
  function drawBrush(x, y) {
    const bSize = parseInt(brushSlider.value);
    
    // ALWAYS reset composite before drawing path
    offCtx.globalCompositeOperation = 'source-over';
    offCtx.clearRect(0, 0, 1000, 1000);
    
    offCtx.shadowBlur = bSize / 4;
    offCtx.shadowColor = 'black';
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

  // --- FINISH & SAVE ---
  btnDone.addEventListener('click', () => {
    btnDone.innerText = "Saving...";
    btnDone.disabled = true;
    cv.style.opacity = '0.5';

    cv.toBlob(async (blob) => {
        const fd = new FormData();
        fd.append("file", blob, "edited_logo.png");
        
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