# app.py
# --------------------------------------------------------------------------------------
# WhatsApp roles bot (Flask + Gupshup) - MULTI-CLUB con MENÚS por LISTAS/QR + fallback numérico
#
# Cambios clave:
# - Parser robusto de Gupshup (list_reply/button/quick_reply) → extrae id/postbackText/title/reply.
# - Despacho directo por etiquetas visibles (p. ej., "🛠️ Menú de admin") SIN depender del orden del menú.
# - Coincidencia basada en norm() (sin acentos/emoji) + soporte numérico previo.
# - Menús de "socio", "admin" y "volver" también matchean por etiqueta.
# - IDs de opciones = norm(label) para garantizar retorno estable.
#
# .env mínimo:
#   GUPSHUP_API_KEY=...
#   GUPSHUP_APP_NAME=RolesClubBot
#   GUPSHUP_SOURCE=917834811114
#   CLUBS_DIR=data/clubs
#   VERIFY_TOKEN=rolesclub-verify
#   PORT=5000
# --------------------------------------------------------------------------------------

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Timer
from typing import Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response
from requests.exceptions import RequestException
from weasyprint import HTML

# Modelo POO existente
from models import Club, Member, Role
from session_store import load_session, save_session

# ======================================================================================
# 1) Configuración y logging
# ======================================================================================

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("roles-bot")


@dataclass(frozen=True)
class Config:
    api_key: str
    app_name: str
    source: str
    verify_token: str
    port: int
    clubs_dir: Path


def load_config() -> Config:
    missing: List[str] = []
    api_key = os.getenv("GUPSHUP_API_KEY")
    if not api_key:
        missing.append("GUPSHUP_API_KEY")
    source = os.getenv("GUPSHUP_SOURCE")
    if not source:
        missing.append("GUPSHUP_SOURCE")
    if missing:
        raise RuntimeError(f"Faltan variables: {', '.join(missing)}")

    app_name = os.getenv("GUPSHUP_APP_NAME", "RolesClubBot")
    verify = os.getenv("VERIFY_TOKEN", "rolesclub-verify")
    port = int(os.getenv("PORT", "5000"))
    clubs_dir = Path(os.getenv("CLUBS_DIR", "data/clubs"))
    return Config(
        api_key=api_key,
        app_name=app_name,
        source=source,
        verify_token=verify,
        port=port,
        clubs_dir=clubs_dir,
    )


CFG = load_config()
HEADERS_FORM = {"apikey": CFG.api_key, "Content-Type": "application/x-www-form-urlencoded"}

# ======================================================================================
# 2) Multi-club: registro y contexto por club
# ======================================================================================

REGISTRY_FILE = CFG.clubs_dir / "registry.json"


@dataclass
class Ctx:
    club_id: str
    club: Club
    state_store: "StateStore"
    club_file: Path
    admins: Set[str]
    all_numbers: Tuple[str, ...]
    members_index: Set[str]


def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {"clubs": {}}


def _dump_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def load(self) -> dict:
        with self._lock:
            if not self.path.exists():
                st = {
                    "round": 0,
                    "pending": {},
                    "accepted": {},
                    "members_cycle": {},
                    "last_summary": None,
                    "canceled": False,
                }
                _dump_json_atomic(self.path, st)
                return st
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, st: dict) -> None:
        with self._lock:
            _dump_json_atomic(self.path, st)


_CTX: Dict[str, Ctx] = {}


def load_club_into_registry(club_id: str, meta: dict):
    club_dir = CFG.clubs_dir / club_id
    club_file = club_dir / "club.json"
    state_file = club_dir / "state.json"
    if not club_file.exists():
        raise FileNotFoundError(f"[{club_id}] Falta {club_file}. Corre el semillador.")

    c = Club()
    c.load_from_json(str(club_file))
    st = StateStore(state_file)

    s = st.load()
    mc = s.get("members_cycle", {})
    changed = False
    for m in c.members:
        if m.waid not in mc:
            mc[m.waid] = []
            changed = True
    if changed:
        s["members_cycle"] = mc
    st.save(s)

    admins = set(meta.get("admins", []))
    ctx = Ctx(
        club_id=club_id,
        club=c,
        state_store=st,
        club_file=club_file,
        admins=admins,
        all_numbers=tuple(m.waid for m in c.members),
        members_index={m.waid for m in c.members},
    )
    _CTX[club_id] = ctx
    log.info("Cargado club %s (socios=%d, admins=%d)", club_id, len(ctx.members_index), len(ctx.admins))


def load_all_clubs():
    reg = load_registry()
    _CTX.clear()
    for cid, meta in reg.get("clubs", {}).items():
        load_club_into_registry(cid, meta)


load_all_clubs()


def admin_clubs(waid: str) -> List[str]:
    return [cid for cid, ctx in _CTX.items() if waid in ctx.admins]


def member_clubs(waid: str) -> List[str]:
    return [cid for cid, ctx in _CTX.items() if waid in ctx.members_index]


def member_club(waid: str) -> Optional[str]:
    clubs = member_clubs(waid)
    return clubs[0] if clubs else None

# ======================================================================================
# 3) Utilidades
# ======================================================================================

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def mx_public_from_internal(waid: str) -> str:
    digits = "".join(ch for ch in waid if ch.isdigit())
    if digits.startswith("521") and len(digits) >= 13:
        return digits[-10:]
    return digits


def mx_internal_from_any(s: str) -> str:
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return "521" + digits
    if digits.startswith("521") and len(digits) >= 13:
        return digits
    return digits


def pending_candidates(st: dict, exclude_role: Optional[str] = None) -> Set[str]:
    cands: Set[str] = set()
    for r, info in st.get("pending", {}).items():
        if exclude_role is not None and r == exclude_role:
            continue
        cand = info.get("candidate")
        if cand:
            cands.add(cand)
    return cands


def send_text(to_e164_no_plus: str, text: str) -> dict:
    log.info("→ Enviando texto a %s: %s", mx_public_from_internal(to_e164_no_plus), text[:100])
    url = "https://api.gupshup.io/wa/api/v1/msg"
    data = {
        "channel": "whatsapp",
        "source": CFG.source,
        "destination": to_e164_no_plus,
        "message": text,
        "src.name": CFG.app_name,
    }
    try:
        r = requests.post(url, headers=HEADERS_FORM, data=data, timeout=15)
        if r.ok:
            return r.json()
        log.warning("Gupshup %s: %s", r.status_code, r.text)
        return {"ok": False, "status": r.status_code, "text": r.text}
    except RequestException as e:
        log.exception("Error al llamar Gupshup")
        return {"ok": False, "error": str(e)}


def broadcast_text(numbers: Set[str] | List[str] | Tuple[str, ...], text: str) -> Dict[str, int]:
    ok = fail = 0
    for n in numbers:
        res = send_text(n, text)
        if res.get("ok") is False or res.get("status_code", 200) >= 400:
            fail += 1
        else:
            ok += 1
    return {"ok": ok, "fail": fail}


def send_document(to_e164_no_plus: str, file_url: str, caption: str = "", filename: str = "") -> dict:
    """Envía un documento PDF u otro archivo via WhatsApp usando Gupshup API."""
    log.info("→ Enviando documento a %s: %s", mx_public_from_internal(to_e164_no_plus), filename or file_url)
    url = "https://api.gupshup.io/wa/api/v1/msg"
    
    # Construir mensaje JSON con formato de documento
    import json
    message_payload = {
        "type": "file",
        "url": file_url,
    }
    if caption:
        message_payload["caption"] = caption
    if filename:
        message_payload["filename"] = filename
    
    data = {
        "channel": "whatsapp",
        "source": CFG.source,
        "destination": to_e164_no_plus,
        "message": json.dumps(message_payload),
        "src.name": CFG.app_name,
    }
    
    try:
        r = requests.post(url, headers=HEADERS_FORM, data=data, timeout=30)
        if r.ok:
            return r.json()
        log.warning("Gupshup documento %s: %s", r.status_code, r.text)
        return {"ok": False, "status": r.status_code, "text": r.text}
    except RequestException as e:
        log.exception("Error al enviar documento via Gupshup")
        return {"ok": False, "error": str(e)}


def _normalize_option(option: str | Tuple[str, str]) -> Tuple[str, str]:
    if isinstance(option, tuple):
        label = option[0]
        description = option[1] if len(option) > 1 else ""
    else:
        label = option
        description = ""
    return label.strip(), description.strip()


def _build_menu_text(title: str, options: List[str | Tuple[str, str]]) -> str:
    title_clean = title.rstrip("\n")
    lines: List[str] = []
    for opt in options:
        label, description = _normalize_option(opt)
        if description:
            lines.append(f"- {label} — {description}")
        else:
            lines.append(f"- {label}")
    if title_clean and lines:
        return title_clean + "\n" + "\n".join(lines)
    if title_clean:
        return title_clean
    return "\n".join(lines)


def matches_option(user_raw: str, option: str | Tuple[str, str]) -> bool:
    label, description = _normalize_option(option)
    u = (user_raw or "").strip()
    if not u:
        return False

    u_norm = norm(u)
    label_norm = norm(label)
    desc_norm = norm(description)

    # 1) Etiqueta exacta
    if u == label or u_norm == label_norm:
        return True

    # 2) Etiqueta + descripción (varios formatos)
    if description:
        combos = [
            f"{label}\n{description}",
            f"{label} — {description}",
            f"{label} - {description}",
            f"{label}  {description}",
        ]
        for combo in combos:
            if u == combo or u_norm == norm(combo):
                return True

        # 3) Prefijo: empieza por la etiqueta y luego salto/guion/emdash/espacios/:
        if u.startswith(label) or u_norm.startswith(label_norm):
            tail = u[len(label):].lstrip()
            if not tail or tail.startswith(("\n", "-", "—", ":", " ")):
                return True

    # 3b) NUEVO: Si la etiqueta empieza con lo que envió el usuario (texto truncado)
    # Ej: usuario envía "🎤 Quiero dar un discurso" y label es "🎤 Quiero dar un discurso preparado"
    if len(u_norm) >= 10 and label_norm.startswith(u_norm):
        return True

    # 4) Fallback numérico
    opt_digit = re.match(r"^\s*(\d+)", label)
    user_digit = re.match(r"^\s*(\d+)", u)
    if opt_digit and user_digit and opt_digit.group(1) == user_digit.group(1):
        return True

    return False


# --- Despacho directo por etiquetas visibles -----------------------------------------
# Precompute sets normalizados para coincidencia exacta sin emojis/acentos.
def _set_norm(labels: List[str]) -> Set[str]:
    return {norm(x) for x in labels}

ROOT_MEMBER_SET = _set_norm(["👤 Menú de socio", "Menú de socio"])
ROOT_ADMIN_SET  = _set_norm(["🛠️ Menú de admin", "Menú de admin"])
ROOT_STATUS_SET = _set_norm(["📌 Mi estado de cargo", "Mi estado de cargo", "Mi estado"])
BACK_SET        = _set_norm(["🔙 Volver", "Volver"])
MEM_ROLE_SET    = _set_norm(["🎯 Mi cargo", "Mi cargo"])
MEM_STATUS_SET  = _set_norm(["📊 Estado de la ronda", "Estado de la ronda"])
MEM_SPEECH_SET  = _set_norm(["🎤 Discurso preparado", "🎤 Dar un discurso preparado", "Quiero dar un discurso preparado", "Discurso preparado"])
MEM_EDUCATION_SET = _set_norm(["📚 Quiero dar una Sección Educativa", "Quiero dar una Sección Educativa", "Sección Educativa", "Dar una educativa"])
MEM_CANCEL_SPEECH_SET = _set_norm(["❌ Cancelar mi discurso", "Cancelar mi discurso", "Cancelar discurso"])
MEM_CANCEL_SECTION_SET = _set_norm(["❌ Cancelar mi educativa", "Cancelar mi educativa", "Cancelar educativa"])
MEM_LEAVE_ROLE_SET = _set_norm(["❌ Dejar mi cargo", "Dejar mi cargo", "Renunciar al cargo asignado"])

def _is_choice(body_raw: str, target_set: Set[str]) -> bool:
    raw = (body_raw or "").strip()
    if not raw:
        return False

    # Consider the full text and common "label + descripción" formats.
    candidates: Set[str] = {norm(raw)}
    first_line = raw.splitlines()[0]
    candidates.add(norm(first_line))
    for sep in (" — ", " - ", ":", "  "):
        if sep in raw:
            candidates.add(norm(raw.split(sep, 1)[0]))
    return any(candidate in target_set for candidate in candidates)


def send_list_menu(
    to_e164_no_plus: str,
    title: str,
    options: List[str | Tuple[str, str]],
    button: str = "Elige una opción",
) -> dict:
    log.info("→ Enviando menú lista a %s: %s (opciones: %d)", mx_public_from_internal(to_e164_no_plus), title[:60], len(options))
    menu_text = _build_menu_text(title, options)
    if not options:
        return send_text(to_e164_no_plus, menu_text)

    title_lines = [line.strip() for line in title.splitlines() if line.strip()]
    body_text = title_lines[0] if title_lines else "Elige una opción"
    button_text = (button or "Elige una opción").strip() or "Elige una opción"
    button_trimmed = button_text[:24]
    msg_id = f"list-{uuid.uuid4().hex[:12]}"

    option_payloads: List[dict] = []
    for opt in options:
        label, description = _normalize_option(opt)
        option_title = label[:24] if label else ""
        entry = {
            "type": "text",
            "title": option_title,
            "id": norm(label)[:32] or uuid.uuid4().hex[:8],  # ← ID estable = norm(label)
            "postbackText": label,
        }
        if description:
            entry["description"] = description[:72]
        option_payloads.append(entry)

    payload = {
        "type": "list",
        "title": button_trimmed,
        "body": body_text,
        "msgid": msg_id,
        "globalButtons": [{"type": "text", "title": button_trimmed}],
        "items": [
            {
                "title": "Opciones",
                "options": option_payloads,
            }
        ],
    }

    data = {
        "channel": "whatsapp",
        "source": CFG.source,
        "destination": to_e164_no_plus,
        "message": json.dumps(payload, ensure_ascii=False),
        "src.name": CFG.app_name,
    }
    url = "https://api.gupshup.io/wa/api/v1/msg"
    try:
        resp = requests.post(url, headers=HEADERS_FORM, data=data, timeout=15)
        if resp.ok:
            log.info("✓ Menú lista enviado correctamente")
            return resp.json()
        log.warning("Gupshup list %s: %s", resp.status_code, resp.text)
    except RequestException:
        log.exception("Error al enviar lista Gupshup")

    return send_text(to_e164_no_plus, menu_text)


# --- Detección/parse de eventos Gupshup ----------------------------------------------
def _is_gupshup_event(data: dict) -> bool:
    return isinstance(data, dict) and isinstance(data.get("payload"), dict) and isinstance(data.get("type"), str)


def _extract_gupshup_text(payload: dict) -> str:
    t = (payload.get("type") or "").lower()

    # Texto plano
    if t == "text":
        txt = payload.get("text")
        return txt.strip() if isinstance(txt, str) else ""

    # Quick/button
    if t in ("quick_reply", "button_postback", "button_reply", "reply", "button"):
        p = payload.get("payload") or {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                return p.strip()
        v = (
            (p.get("postbackText") if isinstance(p, dict) else None)
            or (p.get("reply") if isinstance(p, dict) else None)
            or (p.get("id") if isinstance(p, dict) else None)
            or (p.get("title") if isinstance(p, dict) else None)
            or payload.get("postbackText")
            or payload.get("id")
            or payload.get("title")
            or payload.get("text")
        )
        return v.strip() if isinstance(v, str) else ""

    # List reply
    if t == "list_reply":
        p = payload.get("payload") or {}
        v = p.get("postbackText") or p.get("title") or p.get("id")
        return v.strip() if isinstance(v, str) else ""

    # Interactive (reenviado como tal por pasarela)
    if t == "interactive":
        inter = payload.get("interactive") or {}
        if isinstance(inter, dict):
            lr = inter.get("list_reply")
            if isinstance(lr, dict):
                v = lr.get("postbackText") or lr.get("title") or lr.get("id")
                return v.strip() if isinstance(v, str) else ""
            br = inter.get("button_reply") or inter.get("reply")
            if isinstance(br, dict):
                v = br.get("postbackText") or br.get("title") or br.get("text") or br.get("id")
                return v.strip() if isinstance(v, str) else ""
        return ""

    return ""


def _is_gupshup_interactive(payload: dict) -> bool:
    return (payload.get("type") or "").lower() in ("quick_reply", "button_postback", "button_reply", "list_reply", "interactive")


def send_menu_with_quick_replies(to_e164_no_plus: str, title: str, options: List[str]) -> dict:
    menu_text = _build_menu_text(title, options)
    if not options:
        return send_text(to_e164_no_plus, menu_text)

    payload = {
        "type": "quick_reply",
        "content": {"type": "text", "text": title},
        "options": [],
    }
    for idx, option in enumerate(options, 1):
        opt_text = option.strip()
        m = re.match(r"\s*(\d+)", opt_text)
        postback = m.group(1) if m else str(idx)
        payload["options"].append({
            "type": "text",
            "title": opt_text,
            "postbackText": postback,
            "id": postback,
            "postback": postback,
            "payload": postback,
        })

    data = {
        "channel": "whatsapp",
        "source": CFG.source,
        "destination": to_e164_no_plus,
        "message": json.dumps(payload, ensure_ascii=False),
        "src.name": CFG.app_name,
    }
    url = "https://api.gupshup.io/wa/api/v1/msg"
    try:
        resp = requests.post(url, headers=HEADERS_FORM, data=data, timeout=15)
        if resp.ok:
            return resp.json()
        log.warning("Gupshup quick replies %s: %s", resp.status_code, resp.text)
    except RequestException:
        log.exception("Error al enviar quick replies a Gupshup")

    return send_text(to_e164_no_plus, menu_text)


def pretty_name(ctx: Ctx, waid: str) -> str:
    m = next((m for m in ctx.club.members if m.waid == waid), None)
    return m.name if m else waid


def role_min_level(ctx: Ctx, role_name: str) -> int:
    r = next((r for r in ctx.club.roles if r.name == role_name), None)
    return max(1, int(getattr(r, "difficulty", 1) or 1)) if r else 1


def choose_candidate_hier(ctx: Ctx, role: str, excluded: Set[str]) -> Optional[str]:
    st = ctx.state_store.load()
    min_lvl = role_min_level(ctx, role)

    def lvl(m: Member) -> int:
        return int(getattr(m, "level", 1) or 1)

    def pool(filter_fn, allow_repeat: bool) -> List[str]:
        res = []
        for m in ctx.club.members:
            if m.waid in excluded:
                continue
            if not filter_fn(m):
                continue
            done = set(st["members_cycle"].get(m.waid, []))
            if not allow_repeat and role in done:
                continue
            res.append(m.waid)
        return res

    p = pool(lambda m: lvl(m) >= min_lvl, allow_repeat=False)
    if p:
        return random.choice(p)

    p = pool(lambda m: lvl(m) >= min_lvl, allow_repeat=True)
    if p:
        return random.choice(p)

    for L in range(min_lvl - 1, 0, -1):
        p = pool(lambda m, L=L: lvl(m) == L, allow_repeat=False)
        if p:
            return random.choice(p)
        p = pool(lambda m, L=L: lvl(m) == L, allow_repeat=True)
        if p:
            return random.choice(p)

    return None

# --- Admin helpers --------------------------------------------------------------------

def admin_list_members(ctx: Ctx) -> str:
    if not ctx.club.members:
        return f"No hay socios registrados aún en {ctx.club_id}."
    lines = [f"👥 Socios de {ctx.club_id}"]
    for m in ctx.club.members:
        pub = mx_public_from_internal(m.waid)
        lines.append(f"- {m.name} — {pub}  · nivel {getattr(m, 'level', 1)}")
    admin_labels = []
    for a in ctx.admins:
        member = next((m for m in ctx.club.members if m.waid == a), None)
        if member:
            admin_labels.append(f"{member.name} ({mx_public_from_internal(a)})")
        else:
            admin_labels.append(mx_public_from_internal(a))
    if admin_labels:
        lines.append("")
        lines.append("🔑 Administradores: " + ", ".join(admin_labels))
    return "\n".join(lines)


def _find_member_by_waid_or_name(ctx: Ctx, token: str):
    t_clean = token.strip()
    digits = "".join(ch for ch in t_clean if ch.isdigit())
    if digits:
        target = mx_internal_from_any(digits)
        m = next((m for m in ctx.club.members if m.waid == target), None)
        if m:
            return m
    t_norm = norm(t_clean)
    return next((m for m in ctx.club.members if norm(m.name) == t_norm), None)


def admin_add_member(ctx: Ctx, name: str, raw_number: str, level: int = 1, is_guest: bool = False) -> str:
    name = name.strip()
    waid = mx_internal_from_any(raw_number)
    if not name or not waid:
        return "Formato no válido. Usa: Nombre, 55XXXXXXXX"
    if any(m.waid == waid for m in ctx.club.members):
        return "Ese número ya está registrado en el club."

    new_m = Member(name=name, waid=waid, is_guest=is_guest, level=level)
    ctx.club.members.append(new_m)

    st = ctx.state_store.load()
    st.setdefault("members_cycle", {})[waid] = []
    ctx.state_store.save(st)

    ctx.club.save_to_json(str(ctx.club_file))

    ctx.members_index.add(waid)
    ctx.all_numbers = tuple(m.waid for m in ctx.club.members)

    return f"✅ Listo: {name} agregado a {ctx.club_id} (tel. {mx_public_from_internal(waid)}, nivel {level})."


def admin_remove_member(ctx: Ctx, waid_or_name: str) -> str:
    target = _find_member_by_waid_or_name(ctx, waid_or_name)
    if not target:
        return "No encontré a esa persona. Ingresa 10 dígitos MX o el nombre exacto."

    st = ctx.state_store.load()
    in_pending = any(d.get("candidate") == target.waid and not d.get("accepted") for d in st.get("pending", {}).values())
    in_accepted = any(v.get("waid") == target.waid for v in st.get("accepted", {}).values())
    if in_pending or in_accepted:
        return "No se puede eliminar ahora: tiene un cargo pendiente o aceptado en esta ronda."

    ctx.club.members = [m for m in ctx.club.members if m.waid != target.waid]
    ctx.club.save_to_json(str(ctx.club_file))
    st["members_cycle"].pop(target.waid, None)
    ctx.state_store.save(st)

    if target.waid in ctx.members_index:
        ctx.members_index.remove(target.waid)
    ctx.all_numbers = tuple(m.waid for m in ctx.club.members)

    return f"🗑️ Eliminado de {ctx.club_id}: {target.name} (tel. {mx_public_from_internal(target.waid)})."

# ======================================================================================
# 4) Reglas de la ronda (multi-club)
# ======================================================================================

def start_new_round(ctx: Ctx, by_admin: str) -> str:
    st = ctx.state_store.load()
    if any(not v.get("accepted") for v in st["pending"].values()):
        return "Ya hay invitaciones pendientes. Primero cierra o cancela esa ronda."

    st["round"] += 1
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = False

    roles_sorted = sorted(ctx.club.roles, key=lambda r: int(getattr(r, "difficulty", 1) or 1), reverse=True)

    # Conjunto de candidatos ya invitados en esta ronda (para evitar doble invitación simultánea)
    invited_in_this_batch = set()

    for r in roles_sorted:
        role = r.name
        excluded = set(a["waid"] for a in st["accepted"].values())
        excluded.update(pending_candidates(st))
        # También excluimos a los que ya recibieron invitación en este mismo lote
        excluded.update(invited_in_this_batch)

        cand = choose_candidate_hier(ctx, role, excluded)
        if not cand:
            continue
        
        st["pending"][role] = {
            "candidate": cand,
            "declined_by": [],
            "accepted": False,
            "timestamp": time.time()
        }
        invited_in_this_batch.add(cand)

    ctx.state_store.save(st)

    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] ▶️ Iniciamos la ronda #{st['round']}.")

    for role, info in st["pending"].items():
        cand = info.get("candidate")
        if cand:
            begin_invite_flow(ctx, cand, role, st["round"])
            # Programar temporizadores para este rol
            _schedule_invite_timers(ctx, role, cand, st["round"])

    assigned_roles = set(st["pending"].keys())
    not_assigned = [r.name for r in ctx.club.roles if r.name not in assigned_roles]
    if not_assigned:
        broadcast_text(
            ctx.admins,
            f"[{ctx.club_id}] Algunos roles quedaron sin candidato: {', '.join(not_assigned)}. "
            "Agrega más socios o intenta de nuevo."
        )

    return f"Ronda #{st['round']} iniciada en {ctx.club_id}."


def handle_accept(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info.get("waid") == waid and not info.get("accepted"):
            # Detectar si es una cesión de cargo (from_handoff)
            from_handoff = info.get("from_handoff", False)
            original_waid = info.get("original_waid")
            action_type = info.get("action_type")
            
            info["accepted"] = True
            st["accepted"][role] = {"waid": waid, "name": pretty_name(ctx, waid)}

            done_list = list(st["members_cycle"].get(waid, []))
            if role not in done_list:
                done_list.append(role)
            if len(done_list) >= len(ctx.club.roles):
                done_list = []
            st["members_cycle"][waid] = done_list

            member = next((m for m in ctx.club.members if m.waid == waid), None)
            role_obj = next((r for r in ctx.club.roles if r.name == role), None)
            if member and role_obj:
                member.add_role(role_obj)
                ctx.club.save_to_json(str(ctx.club_file))

            ctx.state_store.save(st)
            
            # Si es una cesión, notificar al solicitante original y continuar su flujo
            if from_handoff and original_waid:
                action_text = "discurso preparado" if action_type == "speech" else "sección educativa"
                send_text(
                    original_waid,
                    f"✅ {pretty_name(ctx, waid)} aceptó tomar el cargo de *{role}*.\n\n"
                    f"Ahora puedes continuar con tu {action_text}."
                )
                
                # Continuar con el flujo del solicitante
                if action_type == "speech":
                    set_session(original_waid, awaiting="speech_step1_pathway", buffer={
                        "waid": original_waid,
                        "club": ctx.club_id,
                        "round": st["round"]
                    })
                    pathways = [
                        "Liderazgo dinámico",
                        "Cautiva con humor",
                        "Asesoría eficaz",
                        "Influencia persuasiva",
                        "Dominio de las presentaciones",
                        "Comunicación visionaria"
                    ]
                    send_list_menu(original_waid, "📚 Selecciona tu Pathway:", pathways, "Seleccionar pathway")
                else:  # section
                    set_session(original_waid, awaiting="section_step1_serie", buffer={
                        "waid": original_waid,
                        "club": ctx.club_id,
                        "round": st["round"]
                    })
                    series = [
                        ("🏆 Serie del mejor orador", "Técnicas de oratoria"),
                        ("🎖️ Club exitoso", "Gestión de clubes"),
                        ("💼 Liderazgo", "Habilidades de liderazgo"),
                        ("💡 Tema libre", "Cualquier tema educativo")
                    ]
                    send_list_menu(original_waid, "📚 Selecciona el tipo de serie educativa:", series, "Seleccionar serie")
            
            check_and_announce_if_complete(ctx)
            return f"✅ Aceptado: {role} por {pretty_name(ctx, waid)}."
    
    # Buscar en formato antiguo (candidate en lugar de waid)
    for role, info in st["pending"].items():
        if info.get("candidate") == waid and not info.get("accepted"):
            info["accepted"] = True
            st["accepted"][role] = {"waid": waid, "name": pretty_name(ctx, waid)}

            done_list = list(st["members_cycle"].get(waid, []))
            if role not in done_list:
                done_list.append(role)
            if len(done_list) >= len(ctx.club.roles):
                done_list = []
            st["members_cycle"][waid] = done_list

            member = next((m for m in ctx.club.members if m.waid == waid), None)
            role_obj = next((r for r in ctx.club.roles if r.name == role), None)
            if member and role_obj:
                member.add_role(role_obj)
                ctx.club.save_to_json(str(ctx.club_file))

            ctx.state_store.save(st)
            check_and_announce_if_complete(ctx)
            return f"✅ Aceptado: {role} por {pretty_name(ctx, waid)}."
    
    return "No hay nada pendiente para aceptar."


def handle_reject(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in list(st["pending"].items()):
        # Verificar tanto "waid" (nuevo formato) como "candidate" (formato antiguo)
        is_pending = (info.get("waid") == waid or info.get("candidate") == waid) and not info.get("accepted")
        
        if is_pending:
            # Detectar si es una cesión de cargo
            from_handoff = info.get("from_handoff", False)
            original_waid = info.get("original_waid")
            action_type = info.get("action_type")
            
            if from_handoff and original_waid:
                # Es una cesión rechazada - el bot debe elegir otro reemplazo
                send_text(waid, f"✅ Entendido. Rechazaste el cargo de *{role}*.")
                
                # Buscar otro reemplazo automáticamente
                role_obj = next((r for r in ctx.club.roles if r.name == role), None)
                role_min_level = role_obj.difficulty if role_obj else 1
                
                # Encontrar candidatos elegibles
                candidates = []
                for member in ctx.club.members:
                    if member.waid == original_waid or member.waid == waid:
                        continue  # Excluir al que cedió y al que rechazó
                    if member.level < role_min_level:
                        continue
                    # Verificar que no tenga rol aceptado
                    has_role = any(a.get("waid") == member.waid for a in st.get("accepted", {}).values())
                    if not has_role:
                        candidates.append(member)
                
                if candidates:
                    # Elegir candidato aleatorio
                    import random
                    replacement = random.choice(candidates)
                    
                    # Actualizar el pendiente
                    info["waid"] = replacement.waid
                    info["nombre"] = replacement.name
                    ctx.state_store.save(st)
                    
                    # Notificar al nuevo candidato
                    action_text = "discurso preparado" if action_type == "speech" else "sección educativa"
                    send_text(
                        replacement.waid,
                        f"📧 {pretty_name(ctx, original_waid)} necesita ceder el cargo de *{role}* porque desea dar un {action_text}.\n\n"
                        f"¿Puedes tomar el cargo de *{role}* para la reunión #{st['round']}?\n\n"
                        f"👉 Ve al Menú de socio ({ctx.club_id}) → «🎯 Mi cargo» para aceptar o rechazar."
                    )
                    
                    # Notificar al solicitante original
                    send_text(
                        original_waid,
                        f"⚠️ {pretty_name(ctx, waid)} no pudo aceptar el cargo.\n\n"
                        f"🤖 El bot seleccionó a *{replacement.name}* como nuevo candidato. Esperando su confirmación..."
                    )
                    
                    return f"↪️ Rechazado por {pretty_name(ctx, waid)}. Bot eligió: {replacement.name}."
                else:
                    # No hay más candidatos
                    del st["pending"][role]
                    ctx.state_store.save(st)
                    
                    action_text = "discurso preparado" if action_type == "speech" else "sección educativa"
                    send_text(
                        original_waid,
                        f"❌ Lamentablemente, no hay más socios disponibles para reemplazarte en el cargo de *{role}*.\n\n"
                        f"No podrás dar tu {action_text} siendo Toastmaster. Contacta al vicepresidente educativo."
                    )
                    
                    # Limpiar sesión del solicitante
                    set_session(original_waid, awaiting=None, buffer=None, mode="root")
                    send_root_menu(original_waid)
                    
                    return "Sin candidatos disponibles para cesión."
            
            # Flujo normal de rechazo (no es cesión)
            info.setdefault("declined_by", []).append(waid)

            excluded = set(info.get("declined_by", []))
            excluded.update(a["waid"] for a in st.get("accepted", {}).values())
            excluded.update(pending_candidates(st, exclude_role=role))

            cand = choose_candidate_hier(ctx, role, excluded)
            if cand:
                # Actualizar con formato adecuado
                if "candidate" in info:
                    info["candidate"] = cand
                else:
                    info["waid"] = cand
                    info["nombre"] = pretty_name(ctx, cand)
                ctx.state_store.save(st)
                begin_invite_flow(ctx, cand, role, st["round"])
                return f"↪️ Rechazado por {pretty_name(ctx, waid)}. Nuevo candidato: {pretty_name(ctx, cand)}."
            else:
                del st["pending"][role]
                ctx.state_store.save(st)
                broadcast_text(ctx.admins, f"[{ctx.club_id}] No hay más opciones para el cargo: {role}.")
                return "Sin candidatos."
    
    return "No hay nada pendiente para rechazar."


def make_summary(ctx: Ctx, st: dict) -> str:
    lines = [f"📋 Reunión #{st['round']} — Resumen de roles"]
    wod = st.get("word_of_the_day")
    if wod:
        wod_nombre = wod.get("nombre") or (wod.get("waid") and pretty_name(ctx, wod["waid"])) or "Desconocido"
        lines.append(f"📖 Palabra del día: {wod.get('palabra', '(sin palabra)')} — {wod_nombre}")
    theme = st.get("session_theme")
    if theme:
        theme_nombre = theme.get("nombre") or (theme.get("waid") and pretty_name(ctx, theme["waid"])) or "Desconocido"
        lines.append(f"📝 Temática de la sesión: {theme.get('topic', '(sin temática)')} — {theme_nombre}.")
    for role in [r.name for r in ctx.club.roles]:
        if role in st["accepted"]:
            w = st["accepted"][role]["waid"]
            lines.append(f"- {role}: {pretty_name(ctx, w)}")
        else:
            lines.append(f"- {role}: por confirmar")
    return "\n".join(lines)


def check_and_announce_if_complete(ctx: Ctx) -> None:
    st = ctx.state_store.load()
    all_ok = all(role in st["accepted"] for role in [r.name for r in ctx.club.roles])
    if not all_ok or st.get("canceled"):
        return
    summary = make_summary(ctx, st)
    if st.get("last_summary") == summary:
        return
    st["last_summary"] = summary
    
    # Programar confirmaciones para el lunes si aún no se han programado
    if not st.get("confirmations_scheduled"):
        _schedule_monday_confirmations(ctx, st["round"])
        st["confirmations_scheduled"] = True
        ctx.state_store.save(st)
        broadcast_text(ctx.admins, f"[{ctx.club_id}] ✅ Todos los cargos aceptados. Confirmaciones programadas para el lunes 3 PM.")
    
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] {summary}")


def who_am_i(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        candidate = info.get("candidate")
        if candidate == waid and not info.get("accepted"):
            title, options, _ = invite_menu_parts(ctx, role, st["round"])
            return _build_menu_text(title, options)
    for role, acc in st["accepted"].items():
        if acc.get("waid") == waid:
            return f"✅ Confirmaste el cargo {role} en la ronda #{st['round']} ({ctx.club_id})."
    return "➖ No tienes roles asignados ni pendientes."


def who_am_i_summary(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        candidate = info.get("candidate")
        if candidate == waid and not info.get("accepted"):
            return (
                f"[{ctx.club_id}] 🔔 Tienes una invitación pendiente: {role} en la ronda #{st['round']} ({ctx.club_id}).\n"
                f"👉 Ve al Menú de socio ({ctx.club_id}) → «🎯 Mi cargo» para aceptar o rechazar."
            )
    for role, acc in st["accepted"].items():
        if acc.get("waid") == waid:
            return f"[{ctx.club_id}] ✅ Confirmaste el cargo {role} en la ronda #{st['round']} ({ctx.club_id})."
    return f"[{ctx.club_id}] ➖ No tienes roles asignados ni pendientes."


def status_text(ctx: Ctx) -> str:
    st = ctx.state_store.load()
    summary_lines = make_summary(ctx, st).splitlines()
    lines = summary_lines + ["", "⏳ Pendientes por confirmar:"]
    any_pending = False
    for role, info in st["pending"].items():
        if not info.get("accepted"):
            any_pending = True
            cand = info.get("candidate")
            if cand:
                lines.append(f"- {role}: propuesto a {pretty_name(ctx, cand)} (rechazos: {len(info.get('declined_by', []))})")
            else:
                lines.append(f"- {role}: sin candidato asignado")
    if not any_pending:
        lines.append("- Ninguno")
    if st.get("canceled"):
        lines.append("\nEstado: Ronda cancelada.")
    return "\n".join(lines)


def cancel_round(ctx: Ctx, by_admin: str) -> str:
    st = ctx.state_store.load()
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = True
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] 🛑 La ronda se canceló.")
    return f"La ronda #{st['round']} fue cancelada."


def reset_all(ctx: Ctx, by_admin: str) -> str:
    st = {
        "round": 0,
        "pending": {},
        "accepted": {},
        "members_cycle": {m.waid: [] for m in ctx.club.members},
        "last_summary": None,
        "canceled": False,
    }
    ctx.state_store.save(st)
    
    # Resetear sesiones de todos los miembros del club
    with SLOCK:
        for waid in ctx.all_numbers:
            if waid in SESSION:
                SESSION[waid] = _default_session()
                save_session(waid, SESSION[waid])
    
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] ♻️ Se reinició el estado del club.")
    return "Estado del club reiniciado."

# ======================================================================================
# 5) Sesiones y menús
# ======================================================================================

SESSION: Dict[str, dict] = {}
SLOCK = Lock()


def _default_session() -> dict:
    return {"mode": "root", "club": None, "awaiting": None, "buffer": None}


def get_session(waid: str) -> dict:
    with SLOCK:
        s = SESSION.get(waid)
        if s:
            return s
        loaded = load_session(waid)
        if loaded:
            SESSION[waid] = loaded
            return loaded
        fresh = _default_session()
        SESSION[waid] = fresh
        save_session(waid, fresh)
        return fresh


def set_session(waid: str, **kwargs) -> None:
    with SLOCK:
        s = SESSION.get(waid)
        if not s:
            s = load_session(waid)
            if not s:
                s = _default_session()
            SESSION[waid] = s
        s.update(kwargs)
        save_session(waid, s)

def _root_menu_parts(waid: str) -> Tuple[str, List[Tuple[str, str]], str]:
    mclubs = member_clubs(waid)
    aclubs = admin_clubs(waid)
    options: List[Tuple[str, str]] = []
    header = "Asistente de asignación de cargos: Menú principal. Elija una opción"
    
    # Para socios: mostrar opciones directamente si tiene un solo club
    if mclubs:
        if len(mclubs) == 1:
            # Un solo club: mostrar opciones de socio directamente
            cid = mclubs[0]
            ctx = _CTX.get(cid)
            if ctx:
                st = ctx.state_store.load()
                # Verificar si tiene cargo confirmado
                has_confirmed_role = any(info.get("waid") == waid for info in st.get("accepted", {}).values())
                # Verificar si tiene discurso registrado
                has_speech = any(s.get("waid") == waid for s in st.get("prepared_speeches", []))
                # Verificar si tiene educativa registrada
                has_section = any(sec.get("waid") == waid for sec in st.get("educational_sections", []))
                
                options.append(("🎯 Mi cargo", "Pendiente o confirmado"))
                options.append(("📊 Estado de la ronda", "Resumen y pendientes"))
                options.append(("🎤 Discurso preparado", "Registrar discurso de pathway"))
                if has_speech:
                    options.append(("❌ Cancelar mi discurso", "Cancelar discurso registrado"))
                options.append(("📚 Dar una educativa", "Registrar sección educativa"))
                if has_section:
                    options.append(("❌ Cancelar mi educativa", "Cancelar sección registrada"))
                if has_confirmed_role:
                    options.append(("❌ Dejar mi cargo", "Renunciar al cargo asignado"))
        else:
            # Múltiples clubes: mostrar selector
            options.append(("👤 Menú de socio", "Elegir club"))
    
    if aclubs:
        desc = f"Club único: {aclubs[0]}" if len(aclubs) == 1 else "Elegir club"
        options.append(("🛠️ Menú de admin", desc))
    
    return header, options, "Menú principal"


def render_root_menu(waid: str) -> str:
    title, options, _ = _root_menu_parts(waid)
    return _build_menu_text(title, options)


def send_root_menu(waid: str) -> dict:
    title, options, button = _root_menu_parts(waid)
    return send_list_menu(waid, title, options, button)


def _member_menu_parts(ctx: Ctx, waid: str) -> Tuple[str, List[Tuple[str, str]], str]:
    title = f"Asistente de asignación de cargos: Menú de socio [{ctx.club_id}]. Elija una opción"
    options: List[Tuple[str, str]] = [
        ("🎯 Mi cargo", "Pendiente o confirmado"),
        ("📊 Estado de la ronda", "Resumen y pendientes"),
        ("🎤 Discurso preparado", "Registrar discurso de pathway"),
        ("📚 Dar una educativa", "Registrar sección educativa"),
    ]
    
    # Verificar si el socio tiene algún cargo confirmado
    st = ctx.state_store.load()
    has_confirmed_role = False
    for role, info in st.get("accepted", {}).items():
        if info.get("waid") == waid:
            has_confirmed_role = True
            break
    
    # Si tiene cargo confirmado, agregar opción para dejarlo
    if has_confirmed_role:
        options.append(("❌ Dejar mi cargo", "Renunciar al cargo asignado"))
    
    options.append(("🔙 Volver", "Regresar al menú principal"))
    return title, options, "Menú de socio"

def render_member_menu(ctx: Ctx, waid: str) -> str:
    title, options, _ = _member_menu_parts(ctx, waid)
    return _build_menu_text(title, options)



def send_member_menu(ctx: Ctx, waid: str) -> dict:
    title, options, button = _member_menu_parts(ctx, waid)
    return send_list_menu(waid, title, options, button)


def member_club_picker_parts(mclubs: List[str]) -> Tuple[str, List[Tuple[str, str]], str]:
    title = "Asistente de asignación de roles: Selecciona club para menú de socio. Elija una opción"
    options: List[Tuple[str, str]] = [(cid, "Seleccionar este club") for cid in mclubs]
    options.append(("🔙 Volver", "Regresar al menú principal"))
    return title, options, "Elegir club"


def render_member_club_picker(mclubs: List[str]) -> str:
    title, options, _ = member_club_picker_parts(mclubs)
    return _build_menu_text(title, options)


def admin_club_picker_parts(aclubs: List[str]) -> Tuple[str, List[Tuple[str, str]], str]:
    title = "Asistente de asignación de roles: Selecciona club para administrar. Elija una opción"
    options: List[Tuple[str, str]] = [(cid, "Seleccionar este club") for cid in aclubs]
    options.append(("🔙 Volver", "Regresar al menú principal"))
    return title, options, "Elegir club"


def render_admin_club_picker(aclubs: List[str]) -> str:
    title, options, _ = admin_club_picker_parts(aclubs)
    return _build_menu_text(title, options)


def _admin_menu_parts(ctx: Ctx) -> Tuple[str, List[Tuple[str, str]], str]:
    title = f"Asistente de asignación de cargos: Menú admin [{ctx.club_id}]. Elija una opción"
    options: List[Tuple[str, str]] = [
        ("▶️ Iniciar ronda", "Proponer candidatos por cargo"),
        ("📊 Ver estado", "Resumen actual y pendientes"),
        ("🛑 Cancelar ronda", "Borrar pendientes y aceptados"),
        ("♻️ Resetear estado", "Reiniciar club a cero"),
        ("👥 Ver socios", "Lista y niveles"),
        ("➕ Agregar socio", "Nombre y teléfono"),
        ("➖ Eliminar socio", "Por nombre o número"),
        ("🔁 Cambiar de club", "Seleccionar otro club"),
        ("🔙 Volver", "Regresar al menú principal"),
    ]
    return title, options, "Menú admin"

def render_admin_menu(ctx: Ctx) -> str:
    title, options, _ = _admin_menu_parts(ctx)
    return _build_menu_text(title, options)


def send_admin_menu(ctx: Ctx, waid: str) -> dict:
    title, options, button = _admin_menu_parts(ctx)
    return send_list_menu(waid, title, options, button)


def _get_role_timeout_hours(role: str) -> float:
    """Retorna el tiempo límite en horas para responder según el rol."""
    role_lower = role.lower()
    if "evaluador gramatical" in role_lower or "toastmasters de la noche" in role_lower or "toastmaster" in role_lower:
        return 4.0
    return 24.0  # 24 horas por defecto para otros roles


def invite_menu_parts(ctx: Ctx, role: str, round_no: int) -> Tuple[str, List[Tuple[str, str]], str]:
    # Obtener temática si existe
    st = ctx.state_store.load()
    theme_info = st.get("session_theme")
    theme_text = ""
    
    role_lower = role.lower()
    # Roles que necesitan conocer la temática
    if "evaluador gramatical" in role_lower or "topic master" in role_lower or "temas improvisados" in role_lower:
        if theme_info and theme_info.get("topic"):
            theme_text = f"\n\n📝 Temática de la sesión: '{theme_info['topic']}'"
        else:
            theme_text = "\n\n⚠️ Aún no se ha definido la temática de la sesión."
    
    # Definir tiempo de respuesta según el rol
    timeout_hours = _get_role_timeout_hours(role)
    time_limit = f"\n⏰ Tienes {int(timeout_hours)} horas para responder."
    
    title = (
        f"🔔 Invitación: {role} en la reunión #{round_no} ({ctx.club_id}).{theme_text}{time_limit}\n"
        "Elija una opción para responder."
    )
    options: List[Tuple[str, str]] = [
        ("✅ Aceptar", "Confirmar cargo"),
        ("❌ Rechazar", "Ceder a otra persona"),
    ]
    return title, options, "Responder invitación"

def invite_text(ctx: Ctx, role: str, round_no: int) -> str:
    title, options, _ = invite_menu_parts(ctx, role, round_no)
    return _build_menu_text(title, options)


def begin_invite_flow(ctx: Ctx, waid: str, role: str, round_no: int) -> None:
    current = get_session(waid)
    prev_mode = current.get("mode") if current else None
    prev_club = current.get("club") if current else None
    set_session(
        waid,
        awaiting="invite_decision",
        buffer={
            "role": role,
            "waid": waid,
            "club": ctx.club_id,
            "round": round_no,
            "prev_mode": prev_mode,
            "prev_club": prev_club,
        },
    )
    title, options, _ = invite_menu_parts(ctx, role, round_no)
    # send_text(waid, invite_text(ctx, role, round_no))  <-- Eliminado para evitar duplicidad visual
    send_menu_with_quick_replies(waid, title, [opt[0] for opt in options])


def send_invite_menu(ctx: Ctx, waid: str, role: str, round_no: int) -> None:
    begin_invite_flow(ctx, waid, role, round_no)


def _schedule_invite_timers(ctx: Ctx, role: str, waid: str, round_no: int) -> None:
    """Programa temporizadores escalonados para recordatorios y auto-rechazo."""
    timeout_hours = _get_role_timeout_hours(role)
    timeout_seconds = timeout_hours * 3600
    
    # Notificaciones escalonadas según el tiempo total
    if timeout_hours >= 4:
        # Para roles con 4+ horas:
        # - Recordatorio a mitad de tiempo (2 horas)
        # - Alerta urgente 30 min antes
        # - Auto-rechazo al vencer
        Timer(2 * 3600, _send_reminder, args=(ctx, role, waid, round_no, "mitad")).start()
        Timer(timeout_seconds - 1800, _send_reminder, args=(ctx, role, waid, round_no, "urgente")).start()
        Timer(timeout_seconds, _auto_reject_invite, args=(ctx, role, waid, round_no)).start()
    else:
        # Para roles con menos tiempo: solo alerta 1 hora antes y auto-rechazo
        if timeout_hours > 1:
            Timer(timeout_seconds - 3600, _send_reminder, args=(ctx, role, waid, round_no, "urgente")).start()
        Timer(timeout_seconds, _auto_reject_invite, args=(ctx, role, waid, round_no)).start()
    
    log.info(f"Temporizadores programados para {role} ({waid}): {timeout_hours}h")


def _send_reminder(ctx: Ctx, role: str, waid: str, round_no: int, reminder_type: str) -> None:
    """Envía recordatorio escalonado si la invitación sigue pendiente."""
    st = ctx.state_store.load()
    
    # Verificar si la invitación sigue pendiente
    if role not in st.get("pending", {}):
        return
    
    info = st["pending"][role]
    if info.get("candidate") != waid or info.get("accepted"):
        return
    
    if reminder_type == "mitad":
        timeout_hours = _get_role_timeout_hours(role)
        remaining = timeout_hours / 2
        msg = f"⏰ Recordatorio: Te quedan {int(remaining)} horas para responder a tu invitación de *{role}* (reunión #{round_no})."
    elif reminder_type == "urgente":
        msg = f"⚠️ URGENTE: Te quedan 30 minutos para responder a tu invitación de *{role}* (reunión #{round_no}). Si no respondes, se asignará a otro socio."
    else:
        msg = f"⏰ Recordatorio: Tienes una invitación pendiente para *{role}* (reunión #{round_no})."
    
    send_text(waid, msg)
    log.info(f"Recordatorio {reminder_type} enviado a {waid} para {role}")


def _auto_reject_invite(ctx: Ctx, role: str, waid: str, round_no: int) -> None:
    """Auto-rechaza invitación vencida y busca nuevo candidato."""
    st = ctx.state_store.load()
    
    # Verificar si la invitación sigue pendiente y sin aceptar
    if role not in st.get("pending", {}):
        return
    
    info = st["pending"][role]
    if info.get("candidate") != waid or info.get("accepted"):
        return
    
    # Marcar como rechazado por timeout
    info["declined_by"] = info.get("declined_by", [])
    if waid not in info["declined_by"]:
        info["declined_by"].append(waid)
    info["candidate"] = None
    info["accepted"] = False
    
    ctx.state_store.save(st)
    
    # Notificar al socio
    send_text(waid, f"⏰ Tiempo agotado: Tu invitación para *{role}* ha expirado y se asignará a otro socio.")
    
    # Buscar nuevo candidato
    excluded = set(a["waid"] for a in st["accepted"].values())
    excluded.update(pending_candidates(st, exclude_role=role))
    excluded.update(info.get("declined_by", []))
    
    new_cand = choose_candidate_hier(ctx, role, excluded)
    
    if new_cand:
        info["candidate"] = new_cand
        info["timestamp"] = time.time()
        ctx.state_store.save(st)
        
        send_text(new_cand, f"🔄 Reasignación: Se te invita a *{role}* (reunión #{round_no}) porque el candidato anterior no respondió a tiempo.")
        begin_invite_flow(ctx, new_cand, role, round_no)
        _schedule_invite_timers(ctx, role, new_cand, round_no)
        
        # Notificar a admins
        broadcast_text(ctx.admins, f"[{ctx.club_id}] Rol *{role}* reasignado a {pretty_name(ctx, new_cand)} por timeout.")
        log.info(f"Rol {role} reasignado a {new_cand} por timeout de {waid}")
    else:
        # No hay más candidatos
        broadcast_text(ctx.admins, f"[{ctx.club_id}] ⚠️ Rol *{role}* sin candidatos disponibles después de timeout.")
        log.warning(f"No hay candidatos para {role} después de timeout de {waid}")


# ======================================================================================
# Sistema de Confirmaciones de Asistencia (Lunes antes de la sesión)
# ======================================================================================

def _schedule_monday_confirmations(ctx: Ctx, round_no: int) -> None:
    """Programa envío de confirmaciones para el lunes 3 PM."""
    from datetime import datetime, timedelta
    
    # Calcular próximo lunes 3 PM
    now = datetime.now()
    days_until_monday = (0 - now.weekday()) % 7  # 0 = Lunes
    if days_until_monday == 0 and now.hour >= 15:
        days_until_monday = 7
    
    next_monday_3pm = now + timedelta(days=days_until_monday)
    next_monday_3pm = next_monday_3pm.replace(hour=15, minute=0, second=0, microsecond=0)
    
    seconds_until_monday = (next_monday_3pm - now).total_seconds()
    
    if seconds_until_monday > 0:
        Timer(seconds_until_monday, _send_monday_confirmations, args=(ctx, round_no)).start()
        log.info(f"Confirmaciones programadas para {next_monday_3pm} (en {seconds_until_monday/3600:.1f} horas)")
    else:
        # Si ya pasó el lunes, enviar inmediatamente (para pruebas)
        log.warning("Ya pasó el lunes 3 PM, enviando confirmaciones inmediatamente")
        _send_monday_confirmations(ctx, round_no)


def _send_monday_confirmations(ctx: Ctx, round_no: int) -> None:
    """Envía mensajes de confirmación a todos los socios con roles aceptados."""
    st = ctx.state_store.load()
    
    if st.get("round") != round_no:
        log.warning(f"Ronda cambió de {round_no} a {st.get('round')}, cancelando confirmaciones")
        return
    
    # Inicializar estructura de confirmaciones
    if "confirmations" not in st:
        st["confirmations"] = {}
    
    confirmed_count = 0
    for role, info in st.get("accepted", {}).items():
        waid = info.get("waid")
        if not waid:
            continue
        
        # Marcar como pendiente de confirmación
        st["confirmations"][waid] = {
            "role": role,
            "confirmed": False,
            "timestamp": time.time()
        }
        
        msg = (
            f"⚠️ *Confirmación de asistencia*\n\n"
            f"Hola {info.get('name', '')}!\n\n"
            f"Tienes asignado el cargo de *{role}* para la sesión de mañana martes.\n\n"
            f"Por favor confirma tu asistencia respondiendo:\n"
            f"*CONFIRMO* o *SI*\n\n"
            f"⏰ Plazo: Hoy antes de las 7:00 PM\n\n"
            f"Si no confirmas, buscaremos un reemplazo."
        )
        send_text(waid, msg)
        confirmed_count += 1
    
    ctx.state_store.save(st)
    
    # Programar verificación a las 7 PM
    _schedule_confirmation_deadline(ctx, round_no)
    
    broadcast_text(ctx.admins, f"[{ctx.club_id}] 📨 Enviadas {confirmed_count} solicitudes de confirmación. Plazo: Hoy 7 PM.")


def _schedule_confirmation_deadline(ctx: Ctx, round_no: int) -> None:
    """Programa verificación de confirmaciones para las 7 PM del lunes."""
    from datetime import datetime, timedelta
    
    now = datetime.now()
    today_7pm = now.replace(hour=19, minute=0, second=0, microsecond=0)
    
    if now >= today_7pm:
        # Si ya pasó, ejecutar en 1 minuto (para pruebas)
        seconds_until_deadline = 60
    else:
        seconds_until_deadline = (today_7pm - now).total_seconds()
    
    Timer(seconds_until_deadline, _process_confirmation_deadline, args=(ctx, round_no)).start()
    log.info(f"Deadline de confirmaciones programado para {today_7pm} (en {seconds_until_deadline/3600:.1f} horas)")


def _process_confirmation_deadline(ctx: Ctx, round_no: int) -> None:
    """Procesa las confirmaciones al llegar el deadline de 7 PM."""
    st = ctx.state_store.load()
    
    if st.get("round") != round_no:
        return
    
    confirmations = st.get("confirmations", {})
    unconfirmed = []
    
    for waid, conf_data in confirmations.items():
        if not conf_data.get("confirmed"):
            role = conf_data.get("role")
            unconfirmed.append((waid, role))
    
    if not unconfirmed:
        # Todos confirmaron, generar PDF
        log.info("Todos confirmaron, generando PDF")
        _generate_and_send_pdf(ctx, round_no)
        return
    
    # Buscar reemplazos para los que no confirmaron
    for waid, role in unconfirmed:
        log.info(f"Buscando reemplazo para {role} (no confirmó: {waid})")
        
        # Buscar nuevo candidato
        excluded = set(a["waid"] for a in st.get("accepted", {}).values())
        excluded.discard(waid)  # Quitar al que no confirmó
        excluded.update(c[0] for c in unconfirmed if c[0] != waid)  # Excluir otros no confirmados
        
        new_cand = choose_candidate_hier(ctx, role, excluded)
        
        if new_cand:
            # Reasignar rol
            st["accepted"][role] = {"waid": new_cand, "name": pretty_name(ctx, new_cand)}
            st["confirmations"][new_cand] = {"role": role, "confirmed": True, "timestamp": time.time()}
            
            send_text(waid, f"❌ No confirmaste a tiempo. El cargo *{role}* fue reasignado.")
            send_text(new_cand, f"🔄 Se te asignó el cargo *{role}* para mañana porque el socio anterior no confirmó. Por favor confirma: CONFIRMO")
            
            broadcast_text(ctx.admins, f"[{ctx.club_id}] Cargo *{role}* reasignado de {pretty_name(ctx, waid)} a {pretty_name(ctx, new_cand)}")
        else:
            # No hay reemplazo, mantener al original
            log.warning(f"No hay reemplazo para {role}, manteniendo a {waid}")
            st["confirmations"][waid]["confirmed"] = True  # Forzar confirmación
            send_text(waid, f"⚠️ No confirmaste, pero no hay reemplazo disponible. Se espera tu asistencia para *{role}*.")
            broadcast_text(ctx.admins, f"[{ctx.club_id}] ⚠️ {pretty_name(ctx, waid)} no confirmó *{role}*, pero no hay reemplazo. Se mantiene asignado.")
    
    ctx.state_store.save(st)
    
    # Verificar si ahora todos están confirmados
    all_confirmed = all(c.get("confirmed") for c in st.get("confirmations", {}).values())
    if all_confirmed:
        _generate_and_send_pdf(ctx, round_no)


def _generate_and_send_pdf(ctx: Ctx, round_no: int) -> None:
    """Genera el PDF del programa y lo envía al Toastmaster de la noche."""
    try:
        from programa_generator import generate_program_pdf
        
        st = ctx.state_store.load()
        pdf_path = generate_program_pdf(ctx, st, round_no)
        
        # Encontrar al Toastmaster de la noche
        toastmaster_waid = None
        toastmaster_name = ""
        for role, info in st.get("accepted", {}).items():
            if "toastmaster" in role.lower():
                toastmaster_waid = info.get("waid")
                toastmaster_name = info.get("name", "")
                break
        
        if toastmaster_waid:
            # NOTA: Para enviar el PDF, Gupshup requiere una URL pública.
            # Opciones:
            # 1. Subir a un servidor web/S3/cloud storage y obtener URL
            # 2. Usar ngrok u otro túnel para servir archivos localmente
            # 3. Por ahora, notificamos que está listo y los admins pueden descargarlo
            
            # Notificar al Toastmaster
            msg = (
                f"📋 *Programa de la sesión #{round_no}*\n\n"
                f"¡Hola {toastmaster_name}! El programa de la sesión está listo.\n\n"
                f"Los administradores te harán llegar el documento.\n\n"
                f"¡Nos vemos mañana! 🎉"
            )
            send_text(toastmaster_waid, msg)
            
            # Notificar a admins con la ruta del archivo
            admin_msg = (
                f"[{ctx.club_id}] ✅ *PDF generado exitosamente*\n\n"
                f"📄 Archivo: `{pdf_path.name}`\n"
                f"📁 Ruta: `{pdf_path}`\n"
                f"👤 Toastmaster: {toastmaster_name} ({mx_public_from_internal(toastmaster_waid)})\n\n"
                f"💡 Para enviar automáticamente por WhatsApp, configura una URL pública del PDF."
            )
            broadcast_text(ctx.admins, admin_msg)
            
            log.info(f"PDF generado: {pdf_path}")
        else:
            broadcast_text(ctx.admins, f"[{ctx.club_id}] ⚠️ PDF generado ({pdf_path.name}) pero no se encontró Toastmaster para enviarlo.")
    
    except Exception as e:
        log.exception("Error generando PDF")
        broadcast_text(ctx.admins, f"[{ctx.club_id}] ❌ Error generando PDF: {str(e)}")


def _resume_after_invite(waid: str, buffer: dict, default_mode: str = "root") -> None:
    prev_mode = buffer.get("prev_mode")
    prev_club = buffer.get("prev_club") or buffer.get("club")
    target_mode = prev_mode or default_mode
    session_updates: dict = {"awaiting": None, "buffer": None, "mode": target_mode}
    if prev_club:
        session_updates["club"] = prev_club
    set_session(waid, **session_updates)

    ctx = _CTX.get(prev_club) if prev_club else None
    if target_mode == "admin" and ctx:
        send_admin_menu(ctx, waid)
    elif target_mode == "member" and ctx:
        send_member_menu(ctx, waid)
    else:
        send_root_menu(waid)


def _theme_confirm_summary(buffer: dict) -> str:
    return (
        f"📝 Temática de la sesión: {buffer['topic']}\n\n"
        f"Elige:\n"
        f"1) 💾 Guardar\n"
        f"2) ✏️ Editar temática"
    )


def _send_theme_confirm_prompt(waid: str, buffer: dict) -> None:
    summary = _theme_confirm_summary(buffer)
    send_text(waid, summary)
    send_menu_with_quick_replies(waid, "Elige una opción", ["💾 Guardar", "✏️ Editar temática"])


def _word_confirm_summary(buffer: dict) -> str:
    return (
        f"📋 Resumen de Palabra del Día\n\n"
        f"📖 Palabra: {buffer['palabra']}\n\n"
        f"✍️ Significado: {buffer['significado']}\n\n"
        f"💡 Ejemplo: {buffer['ejemplo']}\n\n"
        f"Elige:\n"
        f"1) 💾 Guardar\n"
        f"2) ✏️ Editar palabra\n"
        f"3) ✏️ Editar significado\n"
        f"4) ✏️ Editar ejemplo"
    )


def _send_word_confirm_menu(waid: str, buffer: dict) -> None:
    summary = _word_confirm_summary(buffer)
    send_text(waid, summary)
    send_list_menu(
        waid,
        "📋 Palabra del Día",
        ["💾 Guardar", "✏️ Editar palabra", "✏️ Editar significado", "✏️ Editar ejemplo"],
        button="Elige una opción",
    )


# ======================================================================================
# 5.0 Gestión de Discursos Preparados y Secciones Educativas
# ======================================================================================

def _get_speeches_and_sections(st: dict) -> Tuple[list, list]:
    """Retorna (lista_discursos, lista_secciones) del state actual."""
    speeches = st.get("prepared_speeches", [])
    sections = st.get("educational_sections", [])
    return speeches, sections


def _extract_duration_numbers(text: str) -> Tuple[int, int]:
    """
    Extrae dos números de un texto y retorna (min, max) ordenados.
    Ejemplos válidos:
    - "5-7" -> (5, 7)
    - "de 5 a 7 minutos" -> (5, 7)
    - "máximo 7 y mínimo 5" -> (5, 7)
    - "entre 5 y 7" -> (5, 7)
    
    Retorna (None, None) si no encuentra exactamente 2 números.
    """
    import re
    # Extraer todos los números del texto
    numbers = re.findall(r'\d+', text)
    
    if len(numbers) != 2:
        return None, None
    
    try:
        num1 = int(numbers[0])
        num2 = int(numbers[1])
        
        # Validar que sean positivos
        if num1 <= 0 or num2 <= 0:
            return None, None
        
        # Retornar ordenados (menor primero)
        return (min(num1, num2), max(num1, num2))
    except (ValueError, IndexError):
        return None, None


def _is_toastmaster(st: dict, waid: str) -> bool:
    """Verifica si el socio tiene el rol de Toastmaster de la noche."""
    for role, info in st.get("accepted", {}).items():
        if "toastmaster" in role.lower() and info.get("waid") == waid:
            return True
    return False


def _get_max_level_members(ctx: Ctx, exclude_waid: str = None) -> List[Tuple[str, str]]:
    """Retorna lista de socios de nivel máximo como tuplas (nombre, waid) excluyendo el socio especificado."""
    if not ctx.club.members:
        return []
    
    max_level = max(m.level for m in ctx.club.members)
    candidates = [
        (m.name, m.waid) 
        for m in ctx.club.members 
        if m.level == max_level and m.waid != exclude_waid
    ]
    return candidates


def _can_add_speech(st: dict, waid: str) -> Tuple[bool, str]:
    """
    Valida si se puede agregar un discurso preparado.
    Retorna (puede_agregar, mensaje_error)
    Usa 'TOASTMASTER_CONFLICT' como código especial para indicar conflicto con rol de Toastmaster.
    """
    speeches, sections = _get_speeches_and_sections(st)
    
    # 1. Verificar si el socio es Toastmaster de la noche (incompatible)
    if _is_toastmaster(st, waid):
        return False, "TOASTMASTER_CONFLICT"
    
    # 2. Verificar si el socio ya tiene discurso
    if any(s.get("waid") == waid for s in speeches):
        return False, "❌ Ya tienes un discurso preparado registrado para esta sesión."
    
    # 3. Verificar si el socio ya tiene sección educativa
    if any(s.get("waid") == waid for s in sections):
        return False, "❌ No puedes dar un discurso preparado si ya tienes una sección educativa."
    
    # 4. Verificar límite de 3 discursos
    if len(speeches) >= 3:
        return False, "❌ Ya se alcanzó el límite de 3 discursos preparados para esta sesión."
    
    # 5. Verificar incompatibilidad: si hay sección educativa, solo 1 discurso
    if len(sections) > 0 and len(speeches) >= 1:
        return False, "❌ Solo se permite 1 discurso cuando hay una sección educativa en la sesión."
    
    return True, ""


def _can_add_section(st: dict, waid: str) -> Tuple[bool, str]:
    """
    Valida si se puede agregar una sección educativa.
    Retorna (puede_agregar, mensaje_error)
    Usa 'TOASTMASTER_CONFLICT' como código especial para indicar conflicto con rol de Toastmaster.
    """
    speeches, sections = _get_speeches_and_sections(st)
    
    # 1. Verificar si el socio es Toastmaster de la noche (incompatible)
    if _is_toastmaster(st, waid):
        return False, "TOASTMASTER_CONFLICT"
    
    # 2. Verificar si el socio ya tiene sección educativa
    if any(s.get("waid") == waid for s in sections):
        return False, "❌ Ya tienes una sección educativa registrada para esta sesión."
    
    # 3. Verificar si el socio ya tiene discurso
    if any(s.get("waid") == waid for s in speeches):
        return False, "❌ No puedes dar una sección educativa si ya tienes un discurso preparado."
    
    # 4. Solo puede haber 1 sección educativa
    if len(sections) >= 1:
        return False, "❌ Ya hay una sección educativa registrada para esta sesión."
    
    # 5. Verificar incompatibilidad: si hay 2+ discursos, no se permite sección
    if len(speeches) >= 2:
        return False, "❌ No se permite sección educativa cuando hay 2 o más discursos preparados."
    
    return True, ""


def _find_toastmaster_replacement(ctx: Ctx, exclude_waid: str) -> Optional[str]:
    """
    Busca un socio disponible para reemplazar al Toastmaster de la noche.
    Retorna waid del candidato o None si no hay disponibles.
    """
    st = ctx.state_store.load()
    
    # Construir set de socios ya asignados (excepto el Toastmaster actual)
    assigned = set()
    for role, info in st.get("accepted", {}).items():
        waid = info.get("waid")
        if waid and waid != exclude_waid and "toastmaster" not in role.lower():
            assigned.add(waid)
    
    # Buscar candidatos: nivel suficiente y no asignados
    toastmaster_role = next((r for r in ctx.club.roles if "toastmaster" in r.name.lower()), None)
    if not toastmaster_role:
        return None
    
    min_level = toastmaster_role.difficulty
    candidates = []
    
    for m in ctx.club.members:
        if m.waid == exclude_waid or m.waid in assigned:
            continue
        if m.level >= min_level:
            candidates.append(m.waid)
    
    return random.choice(candidates) if candidates else None


def _revoke_incompatible_roles(ctx: Ctx, waid: str) -> List[str]:
    """
    Revoca roles incompatibles (Toastmaster, Evaluador gramatical) si el socio
    registra discurso o sección educativa. Retorna lista de roles revocados.
    """
    incompatible_roles = ["Toastmasters de la noche", "Evaluador gramatical"]
    revoked = []
    
    st = ctx.state_store.load()
    
    # Verificar en roles aceptados
    for role in incompatible_roles:
        if role in st.get("accepted", {}) and st["accepted"][role].get("waid") == waid:
            del st["accepted"][role]
            revoked.append(role)
            log.info(f"Revocado rol aceptado '{role}' de {waid} por incompatibilidad con discurso/sección")
    
    # Verificar en roles pendientes
    for role in incompatible_roles:
        if role in st.get("pending", {}) and st["pending"][role].get("candidate") == waid:
            # Marcar como rechazado automáticamente
            st["pending"][role]["declined_by"] = st["pending"][role].get("declined_by", [])
            if waid not in st["pending"][role]["declined_by"]:
                st["pending"][role]["declined_by"].append(waid)
            st["pending"][role]["candidate"] = None
            st["pending"][role]["accepted"] = False
            revoked.append(role)
            log.info(f"Revocado rol pendiente '{role}' de {waid} por incompatibilidad con discurso/sección")
    
    if revoked:
        ctx.state_store.save(st)
    
    return revoked


def _check_evaluator_timeouts():
    """
    Verifica si hay solicitudes de evaluador que excedieron las 2 horas sin respuesta.
    Notifica al solicitante y limpia la sesión del evaluador.
    """
    import time
    current_time = int(time.time())
    timeout_seconds = 2 * 60 * 60  # 2 horas
    
    for club_id, ctx in _CTX.items():
        st = ctx.state_store.load()
        speeches_updated = False
        
        for speech in st.get("prepared_speeches", []):
            if speech.get("status") != "pendiente_evaluador":
                continue
            
            solicitud_time = speech.get("solicitud_timestamp", 0)
            if current_time - solicitud_time > timeout_seconds:
                # Timeout excedido
                solicitante_waid = speech.get("waid")
                evaluador_waid = speech.get("evaluador_waid")
                evaluador_nombre = speech.get("evaluador")
                
                # Eliminar el discurso
                st["prepared_speeches"] = [
                    s for s in st.get("prepared_speeches", [])
                    if s.get("id") != speech.get("id")
                ]
                speeches_updated = True
                
                # Notificar al solicitante
                if solicitante_waid:
                    msg = (
                        f"⏰ {evaluador_nombre} no respondió en las 2 horas.\n\n"
                        "Por favor, elige otro evaluador desde el menú de miembro."
                    )
                    send_text(solicitante_waid, msg)
                
                # Limpiar sesión del evaluador si aún está esperando
                if evaluador_waid:
                    with SLOCK:
                        eval_session = SESSION.get(evaluador_waid, {})
                        if eval_session.get("awaiting") == "evaluator_response":
                            buffer = eval_session.get("buffer", {})
                            if buffer.get("speech_id") == speech.get("id"):
                                SESSION[evaluador_waid] = _default_session()
                                save_session(evaluador_waid, SESSION[evaluador_waid])
                
                log.info(f"Timeout de solicitud de evaluador para speech_id={speech.get('id')}")
        
        if speeches_updated:
            ctx.state_store.save(st)


# ======================================================================================
# 5.1 Router MENÚS con despacho por etiqueta
# ======================================================================================

def _process_message_router(
    waid: str,
    body_raw: str,
    body_norm: str,
    is_interactive: bool,
    msg_type: str,
) -> Optional[Response]:
    s = get_session(waid)
    body_raw_clean = body_raw.strip()
    log.info("Mensaje de %s: %s", waid, body_norm)

    is_number = re.fullmatch(r"\d{1,3}", body_norm) is not None
    log.info(
        "📋 Sesión actual: mode=%s, awaiting=%s, club=%s",
        s.get("mode"),
        s.get("awaiting"),
        s.get("club"),
    )

    if body_norm == "home":
        set_session(waid, awaiting=None, buffer=None, mode="root")
        send_root_menu(waid)
        return jsonify({"status": "ok"})

    # Comando RESET rápido para admins (interrumpe cualquier flujo y resetea el club)
    if body_norm == "reset":
        acls = admin_clubs(waid)
        if not acls:
            send_text(waid, "❌ No tienes permisos de administrador.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return jsonify({"status": "ok"})
        
        # Limpiar sesión del usuario (interrumpe cualquier flujo)
        set_session(waid, awaiting=None, buffer=None, mode="root", club=None)
        
        # Resetear club(s)
        if len(acls) == 1:
            reset_ctx = _CTX[acls[0]]
            reset_all(reset_ctx, pretty_name(reset_ctx, waid))
            send_text(waid, f"♻️ Club '{acls[0]}' reiniciado. Todos los roles y estados fueron limpiados.")
        else:
            for club_id in acls:
                reset_ctx = _CTX[club_id]
                reset_all(reset_ctx, pretty_name(reset_ctx, waid))
            send_text(waid, f"♻️ {len(acls)} clubes reiniciados: {', '.join(acls)}")
        
        send_root_menu(waid)
        return jsonify({"status": "ok"})

    # Si llega sólo el título del listado ("Menú principal") ignóralo y vuelve a pintar
    if _is_choice(body_raw_clean, _set_norm(["Menú principal"])):
        log.info("Usuario hizo clic en botón 'Menú principal' - reenviando menú")
        send_root_menu(waid)
        return jsonify({"status": "ok"})

    # Si llega sólo el título del botón ("Menú de socio") ignoralo y vuelve a pintar
    if _is_choice(body_raw_clean, _set_norm(["Menú de socio"])):
        log.info("Usuario hizo clic en botón 'Menú de socio' - cambiando a modo member")
        current_cid_temp = s.get("club") or infer_user_club(waid)
        if current_cid_temp and current_cid_temp in _CTX:
            set_session(waid, mode="member", club=current_cid_temp, awaiting=None)
            send_member_menu(_CTX[current_cid_temp], waid)
        else:
            send_root_menu(waid)
        return jsonify({"status": "ok"})

    # Si llega sólo el título del botón ("Menú de admin") ignoralo y vuelve a pintar
    if _is_choice(body_raw_clean, _set_norm(["Menú de admin"])):
        log.info("Usuario hizo clic en botón 'Menú de admin' - cambiando a modo admin")
        aclubs_temp = admin_clubs(waid)
        if not aclubs_temp:
            send_text(waid, "❌ No tienes permisos de administrador.")
            send_root_menu(waid)
            return jsonify({"status": "ok"})
        current_cid_temp = s.get("club") or infer_user_club(waid)
        if current_cid_temp and current_cid_temp in _CTX and current_cid_temp in aclubs_temp:
            set_session(waid, mode="admin", club=current_cid_temp, awaiting=None)
            send_admin_menu(_CTX[current_cid_temp], waid)
        else:
            # Si es admin pero de otro club o múltiples clubs
            if len(aclubs_temp) == 1:
                set_session(waid, mode="admin", club=aclubs_temp[0], awaiting=None)
                send_admin_menu(_CTX[aclubs_temp[0]], waid)
            else:
                send_root_menu(waid)
        return jsonify({"status": "ok"})

    if not s.get("club"):
        mclubs = member_clubs(waid)
        acls = admin_clubs(waid)
        if len(mclubs) == 1:
            set_session(waid, club=mclubs[0])
        elif len(acls) == 1:
            set_session(waid, club=acls[0])

    current_cid = s.get("club") or infer_user_club(waid, extract_trailing_club_id(body_raw))
    ctx = _CTX[current_cid] if current_cid and current_cid in _CTX else None

    awaiting = s.get("awaiting")

    # --------- Flujos de invitación (persisten sobre cualquier menú) -------------------
    if awaiting == "invite_decision":
        accept_option = ("✅ Aceptar", "Confirmar cargo")
        reject_option = ("❌ Rechazar", "Ceder a otra persona")

        wants_accept = matches_option(body_raw_clean, accept_option) or body_norm in ("1", "acepto", "aceptar", "accept", "si", "sí", "ok")
        wants_reject = matches_option(body_raw_clean, reject_option) or body_norm in ("2", "rechazo", "rechazar", "reject", "no", "cancelar", "cancelo")

        if wants_accept:
            buffer = s.get("buffer", {})
            club_ctx = _CTX[buffer["club"]]
            role_name = buffer["role"]
            accept_msg = handle_accept(club_ctx, waid)
            send_text(waid, accept_msg)

            role_norm = role_name.lower()
            st_now = club_ctx.state_store.load()
            if "evaluador gramatical" in role_norm:
                set_session(
                    waid,
                    awaiting="word_step1_palabra",
                    buffer={"role": role_name, "waid": waid, "club": club_ctx.club_id, "round": st_now["round"]},
                )
                send_text(waid, 
                    "📖 *Envía la palabra del día:*\n\n"
                    "_💡 Tip: No te preocupes si cometes algún error, después tendrás la oportunidad de revisar y corregir toda la información antes de guardarla._"
                )
            elif "toastmaster" in role_norm or "toastmasters de la noche" in role_norm:
                set_session(
                    waid,
                    awaiting="theme_step1_topic",
                    buffer={"role": role_name, "waid": waid, "club": club_ctx.club_id, "round": st_now["round"]},
                )
                send_text(waid, "📝 Envía la temática de la sesión:")
            else:
                _resume_after_invite(waid, buffer)
            return jsonify({"status": "ok"})

        if wants_reject:
            buffer = s.get("buffer", {})
            club_ctx = _CTX[buffer["club"]]
            reject_msg = handle_reject(club_ctx, waid)
            send_text(waid, reject_msg)
            _resume_after_invite(waid, buffer)
            return jsonify({"status": "ok"})

        if is_interactive:
            buffer = s.get("buffer", {})
            club_ctx = _CTX.get(buffer.get("club"))
            if club_ctx:
                send_text(waid, "_❗ Opción inválida. Por favor, usa los botones:_ *✅ Aceptar* / *❌ Rechazar*")
                title, opts, _ = invite_menu_parts(club_ctx, buffer["role"], buffer["round"])
                send_menu_with_quick_replies(waid, title, [opt[0] for opt in opts])
            return jsonify({"status": "ok"})

    # --------- Flujos admin de agregar/eliminar miembros --------------------------------
    if awaiting == "admin_add_member" and s.get("mode") == "admin" and ctx:
        tail = body_raw.strip()
        if "," in tail:
            name, num = tail.split(",", 1)
        else:
            parts = tail.rsplit(" ", 1)
            if len(parts) != 2:
                send_text(waid, "Formato no válido. Usa: Nombre, 55XXXXXXXX")
                return None
            name, num = parts[0], parts[1]
        out = admin_add_member(ctx, name.strip(), num.strip())
        send_text(waid, out)
        set_session(waid, awaiting=None, buffer=None)
        send_admin_menu(ctx, waid)
        return None

    if awaiting == "admin_remove_member" and s.get("mode") == "admin" and ctx:
        tail = body_raw.strip()
        out = admin_remove_member(ctx, tail)
        send_text(waid, out)
        set_session(waid, awaiting=None, buffer=None)
        send_admin_menu(ctx, waid)
        return None

    # --------- Flujos Palabra del día / Temática ---------------------------------------
    if awaiting == "word_step1_palabra":
        if is_interactive:
            send_text(waid, "Escribe la palabra del día con texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["palabra"] = body_raw.strip()
        set_session(waid, awaiting="word_step2_significado", buffer=buffer)
        send_text(waid, 
            "✍️ *Envía el significado de la palabra:*\n\n"
            "_💡 Recuerda: Podrás revisar y corregir cualquier información antes de guardarla._"
        )
        return None

    if awaiting == "word_step2_significado":
        if is_interactive:
            send_text(waid, "Escribe el significado manualmente, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["significado"] = body_raw.strip()
        set_session(waid, awaiting="word_step3_ejemplo", buffer=buffer)
        send_text(waid, 
            "💡 *Envía un ejemplo de uso de la palabra:*\n\n"
            "_✅ ¡Último paso! En el siguiente mensaje podrás revisar toda la información y corregir lo que necesites._"
        )
        return None

    if awaiting == "word_step3_ejemplo":
        if is_interactive:
            send_text(waid, "Escribe un ejemplo en texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["ejemplo"] = body_raw.strip()
        set_session(waid, awaiting="word_confirm", buffer=buffer)
        _send_word_confirm_menu(waid, buffer)
        return None

    if awaiting == "word_confirm":
        buffer = s.get("buffer", {})
        options = ["💾 Guardar", "✏️ Editar palabra", "✏️ Editar significado", "✏️ Editar ejemplo"]
        wants_save = matches_option(body_raw_clean, options[0]) or body_norm == "1"
        wants_edit_word = matches_option(body_raw_clean, options[1]) or body_norm == "2"
        wants_edit_meaning = matches_option(body_raw_clean, options[2]) or body_norm == "3"
        wants_edit_example = matches_option(body_raw_clean, options[3]) or body_norm == "4"

        if wants_save:
            club_ctx = _CTX[buffer["club"]]
            st = club_ctx.state_store.load()
            st["word_of_the_day"] = {
                "palabra": buffer["palabra"],
                "significado": buffer["significado"],
                "ejemplo": buffer["ejemplo"],
                "waid": buffer["waid"],
                "nombre": pretty_name(club_ctx, buffer["waid"]),
                "round": buffer["round"],
            }
            club_ctx.state_store.save(st)
            send_text(waid, f"✅ Palabra del día guardada: '{buffer['palabra']}'")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        if wants_edit_word:
            set_session(waid, awaiting="word_edit_palabra", buffer=buffer)
            send_text(waid, f"📖 Palabra actual: {buffer['palabra']}\nEnvía la nueva palabra:")
            return None
        if wants_edit_meaning:
            set_session(waid, awaiting="word_edit_significado", buffer=buffer)
            send_text(waid, f"✍️ Significado actual: {buffer['significado']}\nEnvía el nuevo significado:")
            return None
        if wants_edit_example:
            set_session(waid, awaiting="word_edit_ejemplo", buffer=buffer)
            send_text(waid, f"💡 Ejemplo actual: {buffer['ejemplo']}\nEnvía el nuevo ejemplo:")
            return None
        if is_interactive:
            send_text(waid, "❗Opción inválida. Usa la lista.")
            _send_word_confirm_menu(waid, buffer)
        else:
            send_text(waid, "Opción inválida. Envía 1, 2, 3 o 4.")
        return None

    if awaiting == "word_edit_palabra":
        if is_interactive:
            send_text(waid, "Escribe la nueva palabra directamente, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["palabra"] = body_raw.strip()
        set_session(waid, awaiting="word_confirm", buffer=buffer)
        _send_word_confirm_menu(waid, buffer)
        return None

    if awaiting == "word_edit_significado":
        if is_interactive:
            send_text(waid, "Escribe el nuevo significado manualmente, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["significado"] = body_raw.strip()
        set_session(waid, awaiting="word_confirm", buffer=buffer)
        _send_word_confirm_menu(waid, buffer)
        return None

    if awaiting == "word_edit_ejemplo":
        if is_interactive:
            send_text(waid, "Escribe el nuevo ejemplo en texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["ejemplo"] = body_raw.strip()
        set_session(waid, awaiting="word_confirm", buffer=buffer)
        _send_word_confirm_menu(waid, buffer)
        return None

    if awaiting == "theme_step1_topic":
        if is_interactive:
            send_text(waid, "Escribe la temática de la sesión con texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["topic"] = body_raw.strip()
        set_session(waid, awaiting="theme_confirm", buffer=buffer)
        _send_theme_confirm_prompt(waid, buffer)
        return None

    if awaiting == "theme_confirm":
        buffer = s.get("buffer", {})
        options = ["💾 Guardar", "✏️ Editar temática"]
        wants_save = matches_option(body_raw_clean, options[0]) or body_norm == "1"
        wants_edit = matches_option(body_raw_clean, options[1]) or body_norm == "2"

        if wants_save:
            club_ctx = _CTX[buffer["club"]]
            st = club_ctx.state_store.load()
            st["session_theme"] = {
                "topic": buffer["topic"],
                "waid": buffer["waid"],
                "nombre": pretty_name(club_ctx, buffer["waid"]),
                "round": buffer["round"],
            }
            club_ctx.state_store.save(st)
            send_text(waid, f"✅ Temática guardada: '{buffer['topic']}'")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        if wants_edit:
            set_session(waid, awaiting="theme_edit_topic", buffer=buffer)
            send_text(waid, f"📝 Temática actual: {buffer['topic']}\nEnvía la nueva temática:")
            return None
        if is_interactive:
            send_text(waid, "❗Opción inválida. Usa los botones: 💾 Guardar / ✏️ Editar temática.")
            _send_theme_confirm_prompt(waid, buffer)
        else:
            send_text(waid, "Opción inválida. Envía 1 o 2.")
        return None

    if awaiting == "theme_edit_topic":
        if is_interactive:
            send_text(waid, "Escribe la nueva temática con texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["topic"] = body_raw.strip()
        set_session(waid, awaiting="theme_confirm", buffer=buffer)
        _send_theme_confirm_prompt(waid, buffer)
        return None

    # --------- Flujo de cesión de cargo de Toastmaster --------------------------------
    
    if awaiting in ("toastmaster_handoff_speech", "toastmaster_handoff_section"):
        buffer = s.get("buffer", {})
        club_ctx = _CTX[buffer["club"]]
        replacement_waid = buffer.get("replacement")
        action_type = buffer.get("action")  # "speech" o "section"
        
        # Detectar respuesta (aceptar botones interactivos o texto)
        body_clean = body_raw.strip()
        wants_handoff = (
            matches_option(body_clean, "✅ Sí, ceder el cargo para dar mi discurso preparado") or
            matches_option(body_clean, "✅ Sí, ceder el cargo para dar mi sección educativa") or
            matches_option(body_clean, "Transferir rol") or
            body_norm in ("1", "si", "sí", "s", "yes", "aceptar", "ceder")
        )
        wants_keep = (
            matches_option(body_clean, "❌ No, quiero conservarlo, sí quiero ser Toastmaster") or
            matches_option(body_clean, "Conservar rol") or
            body_norm in ("2", "no", "n", "mantener", "cancelar", "conservar")
        )
        
        if wants_handoff:
            # Ceder el cargo de Toastmaster
            st = club_ctx.state_store.load()
            
            # Encontrar el rol de Toastmaster
            toastmaster_role = None
            for role in st.get("accepted", {}).keys():
                if "toastmaster" in role.lower():
                    toastmaster_role = role
                    break
            
            if toastmaster_role and replacement_waid:
                # Remover el rol actual del que cede
                if toastmaster_role in st.get("accepted", {}):
                    del st["accepted"][toastmaster_role]
                
                # Crear pendiente para el reemplazo (necesita confirmación)
                if "pending" not in st:
                    st["pending"] = {}
                st["pending"][toastmaster_role] = {
                    "waid": replacement_waid,
                    "nombre": pretty_name(club_ctx, replacement_waid),
                    "propuesto_por": pretty_name(club_ctx, waid),
                    "timestamp": int(__import__("time").time()),
                    "from_handoff": True,  # Marca especial para saber que viene de cesión
                    "original_waid": waid,  # Para saber quién cedió
                    "action_type": action_type  # "speech" o "section"
                }
                club_ctx.state_store.save(st)
                
                # Enviar solicitud de confirmación al reemplazo
                action_text = "discurso preparado" if action_type == "speech" else "sección educativa"
                send_text(
                    replacement_waid,
                    f"📧 {pretty_name(club_ctx, waid)} necesita ceder el cargo de *{toastmaster_role}* porque desea dar un {action_text}.\n\n"
                    f"¿Puedes tomar el cargo de *{toastmaster_role}* para la reunión #{st['round']}?\n\n"
                    f"👉 Ve al Menú de socio ({club_ctx.club_id}) → «🎯 Mi cargo» para aceptar o rechazar."
                )
                
                # Confirmar al socio original que se envió la solicitud
                send_text(
                    waid,
                    f"✅ Se envió solicitud a {pretty_name(club_ctx, replacement_waid)} para que tome el cargo de *{toastmaster_role}*.\n\n"
                    f"⏳ Esperando su confirmación. Si rechaza, el bot elegirá automáticamente otro reemplazo."
                )
                
                # Poner al solicitante en espera
                set_session(waid, awaiting="waiting_handoff_confirmation", buffer={
                    "waid": waid,
                    "club": club_ctx.club_id,
                    "round": st["round"],
                    "action": action_type,
                    "role": toastmaster_role
                })
                
                return None
            else:
                send_text(waid, "❌ Ocurrió un error al intentar ceder el cargo. Por favor contacta al administrador.")
                set_session(waid, awaiting=None, buffer=None)
                send_member_menu(club_ctx, waid)
                return None
        
        elif wants_keep:
            # Mantener el cargo
            send_text(waid, "👍 Has decidido mantener tu cargo de Toastmaster. No se realizaron cambios.")
            set_session(waid, awaiting=None, buffer=None)
            send_member_menu(club_ctx, waid)
            return None
        else:
            # Respuesta inválida
            send_text(waid, "❌ Respuesta no válida. Por favor responde:\n1️⃣ para ceder el cargo\n2️⃣ para mantenerlo")
            return None

    # --------- Flujo de respuesta del evaluador ----------------------------------------
    
    if awaiting == "evaluator_response":
        buffer = s.get("buffer", {})
        speech_id = buffer.get("speech_id")
        club_id = buffer.get("club")
        solicitante_waid = buffer.get("solicitante_waid")
        solicitante_nombre = buffer.get("solicitante_nombre")
        
        if not speech_id or not club_id:
            send_text(waid, "❌ Error: No se encontró la solicitud.")
            set_session(waid, awaiting=None, buffer=None)
            return None
        
        club_ctx = _CTX.get(club_id)
        if not club_ctx:
            send_text(waid, "❌ Error: Club no encontrado.")
            set_session(waid, awaiting=None, buffer=None)
            return None
        
        wants_accept = matches_option(body_raw_clean, ("✅ Sí, puedo evaluar", "Aceptar")) or body_norm in ("1", "si", "sí", "aceptar", "acepto")
        wants_reject = matches_option(body_raw_clean, ("❌ No puedo", "Rechazar")) or body_norm in ("2", "no", "rechazar", "rechazo")
        
        if wants_accept:
            # Actualizar estado del discurso a confirmado
            st = club_ctx.state_store.load()
            for speech in st.get("prepared_speeches", []):
                if speech.get("id") == speech_id:
                    speech["status"] = "confirmado"
                    speech["evaluador_confirmado_timestamp"] = int(__import__("time").time())
                    break
            club_ctx.state_store.save(st)
            
            send_text(waid, f"✅ Perfecto. Confirmas que evaluarás a {solicitante_nombre}.")
            send_text(solicitante_waid, f"✅ {pretty_name(club_ctx, waid)} aceptó ser tu evaluador.")
            
            set_session(waid, awaiting=None, buffer=None)
            return None
        
        if wants_reject:
            # Eliminar el discurso o marcarlo como rechazado
            st = club_ctx.state_store.load()
            st["prepared_speeches"] = [
                speech for speech in st.get("prepared_speeches", [])
                if speech.get("id") != speech_id
            ]
            club_ctx.state_store.save(st)
            
            send_text(waid, "✅ Entendido. Se notificará al solicitante.")
            send_text(
                solicitante_waid,
                f"⚠️ {pretty_name(club_ctx, waid)} no puede evaluarte.\n\n"
                "Por favor, elige otro evaluador desde el menú de miembro."
            )
            
            set_session(waid, awaiting=None, buffer=None)
            return None
        
        send_text(waid, "_❌ Opción inválida. Por favor, usa los botones:_ *✅ Sí, puedo evaluar* / *❌ No puedo*")
        send_menu_with_quick_replies(waid, "Responde:", ["✅ Sí, puedo evaluar", "❌ No puedo"])
        return None
    
    # Confirmación para dejar un cargo
    if awaiting == "confirm_leave_role":
        buffer = s.get("buffer", {})
        club_id = buffer.get("club")
        role = buffer.get("role")
        
        if not club_id or not role:
            send_text(waid, "❌ Error: No se encontró la información del cargo.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        club_ctx = _CTX.get(club_id)
        if not club_ctx:
            send_text(waid, "❌ Error: Club no encontrado.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        # Normalizar para comparación más flexible
        body_lower = body_raw.strip().lower()
        
        wants_confirm = (
            matches_option(body_raw_clean, ("✅ Sí, dejar el cargo", "Confirmar")) or 
            "confirmar" in body_lower or 
            "sí" in body_lower or
            body_norm in ("1", "si")
        )
        wants_cancel = (
            matches_option(body_raw_clean, ("❌ No, mantener el cargo", "Cancelar")) or 
            "cancelar" in body_lower or
            "mantener" in body_lower or
            body_norm in ("2", "no")
        )
        
        if wants_confirm:
            # Preguntar cómo quiere manejar el reemplazo
            set_session(waid, awaiting="leave_role_replacement_choice", buffer=buffer)
            replacement_options = [
                ("🤖 Que el bot elija", "Automático"),
                ("👤 Elegir yo el reemplazo", "Manual")
            ]
            send_text(waid, f"Has confirmado dejar el cargo de *{role}*.\n\n¿Cómo deseas proceder con el reemplazo?")
            send_list_menu(waid, "Selecciona una opción:", replacement_options, "Elegir")
            return None
        
        if wants_cancel:
            send_text(waid, "_✅ Mantuviste tu cargo. No se realizó ningún cambio._")
            set_session(waid, awaiting=None, buffer=None, mode="member")
            send_member_menu(club_ctx, waid)
            return None
        
        send_text(waid, "❌ Opción inválida. Usa los botones para responder.")
        confirm_options = [
            ("✅ Sí, dejar el cargo", "Confirmar"),
            ("❌ No, mantener el cargo", "Cancelar")
        ]
        send_list_menu(waid, "Confirma tu decisión:", confirm_options, "Responder")
        return None
    
    # Confirmación para cancelar un discurso preparado
    if awaiting == "confirm_cancel_speech":
        buffer = s.get("buffer", {})
        club_id = buffer.get("club")
        speech_id = buffer.get("speech_id")
        
        if not club_id or not speech_id:
            send_text(waid, "❌ Error: No se encontró la información del discurso.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        club_ctx = _CTX.get(club_id)
        if not club_ctx:
            send_text(waid, "❌ Error: Club no encontrado.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        # Normalizar para comparación
        body_lower = body_raw.strip().lower()
        
        wants_confirm = (
            matches_option(body_raw_clean, ("✅ Sí, cancelar discurso", "Confirmar cancelación")) or 
            "confirmar" in body_lower or 
            ("sí" in body_lower and "cancelar" in body_lower) or
            body_norm in ("1", "si")
        )
        wants_keep = (
            matches_option(body_raw_clean, ("❌ No, mantener discurso", "Mantener")) or 
            "mantener" in body_lower or
            "no" in body_lower or
            body_norm in ("2", "no")
        )
        
        if wants_confirm:
            st = club_ctx.state_store.load()
            
            # Buscar el discurso
            speech = next((sp for sp in st.get("prepared_speeches", []) if sp.get("id") == speech_id), None)
            
            if not speech:
                send_text(waid, "❌ Error: No se encontró el discurso.")
                set_session(waid, awaiting=None, buffer=None, mode="member")
                send_member_menu(club_ctx, waid)
                return None
            
            # Obtener información del discurso antes de eliminarlo
            speech_title = speech.get("titulo", "Sin título")
            evaluador_waid = speech.get("evaluador_waid")
            
            # Eliminar el discurso
            st["prepared_speeches"] = [
                sp for sp in st.get("prepared_speeches", [])
                if sp.get("id") != speech_id
            ]
            club_ctx.state_store.save(st)
            
            # Notificar al evaluador si existe
            if evaluador_waid:
                speaker_name = pretty_name(club_ctx, waid)
                send_text(
                    evaluador_waid,
                    f"⚠️ *Discurso cancelado*\n\n"
                    f"{speaker_name} ha cancelado su discurso:\n"
                    f"📄 *{speech_title}*\n\n"
                    f"Ya no necesitarás evaluarlo."
                )
            
            send_text(waid, f"✅ Has cancelado tu discurso: *{speech_title}*")
            set_session(waid, awaiting=None, buffer=None, mode="member")
            send_member_menu(club_ctx, waid)
            return None
        
        if wants_keep:
            send_text(waid, "_✅ Mantuviste tu discurso. No se realizó ningún cambio._")
            set_session(waid, awaiting=None, buffer=None, mode="member")
            send_member_menu(club_ctx, waid)
            return None
        
        send_text(waid, "❌ Opción inválida. Usa los botones para responder.")
        cancel_options = [
            ("✅ Sí, cancelar discurso", "Confirmar cancelación"),
            ("❌ No, mantener discurso", "Mantener")
        ]
        send_list_menu(waid, "Confirma tu decisión:", cancel_options, "Responder")
        return None
    
    # Confirmación para cancelar una educativa
    if awaiting == "confirm_cancel_section":
        buffer = s.get("buffer", {})
        club_id = buffer.get("club")
        section_waid = buffer.get("section_waid")
        
        if not club_id or not section_waid:
            send_text(waid, "❌ Error: No se encontró la información de la educativa.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        club_ctx = _CTX.get(club_id)
        if not club_ctx:
            send_text(waid, "❌ Error: Club no encontrado.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        # Normalizar para comparación
        body_lower = body_raw.strip().lower()
        
        wants_confirm = (
            matches_option(body_raw_clean, ("✅ Sí, cancelar educativa", "Confirmar cancelación")) or 
            "confirmar" in body_lower or 
            ("sí" in body_lower and "cancelar" in body_lower) or
            body_norm in ("1", "si")
        )
        wants_keep = (
            matches_option(body_raw_clean, ("❌ No, mantener educativa", "Mantener")) or 
            "mantener" in body_lower or
            "no" in body_lower or
            body_norm in ("2", "no")
        )
        
        if wants_confirm:
            st = club_ctx.state_store.load()
            
            # Buscar la educativa
            section = next((sec for sec in st.get("educational_sections", []) if sec.get("waid") == section_waid), None)
            
            if not section:
                send_text(waid, "❌ Error: No se encontró la educativa.")
                set_session(waid, awaiting=None, buffer=None, mode="member")
                send_member_menu(club_ctx, waid)
                return None
            
            # Obtener información antes de eliminar
            section_name = section.get("nombre_seccion", "Sin nombre")
            
            # Eliminar la educativa
            st["educational_sections"] = [
                sec for sec in st.get("educational_sections", [])
                if sec.get("waid") != section_waid
            ]
            club_ctx.state_store.save(st)
            
            send_text(waid, f"✅ Has cancelado tu educativa: *{section_name}*")
            set_session(waid, awaiting=None, buffer=None, mode="member")
            send_member_menu(club_ctx, waid)
            return None
        
        if wants_keep:
            send_text(waid, "_✅ Mantuviste tu educativa. No se realizó ningún cambio._")
            set_session(waid, awaiting=None, buffer=None, mode="member")
            send_member_menu(club_ctx, waid)
            return None
        
        send_text(waid, "❌ Opción inválida. Usa los botones para responder.")
        cancel_options = [
            ("✅ Sí, cancelar educativa", "Confirmar cancelación"),
            ("❌ No, mantener educativa", "Mantener")
        ]
        send_list_menu(waid, "Confirma tu decisión:", cancel_options, "Responder")
        return None
    
    # Manejo del reemplazo al dejar un cargo
    if awaiting == "leave_role_replacement_choice":
        buffer = s.get("buffer", {})
        club_id = buffer.get("club")
        role = buffer.get("role")
        
        if not club_id or not role:
            send_text(waid, "❌ Error: No se encontró la información del cargo.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        club_ctx = _CTX.get(club_id)
        if not club_ctx:
            send_text(waid, "❌ Error: Club no encontrado.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        body_lower = body_raw.strip().lower()
        
        # Opción 1: Bot elige automáticamente
        if matches_option(body_raw_clean, ("🤖 Que el bot elija", "Automático")) or "bot" in body_lower or "automático" in body_lower or "automatico" in body_lower:
            st = club_ctx.state_store.load()
            
            # Obtener el nivel mínimo requerido para el rol (buscar en la lista de roles)
            role_obj = next((r for r in club_ctx.club.roles if r.name == role), None)
            role_min_level = role_obj.difficulty if role_obj else 1
            
            # Encontrar candidatos elegibles (nivel suficiente, sin rol aceptado)
            candidates = []
            for member in club_ctx.club.members:
                if member.waid == waid:
                    continue  # Excluir al que está dejando el cargo
                if member.level < role_min_level:
                    continue  # No cumple nivel mínimo
                # Verificar que no tenga rol aceptado
                has_role = any(info.get("waid") == member.waid for info in st.get("accepted", {}).values())
                if not has_role:
                    candidates.append(member)
            
            if not candidates:
                # Eliminar cargo sin reemplazo
                if role in st.get("accepted", {}):
                    del st["accepted"][role]
                    club_ctx.state_store.save(st)
                send_text(waid, f"✅ Has dejado el cargo de *{role}*.\n\n⚠️ No hay candidatos disponibles para reemplazarte. El cargo queda vacío.")
                set_session(waid, awaiting=None, buffer=None, mode="member")
                send_member_menu(club_ctx, waid)
                return None
            
            # Elegir candidato aleatorio
            import random
            replacement = random.choice(candidates)
            
            # Actualizar estado
            if role in st.get("accepted", {}):
                del st["accepted"][role]
            
            # Crear pendiente para el reemplazo
            if "pending" not in st:
                st["pending"] = {}
            st["pending"][role] = {
                "waid": replacement.waid,
                "nombre": replacement.name,
                "propuesto_por": pretty_name(club_ctx, waid),
                "timestamp": int(__import__("time").time())
            }
            club_ctx.state_store.save(st)
            
            # Notificar
            send_text(waid, f"✅ Has dejado el cargo de *{role}*.\n\n🤖 El bot seleccionó a *{replacement.name}* como reemplazo.")
            send_text(
                replacement.waid,
                f"📧 {pretty_name(club_ctx, waid)} dejó el cargo de *{role}* y el bot te seleccionó como reemplazo.\n\n"
                f"👉 Ve al Menú de socio ({club_ctx.club_id}) → «🎯 Mi cargo» para aceptar o rechazar."
            )
            
            set_session(waid, awaiting=None, buffer=None, mode="member")
            send_member_menu(club_ctx, waid)
            return None
        
        # Opción 2: Elegir manualmente
        elif matches_option(body_raw_clean, ("👤 Elegir yo el reemplazo", "Manual")) or "elegir" in body_lower or "manual" in body_lower:
            st = club_ctx.state_store.load()
            
            # Obtener el nivel mínimo requerido para el rol (buscar en la lista de roles)
            role_obj = next((r for r in club_ctx.club.roles if r.name == role), None)
            role_min_level = role_obj.difficulty if role_obj else 1
            
            # Encontrar candidatos elegibles
            candidates = []
            for member in club_ctx.club.members:
                if member.waid == waid:
                    continue
                if member.level < role_min_level:
                    continue
                # Verificar que no tenga rol aceptado
                has_role = any(info.get("waid") == member.waid for info in st.get("accepted", {}).values())
                if not has_role:
                    candidates.append((member.name, member.waid))
            
            if not candidates:
                # Eliminar cargo sin reemplazo
                if role in st.get("accepted", {}):
                    del st["accepted"][role]
                    club_ctx.state_store.save(st)
                send_text(waid, f"✅ Has dejado el cargo de *{role}*.\n\n⚠️ No hay candidatos disponibles. El cargo queda vacío.")
                set_session(waid, awaiting=None, buffer=None, mode="member")
                send_member_menu(club_ctx, waid)
                return None
            
            # Mostrar menú de candidatos
            set_session(waid, awaiting="leave_role_select_replacement", buffer=buffer)
            candidate_options = [(nombre, "") for nombre, _ in candidates]
            buffer["candidates_map"] = {nombre: waid_c for nombre, waid_c in candidates}
            set_session(waid, awaiting="leave_role_select_replacement", buffer=buffer)
            send_text(waid, f"👥 Estos son los socios elegibles para *{role}* (nivel {role_min_level}+):")
            send_list_menu(waid, "Selecciona un reemplazo:", candidate_options, "Elegir reemplazo")
            return None
        
        send_text(waid, "❌ Opción inválida. Usa los botones para responder.")
        replacement_options = [
            ("🤖 Que el bot elija", "Automático"),
            ("👤 Elegir yo el reemplazo", "Manual")
        ]
        send_list_menu(waid, "Selecciona una opción:", replacement_options, "Elegir")
        return None
    
    # Selección manual del reemplazo
    if awaiting == "leave_role_select_replacement":
        buffer = s.get("buffer", {})
        club_id = buffer.get("club")
        role = buffer.get("role")
        candidates_map = buffer.get("candidates_map", {})
        
        if not club_id or not role:
            send_text(waid, "❌ Error: No se encontró la información del cargo.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        club_ctx = _CTX.get(club_id)
        if not club_ctx:
            send_text(waid, "❌ Error: Club no encontrado.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        # Buscar el candidato seleccionado
        selected_waid = None
        selected_name = None
        for nombre, waid_c in candidates_map.items():
            if norm(body_raw.strip()) == norm(nombre) or matches_option(body_raw.strip(), (nombre, "")):
                selected_waid = waid_c
                selected_name = nombre
                break
        
        if not selected_waid:
            send_text(waid, "_❌ No se reconoció al socio. Por favor, selecciona de la lista._")
            candidate_options = [(nombre, "") for nombre in candidates_map.keys()]
            send_list_menu(waid, "Selecciona un reemplazo:", candidate_options, "Elegir reemplazo")
            return None
        
        # Actualizar estado
        st = club_ctx.state_store.load()
        if role in st.get("accepted", {}):
            del st["accepted"][role]
        
        # Crear pendiente para el reemplazo
        if "pending" not in st:
            st["pending"] = {}
        st["pending"][role] = {
            "waid": selected_waid,
            "nombre": selected_name,
            "propuesto_por": pretty_name(club_ctx, waid),
            "timestamp": int(__import__("time").time())
        }
        club_ctx.state_store.save(st)
        
        # Notificar
        send_text(waid, f"✅ Has dejado el cargo de *{role}*.\n\n👤 Seleccionaste a *{selected_name}* como reemplazo.")
        send_text(
            selected_waid,
            f"📧 {pretty_name(club_ctx, waid)} dejó el cargo de *{role}* y te seleccionó como reemplazo.\n\n"
            f"👉 Ve al Menú de socio ({club_ctx.club_id}) → «🎯 Mi cargo» para aceptar o rechazar."
        )
        
        set_session(waid, awaiting=None, buffer=None, mode="member")
        send_member_menu(club_ctx, waid)
        return None


    # --------- Flujos Discurso Preparado ----------------------------------------------
    
    # Paso 1: Pathway
    if awaiting == "speech_step1_pathway":
        log.info("🎯 Step 1 - Pathway recibido: '%s' (interactive: %s, type: %s)", body_raw.strip(), is_interactive, msg_type)
        
        # Lista de pathways válidos
        valid_pathways = [
            "Liderazgo dinámico",
            "Cautiva con humor",
            "Asesoría eficaz",
            "Influencia persuasiva",
            "Dominio de las presentaciones",
            "Comunicación visionaria"
        ]
        
        pathway_text = body_raw.strip()
        
        # Ignorar el clic en el botón de la lista SOLO si no es un pathway válido
        pathway_button_texts = ["Seleccionar pathway", "Elige una opción", "Opciones"]
        if pathway_text in pathway_button_texts or norm(pathway_text) in [norm(t) for t in pathway_button_texts]:
            log.info("⏩ Ignorando clic en botón de lista, esperando selección de pathway")
            return jsonify({"status": "ok"})
        
        # Verificar si es un pathway válido
        matched_pathway = None
        if pathway_text in valid_pathways:
            matched_pathway = pathway_text
        else:
            # Intentar match parcial (por si WhatsApp trunca)
            for vp in valid_pathways:
                if norm(pathway_text) == norm(vp) or (len(pathway_text) > 10 and norm(vp).startswith(norm(pathway_text))):
                    matched_pathway = vp
                    break
        
        if not matched_pathway:
            log.warning("❌ Pathway inválido recibido: '%s'", pathway_text)
            send_text(waid, f"❌ Pathway inválido. Por favor selecciona uno de la lista.")
            send_list_menu(waid, "📚 Selecciona tu Pathway:", valid_pathways, "Seleccionar pathway")
            return jsonify({"status": "ok"})
        
        buffer = s.get("buffer", {})
        buffer["pathway"] = matched_pathway
        log.info("✅ Pathway guardado: %s - Avanzando al paso 2 (nivel)", matched_pathway)
        set_session(waid, awaiting="speech_step2_nivel", buffer=buffer)
        
        # Enviar mensaje con botones de nivel
        nivel_options = [
            ("1️⃣ Nivel 1"),
            ("2️⃣ Nivel 2"),
            ("3️⃣ Nivel 3"),
            ("4️⃣ Nivel 4"),
            ("5️⃣ Nivel 5")
        ]
        send_list_menu(waid, "📊 Selecciona el nivel de tu proyecto:", nivel_options, "Elegir nivel")
        return jsonify({"status": "ok"})
    
    # Paso 2: Nivel
    if awaiting == "speech_step2_nivel":
        # Detectar nivel seleccionado
        nivel_seleccionado = None
        
        # Intentar detectar desde opciones del menú
        if "nivel 1" in body_norm or body_norm == "1":
            nivel_seleccionado = 1
        elif "nivel 2" in body_norm or body_norm == "2":
            nivel_seleccionado = 2
        elif "nivel 3" in body_norm or body_norm == "3":
            nivel_seleccionado = 3
        elif "nivel 4" in body_norm or body_norm == "4":
            nivel_seleccionado = 4
        elif "nivel 5" in body_norm or body_norm == "5":
            nivel_seleccionado = 5
        
        if not nivel_seleccionado:
            send_text(waid, "_❌ Nivel inválido. Por favor, selecciona un nivel del *1 al 5*._")
            nivel_options = [
                ("1️⃣ Nivel 1", "Nivel 1"),
                ("2️⃣ Nivel 2", "Nivel 2"),
                ("3️⃣ Nivel 3", "Nivel 3"),
                ("4️⃣ Nivel 4", "Nivel 4"),
                ("5️⃣ Nivel 5", "Nivel 5")
            ]
            send_list_menu(waid, "📊 Selecciona el nivel de tu proyecto:", nivel_options, "Elegir nivel")
            return jsonify({"status": "ok"})
        
        buffer = s.get("buffer", {})
        buffer["nivel"] = nivel_seleccionado
        set_session(waid, awaiting="speech_step3_proyecto", buffer=buffer)
        send_text(waid, 
            "📝 *Envía el nombre de tu proyecto:*\n\n"
            "_Ejemplos:_\n"
            "• `Rompehielos`\n"
            "• `Desarrollo de la comunicación no verbal`\n\n"
            "_Nota: Si cometes algún error al escribir el nombre, podrás corregirlo más adelante antes de guardar la información._"
        )
        return jsonify({"status": "ok"})
    
    # Paso 3: Nombre del proyecto
    if awaiting == "speech_step3_proyecto":
        if is_interactive:
            send_text(waid, "Escribe el nombre del proyecto con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["proyecto"] = body_raw.strip()
        set_session(waid, awaiting="speech_step4_titulo", buffer=buffer)
        send_text(waid, 
            "📢 *Envía el título de tu discurso:*\n\n"
            "_Ejemplo:_ `Cómo influir con integridad`\n\n"
            "_Nota: Si cometes algún error al escribir el título, podrás corregirlo más adelante antes de guardar la información._"
        )
        return jsonify({"status": "ok"})
    
    # Paso 4: Título del discurso
    if awaiting == "speech_step4_titulo":
        if is_interactive:
            send_text(waid, "Escribe el título del discurso con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["titulo"] = body_raw.strip()
        set_session(waid, awaiting="speech_step5_duracion", buffer=buffer)
        send_text(waid, 
            "⏱️ *Indica el tiempo mínimo y máximo de tu discurso:*\n\n"
            "_Puedes usar cualquier formato natural. El sistema detectará automáticamente ambos valores._\n\n"
            "_Ejemplos válidos:_\n"
            "• `5-7`\n"
            "• `de 5 a 7 minutos`\n"
            "• `entre 5 y 7`\n"
            "• `mínimo 5 máximo 7`"
        )
        return jsonify({"status": "ok"})
    
    # Paso 5: Duración
    if awaiting == "speech_step5_duracion":
        if is_interactive:
            send_text(waid, "Escribe la duración con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        duration_text = body_raw.strip()
        
        # Extraer números con regex (flexible)
        min_time, max_time = _extract_duration_numbers(duration_text)
        
        if min_time is None or max_time is None:
            send_text(waid, 
                "❌ *Debes indicar dos números:* el tiempo mínimo y el tiempo máximo.\n\n"
                "_⏱️ Intenta nuevamente con cualquiera de estos formatos:_\n"
                "• `5-7`\n"
                "• `de 5 a 7 minutos`\n"
                "• `entre 5 y 7`\n"
                "• `mínimo 5 máximo 7`"
            )
            return jsonify({"status": "ok"})
        
        buffer = s.get("buffer", {})
        buffer["duracion_min"] = min_time
        buffer["duracion_max"] = max_time
        
        # Preguntar por el tema del discurso
        set_session(waid, awaiting="speech_step5_tema", buffer=buffer)
        send_text(waid, 
            "📝 *¿De qué tema trata tu discurso?*\n\n"
            "_Describe brevemente el tema que abordarás en tu presentación._"
        )
        return jsonify({"status": "ok"})
    
    # Paso 5: Tema del discurso
    if awaiting == "speech_step5_tema":
        if is_interactive:
            send_text(waid, "Escribe el tema con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["tema"] = body_raw.strip()
        
        # Preguntar por los intereses
        set_session(waid, awaiting="speech_step6_intereses", buffer=buffer)
        send_text(waid, 
            "🎯 *¿Cuáles son tus intereses actuales?*\n\n"
            "_Comparte tus intereses para que el Toastmaster pueda hacer una mejor introducción._"
        )
        return jsonify({"status": "ok"})
    
    # Paso 6: Intereses del orador
    if awaiting == "speech_step6_intereses":
        if is_interactive:
            send_text(waid, "Escribe tus intereses con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["intereses"] = body_raw.strip()
        
        # Obtener evaluadores candidatos (nivel máximo, excluyendo al solicitante)
        ctx_speech = _CTX[buffer["club"]]
        evaluadores = _get_max_level_members(ctx_speech, exclude_waid=waid)
        
        if not evaluadores:
            send_text(waid, "❌ No hay evaluadores disponibles. Envía el nombre manualmente:")
            set_session(waid, awaiting="speech_step7_evaluador", buffer=buffer)
            return None
        
        # Enviar menú con evaluadores
        set_session(waid, awaiting="speech_step7_evaluador", buffer=buffer)
        evaluador_options = [(nombre, "") for nombre, waid_eval in evaluadores]  # Solo nombre, sin description
        # Guardar mapeo nombre -> waid en buffer para recuperarlo después
        buffer["evaluadores_map"] = {nombre: waid_eval for nombre, waid_eval in evaluadores}
        set_session(waid, awaiting="speech_step7_evaluador", buffer=buffer)
        send_list_menu(waid, "👤 Selecciona a tu evaluador:", evaluador_options, "Elegir evaluador")
        return None
    
    # Paso 7: Evaluador
    if awaiting == "speech_step7_evaluador":
        buffer = s.get("buffer", {})
        ctx_speech = _CTX[buffer["club"]]
        st_speech = ctx_speech.state_store.load()
        
        # Intentar encontrar al evaluador seleccionado
        evaluador_waid = None
        evaluador_nombre = None
        
        # Buscar en el mapeo guardado
        evaluadores_map = buffer.get("evaluadores_map", {})
        for nombre, waid_eval in evaluadores_map.items():
            if norm(body_raw.strip()) == norm(nombre) or matches_option(body_raw.strip(), (nombre, "")):
                evaluador_waid = waid_eval
                evaluador_nombre = nombre
                break
        
        # Si no se encontró, asumir que es texto libre
        if not evaluador_waid:
            evaluador_nombre = body_raw.strip()
            buffer["evaluador"] = evaluador_nombre
            buffer["evaluador_waid"] = None
            set_session(waid, awaiting="speech_confirm_evaluador", buffer=buffer)
        else:
            # Validar que el evaluador NO sea el Toastmaster actual
            if _is_toastmaster(st_speech, evaluador_waid):
                send_text(
                    waid,
                    f"❌ No puedes elegir a {evaluador_nombre} como evaluador porque tiene el cargo de Toastmaster de la noche.\n\n"
                    "Por favor, selecciona a otro evaluador:"
                )
                # Reenviar menú de evaluadores
                evaluadores = _get_max_level_members(ctx_speech, exclude_waid=waid)
                evaluador_options = [(nombre, "") for nombre, waid_eval in evaluadores]
                buffer["evaluadores_map"] = {nombre: waid_eval for nombre, waid_eval in evaluadores}
                set_session(waid, awaiting="speech_step7_evaluador", buffer=buffer)
                send_list_menu(waid, "👤 Selecciona a tu evaluador:", evaluador_options, "Elegir evaluador")
                return None
            
            buffer["evaluador"] = evaluador_nombre
            buffer["evaluador_waid"] = evaluador_waid
            set_session(waid, awaiting="speech_confirm_evaluador", buffer=buffer)
        
        # Pedir confirmación del evaluador
        send_text(waid, f"👤 Seleccionaste a {buffer['evaluador']} como tu evaluador.")
        confirm_options = [
            ("✅ Sí, confirmo evaluador", "Confirmar"),
            ("❌ No, elegir otro", "Elegir otro")
        ]
        send_list_menu(waid, "¿Estás seguro de tu elección?", confirm_options, "Responder")
        return None
    
    # Paso 6b: Confirmación del evaluador
    if awaiting == "speech_confirm_evaluador":
        buffer = s.get("buffer", {})
        ctx_speech = _CTX[buffer["club"]]
        
        # Normalizar para comparación más flexible
        body_lower = body_raw.strip().lower()
        
        wants_confirm = (
            matches_option(body_raw_clean, ("✅ Sí, confirmar evaluador", "Confirmar")) or 
            "confirmar" in body_lower or 
            body_norm in ("1", "si", "sí") or
            "sí" in body_lower
        )
        wants_change = (
            matches_option(body_raw_clean, ("❌ No, elegir otro", "Elegir otro")) or 
            "elegir otro" in body_lower or
            body_norm in ("2", "no", "cambiar")
        )
        
        if wants_change:
            # Volver a mostrar el menú de evaluadores
            evaluadores = _get_max_level_members(ctx_speech, exclude_waid=waid)
            if not evaluadores:
                send_text(waid, "❌ No hay evaluadores disponibles.")
                set_session(waid, awaiting=None, buffer=None, mode="root")
                send_root_menu(waid)
                return None
            
            evaluador_options = [(nombre, "") for nombre, waid_eval in evaluadores]
            buffer["evaluadores_map"] = {nombre: waid_eval for nombre, waid_eval in evaluadores}
            set_session(waid, awaiting="speech_step7_evaluador", buffer=buffer)
            send_list_menu(waid, "👤 Selecciona a tu evaluador:", evaluador_options, "Elegir evaluador")
            return None
        
        if wants_confirm:
            # Continuar al resumen
            set_session(waid, awaiting="speech_confirm", buffer=buffer)
            
            # Mostrar resumen y confirmación
            resumen = (
                f"📋 *Resumen de tu solicitud de discurso preparado*\n\n"
                f"📚 Pathway: {buffer['pathway']}\n"
                f"📊 Nivel: {buffer['nivel']}\n"
                f"📝 Proyecto: {buffer['proyecto']}\n"
                f"📢 Título: {buffer['titulo']}\n"
                f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
                f"👤 Evaluador: {buffer['evaluador']}\n\n"
            )
            send_text(waid, resumen)
            confirm_options = [
                ("✅ Confirmar y registrar", "Confirmar"),
                ("✏️ Corregir datos", "Corregir"),
                ("❌ Cancelar registro", "Cancelar")
            ]
            # send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
            send_list_menu(waid, "Confirma que tus datos son correctos.", confirm_options, "Seleccionar acción")
            return None
        
        # Si no confirmó ni rechazó, pedir respuesta válida
        send_text(waid, "❌ Opción inválida. Usa los botones para responder.")
        confirm_options = [
            ("✅ Sí, confirmar evaluador", "Confirmar"),
            ("❌ No, elegir otro", "Elegir otro")
        ]
        send_list_menu(waid, f"👤 ¿Confirmas a {buffer.get('evaluador', 'este evaluador')}?", confirm_options, "Responder")
        return None
    
    # Confirmación de discurso
    if awaiting == "speech_confirm":
        buffer = s.get("buffer", {})
        wants_confirm = matches_option(body_raw_clean, ("✅ Confirmar y registrar", "Confirmar")) or body_norm in ("1", "confirmar", "si", "sí", "ok")
        wants_correct = matches_option(body_raw_clean, ("✏️ Corregir datos", "Corregir")) or body_norm in ("2", "corregir", "editar")
        wants_cancel = matches_option(body_raw_clean, ("❌ Cancelar registro", "Cancelar")) or body_norm in ("3", "cancelar", "no")
        
        if wants_correct:
            # Mostrar menú para elegir qué corregir
            send_text(waid, "✏️ ¿Qué dato deseas corregir?")
            correct_options = [
                ("📚 Pathway", "pathway"),
                ("📊 Nivel", "nivel"),
                ("📝 Proyecto", "proyecto"),
                ("📢 Título", "titulo"),
                ("⏱️ Duración", "duracion"),
                ("👤 Evaluador", "evaluador")
            ]
            set_session(waid, awaiting="speech_correct_choice", buffer=buffer)
            send_list_menu(waid, "Selecciona el campo a corregir:", correct_options, "Corregir campo")
            return None
        
        if wants_confirm:
            club_ctx = _CTX[buffer["club"]]
            
            # Revocar roles incompatibles
            revoked = _revoke_incompatible_roles(club_ctx, waid)
            
            # Guardar discurso con estado pendiente
            st = club_ctx.state_store.load()
            if "prepared_speeches" not in st:
                st["prepared_speeches"] = []
            
            import time
            speech_id = f"speech_{int(time.time())}_{waid[-4:]}"
            
            speech_data = {
                "id": speech_id,
                "waid": buffer["waid"],
                "nombre": pretty_name(club_ctx, buffer["waid"]),
                "pathway": buffer["pathway"],
                "nivel": buffer["nivel"],
                "proyecto": buffer["proyecto"],
                "titulo": buffer["titulo"],
                "duracion_min": buffer["duracion_min"],
                "duracion_max": buffer["duracion_max"],
                "tema": buffer.get("tema", ""),
                "intereses": buffer.get("intereses", ""),
                "evaluador": buffer["evaluador"],
                "evaluador_waid": buffer.get("evaluador_waid"),
                "status": "pendiente_evaluador" if buffer.get("evaluador_waid") else "confirmado",
                "solicitud_timestamp": int(time.time()),
                "round": buffer["round"]
            }
            st["prepared_speeches"].append(speech_data)
            club_ctx.state_store.save(st)
            
            # Notificar al Toastmaster de la noche
            toastmaster_waid = None
            for role, info in st.get("accepted", {}).items():
                if "toastmaster" in role.lower():
                    toastmaster_waid = info.get("waid")
                    break
            
            if toastmaster_waid:
                orador_nombre = pretty_name(club_ctx, waid)
                toastmaster_msg = (
                    f"🎤 *Nuevo discurso registrado*\n\n"
                    f"*Orador:* {orador_nombre}\n"
                    f"*Título:* {buffer['titulo']}\n\n"
                    f"_*Tema:*_ {buffer.get('tema', 'No especificado')}\n\n"
                    f"_*Intereses:*_ {buffer.get('intereses', 'No especificado')}"
                )
                send_text(toastmaster_waid, toastmaster_msg)
            
            msg = f"✅ Tu discurso ha sido registrado.\n\n📢 Título: '{buffer['titulo']}'"
            if revoked:
                msg += f"\n\n⚠️ Se revocaron los siguientes roles por incompatibilidad: {', '.join(revoked)}"
            
            # Si hay evaluador con waid, enviar solicitud
            if buffer.get("evaluador_waid"):
                evaluador_waid = buffer["evaluador_waid"]
                solicitud_msg = (
                    f"📧 {pretty_name(club_ctx, waid)} te ha seleccionado como evaluador de su discurso preparado:\n\n"
                    f"📚 Pathway: {buffer['pathway']}\n"
                    f"📊 Nivel: {buffer['nivel']}\n"
                    f"📋 Proyecto: {buffer['proyecto']}\n"
                    f"📢 Título: {buffer['titulo']}\n"
                    f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n\n"
                    f"¿Puedes evaluar este discurso? Tienes 2 horas para responder."
                )
                send_text(evaluador_waid, solicitud_msg)
                
                # Configurar sesión del evaluador para esperar respuesta
                set_session(evaluador_waid, awaiting="evaluator_response", buffer={
                    "speech_id": speech_id,
                    "club": buffer["club"],
                    "solicitante_waid": waid,
                    "solicitante_nombre": pretty_name(club_ctx, waid)
                })
                
                # Enviar menú con botones
                send_menu_with_quick_replies(evaluador_waid, "Responde:", ["✅ Sí, puedo evaluar", "❌ No puedo"])
                
                msg += f"\n\n📧 Se envió solicitud a {buffer['evaluador']}. Tiene 2 horas para confirmar."
            
            send_text(waid, msg)
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
            return None
        
        if wants_cancel:
            send_text(waid, "❌ Solicitud de discurso cancelada.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        if is_interactive:
            send_text(waid, "❗Opción inválida. Usa los botones: ✅ Confirmar / ✏️ Corregir / ❌ Cancelar.")
        else:
            send_text(waid, "Opción inválida. Envía 1 para confirmar, 2 para corregir o 3 para cancelar.")
        return None

    # Corrección de campos
    if awaiting == "speech_correct_choice":
        buffer = s.get("buffer", {})
        ctx_speech = _CTX[buffer["club"]]
        
        # Detectar qué campo quiere corregir
        field_to_correct = None
        if matches_option(body_raw_clean, ("📚 Pathway", "pathway")) or "pathway" in body_norm:
            field_to_correct = "pathway"
        elif matches_option(body_raw_clean, ("📊 Nivel", "nivel")) or "nivel" in body_norm:
            field_to_correct = "nivel"
        elif matches_option(body_raw_clean, ("📝 Proyecto", "proyecto")) or "proyecto" in body_norm:
            field_to_correct = "proyecto"
        elif matches_option(body_raw_clean, ("📢 Título", "titulo")) or "titulo" in body_norm or "título" in body_norm:
            field_to_correct = "titulo"
        elif matches_option(body_raw_clean, ("⏱️ Duración", "duracion")) or "duracion" in body_norm or "duración" in body_norm:
            field_to_correct = "duracion"
        elif matches_option(body_raw_clean, ("👤 Evaluador", "evaluador")) or "evaluador" in body_norm:
            field_to_correct = "evaluador"
        
        if not field_to_correct:
            send_text(waid, "❌ Opción inválida. Por favor selecciona un campo válido.")
            correct_options = [
                ("📚 Pathway", "pathway"),
                ("📊 Nivel", "nivel"),
                ("📝 Proyecto", "proyecto"),
                ("📢 Título", "titulo"),
                ("⏱️ Duración", "duracion"),
                ("👤 Evaluador", "evaluador")
            ]
            send_list_menu(waid, "Selecciona el campo a corregir:", correct_options, "Corregir campo")
            return None
        
        # Redirigir al paso correspondiente según el campo con estado de corrección
        if field_to_correct == "pathway":
            send_text(waid, "✏️ Corrigiendo Pathway...")
            pathways = ["Liderazgo dinámico", "Cautiva con humor", "Asesoría eficaz", "Influencia persuasiva", "Dominio de las presentaciones", "Comunicación visionaria"]
            send_list_menu(waid, "📚 Selecciona tu Pathway:", pathways, "Seleccionar pathway")
            set_session(waid, awaiting="speech_correct_pathway", buffer=buffer)
        elif field_to_correct == "nivel":
            send_text(waid, "✏️ Corrigiendo Nivel...")
            nivel_options = [
                ("1️⃣ Nivel 1", "Nivel 1"),
                ("2️⃣ Nivel 2", "Nivel 2"),
                ("3️⃣ Nivel 3", "Nivel 3"),
                ("4️⃣ Nivel 4", "Nivel 4"),
                ("5️⃣ Nivel 5", "Nivel 5")
            ]
            send_list_menu(waid, "📊 Selecciona el nivel de tu proyecto:", nivel_options, "Elegir nivel")
            set_session(waid, awaiting="speech_correct_nivel", buffer=buffer)
        elif field_to_correct == "proyecto":
            send_text(waid, 
                "✏️ *Corrigiendo Proyecto...*\n\n"
                "_📝 Envía el nuevo nombre de tu proyecto:_"
            )
            set_session(waid, awaiting="speech_correct_proyecto", buffer=buffer)
        elif field_to_correct == "titulo":
            send_text(waid, 
                "✏️ *Corrigiendo Título...*\n\n"
                "_📢 Envía el nuevo título de tu discurso:_"
            )
            set_session(waid, awaiting="speech_correct_titulo", buffer=buffer)
        elif field_to_correct == "duracion":
            send_text(waid, 
                "✏️ *Corrigiendo Duración...*\n\n"
                "_⏱️ Envía la nueva duración en cualquier formato natural._\n\n"
                "_Ejemplo:_ `5-7`"
            )
            set_session(waid, awaiting="speech_correct_duracion", buffer=buffer)
        elif field_to_correct == "evaluador":
            send_text(waid, "✏️ Corrigiendo Evaluador...")
            evaluadores = _get_max_level_members(ctx_speech, exclude_waid=waid)
            if not evaluadores:
                send_text(waid, "❌ No hay evaluadores disponibles.")
                set_session(waid, awaiting=None, buffer=None, mode="root")
                send_root_menu(waid)
                return None
            evaluador_options = [(nombre, "") for nombre, waid_eval in evaluadores]
            buffer["evaluadores_map"] = {nombre: waid_eval for nombre, waid_eval in evaluadores}
            set_session(waid, awaiting="speech_correct_evaluador", buffer=buffer)
            send_list_menu(waid, "👤 Selecciona a tu evaluador:", evaluador_options, "Elegir evaluador")
        
        return None
    
    # Handlers de corrección individuales que regresan al resumen
    if awaiting == "speech_correct_pathway":
        buffer = s.get("buffer", {})
        pathways = ["Liderazgo dinámico", "Cautiva con humor", "Asesoría eficaz", "Influencia persuasiva", "Dominio de las presentaciones", "Comunicación visionaria"]
        if body_raw.strip() in pathways or any(matches_option(body_raw_clean, (p, "")) for p in pathways):
            buffer["pathway"] = body_raw.strip()
            set_session(waid, awaiting="speech_confirm", buffer=buffer)
            # Mostrar resumen actualizado
            resumen = (
                f"📋 *Resumen actualizado*\n\n"
                f"📚 Pathway: {buffer['pathway']}\n"
                f"📊 Nivel: {buffer['nivel']}\n"
                f"📝 Proyecto: {buffer['proyecto']}\n"
                f"📢 Título: {buffer['titulo']}\n"
                f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
                f"👤 Evaluador: {buffer['evaluador']}\n\n"
            )
            send_text(waid, resumen)
            confirm_options = [
                ("✅ Confirmar y registrar", "Confirmar"),
                ("✏️ Corregir datos", "Corregir"),
                ("❌ Cancelar registro", "Cancelar")
            ]
            send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
            return None
        else:
            send_text(waid, "❌ Pathway inválido. Selecciona uno de la lista:")
            send_list_menu(waid, "📚 Selecciona tu Pathway:", pathways, "Seleccionar pathway")
            return None
    
    if awaiting == "speech_correct_nivel":
        buffer = s.get("buffer", {})
        nivel_seleccionado = None
        if "nivel 1" in body_norm or body_norm == "1":
            nivel_seleccionado = 1
        elif "nivel 2" in body_norm or body_norm == "2":
            nivel_seleccionado = 2
        elif "nivel 3" in body_norm or body_norm == "3":
            nivel_seleccionado = 3
        elif "nivel 4" in body_norm or body_norm == "4":
            nivel_seleccionado = 4
        elif "nivel 5" in body_norm or body_norm == "5":
            nivel_seleccionado = 5
        
        if not nivel_seleccionado:
            send_text(waid, "❌ Nivel inválido. Por favor selecciona un nivel del 1 al 5.")
            nivel_options = [
                ("1️⃣ Nivel 1", "Nivel 1"),
                ("2️⃣ Nivel 2", "Nivel 2"),
                ("3️⃣ Nivel 3", "Nivel 3"),
                ("4️⃣ Nivel 4", "Nivel 4"),
                ("5️⃣ Nivel 5", "Nivel 5")
            ]
            send_list_menu(waid, "📊 Selecciona el nivel de tu proyecto:", nivel_options, "Elegir nivel")
            return None
        
        buffer["nivel"] = nivel_seleccionado
        set_session(waid, awaiting="speech_confirm", buffer=buffer)
        # Mostrar resumen actualizado
        resumen = (
            f"📋 *Resumen actualizado*\n\n"
            f"📚 Pathway: {buffer['pathway']}\n"
            f"📊 Nivel: {buffer['nivel']}\n"
            f"📝 Proyecto: {buffer['proyecto']}\n"
            f"📢 Título: {buffer['titulo']}\n"
            f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
            f"👤 Evaluador: {buffer['evaluador']}\n\n"
        )
        send_text(waid, resumen)
        confirm_options = [
            ("✅ Confirmar y registrar", "Confirmar"),
            ("✏️ Corregir datos", "Corregir"),
            ("❌ Cancelar registro", "Cancelar")
        ]
        send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
        return None
    
    if awaiting == "speech_correct_proyecto":
        buffer = s.get("buffer", {})
        buffer["proyecto"] = body_raw.strip()
        set_session(waid, awaiting="speech_confirm", buffer=buffer)
        # Mostrar resumen actualizado
        resumen = (
            f"📋 *Resumen actualizado*\n\n"
            f"📚 Pathway: {buffer['pathway']}\n"
            f"📊 Nivel: {buffer['nivel']}\n"
            f"📝 Proyecto: {buffer['proyecto']}\n"
            f"📢 Título: {buffer['titulo']}\n"
            f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
            f"👤 Evaluador: {buffer['evaluador']}\n\n"
        )
        send_text(waid, resumen)
        confirm_options = [
            ("✅ Confirmar y registrar", "Confirmar"),
            ("✏️ Corregir datos", "Corregir"),
            ("❌ Cancelar registro", "Cancelar")
        ]
        send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
        return None
    
    if awaiting == "speech_correct_titulo":
        buffer = s.get("buffer", {})
        buffer["titulo"] = body_raw.strip()
        set_session(waid, awaiting="speech_confirm", buffer=buffer)
        # Mostrar resumen actualizado
        resumen = (
            f"📋 *Resumen actualizado*\n\n"
            f"📚 Pathway: {buffer['pathway']}\n"
            f"📊 Nivel: {buffer['nivel']}\n"
            f"📝 Proyecto: {buffer['proyecto']}\n"
            f"📢 Título: {buffer['titulo']}\n"
            f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
            f"👤 Evaluador: {buffer['evaluador']}\n\n"
        )
        send_text(waid, resumen)
        confirm_options = [
            ("✅ Confirmar y registrar", "Confirmar"),
            ("✏️ Corregir datos", "Corregir"),
            ("❌ Cancelar registro", "Cancelar")
        ]
        send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
        return None
    
    if awaiting == "speech_correct_duracion":
        buffer = s.get("buffer", {})
        duration_text = body_raw.strip()
        
        # Extraer números con regex (flexible)
        min_time, max_time = _extract_duration_numbers(duration_text)
        
        if min_time is None or max_time is None:
            send_text(waid, 
                "❌ *No pude identificar la duración.* Por favor, envía dos números.\n\n"
                "_Ejemplos válidos:_\n"
                "• `5-7`\n"
                "• `de 5 a 7 minutos`\n"
                "• `entre 5 y 7`\n"
                "• `mínimo 5 máximo 7`"
            )
            return None
        
        buffer["duracion_min"] = min_time
        buffer["duracion_max"] = max_time
        set_session(waid, awaiting="speech_confirm", buffer=buffer)
        # Mostrar resumen actualizado
        resumen = (
            f"📋 *Resumen actualizado*\n\n"
            f"📚 Pathway: {buffer['pathway']}\n"
            f"📊 Nivel: {buffer['nivel']}\n"
            f"📝 Proyecto: {buffer['proyecto']}\n"
            f"📢 Título: {buffer['titulo']}\n"
            f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
            f"👤 Evaluador: {buffer['evaluador']}\n\n"
        )
        send_text(waid, resumen)
        confirm_options = [
            ("✅ Confirmar y registrar", "Confirmar"),
            ("✏️ Corregir datos", "Corregir"),
            ("❌ Cancelar registro", "Cancelar")
        ]
        send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
        return None
    
    if awaiting == "speech_correct_evaluador":
        buffer = s.get("buffer", {})
        ctx_speech = _CTX[buffer["club"]]
        st_speech = ctx_speech.state_store.load()
        
        evaluador_waid = None
        evaluador_nombre = None
        evaluadores_map = buffer.get("evaluadores_map", {})
        
        for nombre, waid_eval in evaluadores_map.items():
            if norm(body_raw.strip()) == norm(nombre) or matches_option(body_raw.strip(), (nombre, "")):
                evaluador_waid = waid_eval
                evaluador_nombre = nombre
                break
        
        if not evaluador_waid:
            evaluador_nombre = body_raw.strip()
            buffer["evaluador"] = evaluador_nombre
            buffer["evaluador_waid"] = None
        else:
            if _is_toastmaster(st_speech, evaluador_waid):
                send_text(waid, f"❌ No puedes elegir a {evaluador_nombre} como evaluador porque tiene el cargo de Toastmaster de la noche.\n\nPor favor, selecciona a otro evaluador:")
                evaluadores = _get_max_level_members(ctx_speech, exclude_waid=waid)
                evaluador_options = [(nombre, "") for nombre, waid_eval in evaluadores]
                buffer["evaluadores_map"] = {nombre: waid_eval for nombre, waid_eval in evaluadores}
                set_session(waid, awaiting="speech_correct_evaluador", buffer=buffer)
                send_list_menu(waid, "👤 Selecciona a tu evaluador:", evaluador_options, "Elegir evaluador")
                return None
            buffer["evaluador"] = evaluador_nombre
            buffer["evaluador_waid"] = evaluador_waid
        
        set_session(waid, awaiting="speech_confirm", buffer=buffer)
        # Mostrar resumen actualizado
        resumen = (
            f"📋 *Resumen actualizado*\n\n"
            f"📚 Pathway: {buffer['pathway']}\n"
            f"📊 Nivel: {buffer['nivel']}\n"
            f"📝 Proyecto: {buffer['proyecto']}\n"
            f"📢 Título: {buffer['titulo']}\n"
            f"⏱️ Duración: {buffer['duracion_min']}-{buffer['duracion_max']} minutos\n"
            f"👤 Evaluador: {buffer['evaluador']}\n\n"
        )
        send_text(waid, resumen)
        confirm_options = [
            ("✅ Confirmar y registrar", "Confirmar"),
            ("✏️ Corregir datos", "Corregir"),
            ("❌ Cancelar registro", "Cancelar")
        ]
        send_list_menu(waid, "¿Qué deseas hacer?", confirm_options, "Seleccionar acción")
        return None

    # --------- Flujos Sección Educativa -----------------------------------------------
    
    # Paso 1: Serie
    if awaiting == "section_step1_serie":
        buffer = s.get("buffer", {})
        buffer["serie"] = body_raw.strip()
        set_session(waid, awaiting="section_step2_nombre", buffer=buffer)
        send_text(waid, 
            "📝 *Envía el nombre de la sección educativa que presentarás:*\n\n"
            "_Ejemplo:_ `Cómo dar retroalimentación efectiva`"
        )
        return None
    
    # Paso 2: Nombre de la sección
    if awaiting == "section_step2_nombre":
        if is_interactive:
            send_text(waid, "Escribe el nombre de la sección con texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["nombre_seccion"] = body_raw.strip()
        set_session(waid, awaiting="section_confirm", buffer=buffer)
        
        # Mostrar resumen y confirmación
        resumen = (
            f"📋 *Resumen de tu Sección Educativa*\n\n"
            f"📚 Serie: {buffer['serie']}\n"
            f"📝 Nombre: {buffer['nombre_seccion']}\n"
            f"⏱️ Duración: 10-20 minutos (fijo)\n\n"
            f"¿Es correcta esta información?"
        )
        send_text(waid, resumen)
        send_menu_with_quick_replies(waid, "Confirma tu solicitud:", ["✅ Confirmar", "❌ Cancelar"])
        return None
    
    # Confirmación de sección educativa
    if awaiting == "section_confirm":
        buffer = s.get("buffer", {})
        wants_confirm = matches_option(body_raw_clean, ("✅ Confirmar", "Confirmar")) or body_norm in ("1", "confirmar", "si", "sí", "ok")
        wants_cancel = matches_option(body_raw_clean, ("❌ Cancelar", "Cancelar")) or body_norm in ("2", "cancelar", "no")
        
        if wants_confirm:
            club_ctx = _CTX[buffer["club"]]
            
            # Revocar roles incompatibles
            revoked = _revoke_incompatible_roles(club_ctx, waid)
            
            # Guardar sección educativa
            st = club_ctx.state_store.load()
            if "educational_sections" not in st:
                st["educational_sections"] = []
            
            section_data = {
                "waid": buffer["waid"],
                "nombre": pretty_name(club_ctx, buffer["waid"]),
                "serie": buffer["serie"],
                "nombre_seccion": buffer["nombre_seccion"],
                "duracion": "10-20 minutos",
                "round": buffer["round"]
            }
            st["educational_sections"].append(section_data)
            club_ctx.state_store.save(st)
            
            msg = f"✅ Sección Educativa registrada exitosamente.\n\n📚 Nombre: '{buffer['nombre_seccion']}'"
            if revoked:
                msg += f"\n\n⚠️ Se revocaron los siguientes roles por incompatibilidad: {', '.join(revoked)}"
            
            send_text(waid, msg)
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        if wants_cancel:
            send_text(waid, "❌ Solicitud de sección educativa cancelada.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        if is_interactive:
            send_text(waid, "❗Opción inválida. Usa los botones: ✅ Confirmar / ❌ Cancelar.")
        else:
            send_text(waid, "Opción inválida. Envía 1 para confirmar o 2 para cancelar.")
        return None

    # --------- Menú raíz: despacho directo por etiqueta -------------------------------
    mclubs = member_clubs(waid)
    aclubs = admin_clubs(waid)

    if s.get("mode") == "root":
        # 1) Miembro
        if _is_choice(body_raw_clean, ROOT_MEMBER_SET):
            log.info("✓ Detectado: Opción 'Menú de miembro' desde menú raíz")
            if len(mclubs) == 1:
                cid = mclubs[0]
                set_session(waid, mode="member", club=cid, awaiting=None)
                send_member_menu(_CTX[cid], waid)
                return jsonify({"status": "ok"})
            set_session(waid, mode="member_pick", awaiting="pick_member_club", club=None, buffer=None)
            title_pick, opts_pick, button_pick = member_club_picker_parts(mclubs)
            send_list_menu(waid, title_pick, opts_pick, button_pick)
            return jsonify({"status": "ok"})

        # 2) Admin
        if _is_choice(body_raw_clean, ROOT_ADMIN_SET):
            log.info("✓ Detectado: Opción 'Menú de admin' desde menú raíz")
            if not aclubs:
                send_text(waid, "❌ No tienes permisos de administrador.")
                send_root_menu(waid)
                return jsonify({"status": "ok"})
            if len(aclubs) == 1:
                cid = aclubs[0]
                set_session(waid, mode="admin", club=cid, awaiting=None)
                send_admin_menu(_CTX[cid], waid)
                return jsonify({"status": "ok"})
            set_session(waid, mode="admin_pick", awaiting="pick_admin_club")
            title_pick, opts_pick, button_pick = admin_club_picker_parts(aclubs)
            send_list_menu(waid, title_pick, opts_pick, button_pick)
            return jsonify({"status": "ok"})

        # 3) Estado
        if _is_choice(body_raw_clean, ROOT_STATUS_SET):
            log.info("✓ Detectado: Opción 'Mi estado de rol' desde menú raíz")
            cid = s.get("club") or infer_user_club(waid)
            if cid and cid in _CTX and len(mclubs) <= 1:
                send_text(waid, who_am_i_summary(_CTX[cid], waid))
            else:
                if mclubs:
                    lines = [who_am_i_summary(_CTX[c], waid) for c in mclubs]
                    send_text(waid, "\n".join(lines))
                else:
                    if cid and cid in _CTX:
                        send_text(waid, who_am_i_summary(_CTX[cid], waid))
                    else:
                        send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
            send_root_menu(waid)
            return jsonify({"status": "ok"})

        # 4) Detección de opciones de menú de socio desde root (ahora directas si solo tiene un club)
        if (_is_choice(body_raw_clean, MEM_ROLE_SET) or _is_choice(body_raw_clean, MEM_STATUS_SET) or 
            _is_choice(body_raw_clean, MEM_SPEECH_SET) or _is_choice(body_raw_clean, MEM_EDUCATION_SET) or
            _is_choice(body_raw_clean, MEM_CANCEL_SPEECH_SET) or _is_choice(body_raw_clean, MEM_CANCEL_SECTION_SET) or
            _is_choice(body_raw_clean, MEM_LEAVE_ROLE_SET)):
            log.info("✓ Detectado: Opción de socio desde menú raíz")
            if len(mclubs) == 1:
                # Usuario con un solo club: procesar la acción directamente
                cid = mclubs[0]
                set_session(waid, mode="root", club=cid)  # Mantener en modo root para simplicidad
                ctx_member = _CTX[cid]
                
                # Redirigir según la opción seleccionada al handler de menú de miembro
                # Esto lo procesará la sección "if s.get("mode") == "member"" más abajo
                set_session(waid, mode="member", club=cid, awaiting=None)
                # Re-procesar el mensaje en modo member
                s = get_session(waid)
                # Continuar al procesamiento del menú de miembro (no hacer return aquí)
            else:
                # Múltiples clubs - mostrar picker
                set_session(waid, mode="member_pick", awaiting="pick_member_club", club=None, buffer=None)
                title_pick, opts_pick, button_pick = member_club_picker_parts(mclubs)
                send_list_menu(waid, title_pick, opts_pick, button_pick)
                return jsonify({"status": "ok"})

        # Fallback numérico original (por si usas menús enumerados)
        _, root_options, _ = _root_menu_parts(waid)
        selected_idx = None
        if is_number:
            try:
                num = int(body_norm)
                if 1 <= num <= len(root_options):
                    selected_idx = num - 1
            except ValueError:
                selected_idx = None

        if selected_idx is not None:
            opt = root_options[selected_idx]
            if matches_option(opt[0], opt):  # no-op; mantiene compatibilidad
                # Reinvoca por etiqueta para reutilizar la lógica de arriba
                fake_raw = opt[0]
                return _process_message_router(waid, fake_raw, norm(fake_raw), True, "synthetic")

    # --------- Picker de club (miembro) -----------------------------------------------
    if s.get("mode") == "member_pick" and s.get("awaiting") == "pick_member_club":
        if not mclubs:
            set_session(waid, mode="root", awaiting=None, buffer=None, club=None)
            send_root_menu(waid)
            return None
        title_pick, opts_pick, button_pick = member_club_picker_parts(mclubs)
        numeric_choice = None
        if is_number:
            try:
                num = int(body_norm)
                if 1 <= num <= len(mclubs):
                    numeric_choice = num - 1
            except ValueError:
                numeric_choice = None
        if _is_choice(body_raw_clean, BACK_SET) or body_norm == "9":
            set_session(waid, mode="root", awaiting=None, buffer=None, club=None)
            send_root_menu(waid)
            return None
        for idx_opt, cid in enumerate(mclubs):
            if matches_option(body_raw_clean, opts_pick[idx_opt]) or numeric_choice == idx_opt:
                set_session(waid, mode="member", club=cid, awaiting=None, buffer=None)
                send_member_menu(_CTX[cid], waid)
                return None
        send_list_menu(waid, title_pick, opts_pick, button_pick)
        return None

    # --------- Picker de club (admin) -------------------------------------------------
    if s.get("mode") == "admin_pick" and s.get("awaiting") == "pick_admin_club":
        if not aclubs:
            set_session(waid, mode="root", awaiting=None, buffer=None)
            send_root_menu(waid)
            return None
        title_pick, opts_pick, button_pick = admin_club_picker_parts(aclubs)
        numeric_choice = None
        if is_number:
            try:
                num = int(body_norm)
                if 1 <= num <= len(aclubs):
                    numeric_choice = num - 1
            except ValueError:
                numeric_choice = None
        if _is_choice(body_raw_clean, BACK_SET) or body_norm == "9":
            set_session(waid, mode="root", awaiting=None, buffer=None)
            send_root_menu(waid)
            return None
        for idx_opt, cid in enumerate(aclubs):
            if matches_option(body_raw_clean, opts_pick[idx_opt]) or numeric_choice == idx_opt:
                set_session(waid, mode="admin", club=cid, awaiting=None)
                send_admin_menu(_CTX[cid], waid)
                return None
        send_list_menu(waid, title_pick, opts_pick, button_pick)
        return None

    # --------- Menú de miembro --------------------------------------------------------
    if s.get("mode") == "member" and current_cid and current_cid in _CTX:
        log.info("✓ Entrando en modo 'member' para club: %s", current_cid)
        ctx_member = _CTX[current_cid]
        _, member_options, _ = _member_menu_parts(ctx_member, waid)
        log.info("Opciones del menú de miembro: %s", [opt[0] if isinstance(opt, tuple) else opt for opt in member_options])
        log.info("Mensaje recibido: '%s'", body_raw_clean)

        numeric_choice = None
        if is_number:
            try:
                num = int(body_norm)
                if num == 9:
                    numeric_choice = len(member_options) - 1
                elif 1 <= num <= len(member_options):
                    numeric_choice = num - 1
            except ValueError:
                numeric_choice = None

        # 1) 🎯 Mi cargo
        match_1 = matches_option(body_raw_clean, member_options[0]) if member_options else False
        log.info("¿Match opción 1? %s (numeric: %s)", match_1, numeric_choice == 0)
        if member_options and (match_1 or numeric_choice == 0):
            log.info("✓ Detectado: Opción 1 - Mi cargo")
            send_text(waid, who_am_i(ctx_member, waid))
            send_member_menu(ctx_member, waid)
            return jsonify({"status": "ok"})

        # 2) 📊 Estado de la ronda
        match_2 = matches_option(body_raw_clean, member_options[1]) if len(member_options) > 1 else False
        log.info("¿Match opción 2? %s (numeric: %s)", match_2, numeric_choice == 1)
        if len(member_options) > 1 and (match_2 or numeric_choice == 1):
            log.info("✓ Detectado: Opción 2 - Estado de la ronda")
            send_text(waid, status_text(ctx_member))
            send_member_menu(ctx_member, waid)
            return jsonify({"status": "ok"})

        # 3) 🎤 Quiero dar un discurso preparado
        match_3 = matches_option(body_raw_clean, member_options[2]) if len(member_options) > 2 else False
        log.info("¿Match opción 3? %s (numeric: %s)", match_3, numeric_choice == 2)
        if len(member_options) > 2 and (match_3 or numeric_choice == 2):
            log.info("✓ Detectado: Opción 3 - Discurso preparado")
            st = ctx_member.state_store.load()
            can_add, error_msg = _can_add_speech(st, waid)
            if not can_add:
                if error_msg == "TOASTMASTER_CONFLICT":
                    # El socio es Toastmaster, preguntar si quiere ceder el rol
                    replacement = _find_toastmaster_replacement(ctx_member, waid)
                    if replacement:
                        # Enviar primero el mensaje explicativo
                        explanation = (
                            "⚠️ No puedes dar un discurso preparado siendo Toastmaster de la noche, "
                            "ya que no puedes presentarte a ti mismo.\n\n"
                            f"📢 Encontramos a {pretty_name(ctx_member, replacement)} disponible para reemplazarte."
                        )
                        send_text(waid, explanation)
                        
                        # Configurar sesión
                        set_session(waid, awaiting="toastmaster_handoff_speech", buffer={
                            "waid": waid,
                            "club": ctx_member.club_id,
                            "round": st["round"],
                            "replacement": replacement,
                            "action": "speech"
                        })
                        
                        # Enviar menú con la pregunta
                        options = [
                            ("✅ Sí, ceder el cargo", "Transferir rol"),
                            ("❌ No, quiero conservarlo, sí quiero ser Toastmaster", "Conservar rol")
                        ]
                        send_list_menu(waid, "❓ ¿Quieres ceder el cargo a otro socio?", options, "Responder")
                    else:
                        msg = (
                            "⚠️ No puedes dar un discurso preparado siendo Toastmaster de la noche, "
                            "ya que no puedes presentarte a ti mismo.\n\n"
                            "❌ Lamentablemente, no hay socios disponibles para reemplazarte en este momento. Contacta al vicepresidente educativo para solucionar este inconveniente"
                        )
                        send_text(waid, msg)
                        send_member_menu(ctx_member, waid)
                else:
                    send_text(waid, error_msg)
                    send_member_menu(ctx_member, waid)
                return jsonify({"status": "ok"})
            
            # Iniciar flujo de captura de discurso preparado
            set_session(waid, awaiting="speech_step1_pathway", buffer={"waid": waid, "club": ctx_member.club_id, "round": st["round"]})
            pathways = [
                "Liderazgo dinámico",
                "Cautiva con humor",
                "Asesoría eficaz",
                "Influencia persuasiva",
                "Dominio de las presentaciones",
                "Comunicación visionaria"
            ]
            send_list_menu(waid, "📚 Selecciona tu Pathway:", pathways, "Seleccionar pathway")
            return jsonify({"status": "ok"})

        # 4) 📚 Quiero dar una Sección Educativa
        match_4 = matches_option(body_raw_clean, member_options[3]) if len(member_options) > 3 else False
        log.info("¿Match opción 4? %s (numeric: %s)", match_4, numeric_choice == 3)
        if len(member_options) > 3 and (match_4 or numeric_choice == 3):
            log.info("✓ Detectado: Opción 4 - Sección Educativa")
            st = ctx_member.state_store.load()
            can_add, error_msg = _can_add_section(st, waid)
            if not can_add:
                if error_msg == "TOASTMASTER_CONFLICT":
                    # El socio es Toastmaster, preguntar si quiere ceder el rol
                    replacement = _find_toastmaster_replacement(ctx_member, waid)
                    if replacement:
                        # Enviar primero el mensaje explicativo
                        explanation = (
                            "⚠️ No puedes dar una sección educativa siendo Toastmaster de la noche, "
                            "ya que no puedes presentarte a ti mismo.\n\n"
                            f"📢 Encontramos a {pretty_name(ctx_member, replacement)} disponible para reemplazarte."
                        )
                        send_text(waid, explanation)
                        
                        # Configurar sesión
                        set_session(waid, awaiting="toastmaster_handoff_section", buffer={
                            "waid": waid,
                            "club": ctx_member.club_id,
                            "round": st["round"],
                            "replacement": replacement,
                            "action": "section"
                        })
                        
                        # Enviar menú con la pregunta
                        options = [
                            ("✅ Sí, ceder el cargo", "Transferir rol"),
                            ("❌ No, quiero conservarlo, sí quiero ser Toastmaster", "Conservar rol")
                        ]
                        send_list_menu(waid, "❓ ¿Quieres ceder el cargo a otro socio?", options, "Responder")
                    else:
                        msg = (
                            "⚠️ No puedes dar una sección educativa siendo Toastmaster de la noche, "
                            "ya que no puedes presentarte a ti mismo.\n\n"
                            "❌ Lamentablemente, no hay socios disponibles para reemplazarte en este momento."
                        )
                        send_text(waid, msg)
                        send_member_menu(ctx_member, waid)
                else:
                    send_text(waid, error_msg)
                    send_member_menu(ctx_member, waid)
                return jsonify({"status": "ok"})
            
            # Iniciar flujo de captura de sección educativa
            set_session(waid, awaiting="section_step1_serie", buffer={"waid": waid, "club": ctx_member.club_id, "round": st["round"]})
            series = [
                ("🏆 Serie del mejor orador", "Técnicas de oratoria"),
                ("🎖️ Club exitoso", "Gestión de clubes"),
                ("💼 Liderazgo", "Habilidades de liderazgo"),
                ("💡 Tema libre", "Cualquier tema educativo")
            ]
            send_list_menu(waid, "📚 Selecciona el tipo de serie educativa:", series, "Seleccionar serie")
            return jsonify({"status": "ok"})

        # Verificar si quiere cancelar su discurso
        if matches_option(body_raw_clean, ("❌ Cancelar mi discurso", "Cancelar mi discurso")) or "cancelar mi discurso" in body_norm or "cancelar discurso" in body_norm:
            log.info("✓ Detectado: Cancelar mi discurso")
            st = ctx_member.state_store.load()
            
            # Buscar el discurso del socio
            speech_to_cancel = None
            for speech in st.get("prepared_speeches", []):
                if speech.get("waid") == waid:
                    speech_to_cancel = speech
                    break
            
            if speech_to_cancel:
                # Confirmar la acción
                set_session(waid, awaiting="confirm_cancel_speech", buffer={"club": ctx_member.club_id, "speech_id": speech_to_cancel.get("id")})
                confirm_options = [
                    ("✅ Sí, cancelar discurso", "Confirmar cancelación"),
                    ("❌ No, mantener discurso", "Mantener")
                ]
                send_text(waid, f"⚠️ Estás a punto de cancelar tu discurso *{speech_to_cancel.get('titulo', 'Sin título')}*.\n\n¿Estás seguro?")
                send_list_menu(waid, "Confirma tu decisión:", confirm_options, "Responder")
            else:
                send_text(waid, "_❌ No tienes ningún discurso registrado actualmente._")
                send_member_menu(ctx_member, waid)
            return jsonify({"status": "ok"})
        
        # Verificar si quiere cancelar su educativa
        if matches_option(body_raw_clean, ("❌ Cancelar mi educativa", "Cancelar mi educativa")) or "cancelar mi educativa" in body_norm or "cancelar educativa" in body_norm:
            log.info("✓ Detectado: Cancelar mi educativa")
            st = ctx_member.state_store.load()
            
            # Buscar la educativa del socio
            section_to_cancel = None
            for section in st.get("educational_sections", []):
                if section.get("waid") == waid:
                    section_to_cancel = section
                    break
            
            if section_to_cancel:
                # Confirmar la acción
                set_session(waid, awaiting="confirm_cancel_section", buffer={"club": ctx_member.club_id, "section_waid": section_to_cancel.get("waid")})
                confirm_options = [
                    ("✅ Sí, cancelar educativa", "Confirmar cancelación"),
                    ("❌ No, mantener educativa", "Mantener")
                ]
                send_text(waid, f"⚠️ Estás a punto de cancelar tu sección educativa *{section_to_cancel.get('nombre_seccion', 'Sin nombre')}*.\n\n¿Estás seguro?")
                send_list_menu(waid, "Confirma tu decisión:", confirm_options, "Responder")
            else:
                send_text(waid, "_❌ No tienes ninguna sección educativa registrada actualmente._")
                send_member_menu(ctx_member, waid)
            return jsonify({"status": "ok"})
        
        # Verificar si existe la opción "Dejar mi cargo" (solo si el socio tiene rol confirmado)
        leave_role_option = None
        for i, opt in enumerate(member_options):
            if matches_option(body_raw_clean, ("❌ Dejar mi cargo", "Renunciar al cargo asignado")) or "dejar mi cargo" in body_norm or "renunciar" in body_norm:
                if opt[0] == "❌ Dejar mi cargo":
                    leave_role_option = i
                    break
        
        if leave_role_option is not None:
            log.info("✓ Detectado: Dejar mi cargo")
            st = ctx_member.state_store.load()
            
            # Buscar el cargo que tiene el socio
            role_to_remove = None
            for role, info in st.get("accepted", {}).items():
                if info.get("waid") == waid:
                    role_to_remove = role
                    break
            
            if role_to_remove:
                # Confirmar la acción
                set_session(waid, awaiting="confirm_leave_role", buffer={"club": ctx_member.club_id, "role": role_to_remove})
                confirm_options = [
                    ("✅ Sí, dejar el cargo", "Confirmar"),
                    ("❌ No, mantener el cargo", "Cancelar")
                ]
                send_text(waid, f"⚠️ Estás a punto de dejar el cargo de *{role_to_remove}*.\n\n¿Estás seguro?")
                send_list_menu(waid, "Confirma tu decisión:", confirm_options, "Responder")
            else:
                send_text(waid, "❌ No tienes ningún cargo asignado actualmente.")
                send_member_menu(ctx_member, waid)
            return jsonify({"status": "ok"})

        # 5) 🔙 Volver
        match_5 = matches_option(body_raw_clean, member_options[-1]) if member_options else False
        log.info("¿Match opción 5 (Volver)? %s (numeric: %s == %s)", match_5, numeric_choice, len(member_options) - 1)
        if match_5 or body_norm == "9" or numeric_choice == len(member_options) - 1:
            log.info("✓ Detectado: Opción 5 - Volver")
            set_session(waid, mode="root", awaiting=None, buffer=None)
            send_root_menu(waid)
            return jsonify({"status": "ok"})

        # Si está en modo member pero no matcheó ninguna opción, mostrar advertencia y quedarse en el menú
        log.warning("⚠️  En modo member pero mensaje no reconocido: '%s'", body_raw[:100])
        send_member_menu(ctx_member, waid)
        return jsonify({"status": "ok"})
            # return None


    # --------- Menú admin -------------------------------------------------------------
    if s.get("mode") == "admin" and current_cid and current_cid in _CTX:
        ctx_admin = _CTX[current_cid]
        _, admin_options, _ = _admin_menu_parts(ctx_admin)

        # Despacho por etiqueta
        if matches_option(body_raw_clean, admin_options[0]) or norm(body_raw_clean) == norm("▶️ Iniciar ronda"):
            msg = start_new_round(ctx_admin, pretty_name(ctx_admin, waid))
            send_text(waid, msg)
            if not has_pending_invite(ctx_admin, waid) and get_session(waid).get("awaiting") is None:
                send_admin_menu(ctx_admin, waid)
            return None

        if matches_option(body_raw_clean, admin_options[1]) or norm(body_raw_clean) == norm("📊 Ver estado"):
            send_text(waid, status_text(ctx_admin))
            send_admin_menu(ctx_admin, waid)
            return None

        if matches_option(body_raw_clean, admin_options[2]) or norm(body_raw_clean) == norm("🛑 Cancelar ronda"):
            send_text(waid, cancel_round(ctx_admin, pretty_name(ctx_admin, waid)))
            send_admin_menu(ctx_admin, waid)
            return None

        if matches_option(body_raw_clean, admin_options[3]) or norm(body_raw_clean) == norm("♻️ Resetear estado"):
            send_text(waid, reset_all(ctx_admin, pretty_name(ctx_admin, waid)))
            send_admin_menu(ctx_admin, waid)
            return None

        if matches_option(body_raw_clean, admin_options[4]) or norm(body_raw_clean) == norm("👥 Ver miembros"):
            send_text(waid, admin_list_members(ctx_admin))
            send_admin_menu(ctx_admin, waid)
            return None

        if matches_option(body_raw_clean, admin_options[5]) or norm(body_raw_clean) == norm("➕ Agregar miembro"):
            set_session(waid, awaiting="admin_add_member", buffer=None)
            send_text(waid, "✍️ Envía: Nombre, 55XXXXXXXX")
            return None

        if matches_option(body_raw_clean, admin_options[6]) or norm(body_raw_clean) == norm("➖ Eliminar miembro"):
            set_session(waid, awaiting="admin_remove_member", buffer=None)
            send_text(waid, "✍️ Envía el número de 10 dígitos o el nombre exacto a eliminar")
            return None

        if matches_option(body_raw_clean, admin_options[7]) or norm(body_raw_clean) == norm("🔁 Cambiar de club"):
            aclubs_more = admin_clubs(waid)
            if len(aclubs_more) > 1:
                set_session(waid, mode="admin_pick", awaiting="pick_admin_club")
                title_pick, opts_pick, button_pick = admin_club_picker_parts(aclubs_more)
                send_list_menu(waid, title_pick, opts_pick, button_pick)
                return None
            send_admin_menu(ctx_admin, waid)
            return None

        if _is_choice(body_raw_clean, BACK_SET) or matches_option(body_raw_clean, admin_options[-1]):
            set_session(waid, mode="root", awaiting=None, buffer=None)
            send_root_menu(waid)
            return None

        # Fallback numérico
        if is_number:
            try:
                num = int(body_norm)
                if 1 <= num <= len(admin_options):
                    # Reinvoca por etiqueta correspondiente
                    fake_raw = admin_options[num - 1][0]
                    return _process_message_router(waid, fake_raw, norm(fake_raw), True, "synthetic")
            except ValueError:
                pass

    # --------- Comandos atajos ---------------------------------------------------------
    if body_norm in ("mi cargo", "mi cargo?", "whoami"):
        cid = infer_user_club(waid, extract_trailing_club_id(body_raw))
        if cid and cid in _CTX:
            send_text(waid, who_am_i(_CTX[cid], waid))
        else:
            send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
        send_root_menu(waid)
        return jsonify({"status": "ok"})

    # Comando de confirmación de asistencia
    if body_norm in ("confirmo", "confirm", "si", "asistire", "asisto"):
        cid = infer_user_club(waid, extract_trailing_club_id(body_raw))
        if cid and cid in _CTX:
            confirmation_ctx = _CTX[cid]
            st = confirmation_ctx.state_store.load()
            confirmations = st.get("confirmations", {})
            
            if waid in confirmations and not confirmations[waid].get("confirmed"):
                # Marcar como confirmado
                confirmations[waid]["confirmed"] = True
                confirmations[waid]["timestamp"] = time.time()
                st["confirmations"] = confirmations
                confirmation_ctx.state_store.save(st)
                
                role = confirmations[waid].get("role", "tu cargo")
                send_text(waid, f"✅ Confirmación recibida para *{role}*. ¡Gracias!\n\nNos vemos mañana en la sesión.")
                
                # Verificar si todos confirmaron
                all_confirmed = all(c.get("confirmed") for c in confirmations.values())
                if all_confirmed:
                    broadcast_text(confirmation_ctx.admins, f"[{cid}] ✅ Todos los socios confirmaron su asistencia. Generando programa...")
                    _generate_and_send_pdf(confirmation_ctx, st.get("round", 0))
                else:
                    pending = sum(1 for c in confirmations.values() if not c.get("confirmed"))
                    broadcast_text(confirmation_ctx.admins, f"[{cid}] ✅ {pretty_name(confirmation_ctx, waid)} confirmó. Pendientes: {pending}")
                
                return jsonify({"status": "ok"})
            else:
                send_text(waid, "ℹ️ No tienes ninguna confirmación pendiente en este momento.")
                return jsonify({"status": "ok"})
        else:
            send_text(waid, "No se pudo determinar tu club.")
            return jsonify({"status": "ok"})

    # Comando de prueba para enviar confirmaciones (solo admins)
    if body_norm in ("test confirmaciones", "enviar confirmaciones"):
        acls = admin_clubs(waid)
        if acls:
            cid = acls[0] if len(acls) == 1 else infer_user_club(waid, extract_trailing_club_id(body_raw))
            if cid and cid in _CTX:
                test_ctx = _CTX[cid]
                st = test_ctx.state_store.load()
                round_no = st.get("round", 0)
                
                if not st.get("accepted"):
                    send_text(waid, "⚠️ No hay cargos aceptados para enviar confirmaciones.")
                    return jsonify({"status": "ok"})
                
                send_text(waid, f"📨 Enviando confirmaciones de prueba para ronda #{round_no}...")
                _send_monday_confirmations(test_ctx, round_no)
                return jsonify({"status": "ok"})
        send_text(waid, "❌ No tienes permisos de administrador.")
        return jsonify({"status": "ok"})
    
    # Comando de prueba para generar PDF (solo admins)
    if body_norm in ("test pdf", "generar pdf"):
        acls = admin_clubs(waid)
        if acls:
            cid = acls[0] if len(acls) == 1 else infer_user_club(waid, extract_trailing_club_id(body_raw))
            if cid and cid in _CTX:
                test_ctx = _CTX[cid]
                st = test_ctx.state_store.load()
                round_no = st.get("round", 0)
                
                send_text(waid, f"📄 Generando PDF de prueba para ronda #{round_no}...")
                _generate_and_send_pdf(test_ctx, round_no)
                return jsonify({"status": "ok"})
        send_text(waid, "❌ No tienes permisos de administrador.")
        return jsonify({"status": "ok"})


    if body_norm in ("acepto", "accept") and ctx:
        send_text(waid, handle_accept(ctx, waid))
        send_root_menu(waid)
        return jsonify({"status": "ok"})

    if body_norm in ("rechazo", "reject") and ctx:
        send_text(waid, handle_reject(ctx, waid))
        send_root_menu(waid)
        return jsonify({"status": "ok"})

    # Default: re-pinta menú raíz
    log.warning("⚠️  Mensaje no reconocido - reenviando menú raíz: '%s'", body_raw[:100])
    send_root_menu(waid)
    return jsonify({"status": "ok"})

# ======================================================================================
# 6) Flask app (endpoints y webhook)
# ======================================================================================

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    info = {}
    for cid, ctx in _CTX.items():
        info[cid] = {"members": len(ctx.members_index), "roles": [r.name for r in ctx.club.roles]}
    return {"ok": True, "app": CFG.app_name, "clubs": info}


@app.route("/webhook", methods=["GET"])
def webhook_get():
    challenge = request.args.get("hub.challenge")
    if challenge:
        return challenge, 200
    return "OK", 200


def extract_trailing_club_id(text: str) -> Optional[str]:
    t = text.strip()
    m = re.search(r"\[([^\]]+)\]\s*$", t)
    if m:
        last = m.group(1).strip()
        return last if last in _CTX else None
    parts = t.split()
    if len(parts) >= 2:
        last = parts[-1].strip()
        if last in _CTX:
            return last
    return None


def strip_trailing_club(text: str, cid: str) -> str:
    t = text.strip()
    t = re.sub(rf"\s*\[\s*{re.escape(cid)}\s*\]\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(rf"\s+{re.escape(cid)}\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def infer_user_club(waid: str, explicit_cid: Optional[str] = None) -> Optional[str]:
    if explicit_cid and explicit_cid in _CTX:
        return explicit_cid
    mclubs = member_clubs(waid)
    if len(mclubs) == 1:
        return mclubs[0]
    if len(mclubs) > 1:
        return None
    candidates = []
    for cid, ctx in _CTX.items():
        st = ctx.state_store.load()
        for info in st.get("pending", {}).values():
            if info.get("candidate") == waid and not info.get("accepted"):
                candidates.append(cid); break
        for info in st.get("accepted", {}).values():
            if info.get("waid") == waid:
                candidates.append(cid); break
    if len(candidates) == 1:
        return candidates[0]
    return None


def has_pending_invite(ctx: Ctx, waid: str) -> Optional[str]:
    st = ctx.state_store.load()
    for role, info in st.get("pending", {}).items():
        if info.get("candidate") == waid and not info.get("accepted"):
            return role
    return None


def _extract_incoming_text(msg: dict) -> str:
    t = (msg.get("type") or "").lower()

    if t == "text":
        body = (msg.get("text") or {}).get("body")
        return body.strip() if isinstance(body, str) else ""

    if t in ("button", "reply", "quick_reply"):
        container = msg.get("reply") or msg.get("button") or {}
        if isinstance(container, str):
            try:
                container = json.loads(container)
            except Exception:
                return container.strip()
        if isinstance(container, dict):
            v = (
                container.get("postbackText")
                or container.get("payload")
                or container.get("postback")
                or container.get("id")
                or container.get("title")
                or container.get("text")
            )
            return v.strip() if isinstance(v, str) else ""
        return ""

    if t == "interactive":
        inter = msg.get("interactive") or {}
        if isinstance(inter, dict):
            lr = inter.get("list_reply")
            if isinstance(lr, dict):
                v = lr.get("postbackText") or lr.get("title") or lr.get("id")
                return v.strip() if isinstance(v, str) else ""
            br = inter.get("button_reply") or inter.get("reply")
            if isinstance(br, dict):
                v = br.get("postbackText") or br.get("title") or br.get("text") or br.get("id")
                return v.strip() if isinstance(v, str) else ""
        return ""


    return ""


def _is_interactive_reply(msg: dict) -> bool:
    t = (msg.get("type") or "").lower()
    if t in ("button", "reply", "quick_reply", "interactive"):
        return True
    inter = msg.get("interactive")
    return isinstance(inter, dict)


@app.route("/webhook", methods=["POST"])
def webhook_post():
    # Verificar timeouts de solicitudes de evaluador
    try:
        _check_evaluator_timeouts()
    except Exception as e:
        log.exception("Error al verificar timeouts de evaluador: %s", e)
    
    data = request.get_json(force=True, silent=True) or {}
    try:
        if _is_gupshup_event(data):
            payload = data["payload"]
            waid = (payload.get("sender") or {}).get("phone") or payload.get("source") or ""
            if not waid or str(waid) == str(CFG.source):
                return jsonify({"status": "ok"})
            body_raw = _extract_gupshup_text(payload)
            if not body_raw:
                return jsonify({"status": "ok"})
            body = norm(body_raw)
            msg_type = (payload.get("type") or "").lower()
            resp = _process_message_router(waid, body_raw, body, _is_gupshup_interactive(payload), msg_type)
            if resp is not None:
                return resp
            return jsonify({"status": "ok"})

        value = (
            (data.get("entry") or [{}])[0]
            .get("changes", [{}])[0]
            .get("value", {})
        )
        for msg in value.get("messages", []):
            waid = msg.get("from", "")
            if str(waid) == str(CFG.source) or not waid:
                continue
            msg_type = (msg.get("type") or "").lower()
            is_interactive = _is_interactive_reply(msg)
            body_raw = _extract_incoming_text(msg)
            if not body_raw:
                continue
            body = norm(body_raw)
            resp = _process_message_router(waid, body_raw, body, is_interactive, msg_type)
            if resp is not None:
                return resp
            continue

    except Exception:
        log.exception("Error procesando webhook; payload=%s", data)

    return jsonify({"status": "ok"})

# ======================================================================================
# 7) Main
# ======================================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, debug=False)
