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

import json
import logging
import os
import random
import re
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response
from requests.exceptions import RequestException

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
ROOT_STATUS_SET = _set_norm(["📌 Mi estado de rol", "Mi estado de rol"])
BACK_SET        = _set_norm(["🔙 Volver", "Volver"])
MEM_ROLE_SET    = _set_norm(["🎯 Mi rol", "Mi rol"])
MEM_STATUS_SET  = _set_norm(["📊 Estado de la ronda", "Estado de la ronda"])
MEM_SPEECH_SET  = _set_norm(["🎤 Quiero dar un discurso preparado", "Quiero dar un discurso preparado", "Discurso preparado"])
MEM_EDUCATION_SET = _set_norm(["📚 Quiero dar una Sección Educativa", "Quiero dar una Sección Educativa", "Sección Educativa"])

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
    in_pending = any(d["candidate"] == target.waid and not d.get("accepted") for d in st.get("pending", {}).values())
    in_accepted = any(v["waid"] == target.waid for v in st.get("accepted", {}).values())
    if in_pending or in_accepted:
        return "No se puede eliminar ahora: tiene un rol pendiente o aceptado en esta ronda."

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
        
        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}
        invited_in_this_batch.add(cand)

    ctx.state_store.save(st)

    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] ▶️ Iniciamos la ronda #{st['round']}.")

    for role, info in st["pending"].items():
        cand = info["candidate"]
        begin_invite_flow(ctx, cand, role, st["round"])

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
        if info["candidate"] == waid and not info["accepted"]:
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
        if info.get("candidate") == waid and not info.get("accepted"):
            info["declined_by"].append(waid)

            excluded = set(info["declined_by"])
            excluded.update(a["waid"] for a in st.get("accepted", {}).values())
            excluded.update(pending_candidates(st, exclude_role=role))

            cand = choose_candidate_hier(ctx, role, excluded)
            if cand:
                info["candidate"] = cand
                ctx.state_store.save(st)
                begin_invite_flow(ctx, cand, role, st["round"])
                return f"↪️ Rechazado por {pretty_name(ctx, waid)}. Nuevo candidato: {pretty_name(ctx, cand)}."
            else:
                del st["pending"][role]
                ctx.state_store.save(st)
                broadcast_text(ctx.admins, f"[{ctx.club_id}] No hay más opciones para el rol: {role}.")
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
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] {summary}")


def who_am_i(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            title, options, _ = invite_menu_parts(ctx, role, st["round"])
            return _build_menu_text(title, options)
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"✅ Confirmaste el rol {role} en la ronda #{st['round']} ({ctx.club_id})."
    return "➖ No tienes roles asignados ni pendientes."


def who_am_i_summary(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            return (
                f"[{ctx.club_id}] 🔔 Tienes una invitación pendiente: {role} en la ronda #{st['round']} ({ctx.club_id}).\n"
                f"👉 Ve al Menú de socio ({ctx.club_id}) → «🎯 Mi rol» para aceptar o rechazar."
            )
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"[{ctx.club_id}] ✅ Confirmaste el rol {role} en la ronda #{st['round']} ({ctx.club_id})."
    return f"[{ctx.club_id}] ➖ No tienes roles asignados ni pendientes."


def status_text(ctx: Ctx) -> str:
    st = ctx.state_store.load()
    summary_lines = make_summary(ctx, st).splitlines()
    lines = summary_lines + ["", "⏳ Pendientes por confirmar:"]
    any_pending = False
    for role, info in st["pending"].items():
        if not info["accepted"]:
            any_pending = True
            cand = info["candidate"]
            lines.append(f"- {role}: propuesto a {pretty_name(ctx, cand)} (rechazos: {len(info['declined_by'])})")
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
    header = "Asistente de asignación de roles: Menú principal. Elija una opción"
    if mclubs:
        desc = f"Club único: {mclubs[0]}" if len(mclubs) == 1 else "Elegir club"
        options.append(("👤 Menú de socio", desc))
    if aclubs:
        desc = f"Club único: {aclubs[0]}" if len(aclubs) == 1 else "Elegir club"
        options.append(("🛠️ Menú de admin", desc))
    options.append(("📌 Mi estado de rol", "Invitación o confirmación"))
    return header, options, "Menú principal"


def render_root_menu(waid: str) -> str:
    title, options, _ = _root_menu_parts(waid)
    return _build_menu_text(title, options)


def send_root_menu(waid: str) -> dict:
    title, options, button = _root_menu_parts(waid)
    return send_list_menu(waid, title, options, button)


def _member_menu_parts(ctx: Ctx) -> Tuple[str, List[Tuple[str, str]], str]:
    title = f"Asistente de asignación de roles: Menú de socio [{ctx.club_id}]. Elija una opción"
    options: List[Tuple[str, str]] = [
        ("🎯 Mi rol", "Pendiente o confirmado"),
        ("📊 Estado de la ronda", "Resumen y pendientes"),
        ("🎤 Quiero dar un discurso preparado", "Registrar discurso de pathway"),
        ("📚 Quiero dar una Sección Educativa", "Registrar sección educativa"),
        ("🔙 Volver", "Regresar al menú principal"),
    ]
    return title, options, "Menú de socio"

def render_member_menu(ctx: Ctx) -> str:
    title, options, _ = _member_menu_parts(ctx)
    return _build_menu_text(title, options)


def send_member_menu(ctx: Ctx, waid: str) -> dict:
    title, options, button = _member_menu_parts(ctx)
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
    title = f"Asistente de asignación de roles: Menú admin [{ctx.club_id}]. Elija una opción"
    options: List[Tuple[str, str]] = [
        ("▶️ Iniciar ronda", "Proponer candidatos por rol"),
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


def invite_menu_parts(ctx: Ctx, role: str, round_no: int) -> Tuple[str, List[Tuple[str, str]], str]:
    title = (
        f"🔔 Invitación: {role} en la reunión #{round_no} ({ctx.club_id}).\n"
        "Elija una opción para responder."
    )
    options: List[Tuple[str, str]] = [
        ("✅ Aceptar", "Confirmar rol"),
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


def _can_add_speech(st: dict, waid: str) -> Tuple[bool, str]:
    """
    Valida si se puede agregar un discurso preparado.
    Retorna (puede_agregar, mensaje_error)
    """
    speeches, sections = _get_speeches_and_sections(st)
    
    # 1. Verificar si el socio ya tiene discurso
    if any(s.get("waid") == waid for s in speeches):
        return False, "❌ Ya tienes un discurso preparado registrado para esta sesión."
    
    # 2. Verificar si el socio ya tiene sección educativa
    if any(s.get("waid") == waid for s in sections):
        return False, "❌ No puedes dar un discurso preparado si ya tienes una sección educativa."
    
    # 3. Verificar límite de 3 discursos
    if len(speeches) >= 3:
        return False, "❌ Ya se alcanzó el límite de 3 discursos preparados para esta sesión."
    
    # 4. Verificar incompatibilidad: si hay sección educativa, solo 1 discurso
    if len(sections) > 0 and len(speeches) >= 1:
        return False, "❌ Solo se permite 1 discurso cuando hay una sección educativa en la sesión."
    
    return True, ""


def _can_add_section(st: dict, waid: str) -> Tuple[bool, str]:
    """
    Valida si se puede agregar una sección educativa.
    Retorna (puede_agregar, mensaje_error)
    """
    speeches, sections = _get_speeches_and_sections(st)
    
    # 1. Verificar si el socio ya tiene sección educativa
    if any(s.get("waid") == waid for s in sections):
        return False, "❌ Ya tienes una sección educativa registrada para esta sesión."
    
    # 2. Verificar si el socio ya tiene discurso
    if any(s.get("waid") == waid for s in speeches):
        return False, "❌ No puedes dar una sección educativa si ya tienes un discurso preparado."
    
    # 3. Solo puede haber 1 sección educativa
    if len(sections) >= 1:
        return False, "❌ Ya hay una sección educativa registrada para esta sesión."
    
    # 4. Verificar incompatibilidad: si hay 2+ discursos, no se permite sección
    if len(speeches) >= 2:
        return False, "❌ No se permite sección educativa cuando hay 2 o más discursos preparados."
    
    return True, ""


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
        current_cid_temp = s.get("club") or infer_user_club(waid)
        if current_cid_temp and current_cid_temp in _CTX:
            set_session(waid, mode="admin", club=current_cid_temp, awaiting=None)
            send_admin_menu(_CTX[current_cid_temp], waid)
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
        accept_option = ("✅ Aceptar", "Confirmar rol")
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
                send_text(waid, "📖 Envía la palabra del día:")
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
                send_text(waid, "❗Opción inválida. Usa los botones: ✅ Aceptar / ❌ Rechazar.")
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
        send_text(waid, "✍️ Envía el significado de la palabra:")
        return None

    if awaiting == "word_step2_significado":
        if is_interactive:
            send_text(waid, "Escribe el significado manualmente, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["significado"] = body_raw.strip()
        set_session(waid, awaiting="word_step3_ejemplo", buffer=buffer)
        send_text(waid, "💡 Envía un ejemplo de uso de la palabra:")
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

    # --------- Flujos Discurso Preparado ----------------------------------------------
    
    # Paso 1: Pathway
    if awaiting == "speech_step1_pathway":
        log.info("🎯 Step 1 - Pathway recibido: '%s' (interactive: %s, type: %s)", body_raw.strip(), is_interactive, msg_type)
        
        # Lista de pathways válidos
        valid_pathways = [
            "Dynamic Leadership",
            "Engaging Humor",
            "Motivational Strategies",
            "Persuasive Influence",
            "Presentation Mastery",
            "Visionary Communication"
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
        send_menu_with_quick_replies(waid, "📊 Selecciona el nivel de tu proyecto:", ["1", "2", "3", "4", "5"])
        return jsonify({"status": "ok"})
    
    # Paso 2: Nivel
    if awaiting == "speech_step2_nivel":
        if is_interactive:
            send_text(waid, "Por favor selecciona un nivel del 1 al 5 usando los botones.")
            return jsonify({"status": "ok"})
        if not is_number or body_norm not in ["1", "2", "3", "4", "5"]:
            send_text(waid, "❌ Nivel inválido. Envía un número del 1 al 5.")
            send_menu_with_quick_replies(waid, "📊 Selecciona el nivel:", ["1", "2", "3", "4", "5"])
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["nivel"] = int(body_norm)
        set_session(waid, awaiting="speech_step3_proyecto", buffer=buffer)
        send_text(waid, "📝 Envía el nombre de tu proyecto:\n\nEjemplo: 'Comunicación en Crisis'")
        return jsonify({"status": "ok"})
    
    # Paso 3: Nombre del proyecto
    if awaiting == "speech_step3_proyecto":
        if is_interactive:
            send_text(waid, "Escribe el nombre del proyecto con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["proyecto"] = body_raw.strip()
        set_session(waid, awaiting="speech_step4_titulo", buffer=buffer)
        send_text(waid, "📢 Envía el título de tu discurso:\n\nEjemplo: 'Cómo influir con integridad'")
        return jsonify({"status": "ok"})
    
    # Paso 4: Título del discurso
    if awaiting == "speech_step4_titulo":
        if is_interactive:
            send_text(waid, "Escribe el título del discurso con texto, sin usar botones.")
            return jsonify({"status": "ok"})
        buffer = s.get("buffer", {})
        buffer["titulo"] = body_raw.strip()
        set_session(waid, awaiting="speech_step5_duracion", buffer=buffer)
        send_text(waid, "⏱️ Envía la duración de tu discurso:\n\nFormato: tiempo_mínimo-tiempo_máximo\nEjemplo: 5-7")
        return jsonify({"status": "ok"})
    
    # Paso 5: Duración
    if awaiting == "speech_step5_duracion":
        if is_interactive:
            send_text(waid, "Escribe la duración con texto en formato min-max, sin usar botones.")
            return jsonify({"status": "ok"})
        duration_text = body_raw.strip()
        # Validar formato min-max
        if "-" not in duration_text:
            send_text(waid, "❌ Formato inválido. Usa el formato: tiempo_mínimo-tiempo_máximo\nEjemplo: 5-7")
            return jsonify({"status": "ok"})
        parts = duration_text.split("-")
        if len(parts) != 2:
            send_text(waid, "❌ Formato inválido. Usa el formato: tiempo_mínimo-tiempo_máximo\nEjemplo: 5-7")
            return jsonify({"status": "ok"})
        try:
            min_time = int(parts[0].strip())
            max_time = int(parts[1].strip())
            if min_time <= 0 or max_time <= 0 or min_time > max_time:
                raise ValueError()
        except ValueError:
            send_text(waid, "❌ Los tiempos deben ser números positivos válidos, con mínimo menor que máximo.")
            return None
        
        buffer = s.get("buffer", {})
        buffer["duracion_min"] = min_time
        buffer["duracion_max"] = max_time
        set_session(waid, awaiting="speech_step6_evaluador", buffer=buffer)
        send_text(waid, "👤 Envía el nombre de tu evaluador:\n\nEjemplo: María González")
        return None
    
    # Paso 6: Evaluador
    if awaiting == "speech_step6_evaluador":
        if is_interactive:
            send_text(waid, "Escribe el nombre del evaluador con texto, sin usar botones.")
            return None
        buffer = s.get("buffer", {})
        buffer["evaluador"] = body_raw.strip()
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
            f"¿Es correcta esta información?"
        )
        send_text(waid, resumen)
        send_menu_with_quick_replies(waid, "Confirma tu solicitud:", ["✅ Confirmar", "❌ Cancelar"])
        return None
    
    # Confirmación de discurso
    if awaiting == "speech_confirm":
        buffer = s.get("buffer", {})
        wants_confirm = matches_option(body_raw_clean, ("✅ Confirmar", "Confirmar")) or body_norm in ("1", "confirmar", "si", "sí", "ok")
        wants_cancel = matches_option(body_raw_clean, ("❌ Cancelar", "Cancelar")) or body_norm in ("2", "cancelar", "no")
        
        if wants_confirm:
            club_ctx = _CTX[buffer["club"]]
            
            # Revocar roles incompatibles
            revoked = _revoke_incompatible_roles(club_ctx, waid)
            
            # Guardar discurso
            st = club_ctx.state_store.load()
            if "prepared_speeches" not in st:
                st["prepared_speeches"] = []
            
            speech_data = {
                "waid": buffer["waid"],
                "nombre": pretty_name(club_ctx, buffer["waid"]),
                "pathway": buffer["pathway"],
                "nivel": buffer["nivel"],
                "proyecto": buffer["proyecto"],
                "titulo": buffer["titulo"],
                "duracion_min": buffer["duracion_min"],
                "duracion_max": buffer["duracion_max"],
                "evaluador": buffer["evaluador"],
                "round": buffer["round"]
            }
            st["prepared_speeches"].append(speech_data)
            club_ctx.state_store.save(st)
            
            msg = f"✅ Discurso preparado registrado exitosamente.\n\n📢 Título: '{buffer['titulo']}'"
            if revoked:
                msg += f"\n\n⚠️ Se revocaron los siguientes roles por incompatibilidad: {', '.join(revoked)}"
            
            send_text(waid, msg)
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        if wants_cancel:
            send_text(waid, "❌ Solicitud de discurso cancelada.")
            set_session(waid, awaiting=None, buffer=None, mode="root")
            send_root_menu(waid)
            return None
        
        if is_interactive:
            send_text(waid, "❗Opción inválida. Usa los botones: ✅ Confirmar / ❌ Cancelar.")
        else:
            send_text(waid, "Opción inválida. Envía 1 para confirmar o 2 para cancelar.")
        return None

    # --------- Flujos Sección Educativa -----------------------------------------------
    
    # Paso 1: Serie
    if awaiting == "section_step1_serie":
        buffer = s.get("buffer", {})
        buffer["serie"] = body_raw.strip()
        set_session(waid, awaiting="section_step2_nombre", buffer=buffer)
        send_text(waid, "📝 Envía el nombre de la sección educativa que presentarás:\n\nEjemplo: 'Cómo dar retroalimentación efectiva'")
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

        # 4) Detección de opciones de menú de miembro desde root -> redirigir
        if _is_choice(body_raw_clean, MEM_SPEECH_SET) or _is_choice(body_raw_clean, MEM_EDUCATION_SET):
            log.info("✓ Detectado: Opción de menú de miembro desde root - redirigiendo a menú de miembro")
            send_text(waid, "ℹ️ Esta opción está disponible en el Menú de miembro. Te llevo allí...")
            if len(mclubs) == 1:
                cid = mclubs[0]
                set_session(waid, mode="member", club=cid, awaiting=None)
                send_member_menu(_CTX[cid], waid)
                return jsonify({"status": "ok"})
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
        _, member_options, _ = _member_menu_parts(ctx_member)
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

        # 1) 🎯 Mi rol
        match_1 = matches_option(body_raw_clean, member_options[0]) if member_options else False
        log.info("¿Match opción 1? %s (numeric: %s)", match_1, numeric_choice == 0)
        if member_options and (match_1 or numeric_choice == 0):
            log.info("✓ Detectado: Opción 1 - Mi rol")
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
                send_text(waid, error_msg)
                send_member_menu(ctx_member, waid)
                return jsonify({"status": "ok"})
            
            # Iniciar flujo de captura de discurso preparado
            set_session(waid, awaiting="speech_step1_pathway", buffer={"waid": waid, "club": ctx_member.club_id, "round": st["round"]})
            pathways = [
                "Dynamic Leadership",
                "Engaging Humor",
                "Motivational Strategies",
                "Persuasive Influence",
                "Presentation Mastery",
                "Visionary Communication"
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
                send_text(waid, error_msg)
                send_member_menu(ctx_member, waid)
                return jsonify({"status": "ok"})
            
            # Iniciar flujo de captura de sección educativa
            set_session(waid, awaiting="section_step1_serie", buffer={"waid": waid, "club": ctx_member.club_id, "round": st["round"]})
            series = [
                ("🏆 Serie del mejor orador", "Técnicas de oratoria"),
                ("🎖️ Serie del club exitoso", "Gestión de clubes"),
                ("💼 Serie de Liderazgo de Excelencia", "Habilidades de liderazgo"),
                ("💡 Tema libre", "Cualquier tema educativo")
            ]
            send_list_menu(waid, "📚 Selecciona el tipo de serie educativa:", series, "Seleccionar serie")
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
    if body_norm in ("mi rol", "mi rol?", "whoami"):
        cid = infer_user_club(waid, extract_trailing_club_id(body_raw))
        if cid and cid in _CTX:
            send_text(waid, who_am_i(_CTX[cid], waid))
        else:
            send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
        send_root_menu(waid)
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
