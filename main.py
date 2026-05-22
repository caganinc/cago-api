"""
CAGO 3D — Slice Events API
Bridge'den gelen slice verilerini alır, kalibrasyon katsayılarını günceller.
Çalıştır: uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Union
import sqlite3, json, math, os
from datetime import datetime, timezone
from pathlib import Path

# ─── Veritabanı ──────────────────────────────────────────────────────────────

# Railway'de /data volume'u varsa oraya, yoksa /tmp'ye yaz
_default_db = "/data/api.db" if Path("/data").exists() else "/tmp/cago_api.db"
DB_PATH = Path(os.getenv("CAGO_DB", _default_db))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 3D model dosyaları (köprünün gönderdiği STL'ler) burada saklanır
MODELS_DIR = DB_PATH.parent / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)
MAX_MODEL_BYTES = 60 * 1024 * 1024   # 60 MB üst sınır

def get_db():
    # check_same_thread=False: FastAPI sync generator dependency'lerinde bağlantı
    # bir thread'de açılıp başka thread'de kapatılabiliyor; bu güvenli çünkü her
    # istek kendi bağlantısını alır (paylaşım yok).
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slice_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            source      TEXT,
            machine     TEXT,
            files       TEXT,
            weight_g    REAL,
            time_min    REAL,
            layer_height REAL,
            infill_pct  REAL,
            filament_type TEXT,
            filament_density REAL,
            raw_json    TEXT
        );

        CREATE TABLE IF NOT EXISTS calibrations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_key   TEXT NOT NULL,   -- örn: "PLA_0.20_15pct"
            k_weight      REAL NOT NULL DEFAULT 1.0,
            k_time        REAL NOT NULL DEFAULT 1.0,
            sample_count  INTEGER NOT NULL DEFAULT 0,
            last_updated  TEXT NOT NULL,
            UNIQUE(profile_key)
        );

        CREATE TABLE IF NOT EXISTS calibration_samples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_key   TEXT NOT NULL,
            slice_event_id INTEGER,
            bambu_weight_g REAL,
            bambu_time_min REAL,
            site_weight_g  REAL,    -- sitenin tahmini (eğer gönderilirse)
            site_time_min  REAL,
            weight_ratio   REAL,    -- bambu / site (site yoksa sadece saklanır)
            time_ratio     REAL,
            created_at    TEXT NOT NULL
        );
        """)

# ─── Modeller ────────────────────────────────────────────────────────────────

class FilamentModel(BaseModel):
    name:           Optional[str] = None
    type:           Optional[str] = None
    color_hex:      Optional[str] = None
    density_g_cm3:  Optional[float] = None
    vendor:         Optional[str] = None
    weight_used_g:  Optional[float] = None
    length_used_mm: Optional[float] = None

class SliceModel(BaseModel):
    estimated_weight_g:      float
    estimated_time_min:      float
    filament_length_mm:      Optional[float] = None
    layer_height_mm:         Optional[float] = None
    first_layer_height_mm:   Optional[float] = None
    infill_pct:              Optional[float] = None
    infill_pattern:          Optional[str]   = None
    wall_count:              Optional[int]   = None
    top_bottom_layers:       Optional[int]   = None
    supports:                Optional[dict]  = None
    brim_width_mm:           Optional[float] = None
    filament:                Optional[Union[FilamentModel, list]] = None
    model_bbox_mm:           Optional[dict]  = None
    model_volume_cm3:        Optional[float] = None
    triangle_count:          Optional[int]   = None

class SliceEventRequest(BaseModel):
    source:          str
    version:         str
    ts:              str
    machine:         Optional[str] = None
    slicer_version:  Optional[str] = None
    files:           list[str] = []
    plate:           Optional[dict] = None
    slice:           SliceModel
    actual:          Optional[dict] = None
    # Opsiyonel: sitenin kendi tahmini (kalibrasyon karşılaştırması için)
    site_estimate:   Optional[dict] = None

class CalibrateRequest(BaseModel):
    """
    Site kendi tahminini göndererek karşılaştırma yapabilir.
    Bridge verisiyle eşleşince k_weight / k_time hesaplanır.

    Eşleştirme önceliği:
      1. bambu_weight_g / bambu_time_min doğrudan verilirse → onlar kullanılır
      2. slice_event_id verilirse → o olayın Bambu değerleri kullanılır
      3. hiçbiri yoksa → en son bambu olayı (geriye dönük uyumluluk)
    """
    profile_key:    str           # örn: "PLA_0.2_15pct"
    site_weight_g:  float         # sitenin tahmini gram
    site_time_min:  float         # sitenin tahmini dakika
    bambu_weight_g: Optional[float] = None   # seçilen olayın Bambu gramı
    bambu_time_min: Optional[float] = None   # seçilen olayın Bambu süresi (dk)
    slice_event_id: Optional[int]   = None   # hangi olayla eşleştirileceği

# ─── Kalibrasyon mantığı ─────────────────────────────────────────────────────

def _profile_key(s: SliceModel) -> str:
    """Slice parametrelerinden benzersiz bir profil anahtarı üretir."""
    fil = s.filament
    if isinstance(fil, list):
        fil = fil[0] if fil else None
    ftype = (fil.type if fil and fil.type else "unknown").upper()
    layer = round(s.layer_height_mm or 0.20, 2)
    infill = int(s.infill_pct or 15)
    return f"{ftype}_{layer}_{infill}pct"


def _update_calibration(conn, profile_key: str, new_weight_ratio: Optional[float],
                         new_time_ratio: Optional[float]):
    """
    Exponential moving average ile k_weight ve k_time günceller.
    EMA alpha = 0.3 — yeni veri %30 ağırlık taşır, geçmiş %70.
    """
    ALPHA = 0.3
    row = conn.execute(
        "SELECT k_weight, k_time, sample_count FROM calibrations WHERE profile_key=?",
        (profile_key,)
    ).fetchone()

    if row is None:
        # İlk kayıt
        k_w = new_weight_ratio if new_weight_ratio else 1.0
        k_t = new_time_ratio   if new_time_ratio   else 1.0
        conn.execute("""
            INSERT INTO calibrations (profile_key, k_weight, k_time, sample_count, last_updated)
            VALUES (?, ?, ?, 1, ?)
        """, (profile_key, k_w, k_t, datetime.now(timezone.utc).isoformat()))
        return k_w, k_t, 1
    else:
        k_w = row["k_weight"]
        k_t = row["k_time"]
        n   = row["sample_count"]

        if new_weight_ratio:
            k_w = ALPHA * new_weight_ratio + (1 - ALPHA) * k_w
        if new_time_ratio:
            k_t = ALPHA * new_time_ratio   + (1 - ALPHA) * k_t

        n += 1
        conn.execute("""
            UPDATE calibrations
            SET k_weight=?, k_time=?, sample_count=?, last_updated=?
            WHERE profile_key=?
        """, (k_w, k_t, n, datetime.now(timezone.utc).isoformat(), profile_key))
        return k_w, k_t, n


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="CAGO 3D Slice API", version="1.1.2")
init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Sitenin domain'iyle kısıtlayabilirsin
    allow_methods=["*"],
    allow_headers=["*"],
)

# Basit API key doğrulama (opsiyonel — config.json'daki api_key ile eşleşir)
API_KEY = os.getenv("CAGO_API_KEY", "")   # Boşsa doğrulama yapılmaz

def verify_key(authorization: Optional[str] = Header(None)):
    if not API_KEY:
        return  # Key tanımlı değilse herkese açık
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Geçersiz API anahtarı")

# ─── Endpoint: slice event al ────────────────────────────────────────────────

@app.post("/v1/slice-events")
def receive_slice_event(
    event: SliceEventRequest,
    conn=Depends(get_db),
    _=Depends(verify_key)
):
    s = event.slice
    now = datetime.now(timezone.utc).isoformat()

    # Filament bilgisini düzleştir
    fil = s.filament
    if isinstance(fil, list):
        fil = fil[0] if fil else None
    ftype   = fil.type    if fil else None
    density = fil.density_g_cm3 if fil else None

    # Slice event'i kaydet
    cur = conn.execute("""
        INSERT INTO slice_events
        (received_at, source, machine, files, weight_g, time_min,
         layer_height, infill_pct, filament_type, filament_density, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now, event.source, event.machine,
        json.dumps(event.files),
        s.estimated_weight_g, s.estimated_time_min,
        s.layer_height_mm, s.infill_pct,
        ftype, density,
        json.dumps(event.model_dump())
    ))
    event_id = cur.lastrowid
    conn.commit()

    profile_key = _profile_key(s)
    calibration_updated = False
    k_weight = k_time = 1.0
    sample_count = 0

    # Eğer site tahmini de varsa → karşılaştır → kalibrasyon güncelle
    if event.site_estimate:
        site_w = event.site_estimate.get("weight_g")
        site_t = event.site_estimate.get("time_min")

        w_ratio = (s.estimated_weight_g / site_w) if site_w and site_w > 0 else None
        t_ratio = (s.estimated_time_min  / site_t) if site_t and site_t > 0 else None

        conn.execute("""
            INSERT INTO calibration_samples
            (profile_key, slice_event_id, bambu_weight_g, bambu_time_min,
             site_weight_g, site_time_min, weight_ratio, time_ratio, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (profile_key, event_id,
              s.estimated_weight_g, s.estimated_time_min,
              site_w, site_t, w_ratio, t_ratio, now))

        k_weight, k_time, sample_count = _update_calibration(conn, profile_key, w_ratio, t_ratio)
        conn.commit()
        calibration_updated = True
    else:
        # Site tahmini yok — sadece Bambu verisini sakla, k güncellenmez ama
        # /v1/calibrate endpoint'iyle sonradan eşleştirilebilir
        row = conn.execute(
            "SELECT k_weight, k_time, sample_count FROM calibrations WHERE profile_key=?",
            (profile_key,)
        ).fetchone()
        if row:
            k_weight     = row["k_weight"]
            k_time       = row["k_time"]
            sample_count = row["sample_count"]

    return {
        "id":                   f"se_{event_id}",
        "received_at":          now,
        "profile_key":          profile_key,
        "calibration_updated":  calibration_updated,
        "new_k_weight":         round(k_weight, 4),
        "new_k_time":           round(k_time, 4),
        "sample_count":         sample_count,
    }


# ─── Endpoint: site kendi tahminini göndererek kalibrasyon günceller ─────────

@app.post("/v1/calibrate")
def calibrate(
    req: CalibrateRequest,
    conn=Depends(get_db),
    _=Depends(verify_key)
):
    """
    Admin paneli bu endpoint'i çağırır:
    - Bir modeli siteye yükleyip hesaplama yaptı (site_weight_g, site_time_min)
    - Aynı modeli Bambu'da slice etti → bridge zaten gönderdi (profile_key ile son kayıt bulunur)
    - Bu iki veri karşılaştırılır, k güncellenir
    """
    # Bambu değerlerini belirle — öncelik sırasına göre
    event_id = req.slice_event_id
    if req.bambu_weight_g is not None and req.bambu_time_min is not None:
        # 1) Site doğrudan seçilen olayın Bambu değerlerini gönderdi (en doğru)
        bambu_w = req.bambu_weight_g
        bambu_t = req.bambu_time_min
    elif req.slice_event_id is not None:
        # 2) Belirli bir olay id'si verildi
        ev = conn.execute(
            "SELECT id, weight_g, time_min FROM slice_events WHERE id=?",
            (req.slice_event_id,)
        ).fetchone()
        if not ev:
            raise HTTPException(status_code=404, detail="Slice olayı bulunamadı")
        bambu_w = ev["weight_g"]
        bambu_t = ev["time_min"]
    else:
        # 3) Geriye dönük uyumluluk: en son bambu olayı
        last_bambu = conn.execute("""
            SELECT id, weight_g, time_min FROM slice_events
            WHERE source LIKE 'bambu%'
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        if not last_bambu:
            raise HTTPException(status_code=404, detail="Henüz bridge'den veri gelmedi")
        event_id = last_bambu["id"]
        bambu_w = last_bambu["weight_g"]
        bambu_t = last_bambu["time_min"]

    w_ratio = (bambu_w / req.site_weight_g) if (bambu_w and req.site_weight_g > 0) else None
    t_ratio = (bambu_t / req.site_time_min)  if (bambu_t and req.site_time_min  > 0) else None

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO calibration_samples
        (profile_key, slice_event_id, bambu_weight_g, bambu_time_min,
         site_weight_g, site_time_min, weight_ratio, time_ratio, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (req.profile_key, event_id,
          bambu_w, bambu_t,
          req.site_weight_g, req.site_time_min,
          w_ratio, t_ratio, now))

    k_weight, k_time, n = _update_calibration(conn, req.profile_key, w_ratio, t_ratio)
    conn.commit()

    return {
        "profile_key":   req.profile_key,
        "bambu_weight_g": bambu_w,
        "bambu_time_min": bambu_t,
        "site_weight_g":  req.site_weight_g,
        "site_time_min":  req.site_time_min,
        "weight_ratio":   round(w_ratio, 4) if w_ratio else None,
        "time_ratio":     round(t_ratio, 4) if t_ratio else None,
        "new_k_weight":   round(k_weight, 4),
        "new_k_time":     round(k_time, 4),
        "sample_count":   n,
    }


# ─── Endpoint: mevcut kalibrasyon katsayılarını getir ────────────────────────

@app.get("/v1/calibrations")
def get_calibrations(conn=Depends(get_db), _=Depends(verify_key)):
    """Admin paneli bu endpoint'ten güncel k değerlerini okur."""
    rows = conn.execute("""
        SELECT profile_key, k_weight, k_time, sample_count, last_updated
        FROM calibrations ORDER BY last_updated DESC
    """).fetchall()
    return {
        "calibrations": [dict(r) for r in rows]
    }


@app.get("/v1/calibrations/{profile_key}")
def get_calibration(profile_key: str, conn=Depends(get_db), _=Depends(verify_key)):
    row = conn.execute(
        "SELECT * FROM calibrations WHERE profile_key=?", (profile_key,)
    ).fetchone()
    if not row:
        return {"profile_key": profile_key, "k_weight": 1.0, "k_time": 1.0, "sample_count": 0}
    return dict(row)


# ─── Endpoint: son slice olaylarını getir (admin paneli için) ─────────────────

@app.get("/v1/slice-events")
def list_slice_events(limit: int = 50, conn=Depends(get_db), _=Depends(verify_key)):
    rows = conn.execute("""
        SELECT id, received_at, source, machine, files,
               weight_g, time_min, layer_height, infill_pct, filament_type
        FROM slice_events ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    events = []
    for r in rows:
        d = dict(r)
        d["has_model"] = (MODELS_DIR / f"{d['id']}.stl").exists()
        events.append(d)
    return {"events": events}


# ─── Endpoint: tek bir slice olayının tüm detayı (geometri dahil) ────────────

@app.get("/v1/slice-events/{event_id}")
def get_slice_event(event_id: int, conn=Depends(get_db), _=Depends(verify_key)):
    """
    Site bu endpoint'ten olayın tüm detayını okur — özellikle geometriyi
    (model_bbox_mm, model_volume_cm3, triangle_count) site slicer'ı için.
    """
    row = conn.execute(
        "SELECT * FROM slice_events WHERE id=?", (event_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Slice olayı bulunamadı")

    d = dict(row)
    # raw_json içinden tam slice paketini (geometri vb.) aç
    try:
        d["raw"] = json.loads(d.get("raw_json") or "{}")
    except (ValueError, TypeError):
        d["raw"] = {}
    d.pop("raw_json", None)
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except (ValueError, TypeError):
        pass
    d["has_model"] = (MODELS_DIR / f"{event_id}.stl").exists()
    d["profile_key"] = _profile_key(SliceModel(**d["raw"].get("slice", {}))) if d["raw"].get("slice") else None
    return d


# ─── Endpoint: bir slice olayına 3D model (STL) yükle ────────────────────────

@app.post("/v1/slice-events/{event_id}/model")
def upload_model(
    event_id: int,
    file: UploadFile = File(...),
    conn=Depends(get_db),
    _=Depends(verify_key),
):
    """
    Köprü, slice event'i POST ettikten sonra dönen id ile 3D modeli (binary STL)
    buraya yükler. Site bu STL'i indirip kendi slicer'ından geçirir.
    """
    row = conn.execute("SELECT id FROM slice_events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Slice olayı bulunamadı")

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Boş dosya")
    if len(data) > MAX_MODEL_BYTES:
        raise HTTPException(status_code=413, detail="Dosya çok büyük (maks 60 MB)")

    dest = MODELS_DIR / f"{event_id}.stl"
    dest.write_bytes(data)
    return {"event_id": event_id, "bytes": len(data), "stored": True}


# ─── Endpoint: bir slice olayının 3D modelini indir ──────────────────────────

@app.get("/v1/slice-events/{event_id}/model")
def download_model(event_id: int, _=Depends(verify_key)):
    """Site bu endpoint'ten STL'i indirir → parseSTL → predict."""
    path = MODELS_DIR / f"{event_id}.stl"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bu olay için model dosyası yok")
    return FileResponse(
        str(path),
        media_type="model/stl",
        filename=f"slice_{event_id}.stl",
    )


# ─── Sağlık kontrolü ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.1.2"}


# ─── Admin paneli ─────────────────────────────────────────────────────────────

@app.get("/admin")
def admin_panel():
    """Kalibrasyon yönetim paneli (şifresiz — gerekirse API key ile koru)."""
    html_path = Path(__file__).parent / "admin.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="admin.html bulunamadı")
    return FileResponse(str(html_path), media_type="text/html")
