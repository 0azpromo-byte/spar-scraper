import csv
import hashlib
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta


import requests
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter


# ============================================================
# KONFIGURACIJA
# ============================================================


SERVICE_ACCOUNT = "serviceAccountKey.json"
TRGOVINA = "Spar"
GRAD = "Bjelovar"
CSV_ENCODING = "cp1250"
DELIMITER = ";"


# Quota
HRANA_QUOTA = 1000
OSTALO_QUOTA = 1000
LOKALNI_TEST = os.environ.get("LOKALNI_TEST", "false").lower() == "true"


# Kategorije mapping
KATEGORIJE_MAP = {
    "Hrana": "HRANA",
    "Pića": "PIĆA",
    "Kozmetika": "KOZMETIKA",
    "Sredstva za čišćenje": "SREDSTVA_ZA_ČIŠĆENJE",
    "Proizvodi za kućanstvo": "PROIZVODI_ZA_KUĆANSTVO",
    "Toaletne potrepštine": "TOALETNE_POTREPŠTINE",
}


# SPAR config
SPAR_JSON_URL = "https://www.spar.hr/datoteke_cjenici/Cjenik{date}.json"
SPAR_SEARCH_KEYWORD = "bjelovar"


# CSV column names
COL_NAZIV = "naziv"
COL_SIFRA = "šifra"
COL_MARKA = "marka"
COL_NETO_KOLICINA = "neto količina"
COL_JEDINICA = "jedinica mjere"
COL_MPC = "MPC (EUR)"
COL_CIJENA_PO_JEDINICI = "cijena za jedinicu mjere (EUR)"
COL_AKCIJA = "MPC za vrijeme posebnog oblika prodaje (EUR)"
COL_NAJNIZA = "Najniža cijena u posljednjih 30 dana (EUR)"
COL_SIDRENA = "sidrena cijena na 2.5.2025. (EUR)"
COL_BARKOD = "barkod"
COL_KATEGORIJA = "kategorija proizvoda"




# ============================================================
# FIREBASE INICIJALIZACIJA
# ============================================================


cred = credentials.Certificate(SERVICE_ACCOUNT)
firebase_admin.initialize_app(cred)
db = firestore.client()


# ---------- PROVJERA DUPLIKATA ----------
def vec_scrapano_danas(trgovina: str) -> bool:
    """Provjeri postoji li već današnji datum za ovu trgovinu u cijene kolekciji."""
    today = datetime.now().strftime("%Y-%m-%d")
    check = (
        db.collection("cijene")
        .where(filter=FieldFilter("trgovina", "==", trgovina))
        .where(filter=FieldFilter("datum", "==", today))
        .limit(1)
        .get()
    )
    return len(check) > 0


# ============================================================
# FUNKCIJE
# ============================================================


def normalize_name(name):
    """Uklanja specijalne znakove i normalizira naziv za generiranje ID-a."""
    if not name:
        return ""
    name = str(name).lower().strip()
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()




def generate_product_id(naziv, marka, kolicina, jedinica, barkod=""):
    """Generira jedinstveni ID proizvoda - koristi barkod ako postoji."""
    if barkod and str(barkod).strip():
        return str(barkod).strip()
    parts = [
        normalize_name(naziv),
        normalize_name(marka),
        str(kolicina).strip(),
        normalize_name(jedinica)
    ]
    parts = [p for p in parts if p]
    if not parts:
        return "unknown_" + hashlib.md5(str(naziv).encode()).hexdigest()[:12]
    return "_".join(parts)




def pronadji_spar_url(datum=None):
    """Pronalazi JSON datoteku s popisom CSV-ova za zada dan."""
    if datum is None:
        datum = datetime.now()
    
    date_str = datum.strftime("%Y%m%d")
    url = SPAR_JSON_URL.format(date=date_str)
    
    print(f"🔍 Pokušavam: {url}")
    
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            files = data.get("files", [])
            
            # Traži Bjelovar
            for f in files:
                if SPAR_SEARCH_KEYWORD in f["name"].lower():
                    print(f"✅ Pronađen CSV za Bjelovar: {f['name']}")
                    return f["URL"]
            
            print(f"⚠️ Nema CSV-a za Bjelovar za {date_str}")
            return None
        else:
            print(f"⚠️ HTTP {resp.status_code} za {date_str}")
            return None
    except Exception as e:
        print(f"⚠️ Greška pri dohvaćanju JSON-a: {e}")
        return None




def pronadji_spar_url_sa_backup(datum=None):
    """Pokušaj pronaći URL, ako ne uspije pokušaj jučer."""
    if datum is None:
        datum = datetime.now()
    
    url = pronadji_spar_url(datum)
    if url:
        return url
    
    # Pokušaj jučer
    print("🔄 Pokušavam jučerašnji datum...")
    juce = datum - timedelta(days=1)
    return pronadji_spar_url(juce)




def preuzmi_csv(url):
    """Preuzima CSV datoteku s SPAR servera."""
    print(f"📥 Preuzimam CSV: {url}")
    
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    
    # SPAR koristi cp1250 encoding
    resp.encoding = CSV_ENCODING
    return resp.text




def normaliziraj_kategoriju(raw):
    """Normalizira kategoriju u standardizirani oblik."""
    if not raw:
        return "OSTALO"
    
    raw = raw.strip()
    
    # Direktno mapiranje
    for key, value in KATEGORIJE_MAP.items():
        if key.lower() in raw.lower():
            return value
    
    return "OSTALO"




def obradi_csv(sadrzaj):
    """Parsira CSV sadržaj i vraća listu proizvoda."""
    products = []
    reader = csv.DictReader(sadrzaj.splitlines(), delimiter=DELIMITER)
    
    # Debug: prikaži nazive kolona
    if reader.fieldnames:
        print(f"📋 Kolone: {reader.fieldnames}")
    
    for row in reader:
        naziv = row.get(COL_NAZIV, "").strip()
        barkod = row.get(COL_BARKOD, "").strip()
        
        # Preskoči redove bez barkoda ili naziva
        if not barkod or not naziv:
            continue
        
        # Očisti barkod od navodnika
        barkod = barkod.replace('"', '').replace("'", "").strip()
        
        # Cijena
        cijena_str = row.get(COL_MPC, "").strip()
        if not cijena_str:
            continue
        
        try:
            cijena = float(cijena_str.replace(',', '.'))
        except ValueError:
            continue
        
        # Akcijska cijena
        akcija_str = row.get(COL_AKCIJA, "").strip()
        tip = "redovno"
        if akcija_str:
            try:
                akcija = float(akcija_str.replace(',', '.'))
                if akcija < cijena:
                    cijena = akcija
                    tip = "akcija"
            except ValueError:
                pass
        
        # Ostala polja
        marka = row.get(COL_MARKA, "").strip()
        kolicina = row.get(COL_NETO_KOLICINA, "").strip()
        jedinica = row.get(COL_JEDINICA, "").strip()
        kategorija_raw = row.get(COL_KATEGORIJA, "").strip()
        kategorija = normaliziraj_kategoriju(kategorija_raw)
        
        # Sidrena cijena (za referencu)
        sidrena = row.get(COL_SIDRENA, "").strip()
        
        products.append({
            "barkod": barkod,
            "naziv": naziv,
            "marka": marka,
            "kolicina": f"{kolicina} {jedinica}" if kolicina and jedinica else "",
            "cijena": round(cijena, 2),
            "tip": tip,
            "trgovina": TRGOVINA,
            "grad": GRAD,
            "kategorija": kategorija,
            "datum": datetime.now().strftime("%Y-%m-%d"),
            "vrijeme": datetime.now().isoformat(),
            "sidrena_cijena": sidrena,
        })
    
    return products




def deterministicki_kljuc(product):
    """Generira deterministički ključ za proizvod."""
    return hashlib.md5(product["barkod"].encode()).hexdigest()




def odaberi_proizvode(products):
    """Odabire proizvode prema kvotama: HRANA 1000, OSTALO 1000."""
    hrana = []
    ostalo = []
    
    for p in products:
        if p["kategorija"] == "HRANA":
            hrana.append(p)
        else:
            ostalo.append(p)
    
    # Sortiraj po determinističkom ključu za stabilnost
    hrana.sort(key=deterministicki_kljuc)
    ostalo.sort(key=deterministicki_kljuc)
    
    # Odaberi prema kvotama
    hrana_limit = min(HRANA_QUOTA, len(hrana)) if not LOKALNI_TEST else min(50, len(hrana))
    ostalo_limit = min(OSTALO_QUOTA, len(ostalo)) if not LOKALNI_TEST else min(50, len(ostalo))
    
    selected = hrana[:hrana_limit] + ostalo[:ostalo_limit]
    
    print(f"📊 HRANA: {hrana_limit}/{len(hrana)}, OSTALO: {ostalo_limit}/{len(ostalo)}")
    print(f"📊 Ukupno odabrano: {len(selected)}")
    
    return selected




def spremi_u_firestore(products):
    """Sprema proizvode u Firestore u batch operacijama."""
    batch_size = 500
    total = len(products)
    
    for i in range(0, total, batch_size):
        batch = db.batch()
        chunk = products[i:i + batch_size]
        
        for p in chunk:
            doc_id = f"{p['barkod']}_{p['trgovina']}_{p['grad']}".replace(" ", "_")
            doc_ref = db.collection("cijene").document(doc_id)
            batch.set(doc_ref, p, merge=True)
        
        batch.commit()
        print(f"✅ Spremljeno {min(i + batch_size, total)}/{total}")
        
        if i + batch_size < total:
            time.sleep(1)




# ============================================================
# MAIN
# ============================================================


def main():
    print(f"🚀 Pokrećem {TRGOVINA} scraper...")
    print(f"📍 Grad: {GRAD}")
    print(f"🔧 Lokalni test: {LOKALNI_TEST}")

    # Provjera duplikata - preskoči ako je već scrapano danas
    if vec_scrapano_danas(TRGOVINA):
        print(f"⏭️ {TRGOVINA} je već scrapano danas ({datetime.now():%Y-%m-%d}). Preskačem.")
        return
    
    try:
        # 1. Pronađi URL
        url = pronadji_spar_url_sa_backup()
        if not url:
            print("❌ Nije pronađen CSV za današnji dan.")
            exit(1)
        
        # 2. Preuzmi CSV
        csv_content = preuzmi_csv(url)
        
        # 3. Obradi CSV
        products = obradi_csv(csv_content)
        print(f"📦 Ukupno proizvoda u CSV-u: {len(products)}")
        
        if not products:
            print("❌ Nema proizvoda za obradu.")
            exit(1)
        
        # 4. Odaberi prema kvotama
        selected = odaberi_proizvode(products)
        
        # 5. Spremi u Firestore
        spremi_u_firestore(selected)
        
        print(f"🎉 Završeno! Spremljeno {len(selected)} proizvoda.")
        
    except Exception as e:
        print(f"❌ GREŠKA: {e}")
        traceback.print_exc()
        exit(1)




if __name__ == "__main__":
    main()
