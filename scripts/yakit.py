"""
Kaput Üstü – günlük akaryakıt fiyatları.

GitHub Actions her sabah bu betiği çalıştırır:
  1. Her il için Opet ve Petrol Ofisi'nin sitelerinde yayınladığı pompa fiyatlarını çeker.
  2. Benzin (kurşunsuz 95), motorin ve LPG için il fiyatını (ilçelerin ortancası) hesaplar.
  3. Sonucu depodaki yakit.json dosyasına yazar. Uygulama sadece bu dosyayı okur.

Bir kaynak çalışmazsa diğerinden gelen değer kullanılır. Hiçbiri çalışmazsa
o il için önceki günün değeri korunur (dosyada "tarih" alanı eski kalır).
debug/ klasörüne her kaynaktan örnek yanıtlar yazılır; bir şey bozulursa oradan bakılır.
Sadece Python standart kütüphanesi kullanılır.
"""

import datetime as dt
import gzip
import html
import json
import os
import re
import statistics
import time
import unicodedata
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "yakit.json")
DEBUG = os.path.join(ROOT, "debug")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# Plaka kodu: il adı
ILLER = {
    1: "Adana", 2: "Adıyaman", 3: "Afyonkarahisar", 4: "Ağrı", 5: "Amasya", 6: "Ankara", 7: "Antalya",
    8: "Artvin", 9: "Aydın", 10: "Balıkesir", 11: "Bilecik", 12: "Bingöl", 13: "Bitlis", 14: "Bolu",
    15: "Burdur", 16: "Bursa", 17: "Çanakkale", 18: "Çankırı", 19: "Çorum", 20: "Denizli", 21: "Diyarbakır",
    22: "Edirne", 23: "Elazığ", 24: "Erzincan", 25: "Erzurum", 26: "Eskişehir", 27: "Gaziantep", 28: "Giresun",
    29: "Gümüşhane", 30: "Hakkari", 31: "Hatay", 32: "Isparta", 33: "Mersin", 34: "İstanbul", 35: "İzmir",
    36: "Kars", 37: "Kastamonu", 38: "Kayseri", 39: "Kırklareli", 40: "Kırşehir", 41: "Kocaeli", 42: "Konya",
    43: "Kütahya", 44: "Malatya", 45: "Manisa", 46: "Kahramanmaraş", 47: "Mardin", 48: "Muğla", 49: "Muş",
    50: "Nevşehir", 51: "Niğde", 52: "Ordu", 53: "Rize", 54: "Sakarya", 55: "Samsun", 56: "Siirt", 57: "Sinop",
    58: "Sivas", 59: "Tekirdağ", 60: "Tokat", 61: "Trabzon", 62: "Tunceli", 63: "Şanlıurfa", 64: "Uşak",
    65: "Van", 66: "Yozgat", 67: "Zonguldak", 68: "Aksaray", 69: "Bayburt", 70: "Karaman", 71: "Kırıkkale",
    72: "Batman", 73: "Şırnak", 74: "Bartın", 75: "Ardahan", 76: "Iğdır", 77: "Yalova", 78: "Karabük",
    79: "Kilis", 80: "Osmaniye", 81: "Düzce",
}

DEBUG_SAMPLES = {47, 34, 6}  # Mardin, İstanbul, Ankara: ham yanıtları debug/ altına yaz


def ascii_slug(name: str) -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    s = name.translate(table).lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm(text: str) -> str:
    """Karşılaştırma için: küçük harf, Türkçe karakterler sadeleşmiş."""
    return ascii_slug(text).replace("-", " ")


def fetch(url: str, accept: str = "*/*", referer: str | None = None) -> tuple[int, str]:
    headers = {"User-Agent": UA, "Accept": accept, "Accept-Language": "tr-TR,tr;q=0.9",
               "Accept-Encoding": "gzip"}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = re.match(r"https?://[^/]+", referer).group(0)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # ağ hatası
        return 0, f"{type(e).__name__}: {e}"


def save_debug(name: str, text: str) -> None:
    os.makedirs(DEBUG, exist_ok=True)
    with open(os.path.join(DEBUG, name), "w", encoding="utf-8") as f:
        f.write(text[:60000])


# ---------------------------------------------------------------- ürün eşleme

def classify(product: str) -> str | None:
    p = norm(product)
    if any(w in p for w in ("gazyag", "kalorifer", "fuel oil", "fuel-oil", "denizcilik", "bunker", "adblue")):
        return None
    if "lpg" in p or "otogaz" in p or "oto gaz" in p or re.search(r"\bpo gaz\b|\bgaz\b", p):
        return "lpg"
    if "motorin" in p or "diesel" in p or "dizel" in p:
        return "motorin"
    if ("benzin" in p or "kursunsuz" in p) and "97" not in p and "100" not in p:
        return "benzin"
    return None


def plausible(value: float) -> bool:
    return 5 < value < 500  # TL/litre için makul aralık


def to_float(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("₺", "").replace("TL", "").strip()
        if re.fullmatch(r"\d{1,3}([.,]\d{1,3})?", s):
            return float(s.replace(",", "."))
    return None


def walk_json(node, out: dict) -> None:
    """JSON içinde {ürün adı, fiyat} çiftlerini arar (alan adlarını bilmeden)."""
    if isinstance(node, dict):
        names = [v for k, v in node.items() if isinstance(v, str) and classify(v)]
        prices = [to_float(v) for k, v in node.items()
                  if re.search(r"amount|price|fiyat|value|tutar", k, re.I)]
        prices = [p for p in prices if p is not None and plausible(p)]
        if names and prices:
            kind = classify(names[0])
            out.setdefault(kind, []).append(prices[0])
        for v in node.values():
            walk_json(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_json(v, out)


# ---------------------------------------------------------------- kaynaklar

def opet(plate: int) -> dict:
    url = (f"https://api.opet.com.tr/api/fuelprices/prices?ProvinceCode={plate}"
           f"&IncludeAllProducts=true")
    status, body = fetch(url, "application/json", "https://www.opet.com.tr/akaryakit-fiyatlari")
    if plate in DEBUG_SAMPLES:
        save_debug(f"opet_{plate}.txt", f"HTTP {status}\n{body}")
    if status != 200:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    found: dict = {}
    walk_json(data, found)
    # Motorinde birden çok çeşit olabilir (standart / premium): en düşük olanlar standarttır.
    result = {}
    for kind, values in found.items():
        if kind == "motorin":
            values = sorted(values)[: max(1, len(values) // 2)]
        result[kind] = statistics.median(values)
    return result


def petrol_ofisi(plate: int) -> dict:
    slug = ascii_slug(ILLER[plate])
    url = f"https://www.petrolofisi.com.tr/akaryakit-fiyatlari/{slug}-akaryakit-fiyatlari"
    status, body = fetch(url, "text/html", "https://www.petrolofisi.com.tr/akaryakit-fiyatlari")
    if plate in DEBUG_SAMPLES:
        save_debug(f"po_{plate}.txt", f"HTTP {status}\n{body}")
    if status != 200 or not body:
        return {}
    # Tablodaki sütun başlıklarını ve satırları düz metne çevir.
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I)
    header_cells: list[str] = []
    found: dict = {}
    for row in rows:
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if not cells:
            continue
        if not header_cells and any(classify(c) for c in cells):
            header_cells = cells
            continue
        if header_cells:
            for name, cell in zip(header_cells, cells):
                kind = classify(name)
                m = re.search(r"\d{1,3}[.,]\d{2}", cell)
                if kind and m:
                    value = float(m.group(0).replace(",", "."))
                    if plausible(value):
                        found.setdefault(kind, []).append(value)
    return {k: statistics.median(v) for k, v in found.items()}


SOURCES = [("Opet", opet), ("Petrol Ofisi", petrol_ofisi)]


# ---------------------------------------------------------------- ana akış

def main() -> None:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date().isoformat()
    try:
        with open(OUT, encoding="utf-8") as f:
            previous = json.load(f).get("iller", {})
    except (OSError, ValueError):
        previous = {}

    provinces = {}
    report = []
    for plate, name in ILLER.items():
        entry = {"ad": name, "tarih": today}
        sources = {}
        for source_name, func in SOURCES:
            try:
                prices = func(plate)
            except Exception as e:
                report.append(f"{plate} {source_name} hata: {e}")
                prices = {}
            for kind in ("benzin", "motorin", "lpg"):
                if kind not in entry and kind in prices:
                    entry[kind] = round(prices[kind], 2)
                    sources[kind] = source_name
            time.sleep(0.4)  # sitelere yük bindirmemek için

        if not any(k in entry for k in ("benzin", "motorin", "lpg")):
            old = previous.get(str(plate))
            if old:
                provinces[str(plate)] = old  # bugün alınamadı: dünkü değer kalsın
            report.append(f"{plate} {name}: veri yok")
            continue
        entry["kaynak"] = sources
        provinces[str(plate)] = entry
        report.append(f"{plate} {name}: " + ", ".join(f"{k}={entry.get(k)}" for k in ("benzin", "motorin", "lpg")))

    out = {
        "guncelleme": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aciklama": "İl ortalaması (ilçelerin ortancası), TL/litre. Benzin: kurşunsuz 95, motorin: standart, LPG: otogaz.",
        "iller": provinces,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    save_debug("rapor.txt", "\n".join(report))
    print("\n".join(report))
    ok = sum(1 for v in provinces.values() if v.get("tarih") == today)
    print(f"\nBugün güncellenen il: {ok}/81")


if __name__ == "__main__":
    main()
