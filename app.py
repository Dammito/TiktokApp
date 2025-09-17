# app.py — Chef's Aura (AE → Builder TikTok con IA) — Presentación simplificada
# Cambios solicitados:
# 1) No mostrar lista siempre: solo formulario "Agregar por link/ID".
# 2) Si el producto ya existe (por Product ID), mostrar "producto existe" y NO agregar.
# 3) Botón para ver la lista guardada (ID, Title, Link) con búsqueda y eliminar.
# 4) Al agregar un producto nuevo, abrir automáticamente el Builder (IA) solo para ese producto.

import os
import re
import io
import json
import time
import html
import hmac
import hashlib
import zipfile
import itertools
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageOps

# ====== OPENAI (IA) — (dejado tal cual nos diste) ======
OPENAI_API_KEY = "   "  #clave de OPEN AI

USE_NEW_OPENAI = False
try:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    USE_NEW_OPENAI = True
except Exception:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
    except Exception:
        openai = None

# =========================
# CONFIG / CREDENTIALS — ALIEXPRESS (dejado igual)
# =========================
APP_KEY      = os.getenv("AE_APP_KEY", "").strip()
APP_SECRET   = os.getenv("AE_APP_SECRET", "").strip()
REDIRECT_URI = os.getenv("AE_REDIRECT_URI", "").strip()

AUTH_BASE = "https://api-sg.aliexpress.com/oauth/authorize"
SYNC_URL  = "https://api-sg.aliexpress.com/sync"

TOKEN_FILE            = ".ae_token.json"
AE_MANUAL_TOKEN_FILE  = ".ae_manual_token.json"
EXCEL_FILE            = "productos_chefs_aura.xlsx"
TIMEOUT               = 45

# =========================
# STREAMLIT PAGE CONFIG + STATE
# =========================
st.set_page_config(page_title="Chef's Aura — AE + TikTok Builder (IA)", page_icon="🛒", layout="wide")

# Guarda el PID actual cuyo Builder se muestra
if "current_pid" not in st.session_state:
    st.session_state["current_pid"] = None

# Toggle para abrir/cerrar la lista guardada
if "show_saved_list" not in st.session_state:
    st.session_state["show_saved_list"] = False

# =========================
# UTILS COMUNES
# =========================
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"])
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def cn_ts_str() -> str:
    bj = timezone(timedelta(hours=8))
    return datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S")

def sign_hmac_sha256(params: dict, secret: str) -> str:
    base = "".join(f"{k}{params[k]}" for k in sorted(params) if params[k] is not None)
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest().upper()

def parse_price_any(x) -> float:
    if x is None: return 0.0
    if isinstance(x, (int, float)): return float(x)
    if isinstance(x, str):
        s = re.sub(r"[^\d\.\,]", "", x).replace(",", "")
        try: return float(s)
        except: return 0.0
    if isinstance(x, dict):
        for k in ("amount","value","price","min","max","usd"):
            if k in x and x[k] is not None:
                return parse_price_any(x[k])
    if isinstance(x, list) and x:
        return parse_price_any(x[0])
    return 0.0

def deep_find(d, want_keys: set):
    from collections import deque
    q = deque([d]); seen = set()
    while q:
        cur = q.popleft()
        if id(cur) in seen: continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in want_keys and v not in (None, "", []): return v
                if isinstance(v, (dict, list)): q.append(v)
        elif isinstance(cur, list):
            q.extend(cur)
    return None

def extract_product_id(s: str) -> str | None:
    if not s: return None
    s = s.strip()
    if s.isdigit(): return s
    m = re.search(r"/item/(\d+)\.html", s)
    if m: return m.group(1)
    m = re.search(r"(\d{11,20})", s)
    return m.group(1) if m else None

def build_product_url(pid: str) -> str:
    return f"https://www.aliexpress.com/item/{pid}.html"

def _variant_label_and_attrs(sku: dict) -> tuple[str, str]:
    props = []
    pnode = (sku.get("ae_sku_property_dtos", {}) or {}).get("ae_sku_property_d_t_o", []) or []
    for p in pnode:
        name = str(p.get("sku_property_name") or "").strip()
        val  = str(p.get("sku_property_value") or p.get("property_value_definition_name") or "").strip()
        if name or val:
            props.append(f"{name}: {val}" if name else val)
    label = " / ".join(props) if props else (sku.get("sku_attr") or "").strip() or "Variant"
    attrs = "; ".join(props) if props else ""
    return label, attrs

def extract_sku_variants(raw: dict) -> list[dict]:
    try:
        skus = (
            raw.get("aliexpress_ds_product_get_response", {})
               .get("result", {})
               .get("ae_item_sku_info_dtos", {})
               .get("ae_item_sku_info_d_t_o", [])
        ) or []
    except Exception:
        skus = []
    out = []
    for s in skus:
        label, attrs = _variant_label_and_attrs(s)
        price      = parse_price_any(s.get("offer_sale_price")) or 0.0
        list_price = parse_price_any(s.get("sku_price")) or 0.0
        bulk_price = parse_price_any(s.get("offer_bulk_sale_price")) or 0.0
        stock      = s.get("sku_available_stock")
        if isinstance(stock, str) and stock.isdigit():
            stock = int(stock)
        image = None
        pnode = (s.get("ae_sku_property_dtos", {}) or {}).get("ae_sku_property_d_t_o", []) or []
        for p in pnode:
            if p.get("sku_image"):
                image = p["sku_image"]; break
        out.append({
            "sku_id": str(s.get("sku_id") or ""),
            "label": label,
            "attrs": attrs,
            "price": round(float(price), 2),
            "list_price": round(float(list_price), 2),
            "bulk_price": round(float(bulk_price), 2),
            "stock": int(stock) if isinstance(stock, (int, float)) and stock >= 0 else None,
            "currency": s.get("currency_code") or "USD",
            "image": image,
        })
    return out

def extract_package_info(raw: dict) -> dict:
    pkg = (
        raw.get("aliexpress_ds_product_get_response", {})
           .get("result", {})
           .get("package_info_dto", {}) or {}
    )
    def _num(x):
        if x is None or x == "": return None
        try: return float(x)
        except: return None
    length_cm = _num(pkg.get("package_length"))
    width_cm  = _num(pkg.get("package_width"))
    height_cm = _num(pkg.get("package_height"))
    weight_kg = _num(pkg.get("gross_weight"))
    return {"length_cm": length_cm, "width_cm": width_cm, "height_cm": height_cm, "weight_kg": weight_kg}

# =========================
# OAUTH — AE (dejado igual)
# =========================
def build_authorize_url() -> str:
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": str(APP_KEY),
        "redirect_uri": REDIRECT_URI,
        "state": "aura_state_123",
        "force_auth": "true",
    }
    return f"{AUTH_BASE}?{urlencode(params)}"

def save_token(payload: dict):
    data = dict(payload)
    exp = int(data.get("expires_in", 3600))
    data["expires_at"] = time.time() + exp - 30
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def read_token() -> dict | None:
    if not os.path.exists(TOKEN_FILE): return None
    try:
        t = json.load(open(TOKEN_FILE, "r", encoding="utf-8"))
        if t.get("access_token") and float(t.get("expires_at", 0)) > time.time() + 30:
            return t
    except Exception:
        pass
    return None

def save_manual_ae_token(token_str: str):
    data = {"access_token": (token_str or "").strip(), "manual": True, "updated_at": time.time()}
    with open(AE_MANUAL_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def read_manual_ae_token() -> dict | None:
    if not os.path.exists(AE_MANUAL_TOKEN_FILE): return None
    try:
        t = json.load(open(AE_MANUAL_TOKEN_FILE, "r", encoding="utf-8"))
        if (t.get("access_token") or "").strip():
            return t
    except Exception:
        pass
    return None

def clear_manual_ae_token():
    try: os.remove(AE_MANUAL_TOKEN_FILE)
    except FileNotFoundError: pass

def get_valid_access_token() -> str | None:
    m = read_manual_ae_token()
    if m and m.get("access_token"):
        return m["access_token"]
    t = read_token()
    return t["access_token"] if t else None

# =========================
# SYNC CALL — AE (dejado igual)
# =========================
def sync_call(method: str, biz_params: dict) -> dict:
    s = make_session()
    base = {
        "method": method,
        "app_key": str(APP_KEY),
        "timestamp": cn_ts_str(),
        "format": "json",
        "v": "2.0",
        "sign_method": "sha256",
    }
    allp = {**base, **(biz_params or {})}
    allp["sign"] = sign_hmac_sha256(allp, APP_SECRET)
    try:
        r = s.get(SYNC_URL, params=allp, timeout=TIMEOUT)
        try:
            return r.json()
        except Exception:
            return {"_status": r.status_code, "_raw": (r.text or "")[:2000]}
    except Exception as e:
        return {"_error": str(e)}

# =========================
# PRODUCT — AE (dejado igual)
# =========================
def ds_product_get(product_id: str, ship_to="US", lang="EN", curr="USD") -> dict:
    token = get_valid_access_token()
    if not token:
        return {"error_response": {"msg": "Missing access_token (pega el token manualmente o autoriza)."}}
    params = {
        "access_token": token,
        "product_id": product_id,
        "ship_to_country": ship_to,
        "target_language": lang,
        "target_currency": curr,
    }
    return sync_call("aliexpress.ds.product.get", params)

def fetch_product_detail(url_or_id: str) -> dict:
    s = (url_or_id or "").strip()
    pid = extract_product_id(s)
    if not pid:
        return {"_raw": {}, "link": s, "product_id": "", "title": "Unavailable", "variants": [],
                "package": {"length_cm": None, "width_cm": None, "height_cm": None, "weight_kg": None}}
    raw = ds_product_get(pid, ship_to="US", lang="EN", curr="USD")
    out = {"_raw": raw, "link": build_product_url(pid), "product_id": pid, "title": "Unavailable",
           "variants": [], "package": {"length_cm": None, "width_cm": None, "height_cm": None, "weight_kg": None}}
    if isinstance(raw, dict) and "error_response" in raw:
        return out
    title = deep_find(raw, {"title","subject","product_title"}) or "Unavailable"
    out["title"] = title
    variants = extract_sku_variants(raw)
    out["variants"] = variants if variants else [{
        "sku_id": str(pid), "label": "Default", "attrs": "",
        "price": 0.0, "list_price": 0.0, "bulk_price": 0.0,
        "stock": None, "currency": "USD", "image": None
    }]
    out["package"] = extract_package_info(raw)
    return out

# =========================
# EXCEL — estructura (dejada igual)
# =========================
BASE_COLS = [
    "Product ID","Title","Link","SKU","Variant","Variant Attrs","Currency",
    "Available Qty","Buy Price (USD)","Margin (%)","Delivery (USD)","Selling Price (USD)","Image",
    "Pkg Length (cm)","Pkg Width (cm)","Pkg Height (cm)","Gross Weight (kg)"
]

def load_table() -> pd.DataFrame:
    if os.path.exists(EXCEL_FILE):
        try:
            df = pd.read_excel(EXCEL_FILE)
            for c in BASE_COLS:
                if c not in df.columns:
                    df[c] = "" if c in ("Variant","Variant Attrs","Currency","Image","Title","Link","SKU") else 0
            return df[BASE_COLS]
        except Exception:
            pass
    return pd.DataFrame(columns=BASE_COLS)

def save_table(df: pd.DataFrame):
    try:
        df.to_excel(EXCEL_FILE, index=False)
    except Exception as e:
        st.error(f"Error saving Excel: {e}")

def calc_selling(buy: float, margin_pct: float, delivery: float) -> float:
    try: m = float(margin_pct)
    except: m = 0.0
    try: d = float(delivery)
    except: d = 0.0
    return round(float(buy) * (1.0 + (m/100.0)) + d, 2)

# =========================
# IA — copy (dejada igual salvo pequeños textos)
# =========================
def _call_openai(messages, model="gpt-4o-mini", temperature=0.7, max_tokens=500) -> str:
    if OPENAI_API_KEY and (USE_NEW_OPENAI or openai):
        try:
            if USE_NEW_OPENAI:
                resp = client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
                )
                return resp.choices[0].message.content
            else:
                resp = openai.ChatCompletion.create(
                    model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
                )
                return resp["choices"][0]["message"]["content"]
        except Exception as e:
            return f"(AI error) {e}"
    return "(AI disabled/no key)"

def summarize_attrs(group_df: pd.DataFrame) -> str:
    samples = set()
    for _, r in group_df.iterrows():
        s = (str(r.get("Variant Attrs") or r.get("Variant") or "").strip())
        if s: samples.add(s)
    return ", ".join(list(samples)[:10])

def pkg_line(pkg_row: dict) -> str:
    dims = []
    if pkg_row.get("Pkg Length (cm)") not in (None,""): dims.append(f"L:{pkg_row.get('Pkg Length (cm)')}")
    if pkg_row.get("Pkg Width (cm)") not in (None,""):  dims.append(f"W:{pkg_row.get('Pkg Width (cm)')}")
    if pkg_row.get("Pkg Height (cm)") not in (None,""): dims.append(f"H:{pkg_row.get('Pkg Height (cm)')}")
    spec = " ".join(dims)
    if pkg_row.get("Gross Weight (kg)") not in (None,""): spec += f"  |  Peso: {pkg_row.get('Gross Weight (kg)')} kg"
    return spec.strip()

def ai_generate_title_desc_es(base_title: str, group_df: pd.DataFrame, pkg_row: dict) -> tuple[str, str]:
    attrs = summarize_attrs(group_df)
    specs = pkg_line(pkg_row)
    context = f'Título base: "{base_title}". Variantes/Atributos: {attrs or "—"}. Especificaciones: {specs or "—"}.'

    title_messages = [
        {"role":"system","content":"Eres un copywriter experto en ecommerce para TikTok Shop US."},
        {"role":"user","content":(
            "generame un titulo llamativo y corto que contenga palabras claves para tiktok shop.\n"
            f"Contexto: {context}\n"
            "Requisitos: máximo 190 caracteres, ingles neutro, sin emojis ni hashtags, claro e impactante."
        )}
    ]
    title = _call_openai(title_messages, model="gpt-4o-mini", temperature=0.7, max_tokens=120).strip()
    title = re.sub(r"\s+", " ", title)[:190]

    desc_messages = [
        {"role":"system","content":"Eres un copywriter experto en ecommerce para TikTok Shop US."},
        {"role":"user","content":(
            'generame una descripcion del producto, que contenga 3 partes, la primera sera una descripcion llamativa, que contenga palabras claves y atrapantes, '
            'la segunda tendra caracteristicas llamativas, y la tercera tendra un mensaje personalizado del producto que contenga '
            'el nombre de la tienda: CHEFs AURA.\n'
            f"Contexto: {context}\n"
            "Formato sugerido:\n"
            "(descripcion) …\n"
            "(caracteristicas) ✔️ item 1 \n✔️ item 2 \n✔️ item 3 …\n"
            "(mensaje) … CHEFs AURA\n"
            "Evita hashtags e intenta agregar emojis, ingles."
        )}
    ]
    desc = _call_openai(desc_messages, model="gpt-4o-mini", temperature=0.7, max_tokens=500).strip()
    return title, desc

# =========================
# IMÁGENES — helpers (dejados, con ZIP JPG)
# =========================
@st.cache_data(show_spinner=False)
def build_images_zip_cached(urls_tuple: tuple, pid: str) -> bytes:
    return download_images_zip(list(urls_tuple), pid)

IMG_EXT_RE = r'\.(jpg|jpeg|png|webp)(\?.*)?$'

def _looks_like_img_url(u: str) -> bool:
    if not u: return False
    u = u.strip()
    if u.startswith('//'): u = 'https:' + u
    if not u.startswith('http'): return False
    return re.search(IMG_EXT_RE, u, re.I) is not None

def _normalize_img_url(u: str) -> str:
    if not u: return u
    u = u.strip()
    if u.startswith('//'): u = 'https:' + u
    u = re.sub(r'(_\w+)*(?=\.(webp|jpg|jpeg|png)(\?.*)?$)', '', u, flags=re.I)
    return u

def collect_all_images_from_raw(raw: dict) -> list[str]:
    urls = set()
    def add_candidate(val):
        if val is None: return
        if isinstance(val, str):
            s = val.strip()
            if not s: return
            if ('http' in s and any(sep in s for sep in [';', ',', '|'])) or (re.search(IMG_EXT_RE, s, re.I) and any(sep in s for sep in [';', ',', '|'])):
                for part in re.split(r'[;,\|\s]+', s):
                    if _looks_like_img_url(part): urls.add(_normalize_img_url(part))
                return
            if _looks_like_img_url(s): urls.add(_normalize_img_url(s))
        elif isinstance(val, list):
            for x in val: add_candidate(x)
    from collections import deque
    dq = deque([raw]); seen = set()
    while dq:
        node = dq.popleft()
        if id(node) in seen: continue
        seen.add(id(node))
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).lower()
                if any(tok in key for tok in ['image','pic','img','url','gallery','multimedia']):
                    add_candidate(v)
                if isinstance(v, (dict, list, str)): dq.append(v)
        elif isinstance(node, list):
            for x in node: dq.append(x)
        elif isinstance(node, str):
            add_candidate(node)
    return list(dict.fromkeys(urls))

def scrape_gallery_from_page(product_url: str, timeout=20) -> list[str]:
    try:
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
        r = requests.get(product_url, headers=hdrs, timeout=timeout)
        r.raise_for_status()
        html_text = r.text
        m = re.search(r'"imagePathList"\s*:\s*(\[[^\]]+\])', html_text)
        if not m:
            m = re.search(r'"imageList"\s*:\s*(\[[^\]]+\])', html_text)
        out = []
        if m:
            arr_raw = html.unescape(m.group(1))
            try:
                arr = json.loads(arr_raw)
                for u in arr:
                    u = str(u).strip()
                    if u.startswith('//'): u = 'https:' + u
                    if _looks_like_img_url(u): out.append(_normalize_img_url(u))
            except Exception:
                for u in re.findall(r'"(https?:[^"]+)"', arr_raw):
                    if _looks_like_img_url(u): out.append(_normalize_img_url(u))
        return list(dict.fromkeys(out))
    except Exception:
        return []

def download_images_zip(urls: list[str], pid: str) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, u in enumerate(urls, 1):
            if not str(u).strip(): continue
            try:
                r = requests.get(u, timeout=20); r.raise_for_status()
                ext = ".jpg"
                name = f"{pid}_img_{i}{ext}"
                zf.writestr(name, r.content)
            except Exception:
                zf.writestr(f"{pid}_img_{i}.txt", f"URL: {u}")
    mem.seek(0)
    return mem.read()

@st.cache_data(show_spinner=False)
def build_images_zip_cached_jpeg(urls_tuple: tuple, pid: str, quality: int = 92) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, u in enumerate(urls_tuple, 1):
            if not str(u).strip(): continue
            try:
                jpg = fetch_url_to_jpeg_bytes(u, quality=quality)
                zf.writestr(f"{pid}_img_{i:02d}.jpg", jpg)
            except Exception as e:
                zf.writestr(f"{pid}_img_{i:02d}.txt", f"ERROR al convertir:\n{u}\n{e}")
    mem.seek(0)
    return mem.getvalue()

def build_images_zip_jpeg_with_uploads(urls: list[str], uploads, pid: str, quality: int = 92) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, u in enumerate(urls, 1):
            if not str(u).strip(): continue
            try:
                jpg = fetch_url_to_jpeg_bytes(u, quality=quality)
                zf.writestr(f"{pid}_img_{i:02d}.jpg", jpg)
            except Exception as e:
                zf.writestr(f"{pid}_img_{i:02d}.txt", f"ERROR al convertir:\n{u}\n{e}")
        if uploads:
            idx = len(urls) + 1
            for up in uploads:
                try:
                    raw = up.getvalue()
                    jpg = bytes_to_jpeg_bytes(raw, quality=quality)
                    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", (up.name or f"upload_{idx}"))
                    base = os.path.splitext(safe)[0]
                    zf.writestr(f"{pid}_{base}.jpg", jpg)
                except Exception as e:
                    zf.writestr(f"{pid}_upload_{idx:02d}.txt", f"ERROR archivo subido:\n{getattr(up,'name','')}\n{e}")
                idx += 1
    mem.seek(0)
    return mem.getvalue()

def fetch_url_to_jpeg_bytes(url: str, timeout: int = 20, quality: int = 92) -> bytes:
    r = requests.get(url, timeout=timeout); r.raise_for_status()
    return bytes_to_jpeg_bytes(r.content, quality=quality)

def bytes_to_jpeg_bytes(raw_bytes: bytes, quality: int = 92) -> bytes:
    im = Image.open(io.BytesIO(raw_bytes))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA","LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255,255,255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()

# =========================
# UI — Encabezado / Sidebar (conexión dejada igual)
# =========================
st.title("Chef’s Aura — AliExpress → Builder TikTok (IA)")

with st.sidebar:
    st.subheader("AliExpress OpenService")
    st.write("**APP_KEY:**", APP_KEY or "—")
    st.write("**Redirect URI:**", REDIRECT_URI or "—")

    col_a1, col_a2 = st.columns(2)
    with col_a1:
        if st.button("Get AuthCode URL", key="ae_authurl"):
            auth_url = build_authorize_url()
            st.markdown(f"[👉 Abrir URL de autorización]({auth_url})", unsafe_allow_html=True)
    with col_a2:
        if st.button("Clear AE (exchange) token", key="ae_signout"):
            try: os.remove(TOKEN_FILE)
            except FileNotFoundError: pass
            st.success("Exchange token cleared.")

    st.markdown("**Pegar Access Token de AE (manual)**")
    manual_default = (read_manual_ae_token() or {}).get("access_token", "")
    ae_token_input = st.text_input("AE Access Token", value=manual_default, type="password")
    col_a3, col_a4 = st.columns(2)
    with col_a3:
        if st.button("💾 Save AE token (manual)"): save_manual_ae_token(ae_token_input); st.success("AE manual token saved.")
    with col_a4:
        if st.button("🧹 Clear AE token (manual)"): clear_manual_ae_token(); st.success("AE manual token cleared.")

# =========================
# FORM: Agregar producto (único visible por defecto)
# =========================
st.markdown("---")
st.subheader("➕ Agregar producto por link/ID de AliExpress (se abrirá el Builder si es nuevo)")

col1, col2, col3 = st.columns([3,1,1])
with col1:
    input_url = st.text_input("Pega el link de AliExpress o Product ID", placeholder="https://www.aliexpress.com/item/XXXXXXXXXXXXXXX.html")
with col2:
    margin = st.number_input("Margin %", value=35.0, step=1.0, min_value=0.0, format="%.2f")
with col3:
    delivery = st.number_input("Delivery (USD)", value=2.99, step=0.5, min_value=0.0, format="%.2f")

submit_col, util_col = st.columns([1,3])
with submit_col:
    submitted = st.button("Agregar / Construir")

# Carga tabla
df = load_table()

# ===== Al enviar: agregar o rechazar si ya existe =====
if submitted:
    url_or_id = (input_url or "").strip()
    if not url_or_id:
        st.warning("Pega un link o Product ID.")
    else:
        data = fetch_product_detail(url_or_id)
        pid = str(data.get("product_id") or "").strip()

        # Debug opcional
        with st.expander("🐞 RAW API RESPONSE (debug)"):
            st.code(json.dumps(data.get("_raw", {}), indent=2)[:20000], language="json")
            st.write("Parsed:", {k: v for k, v in data.items() if k != "_raw"})

        raw = data.get("_raw", {})
        if isinstance(raw, dict) and "error_response" in raw:
            err = raw["error_response"]
            st.error(f"API error: {err.get('code')} - {err.get('msg')}")
        else:
            if not pid:
                st.error("No se pudo extraer Product ID del link/ID proporcionado.")
            else:
                # Duplicado: NO agregar
                if not df.empty and df["Product ID"].astype(str).eq(pid).any():
                    st.warning("⚠️ Producto existe (no se agregó nuevamente).")
                    # Opcional: mostrar Builder del existente
                    st.session_state["current_pid"] = pid
                else:
                    # Agregar todas las variaciones
                    title = data.get("title","Untitled")
                    link  = data.get("link", url_or_id)
                    pkg   = data.get("package", {})
                    rows = []
                    for v in data.get("variants", []):
                        buy = float(v.get("price") or 0)
                        sell = calc_selling(buy, margin, delivery)
                        qty  = v.get("stock")
                        qty_display = 0 if qty == 0 else ("—" if qty is None else int(qty))
                        rows.append({
                            "Product ID": pid,
                            "Title": title,
                            "Link": link,
                            "SKU": str(v.get("sku_id") or ""),
                            "Variant": v.get("label") or "",
                            "Variant Attrs": v.get("attrs") or "",
                            "Currency": v.get("currency") or "USD",
                            "Available Qty": qty_display,
                            "Buy Price (USD)": round(buy, 2),
                            "Margin (%)": float(margin),
                            "Delivery (USD)": float(delivery),
                            "Selling Price (USD)": sell,
                            "Image": v.get("image") or "",
                            "Pkg Length (cm)": pkg.get("length_cm"),
                            "Pkg Width (cm)":  pkg.get("width_cm"),
                            "Pkg Height (cm)": pkg.get("height_cm"),
                            "Gross Weight (kg)": pkg.get("weight_kg"),
                        })
                    if rows:
                        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
                        save_table(df)
                        st.success(f"✅ {len(rows)} variación(es) agregadas para Product ID {pid}.")
                        # Abrir Builder automáticamente para este PID
                        st.session_state["current_pid"] = pid

# =========================
# Builder (solo del producto seleccionado/reciente)
# =========================
def sales_attrs_from_text(txt: str):
    sale_attrs = []
    for part in str(txt or "").split(";"):
        part = part.strip()
        if not part: continue
        if ":" in part:
            name, val = part.split(":", 1)
            sale_attrs.append({"attribute_name": name.strip(), "attribute_value": val.strip()})
        else:
            sale_attrs.append({"attribute_name": "Spec", "attribute_value": part})
    return sale_attrs

def render_builder_for_pid(df_all: pd.DataFrame, pid: str):
    group = df_all[df_all["Product ID"].astype(str).eq(str(pid))]
    if group.empty:
        st.info("No hay filas para este Product ID.")
        return

    first = group.iloc[0]
    title = str(first.get("Title") or "Untitled")
    link  = str(first.get("Link")  or "")

    # Encabezado del Builder
    st.markdown("---")
    st.subheader(f"🧰 Builder TikTok (IA) — {pid}")
    cols = st.columns([4,1])
    with cols[0]:
        if link: st.markdown(f"**AliExpress:** [{link}]({link})")
    with cols[1]:
        if st.button("✖️ Cerrar Builder", key=f"close_builder_single_{pid}"):
            st.session_state["current_pid"] = None
            st.rerun()

    # Paquete
    pkg_cols = ["Pkg Length (cm)","Pkg Width (cm)","Pkg Height (cm)","Gross Weight (kg)"]
    pkg_row = group[pkg_cols].iloc[0].to_dict()
    st.caption(
        f"Package: {pkg_row.get('Pkg Length (cm)')}×{pkg_row.get('Pkg Width (cm)')}×{pkg_row.get('Pkg Height (cm)')} cm  |  "
        f"Gross: {pkg_row.get('Gross Weight (kg)')} kg"
    )

    # IA: generar automáticamente
    ai_title, ai_desc = ai_generate_title_desc_es(title, group, pkg_row)

    cat_cols = st.columns([2,1,1,1])
    with cat_cols[0]:
        tts_title = st.text_input("TikTok Title (IA)", value=ai_title, key=f"tt_title_{pid}")
        tts_desc  = st.text_area("TikTok Description (IA)", value=ai_desc, height=220, key=f"tt_desc_{pid}")
    with cat_cols[1]:
        tts_category_id = st.text_input("Category ID (opcional)", value="", key=f"cat_{pid}")
    with cat_cols[2]:
        default_margin = st.number_input("Margin %", value=40.0, step=1.0, min_value=0.0, key=f"marg_{pid}")
    with cat_cols[3]:
        default_stock = st.number_input("Default stock", value=99, step=1, min_value=0, key=f"stk_{pid}")

    # Variantes editables
    rows = []
    for _, r in group.iterrows():
        buy = float(r.get("Buy Price (USD)") or 0.0)
        sell = float(r.get("Selling Price (USD)") or 0.0)
        sell = sell if sell > 0 else round(buy * (1 + default_margin/100.0), 2)
        rows.append({
            "Enable": True,
            "AE_SKU": r.get("SKU"),
            "Variant": r.get("Variant"),
            "Attrs": r.get("Variant Attrs"),
            "BuyUSD": round(buy, 2),
            "SellUSD": sell,
            "Stock": default_stock if r.get("Available Qty") in ("—", None) else (int(r.get("Available Qty")) if str(r.get("Available Qty")).isdigit() else default_stock),
            "ImageURL": r.get("Image") or "",
        })
    edited = st.data_editor(pd.DataFrame(rows), num_rows="dynamic", use_container_width=True, key=f"edit_{pid}")

    # Imágenes (todas): API + página
    st.markdown("#### 📸 Imágenes (todas)")
    variant_urls = [u for u in group["Image"].dropna().tolist() if str(u).strip()]
    data_full = fetch_product_detail(str(pid))
    api_gallery = collect_all_images_from_raw(data_full.get("_raw", {}))
    if len(api_gallery) < max(3, len(variant_urls)):
        api_gallery = list(dict.fromkeys(api_gallery + scrape_gallery_from_page(data_full.get("link",""))))
    all_urls_base = list(dict.fromkeys(itertools.chain(variant_urls, api_gallery)))
    preview_urls = all_urls_base[:30]

    if preview_urls:
        st.caption(f"Se muestran {len(preview_urls)} / {len(all_urls_base)} imágenes encontradas.")
        grid = st.columns(4)
        for i, url in enumerate(preview_urls):
            with grid[i % 4]:
                try: st.image(url, use_column_width=True)
                except Exception: st.write(url)
    else:
        st.info("No se encontraron imágenes adicionales. Puedes agregarlas manualmente abajo.")

    extra_urls = st.text_area("Añade URLs extra (una por línea)", value="", key=f"extra_urls_{pid}")
    extra_list = [u.strip() for u in extra_urls.splitlines() if u.strip()]
    up_files = st.file_uploader("Sube imágenes adicionales (opcional)", accept_multiple_files=True, key=f"up_{pid}")

    all_urls = list(dict.fromkeys(all_urls_base + extra_list))

    # Descargas JPG
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "⬇️ JPG (solo generadas)",
            data=build_images_zip_cached_jpeg(tuple(all_urls_base), str(pid), quality=92),
            file_name=f"{pid}_images_generated_jpg.zip",
            mime="application/zip",
            key=f"dlzip_gen_jpg_{pid}"
        )
    with c2:
        st.download_button(
            "⬇️ JPG (todas)",
            data=build_images_zip_jpeg_with_uploads(all_urls, up_files, str(pid), quality=92),
            file_name=f"{pid}_images_all_jpg.zip",
            mime="application/zip",
            key=f"dlzip_all_jpg_{pid}"
        )

    # Copiar/pegar
    st.markdown("#### 📋 Copiar y pegar en Seller Center")
    st.code(tts_title, language="text")
    st.code(tts_desc, language="text")

    copy_lines = []
    for _, r in edited.iterrows():
        if not r.get("Enable", True): continue
        line = f"- {r.get('Variant') or 'Variant'} | SKU: {r.get('AE_SKU')} | Price: ${float(r.get('SellUSD') or 0):.2f} | Stock: {int(r.get('Stock') or 0)}"
        copy_lines.append(line)
    st.code("\n".join(copy_lines) or "—", language="text")

    # JSON referencia
    st.markdown("#### 🧪 JSON de referencia (para API futuro)")
    def kg_to_g(x):
        try: return int(round(float(x) * 1000))
        except: return None
    delivery_info = {
        "package_weight": kg_to_g(pkg_row.get("Gross Weight (kg)")),
        "package_length": int(pkg_row.get("Pkg Length (cm)") or 0),
        "package_width": int(pkg_row.get("Pkg Width (cm)") or 0),
        "package_height": int(pkg_row.get("Pkg Height (cm)") or 0),
    }
    img_urls = all_urls[:9]
    skus = []
    for _, r in edited.iterrows():
        if not r.get("Enable", True): continue
        skus.append({
            "external_sku_id": f"AE-{r.get('AE_SKU')}",
            "original_price": float(r.get("SellUSD") or 0.0),
            "sales_attributes": sales_attrs_from_text(r.get("Attrs")),
            "inventory": [{"warehouse_id": "", "quantity": int(r.get("Stock") or 0)}],
            "images": img_urls
        })
    payload = {
        "title": tts_title[:190],
        "description": tts_desc,
        "category_id": str(tts_category_id or ""),
        "images": img_urls,
        "skus": skus,
        "delivery_info": delivery_info
    }
    st.text_area("Payload JSON", value=json.dumps(payload, indent=2), height=260, key=f"json_{pid}")

# Renderizar Builder si hay PID actual
if st.session_state["current_pid"]:
    render_builder_for_pid(df, st.session_state["current_pid"])

# =========================
# Lista de productos guardados (mostrada solo al abrir opción)
# =========================
st.markdown("---")
list_cols = st.columns([1,3])
with list_cols[0]:
    if not st.session_state["show_saved_list"]:
        if st.button("📁 Ver productos guardados"):
            st.session_state["show_saved_list"] = True
            st.rerun()
    else:
        if st.button("Cerrar lista"):
            st.session_state["show_saved_list"] = False
            st.rerun()

if st.session_state["show_saved_list"]:
    st.subheader("📚 Productos guardados")
    if df.empty:
        st.info("Aún no hay productos guardados.")
    else:
        # Vista compacta por Product ID
        compact = (
            df.groupby("Product ID", as_index=False)
              .agg(Title=("Title","first"), Link=("Link","first"))
              .sort_values("Product ID")
        )

        # Búsqueda por nombre
        q = st.text_input("Buscar por nombre (Title contiene)", value="", placeholder="Escribe parte del nombre…")
        if q.strip():
            compact = compact[compact["Title"].str.contains(q.strip(), case=False, na=False)]

        st.caption(f"{len(compact)} producto(s) encontrados.")
        # Render por filas con botón eliminar
        for _, row in compact.iterrows():
            pid = str(row["Product ID"])
            c1, c2, c3, c4 = st.columns([2,5,2,1])
            with c1: st.markdown(f"**{pid}**")
            with c2:
                t = str(row["Title"] or "")
                l = str(row["Link"] or "")
                if l:
                    st.markdown(f"[{t}]({l})")
                else:
                    st.write(t)
            with c3:
                if st.button("🧰 Abrir Builder", key=f"open_builder_{pid}"):
                    st.session_state["current_pid"] = pid
                    st.rerun()
            with c4:
                if st.button("🗑️", key=f"del_{pid}"):
                    # Eliminar todas las filas de ese Product ID
                    new_df = df[~df["Product ID"].astype(str).eq(pid)].copy()
                    save_table(new_df)
                    st.success(f"Producto {pid} eliminado.")
                    st.session_state["current_pid"] = None
                    st.session_state["show_saved_list"] = True
                    st.experimental_rerun()

# =========================
# Help
# =========================
with st.expander("🔎 Help / Tips"):
    st.markdown(
        """
- Esta vista solo muestra el **formulario de agregar**. Al agregar un producto **nuevo**, se abre de inmediato el **Builder (IA)** para ese producto.
- Si intentas agregar un producto ya existente, verás **“producto existe”** y no se duplicará.
- Usa **📁 Ver productos guardados** para listar (ID, Title, Link), **buscar por nombre**, **abrir Builder** o **eliminar**.
- El JSON de referencia del Builder es útil para tu futura integración con TikTok API.
"""
    )
