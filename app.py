from __future__ import annotations

import io
import json
import re
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable
import xml.etree.ElementTree as ET

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

CRZ_EXPORT_URLS = [
    "https://www.crz.gov.sk/export/{date}.zip",
    "https://crz.gov.sk/export/{date}.zip",
]
BENCHMARKS = {"elektrina": 112.5, "plyn": 29.0}
DEFAULT_MONTHLY_MWH = {"elektrina": 18.0, "plyn": 30.0}
MANUAL_DISTRICT_MUNICIPALITIES = {
    "senica": [
        "Borský Svätý Jur",
        "Cerová",
        "Čáry",
        "Dojč",
        "Gbely",
        "Hlboké",
        "Hradište pod Vrátnom",
        "Jablonica",
        "Kátov",
        "Koválovec",
        "Kuklov",
        "Kúty",
        "Lakšárska Nová Ves",
        "Moravský Svätý Ján",
        "Osuské",
        "Petrova Ves",
        "Plavecký Peter",
        "Podbranč",
        "Popudinské Močidľany",
        "Prietrž",
        "Prievaly",
        "Radimov",
        "Radošovce",
        "Rohov",
        "Rovensko",
        "Sekule",
        "Senica",
        "Smolinské",
        "Smrdáky",
        "Sobotište",
        "Šajdíkove Humence",
        "Šaštín-Stráže",
        "Štefanov",
        "Unín",
    ]
}


@dataclass
class ContractRecord:
    published_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    contract_number: str
    title: str
    supplier: str
    customer: str
    price_eur_total: float | None
    commodity: str
    person_name: str
    search_blob: str
    unit_price_eur_mwh: float | None
    party1: str
    party2: str


class JobCancelledError(RuntimeError):
    pass


JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _new_job(
    district: str,
    upload_zip: bytes | None,
    upload_name: str,
    selected_municipalities: list[str] | None = None,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "district": district,
            "status": "queued",
            "logs": [f"{datetime.now().strftime('%H:%M:%S')} Job vytvoreny."],
            "result": None,
            "error": "",
            "cancel_requested": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "upload_zip": upload_zip,
            "upload_name": upload_name,
            "selected_municipalities": selected_municipalities or [],
            "progress_current": 0,
            "progress_total": 0,
            "phase": "queued",
        }
    return job_id


def _append_log(job_id: str, message: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["logs"].append(line)


def _set_status(job_id: str, status: str, error: str = "", result: dict | None = None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = status
        job["error"] = error
        if result is not None:
            job["result"] = result


def _set_progress(job_id: str, current: int, total: int, phase: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["progress_current"] = max(0, int(current))
        job["progress_total"] = max(0, int(total))
        job["phase"] = phase


def _clear_upload(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["upload_zip"] = None


def _is_cancel_requested(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def _check_cancel(job_id: str) -> None:
    if _is_cancel_requested(job_id):
        raise JobCancelledError("Spracovanie bolo zrusene pouzivatelom.")


def _snapshot(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "district": job["district"],
            "status": job["status"],
            "logs": list(job["logs"]),
            "result": job["result"],
            "error": job["error"],
            "cancel_requested": job["cancel_requested"],
            "created_at": job["created_at"],
            "has_upload_zip": bool(job.get("upload_zip")),
            "selected_municipalities": list(job.get("selected_municipalities") or []),
            "progress_current": int(job.get("progress_current") or 0),
            "progress_total": int(job.get("progress_total") or 0),
            "phase": str(job.get("phase") or "queued"),
        }


def strip_accents(text: str) -> str:
    normed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normed if not unicodedata.combining(ch))


def norm(text: str) -> str:
    out = strip_accents(text).lower().strip()
    out = re.sub(r"\s+", " ", out)
    return out


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    formats = (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    )
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return datetime(y, mth, d)
    except ValueError:
        return None


def extract_dates_from_text(value: str | None) -> list[datetime]:
    if not value:
        return []
    out: list[datetime] = []
    for m in re.finditer(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value):
        d, mth, y = map(int, m.groups())
        try:
            out.append(datetime(y, mth, d))
        except ValueError:
            continue
    return out


def parse_money(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace("€", "").replace("EUR", "")
    cleaned = cleaned.replace(" ", "").replace("\xa0", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_decimal(value: str) -> float | None:
    s = value.replace(" ", "").replace("\xa0", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def get_first_by_hints(values: dict[str, str], hints: Iterable[str]) -> str:
    for hint in hints:
        for key, value in values.items():
            if hint in key and value:
                return value
    return ""


def detect_commodity(text: str) -> str:
    t = norm(text)
    electric_markers = (
        "elektrina",
        "elektricka energia",
        "elektrickej energie",
        "silova energia",
        "dodavka elektr",
        "odber elektr",
        "zdruzena dodavka elektr",
    )
    gas_markers = (
        "zemny plyn",
        "dodavka plynu",
        "odber plynu",
        "zdruzena dodavka plynu",
        "komodita plyn",
    )
    if "elektronick" in t and not any(k in t for k in electric_markers):
        return "ine"
    if any(k in t for k in electric_markers):
        return "elektrina"
    if any(k in t for k in gas_markers):
        return "plyn"
    return "ine"


def detect_person_name(values: dict[str, str]) -> str:
    candidate = get_first_by_hints(
        values,
        ["kontakt", "meno", "statutar", "zastup", "podpis", "opravnen", "zodpoved"],
    )
    candidate = re.sub(r"\s+", " ", candidate).strip()
    candidate = re.sub(r"^(ing\.|mgr\.|bc\.|doc\.|prof\.)\s*", "", candidate, flags=re.IGNORECASE)
    if len(candidate.split()) > 4:
        return ""
    if re.search(r"\d", candidate):
        return ""
    return candidate


def is_municipality(customer: str) -> bool:
    c = norm(customer)
    return (
        c.startswith("obec ")
        or c.startswith("mesto ")
        or c.startswith("mestsky cast ")
        or c.startswith("obecny urad ")
        or c.startswith("mestsky urad ")
        or " obec " in f" {c} "
        or " mesto " in f" {c} "
    )


def extract_unit_price_eur_mwh(text: str, commodity: str) -> float | None:
    t = norm(text)
    pattern = re.compile(r"(\d{1,4}(?:[\.,]\d{1,4})?)\s*(?:eur|€)?\s*/\s*(mwh|kwh)")
    matches = list(pattern.finditer(t))
    if not matches:
        return None

    values: list[float] = []
    for m in matches:
        raw_num = m.group(1)
        unit = m.group(2)
        value = parse_decimal(raw_num)
        if value is None:
            continue
        if unit == "kwh":
            value = value * 1000.0
        start = max(0, m.start() - 70)
        end = min(len(t), m.end() + 70)
        window = t[start:end]

        if commodity == "elektrina" and "elektr" not in window and "silov" not in window:
            continue
        if commodity == "plyn" and "plyn" not in window and "gas" not in window:
            continue

        values.append(value)

    if not values:
        for m in matches:
            raw_num = m.group(1)
            unit = m.group(2)
            value = parse_decimal(raw_num)
            if value is None:
                continue
            if unit == "kwh":
                value = value * 1000.0
            values.append(value)

    if not values:
        return None

    plausible = [v for v in values if 5 <= v <= 500]
    if plausible:
        return plausible[0]
    return values[0]


def format_price(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €/MWh"


def format_eur(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def is_probably_energy_contract(row: pd.Series) -> bool:
    t = norm(f"{row.get('title','')} {row.get('search_blob','')}")
    if "elektronick" in t and "elektrina" not in t and "elektrickej energie" not in t:
        return False
    if row.get("commodity") == "elektrina":
        return any(
            m in t
            for m in (
                "elektrina",
                "elektricka energia",
                "elektrickej energie",
                "dodavka elektr",
                "odber elektr",
                "silova energia",
                "mwh",
                "kwh",
            )
        )
    if row.get("commodity") == "plyn":
        return any(
            m in t
            for m in (
                "zemny plyn",
                "dodavka plynu",
                "odber plynu",
                "komodita plyn",
                "mwh",
                "kwh",
            )
        )
    return False


def estimate_duration_months(row: pd.Series) -> int:
    valid_from = row.get("valid_from")
    valid_to = row.get("valid_to")
    published = row.get("published_at")

    start = None
    if pd.notna(valid_from):
        start = pd.to_datetime(valid_from)
    elif pd.notna(published):
        start = pd.to_datetime(published)

    if start is None or pd.isna(valid_to):
        return 12

    end = pd.to_datetime(valid_to)
    delta_days = max(30, int((end - start).days))
    return max(1, round(delta_days / 30.4))


def estimate_monthly_savings(row: pd.Series) -> tuple[float | None, str, str]:
    total = row.get("price_eur_total")
    unit_price = row.get("unit_price_eur_mwh")
    benchmark = row.get("benchmark_eur_mwh")
    diff = row.get("price_diff")
    months = estimate_duration_months(row)

    if pd.notna(total) and pd.notna(unit_price) and pd.notna(benchmark) and pd.notna(diff):
        unit_price_f = float(unit_price)
        diff_f = float(diff)
        total_f = float(total)
        if total_f > 0 and unit_price_f > 0 and diff_f > 0 and months > 0:
            monthly_mwh = (total_f / unit_price_f) / months
            monthly_savings = monthly_mwh * diff_f
            return monthly_savings, "jednotkova_cena", "vysoka"

    if pd.notna(total):
        total_f = float(total)
        if total_f <= 0:
            # Model fallback for missing contract value: 12% from benchmark monthly spend assumption.
            commodity = str(row.get("commodity") or "")
            bench = BENCHMARKS.get(commodity)
            mwh = DEFAULT_MONTHLY_MWH.get(commodity)
            if bench and mwh:
                return bench * mwh * 0.12, "model_12pct_benchmark", "nizka"
            return None, "insufficient_total_price", "nizka"
        monthly_base = total_f / max(1, months)
        monthly_savings = monthly_base * 0.12
        return monthly_savings, "12_percent_fallback", "stredna"

    commodity = str(row.get("commodity") or "")
    bench = BENCHMARKS.get(commodity)
    mwh = DEFAULT_MONTHLY_MWH.get(commodity)
    if bench and mwh:
        return bench * mwh * 0.12, "model_12pct_benchmark", "nizka"
    return None, "insufficient_data", "nizka"


def _cache_zip_path(ds: str) -> Path:
    return CACHE_DIR / f"{ds}.zip"


def _load_cached_zip(ds: str) -> bytes | None:
    p = _cache_zip_path(ds)
    if not p.exists():
        return None
    return p.read_bytes()


def _save_cached_zip(ds: str, data: bytes) -> None:
    _cache_zip_path(ds).write_bytes(data)


def download_zip_for_date(
    ds: str,
    cancel_check: Callable[[], None],
    log: Callable[[str], None],
) -> tuple[bytes | None, str]:
    cancel_check()
    cached = _load_cached_zip(ds)
    if cached is not None:
        log(f"Cache hit pre {ds} ({len(cached):,} B)")
        return cached, f"cache://{ds}.zip"

    headers = {"User-Agent": "Mozilla/5.0 (ENEX-CRZ-Research)"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    for url_tpl in CRZ_EXPORT_URLS:
        cancel_check()
        url = url_tpl.format(date=ds)
        log(f"Skusam {url}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=25) as resp:
                if resp.status != 200:
                    continue
                content = resp.read()
                _save_cached_zip(ds, content)
                log(f"Stiahnute: {len(content):,} B")
                return content, url
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log(f"HTTP chyba {exc.code} pre {ds}")
            continue
        except Exception as exc:
            log(f"Docasna chyba pre {ds}: {exc}")
            continue

    return None, ""


def download_latest_crz_zip(
    days_back: int,
    cancel_check: Callable[[], None],
    log: Callable[[str], None],
) -> tuple[bytes, str, str]:
    today = datetime.now().date()

    log("Hladam najnovsi dostupny CRZ export ZIP.")
    for delta in range(days_back + 1):
        cancel_check()
        d = today - timedelta(days=delta)
        ds = d.strftime("%Y-%m-%d")
        content, source = download_zip_for_date(ds, cancel_check=cancel_check, log=log)
        if content is not None:
            return content, source, ds

    raise ValueError(
        "Nepodarilo sa stiahnut CRZ export (siet blokuje spojenie). "
        "Skus nahrat ZIP manualne cez pole 'CRZ ZIP (volitelne)'."
    )


def extract_records_from_xml(
    xml_bytes: bytes,
    cancel_check: Callable[[], None],
    log: Callable[[str], None],
) -> list[ContractRecord]:
    root = ET.fromstring(xml_bytes)
    rows: list[ContractRecord] = []
    seen: set[tuple[str, str, str, str]] = set()

    processed = 0
    for node in root.iter():
        processed += 1
        if processed % 5000 == 0:
            cancel_check()
            log(f"Parsovanie XML... spracovanych uzlov: {processed}")

        children = list(node)
        if len(children) < 3:
            continue

        values: dict[str, str] = {}
        for child in children:
            if list(child):
                continue
            txt = (child.text or "").strip()
            if txt:
                values[local_tag(child.tag)] = txt

        if not values:
            continue

        title = get_first_by_hints(values, ["nazov", "name", "predmet", "title"])
        customer = get_first_by_hints(values, ["objednavatel", "odberatel", "customer", "mandant"])
        supplier = get_first_by_hints(values, ["dodavatel", "supplier", "zhotovitel"])
        party1 = get_first_by_hints(values, ["zs1"])
        party2 = get_first_by_hints(values, ["zs2"])
        contract_number = get_first_by_hints(values, ["cislo", "number", "id_zmluv", "id"])

        if not customer and party1:
            customer = party1
        if not supplier and party2:
            supplier = party2

        quality_score = sum(bool(x) for x in (title, customer, supplier, contract_number))
        if quality_score < 2:
            continue

        key = (contract_number, title, supplier, customer)
        if key in seen:
            continue
        seen.add(key)

        published_raw = get_first_by_hints(values, ["zverejnen", "publ", "datum_zverejnenia", "created"])
        valid_from_raw = get_first_by_hints(values, ["ucinnost_od", "platnost_od", "valid_from", "from"])
        valid_to_raw = get_first_by_hints(values, ["ucinnost_do", "platnost_do", "valid_to", "to"])
        text_ucinnost = get_first_by_hints(values, ["text_ucinnost"])
        price_raw = get_first_by_hints(values, ["cena", "price", "hodnota", "suma"])

        blob = " ".join(values.values())
        commodity = detect_commodity(" ".join([title, contract_number, blob]))

        parsed_valid_from = parse_date(valid_from_raw)
        parsed_valid_to = parse_date(valid_to_raw)
        dates_from_text = extract_dates_from_text(text_ucinnost)
        if parsed_valid_from is None and dates_from_text:
            parsed_valid_from = dates_from_text[0]
        if parsed_valid_to is None and dates_from_text:
            parsed_valid_to = dates_from_text[-1]

        rows.append(
            ContractRecord(
                published_at=parse_date(published_raw),
                valid_from=parsed_valid_from,
                valid_to=parsed_valid_to,
                contract_number=contract_number,
                title=title,
                supplier=supplier,
                customer=customer,
                price_eur_total=parse_money(price_raw),
                commodity=commodity,
                person_name=detect_person_name(values),
                search_blob=blob,
                unit_price_eur_mwh=extract_unit_price_eur_mwh(blob + " " + title, commodity),
                party1=party1,
                party2=party2,
            )
        )

    log(f"Dokoncene parsovanie XML, zaznamov: {len(rows)}")
    return rows


def read_crz_zip(file_bytes: bytes, cancel_check: Callable[[], None], log: Callable[[str], None]) -> pd.DataFrame:
    cancel_check()
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        xml_names = [name for name in zf.namelist() if name.lower().endswith(".xml")]
        if not xml_names:
            raise ValueError("ZIP neobsahuje XML subor.")
        log(f"ZIP obsahuje XML: {xml_names[0]}")
        with zf.open(xml_names[0]) as fh:
            xml_bytes = fh.read()

    cancel_check()
    records = extract_records_from_xml(xml_bytes, cancel_check=cancel_check, log=log)
    if not records:
        raise ValueError("V XML sa nepodarilo najst pouzitelne zmluvy.")

    df = pd.DataFrame(
        [
            {
                "published_at": r.published_at,
                "valid_from": r.valid_from,
                "valid_to": r.valid_to,
                "contract_number": r.contract_number,
                "title": r.title,
                "supplier": r.supplier,
                "customer": r.customer,
                "price_eur_total": r.price_eur_total,
                "commodity": r.commodity,
                "person_name": r.person_name,
                "search_blob": r.search_blob,
                "unit_price_eur_mwh": r.unit_price_eur_mwh,
                "party1": r.party1,
                "party2": r.party2,
            }
            for r in records
        ]
    )
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
    df["rank_date"] = df["published_at"].fillna(pd.Timestamp("1970-01-01"))
    return df.sort_values("rank_date", ascending=False)


def _contract_identity(df: pd.DataFrame) -> pd.Series:
    return (
        df["contract_number"].fillna("")
        + "|"
        + df["customer"].fillna("")
        + "|"
        + df["supplier"].fillna("")
        + "|"
        + df["title"].fillna("")
    ).map(norm)


def collect_contracts_window(
    days_back: int,
    cancel_check: Callable[[], None],
    log: Callable[[str], None],
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    today = datetime.now().date()
    all_frames: list[pd.DataFrame] = []
    used_sources: list[dict[str, str]] = []
    seen_contract_ids: set[str] = set()

    for delta in range(days_back + 1):
        cancel_check()
        if progress:
            progress(delta + 1, days_back + 1, "download_parse")
        d = today - timedelta(days=delta)
        ds = d.strftime("%Y-%m-%d")
        content, source = download_zip_for_date(ds, cancel_check=cancel_check, log=log)
        if content is None:
            continue

        try:
            df_day = read_crz_zip(content, cancel_check=cancel_check, log=log)
        except Exception as exc:
            log(f"Preskakujem {ds}, parse chyba: {exc}")
            continue

        ids = _contract_identity(df_day)
        keep_mask = ~ids.isin(seen_contract_ids)
        df_new = df_day[keep_mask].copy()
        for cid in ids[keep_mask]:
            seen_contract_ids.add(cid)

        used_sources.append({"date": ds, "source": source, "rows": str(len(df_new))})
        all_frames.append(df_new)
        if delta % 10 == 0:
            log(f"Doteraz agregovanych zaznamov: {sum(len(x) for x in all_frames)}")

    if not all_frames:
        raise ValueError(
            "Nepodarilo sa nacitat ziadne CRZ data. Skus manualny ZIP alebo vacsi network timeout."
        )

    out = pd.concat(all_frames, ignore_index=True)
    out = out.sort_values("rank_date", ascending=False)
    log(f"Agregacia dokoncena, zaznamov celkom: {len(out)}")
    return out, used_sources


def filter_district_municipality_contracts(df: pd.DataFrame, district: str) -> pd.DataFrame:
    district_n = norm(district)
    if not district_n:
        raise ValueError("Zadaj okres.")
    district_mask_all = (
        df["customer"].fillna("").map(norm).str.contains(district_n, regex=False)
        | df["title"].fillna("").map(norm).str.contains(district_n, regex=False)
        | df["search_blob"].fillna("").map(norm).str.contains(district_n, regex=False)
    )
    district_rows = df[district_mask_all].copy()
    if district_rows.empty:
        raise ValueError(f"Pre okres '{district}' sa nenasli ziadne zmluvy.")

    manual = MANUAL_DISTRICT_MUNICIPALITIES.get(district_n, [])
    if manual:
        municipality_norm = {norm(x) for x in manual if x}
    else:
        municipality_names = (
            district_rows[district_rows["customer"].fillna("").map(is_municipality)]["customer"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
        municipality_norm = {norm(x) for x in municipality_names if x}

    if not municipality_norm:
        raise ValueError(
            f"Pre okres '{district}' sa nenasli obce/mesta. Skus vacsie lookback okno."
        )

    e = df[df["commodity"].isin(["elektrina", "plyn"])].copy()
    e = e[e.apply(is_probably_energy_contract, axis=1)]
    if e.empty:
        raise ValueError("V exporte sa nenasli energeticke zmluvy na elektrinu/plyn.")

    def text_matches_municipality(text: str) -> bool:
        t = norm(text)
        if not t:
            return False
        for m in municipality_norm:
            if m and (t == m or m in t):
                return True
        return False

    def row_matches_municipality(row: pd.Series) -> bool:
        return any(
            text_matches_municipality(str(row.get(col) or ""))
            for col in ("customer", "supplier", "party1", "party2")
        )

    e = e[e.apply(row_matches_municipality, axis=1)]
    if e.empty:
        raise ValueError(
            f"Nasli sa obce pre okres '{district}', ale zatial bez zmluv na elektrinu/plyn."
        )

    def resolve_org_supplier(row: pd.Series) -> tuple[str, str]:
        p1 = str(row.get("party1") or "")
        p2 = str(row.get("party2") or "")
        c = str(row.get("customer") or "")
        s = str(row.get("supplier") or "")

        if text_matches_municipality(p1):
            return p1, p2 or s or c
        if text_matches_municipality(p2):
            return p2, p1 or s or c
        if text_matches_municipality(c):
            return c, s or p2 or p1
        if text_matches_municipality(s):
            return s, c or p1 or p2
        return c or p1 or p2, s or p2 or p1

    resolved = e.apply(resolve_org_supplier, axis=1, result_type="expand")
    e["org_name"] = resolved[0]
    e["supplier_name"] = resolved[1]

    def is_target_municipality_org(org: str) -> bool:
        t = norm(org)
        if not t:
            return False
        if not is_municipality(t):
            return False
        for m in municipality_norm:
            if not m:
                continue
            if t == m:
                return True
            if t == f"obec {m}" or t == f"mesto {m}" or t == f"mestsky cast {m}":
                return True
            if t.startswith(f"obec {m},") or t.startswith(f"mesto {m},"):
                return True
        return False

    e = e[e["org_name"].fillna("").map(is_target_municipality_org)]
    if e.empty:
        raise ValueError(
            f"Nasli sa energeticke zmluvy, ale nie priamo pre obce/mesta okresu '{district}'."
        )

    e = e.sort_values("rank_date", ascending=False)
    e = e.drop_duplicates(subset=["org_name", "commodity"], keep="first")
    e["benchmark_eur_mwh"] = e["commodity"].map(BENCHMARKS)
    e["price_diff"] = e["unit_price_eur_mwh"] - e["benchmark_eur_mwh"]
    return e.sort_values(["price_diff", "rank_date"], ascending=[False, False])


def filter_by_selected_municipalities(df: pd.DataFrame, selected_municipalities: list[str]) -> pd.DataFrame:
    selected_norm = {norm(x) for x in selected_municipalities if str(x).strip()}
    if not selected_norm:
        raise ValueError("Neboli vybrane ziadne obce.")

    e = df[df["commodity"].isin(["elektrina", "plyn"])].copy()
    e = e[e.apply(is_probably_energy_contract, axis=1)]
    if e.empty:
        raise ValueError("V exporte sa nenasli energeticke zmluvy na elektrinu/plyn.")

    def text_matches_selected(text: str) -> bool:
        t = norm(text)
        if not t:
            return False
        for m in selected_norm:
            if m and (t == m or m in t):
                return True
        return False

    def row_matches_selected(row: pd.Series) -> bool:
        return any(
            text_matches_selected(str(row.get(col) or ""))
            for col in ("customer", "supplier", "party1", "party2")
        )

    e = e[e.apply(row_matches_selected, axis=1)]
    if e.empty:
        raise ValueError("Pre vybrane obce sa nenasli energeticke zmluvy.")

    def resolve_org_supplier(row: pd.Series) -> tuple[str, str]:
        p1 = str(row.get("party1") or "")
        p2 = str(row.get("party2") or "")
        c = str(row.get("customer") or "")
        s = str(row.get("supplier") or "")

        if text_matches_selected(p1):
            return p1, p2 or s or c
        if text_matches_selected(p2):
            return p2, p1 or s or c
        if text_matches_selected(c):
            return c, s or p2 or p1
        if text_matches_selected(s):
            return s, c or p1 or p2
        return c or p1 or p2, s or p2 or p1

    resolved = e.apply(resolve_org_supplier, axis=1, result_type="expand")
    e["org_name"] = resolved[0]
    e["supplier_name"] = resolved[1]

    def is_target_selected_org(org: str) -> bool:
        t = norm(org)
        if not t:
            return False
        if not is_municipality(t):
            return False
        for m in selected_norm:
            if not m:
                continue
            if t == m:
                return True
            if t == f"obec {m}" or t == f"mesto {m}" or t == f"mestsky cast {m}":
                return True
            if t.startswith(f"obec {m},") or t.startswith(f"mesto {m},"):
                return True
        return False

    e = e[e["org_name"].fillna("").map(is_target_selected_org)]
    if e.empty:
        raise ValueError("Nasli sa zaznamy, ale nie priamo pre vybrane obce/mesta.")

    e = e.sort_values("rank_date", ascending=False)
    e = e.drop_duplicates(subset=["org_name", "commodity"], keep="first")
    e["benchmark_eur_mwh"] = e["commodity"].map(BENCHMARKS)
    e["price_diff"] = e["unit_price_eur_mwh"] - e["benchmark_eur_mwh"]
    return e.sort_values(["price_diff", "rank_date"], ascending=[False, False])


def build_email_concept(row: pd.Series) -> tuple[str, str, float | None, str, str]:
    customer = row.get("org_name") or row.get("customer") or "Vasa organizacia"
    commodity = row.get("commodity") or "energia"
    person_name = str(row.get("person_name") or "").strip()

    greeting = f"Dobry den pan/pani {person_name}," if person_name else "Dobry den pan/pani XX,"

    unit_price = row.get("unit_price_eur_mwh")
    benchmark = row.get("benchmark_eur_mwh")
    diff = row.get("price_diff")
    monthly_savings, method, confidence = estimate_monthly_savings(row)
    valid_to = row.get("valid_to")
    valid_to_str = pd.to_datetime(valid_to).strftime("%d.%m.%Y") if pd.notna(valid_to) else "neuvedene"

    if pd.notna(unit_price) and pd.notna(benchmark):
        compare_line = (
            f"V CRZ evidujeme vysutazenu cenu priblizne {format_price(float(unit_price))}, "
            f"pricom nasa referencna cena je {format_price(float(benchmark))}."
        )
        if pd.notna(diff) and float(diff) > 0:
            savings_line = (
                f"Rozdiel je priblizne {format_price(float(diff))} v prospech noveho sutazenia "
                "(pred finalnym navrhom to overime podla odberoveho profilu)."
            )
        else:
            savings_line = "Aj tak vieme preverit, ci je pri novom sutazeni priestor na dalsie zlepsenie podmienok."
    else:
        compare_line = "V zmluve nie je jednoznacne uvedena jednotkova cena, preto navrhujeme rychle bezplatne prepocitanie."
        savings_line = "Na zaklade doplnenych udajov pripravime presny model uspory."

    subject = f"Mozna uspora na {commodity} pre {customer}"

    body = "\n".join(
        [
            greeting,
            "na zaklade verejne dostupnych udajov z CRZ sme pri Vasej organizacii identifikovali poslednu zmluvu:",
            "",
            f"Zmluva: {row.get('contract_number') or 'neuvedene'}",
            f"Dodavatel: {row.get('supplier_name') or row.get('supplier') or 'neuvedene'}",
            f"Platnost do: {valid_to_str}",
            compare_line,
            savings_line,
            (
                f"Odhad uspory za tento mesiac: {format_eur(monthly_savings)}"
                if confidence != "nizka"
                else f"Odhad uspory za tento mesiac (modelovy): {format_eur(monthly_savings)}"
            ),
            "",
            "Ak chcete, pripravime Vam bezplatne porovnanie a navrh postupu sutaze bez zavazku.",
            "Staci odpovedat na tento email alebo navrhnut kratky 15-min call.",
            "",
            "ENEX = Kludnejsie riesenie energii",
            "",
            "S pozdravom,",
            "Karol Luptak",
            "eSYST s.r.o.",
            "+421 910 364 111 | esyst.sk",
        ]
    )

    return subject, body, monthly_savings, method, confidence


def load_contracts_source(
    upload_zip: bytes | None,
    upload_name: str,
    lookback_days: int,
    cancel_check: Callable[[], None],
    log: Callable[[str], None],
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, str]], str, str]:
    if upload_zip:
        export_url = f"manual://{upload_name or 'uploaded.zip'}"
        export_date = datetime.now().strftime("%Y-%m-%d")
        log(f"Pouzity manualne nahraty ZIP: {upload_name or 'uploaded.zip'}")
        contracts = read_crz_zip(upload_zip, cancel_check=cancel_check, log=log)
        used_sources = [{"date": export_date, "source": export_url, "rows": str(len(contracts))}]
        log(f"Pocty zmluv celkom: {len(contracts)}")
        return contracts, used_sources, export_url, export_date

    log(f"Spustam agregaciu za poslednych {lookback_days} dni.")
    contracts, used_sources = collect_contracts_window(
        days_back=lookback_days,
        cancel_check=cancel_check,
        log=log,
        progress=progress,
    )
    export_url = used_sources[0]["source"] if used_sources else "n/a"
    export_date = used_sources[0]["date"] if used_sources else "n/a"
    log(f"Pocty zmluv celkom: {len(contracts)}")
    return contracts, used_sources, export_url, export_date


def _run_job(job_id: str) -> None:
    snapshot = _snapshot(job_id)
    if not snapshot:
        return

    district = snapshot["district"]
    upload_zip = snapshot.get("upload_zip")
    upload_name = snapshot.get("upload_name") or ""
    selected_municipalities = list(snapshot.get("selected_municipalities") or [])
    _set_status(job_id, "running")
    _set_progress(job_id, 0, 1, "starting")
    _append_log(job_id, f"Startujem CRZ research pre okres: {district}")

    def cancel_check() -> None:
        _check_cancel(job_id)

    def log(msg: str) -> None:
        _append_log(job_id, msg)

    def progress(current: int, total: int, phase: str) -> None:
        _set_progress(job_id, current, total, phase)

    try:
        cancel_check()
        lookback_days = 180
        log("Bezi 1 job; prechadzaju sa denné exporty v lookback okne.")
        contracts, used_sources, export_url, export_date = load_contracts_source(
            upload_zip=upload_zip,
            upload_name=upload_name,
            lookback_days=lookback_days,
            cancel_check=cancel_check,
            log=log,
            progress=progress,
        )

        cancel_check()
        _set_progress(job_id, 1, 1, "filtering")
        if selected_municipalities:
            picked = filter_by_selected_municipalities(contracts, selected_municipalities)
        else:
            picked = filter_district_municipality_contracts(contracts, district)
        log(f"Po filtrovani pre okres ostalo zaznamov: {len(picked)}")

        if picked.empty:
            raise ValueError("Nenasli sa ziadne zmluvy po filtrovani.")

        top = picked.iloc[0]
        subject, concept, top_monthly_savings, top_method, top_conf = build_email_concept(top)

        rows = []
        total_monthly_savings = 0.0
        total_count_with_savings = 0
        for _, r in picked.iterrows():
            row_subject, row_concept, row_monthly_savings, row_method, row_conf = build_email_concept(r)
            if row_monthly_savings is not None and row_monthly_savings > 0:
                total_monthly_savings += float(row_monthly_savings)
                total_count_with_savings += 1
            rows.append(
                {
                    "customer": r.get("org_name") or r.get("customer") or "",
                    "commodity": r.get("commodity") or "",
                    "contract_number": r.get("contract_number") or "",
                    "supplier": r.get("supplier_name") or r.get("supplier") or "",
                    "unit_price": format_price(r.get("unit_price_eur_mwh")),
                    "benchmark": format_price(r.get("benchmark_eur_mwh")),
                    "diff": format_price(r.get("price_diff")) if pd.notna(r.get("price_diff")) else "N/A",
                    "subject": row_subject,
                    "concept": row_concept,
                    "monthly_savings_eur": format_eur(row_monthly_savings),
                    "savings_method": row_method,
                    "confidence": row_conf,
                }
            )

        result = {
            "job_id": job_id,
            "district": district,
            "export_url": export_url,
            "export_date": export_date,
            "source_count": len(used_sources),
            "sources_json": json.dumps(used_sources[:12], ensure_ascii=False),
            "count": len(picked),
            "subject": subject,
            "concept": concept,
            "top_monthly_savings": format_eur(top_monthly_savings),
            "top_method": top_method,
            "top_confidence": top_conf,
            "total_monthly_savings": format_eur(total_monthly_savings),
            "total_count_with_savings": total_count_with_savings,
            "rows": rows,
        }
        _append_log(job_id, "Hotovo. Koncept emailu je pripraveny.")
        _set_progress(job_id, 1, 1, "completed")
        _clear_upload(job_id)
        _set_status(job_id, "completed", result=result)
    except JobCancelledError as exc:
        _append_log(job_id, str(exc))
        _set_progress(job_id, 1, 1, "cancelled")
        _clear_upload(job_id)
        _set_status(job_id, "cancelled", error=str(exc))
    except Exception as exc:
        _append_log(job_id, f"Chyba: {exc}")
        _set_progress(job_id, 1, 1, "failed")
        _clear_upload(job_id)
        _set_status(job_id, "failed", error=str(exc))


@app.get("/")
def index_get():
    return render_template("index.html")


@app.post("/start")
def start_job():
    district = (request.form.get("district") or "").strip()
    if not district:
        return jsonify({"ok": False, "error": "Zadaj okres."}), 400

    upload_file = request.files.get("crz_zip")
    upload_zip = None
    upload_name = ""
    if upload_file and upload_file.filename:
        upload_name = upload_file.filename
        upload_zip = upload_file.read()
        if not upload_zip:
            return jsonify({"ok": False, "error": "Nahraty ZIP je prazdny."}), 400

    selected_raw = (request.form.get("selected_municipalities") or "").strip()
    selected_municipalities: list[str] = []
    if selected_raw:
        try:
            parsed = json.loads(selected_raw)
            if isinstance(parsed, list):
                selected_municipalities = [str(x) for x in parsed if str(x).strip()]
        except Exception:
            selected_municipalities = [x.strip() for x in selected_raw.split(",") if x.strip()]

    if not selected_municipalities:
        return jsonify({"ok": False, "error": "Najprv vyber aspon jednu obec na spracovanie."}), 400

    job_id = _new_job(
        district,
        upload_zip=upload_zip,
        upload_name=upload_name,
        selected_municipalities=selected_municipalities,
    )
    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/status/<job_id>")
def status(job_id: str):
    snap = _snapshot(job_id)
    if not snap:
        return jsonify({"ok": False, "error": "Job sa nenasiel."}), 404
    return jsonify({"ok": True, "job": snap})


@app.post("/discover")
def discover():
    district = (request.form.get("district") or "").strip()
    if not district:
        return jsonify({"ok": False, "error": "Zadaj okres."}), 400

    manual_fast = MANUAL_DISTRICT_MUNICIPALITIES.get(norm(district), [])
    if manual_fast:
        options = [{"name": x, "contracts": None, "latest": "N/A"} for x in manual_fast]
        return jsonify(
            {
                "ok": True,
                "district": district,
                "options": options,
                "logs": ["Rychly vyber obci pripraveny. Pocet zmluv sa vyhodnoti az pri spracovani vybranych obci."],
                "warning": "",
            }
        )

    upload_file = request.files.get("crz_zip")
    upload_zip = None
    upload_name = ""
    if upload_file and upload_file.filename:
        upload_name = upload_file.filename
        upload_zip = upload_file.read()
        if not upload_zip:
            return jsonify({"ok": False, "error": "Nahraty ZIP je prazdny."}), 400

    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)

    try:
        contracts, _, _, _ = load_contracts_source(
            upload_zip=upload_zip,
            upload_name=upload_name,
            lookback_days=120,
            cancel_check=lambda: None,
            log=log,
        )
        district_n = norm(district)
        district_mask = (
            contracts["title"].fillna("").map(norm).str.contains(district_n, regex=False)
            | contracts["search_blob"].fillna("").map(norm).str.contains(district_n, regex=False)
            | contracts["customer"].fillna("").map(norm).str.contains(district_n, regex=False)
            | contracts["supplier"].fillna("").map(norm).str.contains(district_n, regex=False)
            | contracts["party1"].fillna("").map(norm).str.contains(district_n, regex=False)
            | contracts["party2"].fillna("").map(norm).str.contains(district_n, regex=False)
        )
        district_rows = contracts[district_mask].copy()
        if district_rows.empty:
            return jsonify({"ok": False, "error": "Nenasli sa ziadne zaznamy pre okres."}), 400

        rows = []
        for _, r in district_rows.iterrows():
            for col in ("customer", "supplier", "party1", "party2"):
                name = str(r.get(col) or "").strip()
                if not name:
                    continue
                if not is_municipality(name):
                    continue
                rows.append(
                    {
                        "name": name,
                        "rank_date": r.get("rank_date"),
                        "is_energy": bool(r.get("commodity") in ("elektrina", "plyn") and is_probably_energy_contract(r)),
                    }
                )
        if not rows:
            return jsonify({"ok": False, "error": "Nenasli sa obce na vyber."}), 400

        options_df = pd.DataFrame(rows)
        options_df["name_norm"] = options_df["name"].map(norm)
        options_df = (
            options_df.groupby(["name_norm", "name"], as_index=False)
            .agg(contracts=("is_energy", "sum"), latest=("rank_date", "max"))
            .sort_values(["contracts", "name"], ascending=[False, True])
        )
        options = []
        for _, r in options_df.iterrows():
            contracts_count = int(r["contracts"]) if pd.notna(r["contracts"]) else 0
            options.append(
                {
                    "name": str(r["name"]),
                    "contracts": contracts_count if contracts_count > 0 else None,
                    "latest": pd.to_datetime(r["latest"]).strftime("%d.%m.%Y") if pd.notna(r["latest"]) else "N/A",
                }
            )
        return jsonify({"ok": True, "district": district, "options": options, "logs": logs[-20:]})
    except Exception as exc:
        manual = MANUAL_DISTRICT_MUNICIPALITIES.get(norm(district), [])
        if manual:
            options = [{"name": x, "contracts": None, "latest": "N/A"} for x in manual]
            return jsonify(
                {
                    "ok": True,
                    "district": district,
                    "options": options,
                    "logs": logs[-20:],
                    "warning": str(exc),
                }
            )
        return jsonify({"ok": False, "error": str(exc), "logs": logs[-20:]}), 400


@app.get("/export/<job_id>.csv")
def export_csv(job_id: str):
    snap = _snapshot(job_id)
    if not snap:
        return "Job sa nenasiel.", 404
    if snap.get("status") != "completed" or not snap.get("result"):
        return "Vysledok este nie je pripraveny.", 400

    rows = (snap["result"] or {}).get("rows") or []
    if not rows:
        return "Ziadne data na export.", 400

    df = pd.DataFrame(rows)
    out = io.BytesIO()
    df.to_csv(out, index=False, encoding="utf-8-sig")
    out.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"enex_koncepty_{snap.get('district','okres')}_{stamp}.csv"
    return send_file(out, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.post("/cancel/<job_id>")
def cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job sa nenasiel."}), 404
        if job["status"] in {"completed", "failed", "cancelled"}:
            return jsonify({"ok": False, "error": "Job je uz ukonceny."}), 400
        job["cancel_requested"] = True
        job["logs"].append(f"{datetime.now().strftime('%H:%M:%S')} Poziadavka na zrusenie prijata.")

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, host="127.0.0.1", port=5055)
