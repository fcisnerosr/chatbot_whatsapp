# app.py
# --------------------------------------------------------------------------------------
# WhatsApp roles bot (Flask + Gupshup) - MULTI-CLUB con MENÚS NUMÉRICOS
#
# - Carga todos los clubes desde data/clubs/registry.json
# - Cada club tiene su propio {club.json, state.json} en data/clubs/<club_id>/
# - Asignación de roles con priorización por dificultad y ciclo de roles.
# - Interfaz 100% por MENÚS numéricos para usuarios y administradores.
#   • Miembro: ve su menú de miembro.
#   • Admin: ve su menú de admin.
#   • Admin y miembro: menú raíz que separa ambos.
#   • Invitaciones: siempre ofrece 1 Aceptar / 2 Rechazar.
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
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
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
    log.info("Cargado club %s (miembros=%d, admins=%d)", club_id, len(ctx.members_index), len(ctx.admins))


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


def _build_menu_text(title: str, options: List[str]) -> str:
    title_clean = title.rstrip("\n")
    option_lines = [opt.rstrip("\n") for opt in options]
    if title_clean and option_lines:
        return title_clean + "\n" + "\n".join(option_lines)
    if title_clean:
        return title_clean
    return "\n".join(option_lines)


def send_menu_with_quick_replies(to_e164_no_plus: str, title: str, options: List[str]) -> dict:
    menu_text = _build_menu_text(title, options)

    if options:
        payload = {
            "type": "quick_reply",
            "content": {"type": "text", "text": title},
            "options": [],
        }
        for idx, option in enumerate(options, 1):
            opt_text = option.strip()
            m = re.match(r"\s*(\d+)", opt_text)
            postback = m.group(1) if m else str(idx)
            payload["options"].append({"type": "text", "title": opt_text, "postbackText": postback})

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
            if not resp.ok:
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
        return f"No hay miembros registrados aún en {ctx.club_id}."
    lines = [f"👥 Miembros de {ctx.club_id}"]
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

    for r in roles_sorted:
        role = r.name
        excluded = set(a["waid"] for a in st["accepted"].values())
        excluded.update(pending_candidates(st))
        cand = choose_candidate_hier(ctx, role, excluded)
        if not cand:
            continue
        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}

    ctx.state_store.save(st)

    for role, info in st["pending"].items():
        cand = info["candidate"]
        begin_invite_flow(ctx, cand, role, st["round"])

    assigned_roles = set(st["pending"].keys())
    not_assigned = [r.name for r in ctx.club.roles if r.name not in assigned_roles]
    if not_assigned:
        broadcast_text(
            ctx.admins,
            f"[{ctx.club_id}] Algunos roles quedaron sin candidato: {', '.join(not_assigned)}. "
            "Agrega más miembros o intenta de nuevo."
        )

    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] ▶️ Iniciamos la ronda #{st['round']}.")
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
            return (
                f"Tienes una invitación pendiente: {role} en la ronda #{st['round']} ({ctx.club_id}).\n"
                "Elige una opción y envía solo el número:\n"
                "1) ✅ Aceptar\n"
                "2) ❌ Rechazar"
            )
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"✅ Confirmaste el rol {role} en la ronda #{st['round']} ({ctx.club_id})."
    return "➖ No tienes roles asignados ni pendientes."


# --- Resumen sin menús (para “Mi estado de rol” del menú raíz) -----------------------
def who_am_i_summary(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            return (
                f"[{ctx.club_id}] 🔔 Tienes una invitación pendiente: {role} en la ronda #{st['round']} ({ctx.club_id}).\n"
                f"👉 Ve al Menú de miembro ({ctx.club_id}) → «🎯 Mi rol» para aceptar o rechazar."
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

def _root_menu_parts(waid: str) -> Tuple[str, List[str]]:
    mclubs = member_clubs(waid)
    aclubs = admin_clubs(waid)
    opts = []
    idx = 1
    header = "🔢 Elige una opción y envía solo el número:"
    if mclubs:
        if len(mclubs) == 1:
            opts.append(f"{idx}) 👤 Menú de miembro ({mclubs[0]})")
        else:
            opts.append(f"{idx}) 👤 Menú de miembro (elegir club)")
        idx += 1
    if aclubs:
        if len(aclubs) == 1:
            opts.append(f"{idx}) 🛠️ Menú de admin ({aclubs[0]})")
        else:
            opts.append(f"{idx}) 🛠️ Menú de admin (elegir club)")
        idx += 1
    opts.append(f"{idx}) 📌 Mi estado de rol")
    return header, opts


def render_root_menu(waid: str) -> str:
    title, options = _root_menu_parts(waid)
    return _build_menu_text(title, options)


def send_root_menu(waid: str) -> dict:
    title, options = _root_menu_parts(waid)
    return send_menu_with_quick_replies(waid, title, options)


def _member_menu_parts(ctx: Ctx) -> Tuple[str, List[str]]:
    title = f"[{ctx.club_id}] 🧭 Menú miembro\n🔢 Elige una opción y envía solo el número:"
    options = [
        "1) 🎯 Mi rol (pendiente/confirmado)",
        "2) 📊 Estado de la ronda",
        "9) 🔙 Volver",
    ]
    return title, options

def render_member_menu(ctx: Ctx) -> str:
    title, options = _member_menu_parts(ctx)
    return _build_menu_text(title, options)


def send_member_menu(ctx: Ctx, waid: str) -> dict:
    title, options = _member_menu_parts(ctx)
    return send_menu_with_quick_replies(waid, title, options)

def render_member_club_picker(mclubs: List[str]) -> str:
    lines = ["👤 Elige club para tu menú de miembro (envía solo el número):"]
    for i, cid in enumerate(mclubs, 1):
        lines.append(f"{i}) {cid}")
    lines.append("9) 🔙 Volver")
    return "\n".join(lines)

def render_admin_club_picker(aclubs: List[str]) -> str:
    lines = ["🛠️ Elige club para administrar (envía solo el número):"]
    for i, cid in enumerate(aclubs, 1):
        lines.append(f"{i}) {cid}")
    lines.append("9) 🔙 Volver")
    return "\n".join(lines)

def _admin_menu_parts(ctx: Ctx) -> Tuple[str, List[str]]:
    title = f"[{ctx.club_id}] 🛡️ Menú admin\n🔢 Elige una opción y envía solo el número:"
    options = [
        "1) ▶️ Iniciar ronda",
        "2) 📊 Ver estado",
        "3) 🛑 Cancelar ronda",
        "4) ♻️ Resetear estado",
        "5) 👥 Ver miembros",
        "6) ➕ Agregar miembro",
        "7) ➖ Eliminar miembro",
        "8) 🔁 Cambiar de club",
        "9) 🔙 Volver",
    ]
    return title, options

def render_admin_menu(ctx: Ctx) -> str:
    title, options = _admin_menu_parts(ctx)
    return _build_menu_text(title, options)


def send_admin_menu(ctx: Ctx, waid: str) -> dict:
    title, options = _admin_menu_parts(ctx)
    return send_menu_with_quick_replies(waid, title, options)


def invite_menu_parts(ctx: Ctx, role: str, round_no: int) -> Tuple[str, List[str]]:
    title = (
        f"🔔 Invitación: {role} en la reunión #{round_no} ({ctx.club_id}).\n"
        "Elige una opción y envía solo el número:"
    )
    options = [
        "1) ✅ Aceptar",
        "2) ❌ Rechazar",
    ]
    return title, options

def invite_text(ctx: Ctx, role: str, round_no: int) -> str:
    title, options = invite_menu_parts(ctx, role, round_no)
    return _build_menu_text(title, options)


def begin_invite_flow(ctx: Ctx, waid: str, role: str, round_no: int) -> None:
    set_session(
        waid,
        awaiting="invite_decision",
        buffer={"role": role, "waid": waid, "club": ctx.club_id, "round": round_no},
    )
    title, options = invite_menu_parts(ctx, role, round_no)
    send_menu_with_quick_replies(waid, title, options)


def send_invite_menu(ctx: Ctx, waid: str, role: str, round_no: int) -> None:
    begin_invite_flow(ctx, waid, role, round_no)

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
        return (msg.get("text") or {}).get("body", "") or ""

    candidates = []
    candidates.append(msg.get("postbackText"))
    candidates.append(msg.get("postback"))
    candidates.append(msg.get("payload"))
    candidates.append(msg.get("text"))
    candidates.append(msg.get("title"))

    for k in ("reply", "button", "interactive", "list"):
        obj = msg.get(k) or {}
        if isinstance(obj, dict):
            candidates.append(
                obj.get("postbackText")
                or obj.get("postback")
                or obj.get("payload")
                or obj.get("text")
                or obj.get("title")
            )
            if "reply" in obj and isinstance(obj["reply"], dict):
                r = obj["reply"]
                candidates.append(
                    r.get("postbackText")
                    or r.get("postback")
                    or r.get("payload")
                    or r.get("id")
                    or r.get("title")
                )

    for v in candidates:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


@app.route("/webhook", methods=["POST"])
def webhook_post():
    data = request.get_json(force=True, silent=True) or {}
    try:
        value = (
            (data.get("entry") or [{}])[0]
            .get("changes", [{}])[0]
            .get("value", {})
        )
        for msg in value.get("messages", []):
            waid = msg.get("from", "")
            body_raw = _extract_incoming_text(msg)
            if not body_raw:
                continue
            body = norm(body_raw)
            log.info("Mensaje de %s: %s", waid, body)
            s = get_session(waid)

            # 0) Identificación inicial
            is_number = re.fullmatch(r"\d{1,3}", body) is not None

            if not s.get("club"):
                mclubs = member_clubs(waid)
                acls = admin_clubs(waid)
                if len(mclubs) == 1:
                    set_session(waid, club=mclubs[0])
                elif len(acls) == 1:
                    set_session(waid, club=acls[0])

            current_cid = s.get("club") or infer_user_club(waid, extract_trailing_club_id(body_raw))
            if current_cid and current_cid in _CTX:
                ctx = _CTX[current_cid]
            else:
                ctx = None

            # PRIORIDAD 2: Flujos awaiting (SIEMPRE antes de menús)
            awaiting = s.get("awaiting")

            # --- Flujo de invitación a rol (solo 1/2). Cualquier otra cosa re-muestra el prompt.
            if awaiting == "invite_decision":
                if body == "1":
                    buffer = s.get("buffer", {})
                    club_ctx = _CTX[buffer["club"]]
                    role_name = buffer["role"]
                    accept_msg = handle_accept(club_ctx, waid)
                    send_text(waid, accept_msg)

                    # Subflujos posteriores según rol (mantén exactamente los mismos emojis/estilo)
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
                        set_session(waid, awaiting=None, buffer=None, mode="root")
                        send_root_menu(waid)
                    return jsonify({"status": "ok"})

                elif body == "2":
                    buffer = s.get("buffer", {})
                    club_ctx = _CTX[buffer["club"]]
                    reject_msg = handle_reject(club_ctx, waid)
                    send_text(waid, reject_msg)
                    set_session(waid, awaiting=None, buffer=None, mode="root")
                    send_root_menu(waid)
                    return jsonify({"status": "ok"})

                else:
                    buffer = s.get("buffer", {})
                    club_ctx = _CTX[buffer["club"]]
                    send_text(waid, "❗Opción inválida. Responde 1 (Aceptar) o 2 (Rechazar).")
                    title, opts = invite_menu_parts(club_ctx, buffer["role"], buffer["round"])
                    send_menu_with_quick_replies(waid, title, opts)
                    return jsonify({"status": "ok"})

            # Agregar miembro
            if awaiting == "admin_add_member" and s.get("mode") == "admin" and ctx:
                tail = body_raw.strip()
                if "," in tail:
                    name, num = tail.split(",", 1)
                else:
                    parts = tail.rsplit(" ", 1)
                    if len(parts) != 2:
                        send_text(waid, "Formato no válido. Usa: Nombre, 55XXXXXXXX")
                        continue
                    name, num = parts[0], parts[1]
                out = admin_add_member(ctx, name.strip(), num.strip())
                send_text(waid, out)
                set_session(waid, awaiting=None, buffer=None)
                send_admin_menu(ctx, waid)
                continue

            # Eliminar miembro
            if awaiting == "admin_remove_member" and s.get("mode") == "admin" and ctx:
                tail = body_raw.strip()
                out = admin_remove_member(ctx, tail)
                send_text(waid, out)
                set_session(waid, awaiting=None, buffer=None)
                send_admin_menu(ctx, waid)
                continue

            # ============ FLUJO: Palabra del Día ============
            if awaiting == "word_step1_palabra":
                buffer = s.get("buffer", {})
                buffer["palabra"] = body_raw.strip()
                set_session(waid, awaiting="word_step2_significado", buffer=buffer)
                send_text(waid, "✍️ Envía el significado de la palabra:")
                continue

            if awaiting == "word_step2_significado":
                buffer = s.get("buffer", {})
                buffer["significado"] = body_raw.strip()
                set_session(waid, awaiting="word_step3_ejemplo", buffer=buffer)
                send_text(waid, "💡 Envía un ejemplo de uso de la palabra:")
                continue

            if awaiting == "word_step3_ejemplo":
                buffer = s.get("buffer", {})
                buffer["ejemplo"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)

                resumen = (
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
                send_text(waid, resumen)
                continue

            if awaiting == "word_confirm" and is_number:
                buffer = s.get("buffer", {})
                if body == "1":
                    club_ctx = _CTX[buffer["club"]]
                    st = club_ctx.state_store.load()
                    st["word_of_the_day"] = {
                        "palabra": buffer["palabra"],
                        "significado": buffer["significado"],
                        "ejemplo": buffer["ejemplo"],
                        "waid": buffer["waid"],
                        "nombre": pretty_name(club_ctx, buffer["waid"]),
                        "round": buffer["round"]
                    }
                    club_ctx.state_store.save(st)
                    send_text(waid, f"✅ Palabra del día guardada: '{buffer['palabra']}'")
                    set_session(waid, awaiting=None, buffer=None, mode="root")
                    send_root_menu(waid)
                    continue
                elif body == "2":
                    set_session(waid, awaiting="word_edit_palabra", buffer=buffer)
                    send_text(waid, f"📖 Palabra actual: {buffer['palabra']}\nEnvía la nueva palabra:")
                    continue
                elif body == "3":
                    set_session(waid, awaiting="word_edit_significado", buffer=buffer)
                    send_text(waid, f"✍️ Significado actual: {buffer['significado']}\nEnvía el nuevo significado:")
                    continue
                elif body == "4":
                    set_session(waid, awaiting="word_edit_ejemplo", buffer=buffer)
                    send_text(waid, f"💡 Ejemplo actual: {buffer['ejemplo']}\nEnvía el nuevo ejemplo:")
                    continue
                else:
                    send_text(waid, "Opción inválida. Envía 1, 2, 3 o 4.")
                    continue

            if awaiting == "word_edit_palabra":
                buffer = s.get("buffer", {})
                buffer["palabra"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                resumen = (
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
                send_text(waid, resumen)
                continue

            if awaiting == "word_edit_significado":
                buffer = s.get("buffer", {})
                buffer["significado"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                resumen = (
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
                send_text(waid, resumen)
                continue

            if awaiting == "word_edit_ejemplo":
                buffer = s.get("buffer", {})
                buffer["ejemplo"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                resumen = (
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
                send_text(waid, resumen)
                continue

            # ============ FLUJO: Temática de la Sesión ============
            if awaiting == "theme_step1_topic":
                buffer = s.get("buffer", {})
                buffer["topic"] = body_raw.strip()
                set_session(waid, awaiting="theme_confirm", buffer=buffer)

                resumen = (
                    f"📝 Temática de la sesión: {buffer['topic']}\n\n"
                    f"Elige:\n"
                    f"1) 💾 Guardar\n"
                    f"2) ✏️ Editar temática"
                )
                send_text(waid, resumen)
                continue

            if awaiting == "theme_confirm" and is_number:
                buffer = s.get("buffer", {})
                if body == "1":
                    club_ctx = _CTX[buffer["club"]]
                    st = club_ctx.state_store.load()
                    st["session_theme"] = {
                        "topic": buffer["topic"],
                        "waid": buffer["waid"],
                        "nombre": pretty_name(club_ctx, buffer["waid"]),
                        "round": buffer["round"]
                    }
                    club_ctx.state_store.save(st)
                    send_text(waid, f"✅ Temática guardada: '{buffer['topic']}'")
                    set_session(waid, awaiting=None, buffer=None, mode="root")
                    send_root_menu(waid)
                    continue
                elif body == "2":
                    set_session(waid, awaiting="theme_edit_topic", buffer=buffer)
                    send_text(waid, f"📝 Temática actual: {buffer['topic']}\nEnvía la nueva temática:")
                    continue
                else:
                    send_text(waid, "Opción inválida. Envía 1 o 2.")
                    continue

            if awaiting == "theme_edit_topic":
                buffer = s.get("buffer", {})
                buffer["topic"] = body_raw.strip()
                set_session(waid, awaiting="theme_confirm", buffer=buffer)
                resumen = (
                    f"📝 Temática de la sesión: {buffer['topic']}\n\n"
                    f"Elige:\n"
                    f"1) 💾 Guardar\n"
                    f"2) ✏️ Editar temática"
                )
                send_text(waid, resumen)
                continue
            # ============ FIN Temática de la Sesión ============

            # Si había awaiting pero no coincidió con ningún flujo, caemos a menús.

            # PRIORIDAD 3: Router por sesión (menús numéricos)
            if is_number:
                # Menú raíz
                if s.get("mode") == "root":
                    idx = 1
                    mclubs = member_clubs(waid)
                    aclubs = admin_clubs(waid)
                    if mclubs:
                        if body == str(idx):
                            if len(mclubs) == 1:
                                cid = mclubs[0]
                                set_session(waid, mode="member", club=cid, awaiting=None)
                                send_member_menu(_CTX[cid], waid); continue
                            set_session(waid, mode="member_pick", awaiting="pick_member_club", club=None, buffer=None)
                            send_text(waid, render_member_club_picker(mclubs)); continue
                        idx += 1
                    if aclubs:
                        if len(aclubs) == 1:
                            if body == str(idx):
                                set_session(waid, mode="admin", club=aclubs[0], awaiting=None)
                                send_admin_menu(_CTX[aclubs[0]], waid); continue
                            idx += 1
                        else:
                            if body == str(idx):
                                set_session(waid, mode="admin_pick", awaiting="pick_admin_club")
                                send_text(waid, render_admin_club_picker(aclubs)); continue
                            idx += 1
                    if body == str(idx):
                        # Mi estado de rol — resumen sin menús para evitar colisión
                        cid = s.get("club") or infer_user_club(waid)
                        if cid and cid in _CTX and len(member_clubs(waid)) <= 1:
                            send_text(waid, who_am_i_summary(_CTX[cid], waid))
                        else:
                            mclubs = member_clubs(waid)
                            if mclubs:
                                lines = []
                                for c in mclubs:
                                    lines.append(who_am_i_summary(_CTX[c], waid))
                                send_text(waid, "\n".join(lines))
                            else:
                                if cid and cid in _CTX:
                                    send_text(waid, who_am_i_summary(_CTX[cid], waid))
                                else:
                                    send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
                        send_root_menu(waid)
                        continue

                # Picker miembro multi-club
                if s.get("mode") == "member_pick" and s.get("awaiting") == "pick_member_club":
                    mclubs = member_clubs(waid)
                    if not mclubs:
                        set_session(waid, mode="root", awaiting=None, buffer=None, club=None)
                        send_root_menu(waid); continue
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None, club=None)
                        send_root_menu(waid); continue
                    try:
                        idx = int(body) - 1
                        cid = mclubs[idx]
                        set_session(waid, mode="member", club=cid, awaiting=None, buffer=None)
                        send_member_menu(_CTX[cid], waid); continue
                    except Exception:
                        send_text(waid, render_member_club_picker(mclubs)); continue

                # Picker admin multi-club
                if s.get("mode") == "admin_pick" and s.get("awaiting") == "pick_admin_club":
                    aclubs = admin_clubs(waid)
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_root_menu(waid); continue
                    try:
                        idx = int(body) - 1
                        cid = aclubs[idx]
                        set_session(waid, mode="admin", club=cid, awaiting=None)
                        send_admin_menu(_CTX[cid], waid); continue
                    except Exception:
                        send_text(waid, render_admin_club_picker(aclubs)); continue

                # Menú miembro
                if s.get("mode") == "member" and current_cid and current_cid in _CTX:
                    ctx = _CTX[current_cid]
                    if body == "1":
                        send_text(waid, who_am_i(ctx, waid))
                        send_member_menu(ctx, waid); continue
                    if body == "2":
                        send_text(waid, status_text(ctx))
                        send_member_menu(ctx, waid); continue
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_root_menu(waid); continue

                # Menú admin
                if s.get("mode") == "admin" and current_cid and current_cid in _CTX:
                    ctx = _CTX[current_cid]
                    if body == "1":
                        msg = start_new_round(ctx, pretty_name(ctx, waid))
                        send_text(waid, msg)
                        # Evita colisión: si ahora el admin tiene invitación pendiente o está en un flujo, no spamees el menú
                        if not has_pending_invite(ctx, waid) and get_session(waid).get("awaiting") is None:
                            send_admin_menu(ctx, waid)
                        continue
                    if body == "2":
                        send_text(waid, status_text(ctx))
                        send_admin_menu(ctx, waid); continue
                    if body == "3":
                        send_text(waid, cancel_round(ctx, pretty_name(ctx, waid)))
                        send_admin_menu(ctx, waid); continue
                    if body == "4":
                        send_text(waid, reset_all(ctx, pretty_name(ctx, waid)))
                        send_admin_menu(ctx, waid); continue
                    if body == "5":
                        send_text(waid, admin_list_members(ctx))
                        send_admin_menu(ctx, waid); continue
                    if body == "6":
                        set_session(waid, awaiting="admin_add_member", buffer=None)
                        send_text(waid, "✍️ Envía: Nombre, 55XXXXXXXX")
                        continue
                    if body == "7":
                        set_session(waid, awaiting="admin_remove_member", buffer=None)
                        send_text(waid, "✍️ Envía el número de 10 dígitos o el nombre exacto a eliminar")
                        continue
                    if body == "8":
                        aclubs = admin_clubs(waid)
                        if len(aclubs) > 1:
                            set_session(waid, mode="admin_pick", awaiting="pick_admin_club")
                            send_text(waid, render_admin_club_picker(aclubs)); continue
                        send_admin_menu(ctx, waid); continue
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_root_menu(waid); continue
    

            # PRIORIDAD 4: Comandos legacy

            if body in ("mi rol", "mi rol?", "whoami"):
                cid = infer_user_club(waid, extract_trailing_club_id(body_raw))
                if cid and cid in _CTX:
                    send_text(waid, who_am_i(_CTX[cid], waid))
                else:
                    send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
                send_root_menu(waid)
                continue

            if body in ("acepto", "accept") and ctx:
                send_text(waid, handle_accept(ctx, waid))
                send_root_menu(waid)
                continue

            if body in ("rechazo", "reject") and ctx:
                send_text(waid, handle_reject(ctx, waid))
                send_root_menu(waid)
                continue

            # FALLBACK: menú principal
            send_root_menu(waid)

    except Exception:
        log.exception("Error procesando webhook; payload=%s", data)

    return jsonify({"status": "ok"})

# ======================================================================================
# 7) Main
# ======================================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, debug=False)
