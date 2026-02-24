from __future__ import annotations

import csv
import io
import json
import os
import re
import ssl
import sys
from pathlib import Path
from html import escape
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from flask import Flask, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))



def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


try:
    import redis
except Exception:
    redis = None

from gate_da_answer_key import DA_ANSWER_KEY
from gate_da_marks_calculator import evaluate_exam, parse_response_html_text

app = Flask(__name__)
load_env_file(PROJECT_ROOT / ".env.local")
load_env_file(PROJECT_ROOT / ".env")

KV_REST_API_URL = os.getenv("KV_REST_API_URL", "").strip().rstrip("/")
KV_REST_API_TOKEN = os.getenv("KV_REST_API_TOKEN", "").strip()
USE_VERCEL_KV = bool(KV_REST_API_URL and KV_REST_API_TOKEN)
TELEGRAM_GROUP_URL = os.getenv("TELEGRAM_GROUP_URL", "").strip()

KV_KEY_RANKS = "gate_da:ranks"
KV_KEY_VISITS = "gate_da:visits"


REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_KEY_RANKS = "gate_da:ranks"
REDIS_KEY_VISITS = "gate_da:visits"

redis_client = None
if REDIS_URL and redis is not None:
    try:
        redis_client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
    except Exception:
        redis_client = None

USE_REDIS_URL = redis_client is not None


def safe_float(v: object) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def parse_candidate_meta(html_text: str) -> dict[str, str]:
    def find(label: str) -> str:
        patt = re.compile(rf"{re.escape(label)}\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>", re.I)
        m = patt.search(html_text)
        return m.group(1).strip() if m else ""

    return {
        "candidate_id": find("Candidate ID"),
        "candidate_name": find("Candidate Name"),
        "test_date": find("Test Date"),
        "subject": find("Subject"),
    }


def fetch_html_from_url(response_url: str) -> str:
    parsed = urlparse(response_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Response URL must start with http:// or https://")

    req = Request(
        response_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    def _download(context: ssl.SSLContext | None = None) -> tuple[bytes, str]:
        with urlopen(req, timeout=30, context=context) as resp:
            raw_local = resp.read()
            charset_local = resp.headers.get_content_charset() or "utf-8"
            return raw_local, charset_local

    try:
        raw, charset = _download()
    except ssl.SSLCertVerificationError:
        raw, charset = _download(ssl._create_unverified_context())
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raw, charset = _download(ssl._create_unverified_context())
        else:
            raise RuntimeError(f"Failed to fetch URL: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch URL: {exc}") from exc

    try:
        return raw.decode(charset, errors="ignore")
    except Exception:
        return raw.decode("utf-8", errors="ignore")


def _download_request(req: Request, timeout: int = 20) -> bytes:
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except ssl.SSLCertVerificationError:
        with urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as resp:
            return resp.read()
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            with urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as resp:
                return resp.read()
        raise


def _kv_request(*segments: object) -> object | None:
    if not USE_VERCEL_KV:
        return None

    path = "/".join(quote(str(seg), safe="") for seg in segments)
    req = Request(
        f"{KV_REST_API_URL}/{path}",
        headers={
            "Authorization": f"Bearer {KV_REST_API_TOKEN}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    raw = _download_request(req, timeout=12)
    payload = json.loads(raw.decode("utf-8", errors="ignore"))
    if isinstance(payload, dict):
        return payload.get("result")
    return None


def load_shared_rank_db() -> list[dict[str, object]]:
    if USE_REDIS_URL:
        try:
            mapping = redis_client.hgetall(REDIS_KEY_RANKS) or {}
            rows: list[dict[str, object]] = []
            for cid, marks_raw in mapping.items():
                cid_s = str(cid).strip()
                if not cid_s:
                    continue
                rows.append({"id": cid_s, "marks": safe_float(marks_raw)})
            rows.sort(key=lambda x: safe_float(x.get("marks", 0)), reverse=True)
            return rows
        except Exception:
            return []

    if USE_VERCEL_KV:
        try:
            flat = _kv_request("hgetall", KV_KEY_RANKS)
            rows: list[dict[str, object]] = []
            if isinstance(flat, list):
                for i in range(0, len(flat), 2):
                    cid = str(flat[i]).strip()
                    marks = safe_float(flat[i + 1] if i + 1 < len(flat) else 0)
                    if cid:
                        rows.append({"id": cid, "marks": marks})
                rows.sort(key=lambda x: safe_float(x.get("marks", 0)), reverse=True)
                return rows
        except Exception:
            return []

    return []


def save_shared_rank_db(rows: list[dict[str, object]]) -> None:
    if USE_REDIS_URL:
        pipe = redis_client.pipeline()
        pipe.delete(REDIS_KEY_RANKS)
        for row in rows:
            cid = str(row.get("id", "")).strip()
            if not cid:
                continue
            marks = safe_float(row.get("marks", 0))
            pipe.hset(REDIS_KEY_RANKS, cid, f"{marks:.6f}")
        pipe.execute()
        return

    if USE_VERCEL_KV:
        _kv_request("del", KV_KEY_RANKS)
        for row in rows:
            cid = str(row.get("id", "")).strip()
            if not cid:
                continue
            marks = safe_float(row.get("marks", 0))
            _kv_request("hset", KV_KEY_RANKS, cid, f"{marks:.6f}")
        return

    raise RuntimeError("Storage unavailable: configure REDIS_URL or KV_REST_API_URL/KV_REST_API_TOKEN")


def upsert_shared_rank(candidate_id: str, marks: float) -> list[dict[str, object]]:
    candidate_id = (candidate_id or "").strip()
    if not candidate_id:
        return load_shared_rank_db()

    if USE_REDIS_URL:
        try:
            redis_client.hset(REDIS_KEY_RANKS, candidate_id, f"{safe_float(marks):.6f}")
            return load_shared_rank_db()
        except Exception:
            return load_shared_rank_db()

    if USE_VERCEL_KV:
        try:
            _kv_request("hset", KV_KEY_RANKS, candidate_id, f"{safe_float(marks):.6f}")
            return load_shared_rank_db()
        except Exception:
            return load_shared_rank_db()

    raise RuntimeError("Storage unavailable: configure REDIS_URL or KV_REST_API_URL/KV_REST_API_TOKEN")


def get_and_increment_visit_count() -> int | None:
    if USE_REDIS_URL:
        try:
            return int(redis_client.incr(REDIS_KEY_VISITS))
        except Exception:
            return None

    if USE_VERCEL_KV:
        try:
            val = _kv_request("incr", KV_KEY_VISITS)
            return int(val) if val is not None else None
        except Exception:
            return None

    return None


def build_csv(report: dict[str, object]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Q#", "Section", "Type", "Max", "Your Answer", "Key", "Earned", "Status"])
    for row in report["results"]:
        writer.writerow(
            [
                row["qnum"],
                row["section"],
                row["qtype"],
                row["max_marks"],
                row["your_answer"],
                row["key_answer"],
                f"{safe_float(row['earned']):+.2f}",
                row["status"],
            ]
        )
    return output.getvalue()


def render_page(
    *,
    response_url: str = "",
    error: str = "",
    report: dict[str, object] | None = None,
    meta: dict[str, str] | None = None,
    visit_count: int | None = None,
    rank_rows: list[dict[str, object]] | None = None,
    current_rank: int | None = None,
) -> str:
    summary = report["summary"] if report else {}
    results = report["results"] if report else []

    score = safe_float(summary.get("total_marks", 0.0))
    ga = safe_float(summary.get("ga_marks", 0.0))
    da = safe_float(summary.get("da_marks", 0.0))
    correct = int(summary.get("correct", 0)) if summary else 0
    wrong = int(summary.get("wrong", 0)) if summary else 0
    unanswered = int(summary.get("unanswered", 0)) if summary else 0

    csv_text = escape(build_csv(report) if report else "")

    candidate_id = escape((meta or {}).get("candidate_id", ""))

    rows = []
    for row in results:
        status = str(row["status"])
        cls = "ok" if status == "CORRECT" else ("na" if status == "UNANSWERED" else "bad")
        rows.append(
            "".join(
                [
                    f"<tr class='{cls}' data-section='{row['section']}' data-status='{escape(status)}'>",
                    f"<td>{row['qnum']}</td>",
                    f"<td>{escape(str(row['section']))}</td>",
                    f"<td>{escape(str(row['qtype']))}</td>",
                    f"<td>{row['max_marks']}</td>",
                    f"<td>{escape(str(row['your_answer']))}</td>",
                    f"<td>{escape(str(row['key_answer']))}</td>",
                    f"<td>{safe_float(row['earned']):+.2f}</td>",
                    f"<td>{escape(status)}</td>",
                    "</tr>",
                ]
            )
        )

    rank_rows = rank_rows or []
    rank_html_rows: list[str] = []
    rank_marks: list[float] = []
    for idx, row in enumerate(rank_rows, start=1):
        marks = safe_float(row.get("marks", 0))
        rank_marks.append(marks)
        rank_html_rows.append(f"<tr><td>{marks:.2f}</td><td>{idx}</td></tr>")
    rank_marks_json = escape(json.dumps(rank_marks))

    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
<title>GATE DA 2026 Report</title>
<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\"><link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
<link href=\"https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Libre+Baskerville:wght@400;700&display=swap\" rel=\"stylesheet\">
<style>
:root{{--ink:#102a43;--muted:#486581;--bg:#d6dce3;--panel:#ffffff;--line:#d9e2ec;--good:#0f766e;--bad:#b91c1c;--na:#6b7280;--hero-1:#16a9ad;--hero-2:#75b092;--hero-3:#e9ad57;--shadow:0 12px 30px rgba(16,42,67,.10);--shadow-lg:0 18px 44px rgba(16,42,67,.18)}}
*{{box-sizing:border-box}}
body{{margin:0;color:var(--ink);font-family:'Space Grotesk',sans-serif;background:var(--bg);transition:background .3s ease,color .3s ease}}
.wrap{{max-width:1220px;margin:0 auto;padding:28px 22px 34px}}
.hero{{background:linear-gradient(118deg,var(--hero-1) 0%,var(--hero-2) 54%,var(--hero-3) 100%);color:#fff;border-radius:24px;padding:30px;box-shadow:var(--shadow-lg);position:relative;overflow:hidden}}
.hero::after{{content:'';position:absolute;right:-40px;top:-36px;width:220px;height:220px;background:radial-gradient(circle,rgba(255,255,255,.34),rgba(255,255,255,0));pointer-events:none}}
h1{{margin:0;font-size:clamp(26px,4.2vw,42px);line-height:1.08;letter-spacing:.2px}}
.tag{{margin-top:10px;opacity:.95;font-size:14px;max-width:620px}}
.grid{{display:grid;grid-template-columns:1.1fr 1fr;gap:18px;margin-top:20px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:18px;box-shadow:var(--shadow);backdrop-filter:blur(4px);transition:transform .22s ease,box-shadow .22s ease,border-color .22s ease}}
.card:hover{{transform:translateY(-2px);box-shadow:0 16px 34px rgba(16,42,67,.14);border-color:#c3d4e6}}
h2{{font-size:20px;line-height:1.2;margin:0 0 12px;letter-spacing:.2px}}
label{{display:block;font-size:12px;color:var(--muted);margin-bottom:7px;font-weight:600;text-transform:uppercase;letter-spacing:.55px}}
input[type=text]{{width:100%;border:1px solid var(--line);border-radius:12px;padding:11px 12px;font:inherit;color:var(--ink);background:#fbfdff;transition:border-color .2s ease,box-shadow .2s ease}}
input[type=text]:focus{{border-color:#3aa2c8;box-shadow:0 0 0 3px rgba(56,189,248,.20);outline:none}}
.row{{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}}
.btn{{border:0;border-radius:12px;padding:11px 16px;font-weight:700;cursor:pointer;color:#fff;background:linear-gradient(130deg,#0f766e,#0ea5a5);box-shadow:0 8px 20px rgba(14,165,165,.34);transition:transform .16s ease,box-shadow .16s ease,filter .16s ease}}
.btn:hover{{transform:translateY(-1px);box-shadow:0 12px 24px rgba(14,165,165,.40);filter:saturate(1.05)}}
.msg{{margin-top:10px;padding:10px 12px;border-radius:12px;font-size:13px}}
.err{{background:#fee2e2;color:#991b1b;border:1px solid #fecaca}}
.score{{font-family:'Libre Baskerville',Georgia,serif;font-size:clamp(34px,6vw,58px);margin:4px 0 0;color:#0f766e;line-height:1.02}}
.kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}}
.kpi{{border:1px solid var(--line);border-radius:14px;padding:10px;background:linear-gradient(180deg,#f9fcff,#f5fbff)}}
.kpi .n{{font-size:21px;font-weight:700;line-height:1.15}}
.tools{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}
.pill{{border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 12px;font-size:12px;cursor:pointer;transition:all .18s ease}}
.pill:hover{{border-color:#a9c6df;background:#f7fbff;transform:translateY(-1px)}}
.scroll{{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:14px;background:linear-gradient(180deg,rgba(240,249,255,.65),rgba(255,255,255,.75))}}
.scroll::-webkit-scrollbar{{height:10px;width:10px}}
.scroll::-webkit-scrollbar-thumb{{background:#c7d8e8;border-radius:999px}}
.scroll::-webkit-scrollbar-track{{background:transparent}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left}}
th{{background:#eef7f6;position:sticky;top:0;z-index:1;font-weight:700}}
tr.ok td:last-child{{color:var(--good);font-weight:700}}
tr.bad td:last-child{{color:var(--bad);font-weight:700}}
tr.na td:last-child{{color:var(--na);font-weight:700}}
.theme-toggle{{margin-top:14px;border:1px solid rgba(255,255,255,.58);background:rgba(255,255,255,.16);color:#eaf4ff;border-radius:999px;padding:8px 18px;font:inherit;font-size:12px;cursor:pointer;transition:all .18s ease;box-shadow:none}}
.theme-toggle:hover{{background:rgba(255,255,255,.24);transform:translateY(-1px);border-color:rgba(255,255,255,.78)}}
.support-card{{position:relative;overflow:hidden;background:linear-gradient(135deg,rgba(30,64,175,.14),rgba(20,184,166,.12));border:1px solid rgba(56,189,248,.28)}}
.support-card::after{{content:'';position:absolute;inset:auto -60px -70px auto;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,rgba(45,212,191,.18),rgba(45,212,191,0));pointer-events:none}}
.support-title{{display:flex;align-items:center;gap:10px;margin:0 0 10px;}}.support-copy{{color:var(--muted);font-size:14px;max-width:760px}}.support-actions{{text-align:center;margin-top:12px}}
.support-cta{{display:inline-flex;align-items:center;gap:8px;margin-top:0;padding:11px 16px;border-radius:999px;border:1px solid rgba(45,212,191,.45);background:linear-gradient(135deg,#0ea5a5,#2563eb);color:#f8fafc !important;font-weight:700;text-decoration:none;box-shadow:0 10px 24px rgba(14,165,233,.32);transition:transform .18s ease,box-shadow .18s ease,filter .18s ease}}
.support-cta:hover{{transform:translateY(-1px);box-shadow:0 14px 28px rgba(14,165,233,.42);filter:saturate(1.06)}}
.support-cta .tg-dot{{width:10px;height:10px;border-radius:50%;background:#a7f3d0;box-shadow:0 0 0 3px rgba(167,243,208,.22)}}
.tg-badge{{width:28px;height:28px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#23a6e0,#2563eb);color:#fff;box-shadow:0 6px 14px rgba(37,99,235,.35)}}
.tg-icon{{display:inline-flex;align-items:center;justify-content:center}}
.tg-icon svg{{width:14px;height:14px;fill:currentColor}}
body.dark .support-card{{background:linear-gradient(140deg,rgba(15,23,42,.88),rgba(15,118,110,.18));border-color:rgba(56,189,248,.30)}}
body.dark .support-copy{{color:#b6c3d6}}
@media(max-width:560px){{.support-cta{{width:100%;justify-content:center}}}}
body.dark{{--ink:#e2e8f0;--muted:#94a3b8;--bg:#0b1220;--panel:#111827;--line:#334155;--good:#34d399;--bad:#fb7185;--na:#94a3b8;--hero-1:#0f172a;--hero-2:#0f5b6e;--hero-3:#1e3a8a;--shadow:0 12px 32px rgba(2,6,23,.42);--shadow-lg:0 18px 48px rgba(2,6,23,.5);background:
radial-gradient(1100px 420px at -5% -12%,rgba(8,47,73,.70) 0%,transparent 62%),
radial-gradient(860px 340px at 108% -11%,rgba(30,64,175,.36) 0%,transparent 66%),
linear-gradient(180deg,#090f1c 0%,#0b1324 100%)}}
body.dark .card{{box-shadow:0 14px 30px rgba(2,6,23,.45);border-color:#314155;background:rgba(15,23,42,.9)}}
body.dark .card:hover{{border-color:#475569;box-shadow:0 18px 36px rgba(2,6,23,.58)}}
body.dark .kpi{{background:linear-gradient(180deg,#0f172a,#101a2d)}}
body.dark input[type=text]{{background:#0f172a;color:var(--ink)}}
body.dark input[type=text]:focus{{border-color:#38bdf8;box-shadow:0 0 0 3px rgba(56,189,248,.22)}}
body.dark .pill{{background:#0f172a;color:var(--ink);border-color:#334155}}
body.dark .pill:hover{{background:#132037;border-color:#455d7a}}
body.dark th{{background:#0f172a}}
body.dark .scroll{{background:linear-gradient(180deg,rgba(15,23,42,.55),rgba(15,23,42,.82))}}
body.dark .err{{background:#3f1d1d;color:#fecaca;border-color:#7f1d1d}}
.reveal{{opacity:0;transform:translateY(14px) scale(.995);transition:opacity .45s ease,transform .45s cubic-bezier(.2,.75,.2,1)}}
.reveal.show{{opacity:1;transform:translateY(0) scale(1)}}
.cta-group{{display:flex;gap:10px;justify-content:flex-end;align-items:center;flex-wrap:wrap}}
.btn-ghost{{border:1px solid var(--line);border-radius:12px;padding:11px 14px;background:linear-gradient(180deg,#ffffff,#f6fbff);color:var(--ink);font-weight:700;cursor:pointer;transition:all .16s ease}}
.btn-ghost:hover{{transform:translateY(-1px);border-color:#a7bfd8;background:#f0f8ff}}
.legend-chip{{display:inline-flex;align-items:center;gap:7px;padding:4px 9px;border:1px solid var(--line);border-radius:999px}}
.legend-swatch{{width:12px;height:12px;border-radius:3px;display:inline-block}}
.legend-swatch.line{{height:2px;width:14px;border-radius:999px}}
.legend-swatch.dash{{height:12px;width:2px;border-radius:999px}}
body.dark .btn-ghost{{background:linear-gradient(180deg,#0f172a,#132037);border-color:#3d4f66;color:var(--ink)}}
body.dark .btn-ghost:hover{{border-color:#5d7ea5;background:#17263f}}
.insight-legend{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
.legend-chip{{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid #c4d4e4;border-radius:999px;background:linear-gradient(180deg,#ffffff,#f5f9fd);color:#375a7f;font-weight:600;box-shadow:0 3px 10px rgba(30,64,175,.06)}}
.legend-chip .legend-swatch{{display:inline-block}}
.legend-chip.freq{{border-color:#a7d8d4}}
.legend-chip.trend{{border-color:#a6e5da}}
.legend-chip.p50{{border-color:#f4d39a}}
.legend-chip.mean{{border-color:#a7c5ff}}
.legend-chip.p90{{border-color:#f8b3b3}}
.insight-stats{{margin-top:12px;padding:12px 18px;border:2px solid rgba(116,150,189,.55);border-radius:16px;background:linear-gradient(90deg,#0b2342 0%,#102b4f 45%,#0f2b50 100%);color:#c6d6ea;font-size:15px;font-weight:700;letter-spacing:.15px;box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 6px 16px rgba(9,24,44,.18)}}
.insight-stats-grid{{display:grid;grid-template-columns:repeat(7,minmax(110px,1fr));gap:8px}}
.stat-box{{border:1px solid rgba(142,171,206,.45);border-radius:10px;padding:8px 10px;background:rgba(255,255,255,.05)}}
.stat-k{{display:block;font-size:11px;opacity:.86;font-weight:600;letter-spacing:.35px;text-transform:uppercase}}
.stat-v{{display:block;font-size:18px;line-height:1.1;font-weight:800;margin-top:2px;color:#e5effa}}
body.dark .insight-stats-grid{{grid-template-columns:repeat(7,minmax(110px,1fr))}}
body.dark .stat-box{{border-color:rgba(124,156,194,.36);background:rgba(255,255,255,.03)}}
@media(max-width:1080px){{.insight-stats-grid{{grid-template-columns:repeat(4,minmax(110px,1fr))}}}}
@media(max-width:640px){{.insight-stats-grid{{grid-template-columns:repeat(2,minmax(110px,1fr))}}.stat-v{{font-size:16px}}}}
body.dark .legend-chip{{border-color:#3b5168;background:linear-gradient(180deg,#111c2e,#152338);color:#b9cde1;box-shadow:none}}
body.dark .legend-chip.freq{{border-color:#2d7e79}}
body.dark .legend-chip.trend{{border-color:#2d8a7f}}
body.dark .legend-chip.p50{{border-color:#9b6d22}}
body.dark .legend-chip.mean{{border-color:#355ca8}}
body.dark .legend-chip.p90{{border-color:#9b3b3b}}
body.dark .insight-stats{{border-color:transparent;background:transparent;color:#d1deef;box-shadow:none;padding:0;margin-top:12px}}
@media(max-width:920px){{.grid{{grid-template-columns:1fr}}.kpis{{grid-template-columns:1fr 1fr}}.wrap{{padding:20px 14px 24px}}.hero{{padding:22px}}}}
@media(max-width:560px){{.kpis{{grid-template-columns:1fr}}.btn{{width:100%}}.row{{grid-template-columns:1fr}}}}
</style></head><body><div class=\"wrap\"><section class=\"hero\"><h1>GATE DA 2026 Report</h1><div class=\"tag\">Paste response-sheet link and get full question-wise report instantly.</div><button id=\"theme-toggle\" class=\"theme-toggle\" type=\"button\">Dark Mode</button></section>
<div class=\"grid\"><section class=\"card reveal\"><h2>Input</h2><form method=\"post\"><label>Response Sheet URL</label><input id=\"response-url\" type=\"text\" name=\"response_url\" placeholder=\"https://cdn.digialm.com/.../DA...html\" value=\"{escape(response_url)}\"/><div class=\"row\" style=\"margin-top:10px;\"><div></div><div class=\"cta-group\"><button class=\"btn\" type=\"submit\">Generate Report</button></div></div></form>{f'<div class="msg err">{escape(error)}</div>' if error else ''}</section>
<section class=\"card reveal\"><h2>Summary</h2><div class=\"score\">{score:.2f}</div><div style=\"margin-top:-6px;color:var(--muted);\">out of 100.00</div><div class=\"kpis\"><div class=\"kpi\"><div>GA</div><div class=\"n\">{ga:.2f}</div><div style=\"font-size:12px;color:var(--muted);\">/ 15.00</div></div><div class=\"kpi\"><div>DA</div><div class=\"n\">{da:.2f}</div><div style=\"font-size:12px;color:var(--muted);\">/ 85.00</div></div><div class=\"kpi\"><div>Accuracy</div><div class=\"n\">{(correct / max(1, (correct + wrong)) * 100):.1f}%</div><div style=\"font-size:12px;color:var(--muted);\">attempted only</div></div></div>
<div class=\"kpis\" style=\"margin-top:8px;\"><div class=\"kpi\"><div>Your Rank</div><div class=\"n\">{current_rank if current_rank is not None else '--'}</div></div></div>
<div class=\"kpis\" style=\"margin-top:8px;\"><div class=\"kpi\"><div>Correct</div><div class=\"n\">{correct}</div></div><div class=\"kpi\"><div>Wrong</div><div class=\"n\">{wrong}</div></div><div class=\"kpi\"><div>Unanswered</div><div class=\"n\">{unanswered}</div></div></div></section></div>
{f'<section class="card reveal support-card" style="margin-top:18px;"><h2 class="support-title"><span class="tg-badge tg-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.5 3.5 2.9 10.7c-1.3.5-1.3 1.2-.2 1.6l4.8 1.5 1.9 6c.2.7.1 1 .9 1 .6 0 .9-.3 1.2-.6l2.3-2.2 4.8 3.5c.9.5 1.5.2 1.8-.8l3.4-16.1c.4-1.2-.4-1.8-1.3-1.1Zm-12 9.7 8.8-5.6c.4-.3.8-.1.4.2l-7.5 6.8-.3 3.3-1.4-4.7Z"/></svg></span><span>Counselling Support</span></h2><div class="support-copy">Join our Telegram group for counselling support, strategy discussion, and latest updates.</div><div class="support-actions"><a href="{escape(TELEGRAM_GROUP_URL)}" target="_blank" rel="noopener noreferrer" class="support-cta"><span class="tg-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.5 3.5 2.9 10.7c-1.3.5-1.3 1.2-.2 1.6l4.8 1.5 1.9 6c.2.7.1 1 .9 1 .6 0 .9-.3 1.2-.6l2.3-2.2 4.8 3.5c.9.5 1.5.2 1.8-.8l3.4-16.1c.4-1.2-.4-1.8-1.3-1.1Zm-12 9.7 8.8-5.6c.4-.3.8-.1.4.2l-7.5 6.8-.3 3.3-1.4-4.7Z"/></svg></span><span>Join @gateda_counselling</span></a></div></section>' if TELEGRAM_GROUP_URL else ''}<section class=\"card reveal\" style=\"margin-top:18px;\"><h2>Rank Table (Unique Students)</h2><div style=\"color:var(--muted);font-size:12px;\">Ranked by total marks (global, unique by Candidate ID).</div><div class=\"scroll\" style=\"max-height:220px;\"><table id=\"rank-table\"><thead><tr><th>Marks</th><th>Rank</th></tr></thead><tbody>{''.join(rank_html_rows)}</tbody></table></div></section>
<section class=\"card reveal\" style=\"margin-top:18px;\"><h2>Score Insights</h2><div style=\"color:var(--muted);font-size:12px;\">Distribution of submitted marks with trend, median, mean, and P90 indicators.</div><div style=\"margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(180deg, rgba(14,165,165,0.04), rgba(15,118,110,0.02));\"><canvas id=\"insight-chart\" width=\"960\" height=\"320\" style=\"width:100%;height:320px;display:block\"></canvas></div><div class=\"insight-legend\"><span class=\"legend-chip freq\"><span class=\"legend-swatch\" style=\"width:12px;height:12px;border-radius:4px;background:linear-gradient(180deg,#14b8a6,#0f766e);\"></span>Frequency</span><span class=\"legend-chip trend\"><span class=\"legend-swatch line\" style=\"width:16px;height:3px;border-radius:999px;background:#2dd4bf;\"></span>Trend</span><span class=\"legend-chip p50\"><span class=\"legend-swatch dash\" style=\"width:2px;height:13px;border-radius:999px;background:#f59e0b;\"></span>Median (P50)</span><span class=\"legend-chip mean\"><span class=\"legend-swatch dash\" style=\"width:2px;height:13px;border-radius:999px;background:#2563eb;\"></span>Mean</span><span class=\"legend-chip p90\"><span class=\"legend-swatch dash\" style=\"width:2px;height:13px;border-radius:999px;background:#ef4444;\"></span>P90</span></div><div id=\"insight-summary\" class=\"insight-stats\"></div></section>
<section class=\"card reveal\" style=\"margin-top:18px;\"><h2>Question-wise Report</h2><div class=\"tools\"><button class=\"pill\" onclick=\"filterRows('ALL')\">All</button><button class=\"pill\" onclick=\"filterRows('GA')\">GA</button><button class=\"pill\" onclick=\"filterRows('DA')\">DA</button><button class=\"pill\" onclick=\"statusRows('CORRECT')\">Correct</button><button class=\"pill\" onclick=\"statusRows('WRONG')\">Wrong</button><button class=\"pill\" onclick=\"statusRows('UNANSWERED')\">Unanswered</button><button class=\"pill\" onclick=\"resetRows()\">Reset</button><button class=\"pill\" onclick=\"downloadCsv()\">Download CSV</button></div>
<div class=\"scroll\"><table id=\"report-table\"><thead><tr><th>Q#</th><th>Section</th><th>Type</th><th>Max</th><th>Your Ans</th><th>Key</th><th>Earned</th><th>Status</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div style=\"margin-top:14px;color:var(--muted);font-size:12px;\">Marking scheme: MCQ negative applies; MSQ/NAT no negative, no partial.</div>
<div id=\"visit-counter\" style=\"margin-top:8px;color:var(--muted);font-size:12px;\">Visits: {visit_count if visit_count is not None else '--'}</div></section></div>
<script>const csvData=`{csv_text}`;const rankMarks=JSON.parse("{rank_marks_json}");const themeKey='gate_da_theme';function applyTheme(t){{document.body.classList.toggle('dark',t==='dark');const b=document.getElementById('theme-toggle');if(b)b.textContent=t==='dark'?'Light Mode':'Dark Mode';}}const savedTheme=localStorage.getItem(themeKey)||'dark';applyTheme(savedTheme);const themeBtn=document.getElementById('theme-toggle');if(themeBtn)themeBtn.addEventListener('click',()=>{{const next=document.body.classList.contains('dark')?'light':'dark';localStorage.setItem(themeKey,next);applyTheme(next);setTimeout(drawInsightChart,60);}});function filterRows(s){{document.querySelectorAll('#report-table tbody tr').forEach(tr=>tr.style.display=(s==='ALL'||tr.dataset.section===s)?'':'none')}}function statusRows(p){{document.querySelectorAll('#report-table tbody tr').forEach(tr=>{{const s=tr.dataset.status||'';tr.style.display=s.startsWith(p)?'':'none'}})}}function resetRows(){{document.querySelectorAll('#report-table tbody tr').forEach(tr=>tr.style.display='')}}function downloadCsv(){{if(!csvData)return;const b=new Blob([csvData],{{type:'text/csv;charset=utf-8;'}});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='gate_da_report.csv';a.click();URL.revokeObjectURL(a.href)}}function pct(arr,p){{if(arr.length===1)return arr[0];const k=(arr.length-1)*p,f=Math.floor(k),c=Math.ceil(k);if(f===c)return arr[k];return arr[f]*(c-k)+arr[c]*(k-f);}}function drawInsightChart(){{const cv=document.getElementById('insight-chart');if(!cv||!rankMarks.length)return;const r=cv.getBoundingClientRect();const dpr=window.devicePixelRatio||1;const w=Math.max(320,Math.floor(r.width*dpr));const h=Math.max(220,Math.floor(r.height*dpr));if(cv.width!==w||cv.height!==h){{cv.width=w;cv.height=h;}}const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,h);const dark=document.body.classList.contains('dark');const arr=[...rankMarks].sort((a,b)=>a-b);const min=arr[0],max=arr[arr.length-1];const bins=14;const step=(max-min||1)/bins;const hist=Array.from({{length:bins}},()=>0);arr.forEach(v=>{{let i=Math.floor((v-min)/step);if(i>=bins)i=bins-1;hist[i]++;}});const top=Math.max(...hist,1);const padL=Math.round(56*dpr),padR=Math.round(20*dpr),padT=Math.round(20*dpr),padB=Math.round(42*dpr);const gw=w-padL-padR,gh=h-padT-padB;const axis=dark?'#475569':'#cbd5e1';const grid=dark?'rgba(148,163,184,0.16)':'rgba(100,116,139,0.12)';ctx.strokeStyle=axis;ctx.lineWidth=Math.max(1,Math.round(1*dpr));ctx.beginPath();ctx.moveTo(padL,padT);ctx.lineTo(padL,h-padB);ctx.lineTo(w-padR,h-padB);ctx.stroke();for(let g=1;g<=5;g++){{const y=padT+(gh/5)*g;ctx.strokeStyle=grid;ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(w-padR,y);ctx.stroke();}}const slot=gw/bins,bw=slot*0.72;const pts=[];for(let i=0;i<bins;i++){{const v=hist[i];const bh=(v/top)*(gh-8*dpr);const x=padL+i*slot+(slot-bw)/2;const y=h-padB-bh;const barGrad=ctx.createLinearGradient(0,y,0,h-padB);barGrad.addColorStop(0,dark?'rgba(45,212,191,0.92)':'rgba(15,118,110,0.92)');barGrad.addColorStop(1,dark?'rgba(20,184,166,0.35)':'rgba(20,184,166,0.20)');ctx.fillStyle=barGrad;ctx.fillRect(x,y,bw,bh);pts.push([x+bw/2,y]);}}ctx.lineWidth=Math.max(2,Math.round(2*dpr));ctx.strokeStyle=dark?'#5eead4':'#0f766e';ctx.beginPath();pts.forEach((p,i)=>{{const [x,y]=p;if(i===0)ctx.moveTo(x,y);else{{const [px,py]=pts[i-1];const cx=(px+x)/2;ctx.quadraticCurveTo(px,py,cx,(py+y)/2);ctx.quadraticCurveTo(cx,(py+y)/2,x,y);}}}});ctx.stroke();ctx.lineTo(padL+gw,h-padB);ctx.lineTo(padL,h-padB);ctx.closePath();const area=ctx.createLinearGradient(0,padT,0,h-padB);area.addColorStop(0,dark?'rgba(45,212,191,0.18)':'rgba(15,118,110,0.14)');area.addColorStop(1,'rgba(0,0,0,0)');ctx.fillStyle=area;ctx.fill();const xp=v=>padL+((v-min)/(max-min||1))*gw;const p50=pct(arr,0.5),p90=pct(arr,0.9),mean=arr.reduce((a,b)=>a+b,0)/arr.length,sd=Math.sqrt(arr.reduce((s,v)=>s+(v-mean)*(v-mean),0)/arr.length);const markers=[['P50',p50,'#f59e0b'],['Mean',mean,dark?'#93c5fd':'#2563eb'],['P90',p90,'#ef4444']].sort((a,b)=>a[1]-b[1]);let lastX=-1e9;markers.forEach((m,idx)=>{{const x=xp(m[1]);ctx.setLineDash([5*dpr,4*dpr]);ctx.strokeStyle=m[2];ctx.lineWidth=Math.max(1,Math.round(2*dpr));ctx.beginPath();ctx.moveTo(x,padT);ctx.lineTo(x,h-padB);ctx.stroke();ctx.setLineDash([]);const close=Math.abs(x-lastX)<(42*dpr);const y=padT+(close?(idx+2)*15*dpr:(idx+1)*14*dpr);ctx.fillStyle=m[2];ctx.font=`${{Math.max(11,Math.round(11*dpr))}}px Space Grotesk`;ctx.fillText(m[0],x+4*dpr,y);lastX=x;}});ctx.fillStyle=dark?'#94a3b8':'#486581';ctx.font=`${{Math.max(11,Math.round(11*dpr))}}px Space Grotesk`;ctx.fillText(min.toFixed(1),padL-10*dpr,h-padB+20*dpr);ctx.fillText(max.toFixed(1),w-padR-30*dpr,h-padB+20*dpr);const med=p50;const sx=document.getElementById('insight-summary');if(sx)sx.innerHTML='<div class=\"insight-stats-grid\">'+'<div class=\"stat-box\"><span class=\"stat-k\">Samples</span><span class=\"stat-v\">'+arr.length+'</span></div>'+'<div class=\"stat-box\"><span class=\"stat-k\">Mean</span><span class=\"stat-v\">'+mean.toFixed(2)+'</span></div>'+'<div class=\"stat-box\"><span class=\"stat-k\">SD</span><span class=\"stat-v\">'+sd.toFixed(2)+'</span></div>'+'<div class=\"stat-box\"><span class=\"stat-k\">Median</span><span class=\"stat-v\">'+med.toFixed(2)+'</span></div>'+'<div class=\"stat-box\"><span class=\"stat-k\">Min</span><span class=\"stat-v\">'+min.toFixed(2)+'</span></div>'+'<div class=\"stat-box\"><span class=\"stat-k\">Max</span><span class=\"stat-v\">'+max.toFixed(2)+'</span></div>'+'<div class=\"stat-box\"><span class=\"stat-k\">P90</span><span class=\"stat-v\">'+p90.toFixed(2)+'</span></div>'+'</div>';}}const reveals=document.querySelectorAll('.reveal');for(let i=0;i<reveals.length;i++){{const el=reveals[i];el.style.transitionDelay=(i*70)+'ms';requestAnimationFrame(()=>el.classList.add('show'));}}drawInsightChart();window.addEventListener('resize',()=>setTimeout(drawInsightChart,60));</script>
</body></html>"""


@app.get("/")
def index() -> str:
    return render_page(visit_count=get_and_increment_visit_count(), rank_rows=load_shared_rank_db(), current_rank=None)


@app.post("/")
def evaluate() -> str:
    response_url = request.form.get("response_url", "").strip()
    if not response_url:
        return render_page(
            response_url=response_url,
            error="Please paste response-sheet URL.",
            visit_count=get_and_increment_visit_count(),
            rank_rows=load_shared_rank_db(),
            current_rank=None,
        )

    try:
        response_html = fetch_html_from_url(response_url)
        responses = parse_response_html_text(response_html)
        report = evaluate_exam(DA_ANSWER_KEY, responses)
        meta = parse_candidate_meta(response_html)
        candidate_id = (meta or {}).get("candidate_id", "").strip()
        if candidate_id:
            ranks = upsert_shared_rank(candidate_id, safe_float(report["summary"]["total_marks"]))
            current_rank = next((idx + 1 for idx, row in enumerate(ranks) if str(row.get("id", "")).strip() == candidate_id), None)
        else:
            ranks = load_shared_rank_db()
            current_rank = None
        return render_page(
            response_url=response_url,
            report=report,
            meta=meta,
            visit_count=get_and_increment_visit_count(),
            rank_rows=ranks,
            current_rank=current_rank,
        )
    except Exception as exc:
        return render_page(
            response_url=response_url,
            error=str(exc),
            visit_count=get_and_increment_visit_count(),
            rank_rows=load_shared_rank_db(),
            current_rank=None,
        )

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
