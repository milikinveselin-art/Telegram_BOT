from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.error import BadRequest

import json
from pathlib import Path
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
import httpx

import hashlib
import hmac
import secrets
from typing import Optional, Tuple, List, Dict
import re


# =========================
# 🔒 AUTH / ADMIN + PASSWORD
# =========================
ADMIN_ID = 1828526836  # ← ако ти е грешно: пусни /myid и смени с числото
DEFAULT_PASSWORD = "1234"  # ← начална парола (после: /setpass новапарола)

TOKEN = "8225336814:AAF-iTsLTp55WlSioTxwScB3hTS63l5zSYU"

# ✅ ТВОЯТ API KEY ЗА ВРЕМЕТО (OpenWeather)
OPENWEATHER_API_KEY = "acb146b347d10db723fb9eaaa0c5f069"

DATA_FILE = Path(__file__).parent / "data.json"

SOFIA_TZ = ZoneInfo("Europe/Sofia")

# =========================
# 🎉 NAMEDAYS (Bulgarian calendar)
# =========================
NAMEDAYS_FILE = Path(__file__).parent / "namedays_bg.json"

# Минимален вграден стартов календар (ботът ще създаде namedays_bg.json при първо пускане).
# Формат във файла:
#   "ДД.ММ": ["Име1", "Име2", ...]
NAMEDAYS_DEFAULT = {
    "01.01": ["Васил", "Василка", "Васко", "Веселин", "Веселина"],
    "06.01": ["Йордан", "Йорданка", "Богоявление"],
    "07.01": ["Иван", "Иванка", "Ивайло", "Йоан"],
    "17.01": ["Антон", "Антония"],
    "18.01": ["Атанас", "Атанаска"],
    "25.01": ["Григор", "Гергана"],
    "02.02": ["Симеон", "Симона"],
    "14.02": ["Валентин", "Валентина", "Трифон"],
    "25.03": ["Благовещение", "Блага", "Благой"],
    "06.05": ["Георги", "Гергана", "Гергьовден"],
    "21.05": ["Константин", "Елена"],
    "24.05": ["Кирил", "Методий"],
    "29.06": ["Петър", "Павел"],
    "20.07": ["Илия"],
    "15.08": ["Мария"],
    "14.10": ["Петка", "Петко"],
    "08.11": ["Михаил", "Гавраил", "Рангел", "Рангела"],
    "06.12": ["Никола", "Николай", "Николина", "Нина"],
    "26.12": ["Стефан", "Стефка"],
}

_NAMEDAYS_CACHE: Optional[dict] = None



# =========================
# 🔔 REMINDER CONFIG
# =========================
CAR_REMIND_DAYS = [30, 14, 7, 3, 1, 0]       # дни преди (и 0=днес)
TIBO_REMIND_DAYS = [14, 7, 3, 1, 0]
BDAY_REMIND_DAYS = [7, 3, 1, 0]
TASK_REMIND_DAYS = [3, 1, 0]

NOTIFY_LOG_KEEP_DAYS = 90  # пазим лог за anti-dup


# =========================
# SAFE EDIT (fix Message is not modified)
# =========================
async def _safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


def _is_admin(update: Update) -> bool:
    try:
        return bool(update.effective_user and update.effective_user.id == ADMIN_ID)
    except Exception:
        return False


def _pbkdf2_hash(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return dk.hex()


def _ensure_auth_config(data: dict) -> dict:
    data.setdefault("settings", {})
    s = data["settings"]

    s.setdefault("authorized_users", [])
    if not isinstance(s["authorized_users"], list):
        s["authorized_users"] = []

    s.setdefault("password_salt_hex", "")
    s.setdefault("password_hash", "")

    if not s["password_hash"] or not s["password_salt_hex"]:
        salt = secrets.token_bytes(16)
        s["password_salt_hex"] = salt.hex()
        s["password_hash"] = _pbkdf2_hash(DEFAULT_PASSWORD, salt)

    return data


def _is_authorized(update: Update, data: dict) -> bool:
    try:
        uid = update.effective_user.id
    except Exception:
        return False

    if uid == ADMIN_ID:
        return True

    au = data.get("settings", {}).get("authorized_users", []) or []
    return uid in au


def _check_password(data: dict, password: str) -> bool:
    s = data.get("settings", {})
    salt_hex = (s.get("password_salt_hex") or "").strip()
    stored = (s.get("password_hash") or "").strip()
    if not salt_hex or not stored:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except Exception:
        return False
    calc = _pbkdf2_hash(password, salt)
    return hmac.compare_digest(calc, stored)


def _add_authorized_user(data: dict, user_id: int):
    s = data.setdefault("settings", {})
    au = s.setdefault("authorized_users", [])
    if user_id not in au:
        au.append(user_id)


def _remove_authorized_user(data: dict, user_id: int):
    s = data.setdefault("settings", {})
    au = s.setdefault("authorized_users", [])
    if user_id in au:
        au.remove(user_id)


async def _deny_access(update: Update, text: str = "⛔️ Нямаш достъп. Напиши /start и въведи парола."):
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
        return
    if update.message:
        await update.message.reply_text(text)
# =========================
# 📜 AUDIT LOG + ADMIN ALERTS + BROADCAST
# =========================
def _user_label(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "unknown"
    name = (u.full_name or "").strip() or (u.first_name or "").strip() or "unknown"
    uname = (u.username or "").strip()
    if uname:
        return f"{name} (@{uname}, id={u.id})"
    return f"{name} (id={u.id})"


def _chat_label(update: Update) -> str:
    c = update.effective_chat
    if not c:
        return "unknown"
    title = (getattr(c, "title", None) or "").strip()
    ctype = (getattr(c, "type", None) or "").strip()
    if title:
        return f"{title} ({ctype}, chat_id={c.id})"
    return f"{ctype} (chat_id={c.id})"


def _now_sofia() -> datetime:
    return datetime.now(SOFIA_TZ)


def _now_sofia_str() -> str:
    return _now_sofia().strftime("%d.%m.%Y %H:%M:%S")


def _get_broadcast_chat_ids(data: dict) -> list[int]:
    s = data.get("settings", {}) or {}
    ids = set()

    # owner chat id (ако има)
    owner = s.get("owner_chat_id")
    if isinstance(owner, int) and owner != 0:
        ids.add(owner)

    # authorized users (лични чатове, chat_id == user_id)
    au = s.get("authorized_users", []) or []
    for uid in au:
        if isinstance(uid, int) and uid != 0:
            ids.add(uid)

    # винаги админа (за всеки случай)
    if isinstance(ADMIN_ID, int) and ADMIN_ID != 0:
        ids.add(ADMIN_ID)

    return sorted(ids)


def _append_audit(data: dict, record: dict, keep_last: int = 200) -> None:
    s = data.setdefault("settings", {})
    log = s.setdefault("audit_log", [])
    if not isinstance(log, list):
        log = []
        s["audit_log"] = log
    log.append(record)
    # keep only last N
    if len(log) > keep_last:
        s["audit_log"] = log[-keep_last:]


def log_action(data: dict, action: str, update: Update | None = None, details: dict | None = None) -> None:
    rec = {
        "ts": _now_sofia_str(),
        "action": action,
        "details": details or {},
    }
    if update is not None:
        u = update.effective_user
        c = update.effective_chat
        rec["user_id"] = u.id if u else None
        rec["user_name"] = (u.full_name if u else None)
        rec["username"] = (u.username if u else None)
        rec["chat_id"] = c.id if c else None
        rec["chat_title"] = getattr(c, "title", None) if c else None
        rec["chat_type"] = getattr(c, "type", None) if c else None
    _append_audit(data, rec)


async def _notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str, data: dict | None = None) -> None:
    try:
        d = data or load_data()
        s = d.get("settings", {}) or {}
        target = s.get("owner_chat_id") or ADMIN_ID
        if not target:
            return
        await context.bot.send_message(chat_id=int(target), text=text)
    except Exception:
        # не чупим бота заради нотификация
        pass


async def _broadcast_text(context: ContextTypes.DEFAULT_TYPE, data: dict, text: str, reply_markup=None) -> None:
    chat_ids = _get_broadcast_chat_ids(data)
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=text, reply_markup=reply_markup)
        except Exception:
            continue


async def _broadcast_task_added(context: ContextTypes.DEFAULT_TYPE, update: Update, task_text: str, task_date: str) -> None:
    data = load_data()
    date_part = task_date.strip() if task_date else ""
    date_label = date_part if date_part else "без дата"
    msg = (
        "🆕 Добавена задача\n"
        f"👤 От: {_user_label(update)}\n"
        f"💬 Чат: {_chat_label(update)}\n"
        f"📝 Задача: {task_text}\n"
        f"📅 Дата: {date_label}\n"
        f"🕒 Добавено: {_now_sofia_str()}"
    )
    await _broadcast_text(context, data, msg)


# ===== Startup “Ботът работи” + бутон Start =====
async def _broadcast_bot_running(application: Application) -> None:
    try:
        data = load_data()
        chat_ids = _get_broadcast_chat_ids(data)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Start", callback_data="startup:start")]])
        for cid in chat_ids:
            try:
                await application.bot.send_message(chat_id=cid, text="✅ Ботът работи.", reply_markup=kb)
            except Exception:
                continue
    except Exception:
        pass


async def post_init(application: Application) -> None:
    # извиква се при старт/рестарт
    await _broadcast_bot_running(application)


async def startup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = load_data()
    # само ако има достъп
    if not (_is_admin(update) or _is_authorized(update, data)):
        await _safe_edit(q, "🔒 Нямаш достъп. Напиши /start и въведи парола.")
        return
    context.user_data.clear()
    context.chat_data.clear()
    await smart_start_show(update, context)


# ===== Команда: Последни действия (само ADMIN) =====
async def show_last_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not _is_admin(update):
        await _deny_access(update, "⛔️ Само админът може да вижда историята.")
        return

    log = (data.get("settings", {}) or {}).get("audit_log", []) or []
    if not log:
        await update.message.reply_text("📜 Няма записани действия още.")
        return

    last = log[-20:][::-1]
    lines = ["📜 Последни действия (последни 20):"]
    for rec in last:
        ts = rec.get("ts", "?")
        act = rec.get("action", "?")
        uname = rec.get("user_name") or "unknown"
        uid = rec.get("user_id")
        who = f"{uname} (id={uid})" if uid else uname
        det = rec.get("details", {}) or {}
        extra = ""
        if det.get("text"):
            extra = f" | {str(det.get('text'))[:40]}"
        elif det.get("name"):
            extra = f" | {str(det.get('name'))[:40]}"
        lines.append(f"• {ts} | {act} | {who}{extra}")

    await update.message.reply_text("\n".join(lines))



# =========================
# DATA
# =========================
def load_data():
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    else:
        data = {}

    data.setdefault("car", {})
    for k in ["gtp", "vinetka", "maslo", "obslujvane"]:
        data["car"].setdefault(k, "")

    data.setdefault("birthdays", [])
    if not isinstance(data["birthdays"], list):
        data["birthdays"] = []

    data.setdefault("tasks", [])
    if not isinstance(data["tasks"], list):
        data["tasks"] = []

    data.setdefault("tasks_done", [])
    if not isinstance(data["tasks_done"], list):
        data["tasks_done"] = []

    data.setdefault("orders", {})
    data["orders"].setdefault("suppliers", [])
    if not isinstance(data["orders"]["suppliers"], list):
        data["orders"]["suppliers"] = []

    data.setdefault("settings", {})
    data["settings"].setdefault("city", "Sofia,BG")
    data["settings"].setdefault("owner_chat_id", None)

    # ✅ anti-dup reminder log
    data["settings"].setdefault("notify_log", {})
    if not isinstance(data["settings"]["notify_log"], dict):
        data["settings"]["notify_log"] = {}

    # 📜 audit log (последни действия)
    data["settings"].setdefault("audit_log", [])
    if not isinstance(data["settings"]["audit_log"], list):
        data["settings"]["audit_log"] = []
    # 🎉 namedays favorites (по потребител)
    # формат: { "user_id": ["Иван", "Мария", ...], ... }
    data["settings"].setdefault("namedays_favorites", {})
    if not isinstance(data["settings"]["namedays_favorites"], dict):
        data["settings"]["namedays_favorites"] = {}

    data.setdefault("tibo", {})
    data["tibo"].setdefault("bday", "")
    data["tibo"].setdefault("deworm", "")
    data["tibo"].setdefault("vaccine", "")

    data = _ensure_auth_config(data)
    return data


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# =========================
# 🎉 NAMEDAYS HELPERS
# =========================
def _ensure_namedays_file() -> None:
    """Create namedays_bg.json with a starter dataset if missing."""
    try:
        if not NAMEDAYS_FILE.exists():
            NAMEDAYS_FILE.write_text(json.dumps(NAMEDAYS_DEFAULT, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_namedays_calendar() -> dict:
    """Load calendar mapping 'DD.MM' -> [names]. Cached in memory."""
    global _NAMEDAYS_CACHE
    if _NAMEDAYS_CACHE is not None:
        return _NAMEDAYS_CACHE
    _ensure_namedays_file()
    cal = {}
    try:
        raw = json.loads(NAMEDAYS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                key = (k or "").strip()
                if len(key) == 5 and key[2] == "." and key.replace(".", "").isdigit():
                    if isinstance(v, list):
                        cal[key] = [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        cal = {}

    if not cal:
        cal = dict(NAMEDAYS_DEFAULT)

    _NAMEDAYS_CACHE = cal
    return cal




# =========================
# 🎉 NAMEDAYS MOVABLE HOLIDAYS (Orthodox)
# =========================
def _orthodox_easter_gregorian(year: int) -> date:
    """Compute Orthodox Easter date in Gregorian calendar for given year."""
    # Meeus Julian algorithm -> convert to Gregorian
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31  # 3=March, 4=April
    day = ((d + e + 114) % 31) + 1
    # Julian calendar date of Paschal full moon + offset
    julian = date(year, month, day) + timedelta(days=13)  # approx conversion
    # Find the following Sunday
    easter = julian + timedelta(days=(6 - julian.weekday()) % 7)
    return easter


def _movable_key_to_date(key: str, year: int) -> Optional[date]:
    k = (key or "").strip().upper()
    if not k.startswith("MOVABLE:"):
        return None
    tag = k.split(":", 1)[1]
    easter = _orthodox_easter_gregorian(year)
    if tag in ("VELIKDEN", "EASTER"):
        return easter
    if tag in ("CVETNICA", "PALM_SUNDAY"):
        return easter - timedelta(days=7)
    if tag in ("LAZAROVDEN",):
        return easter - timedelta(days=8)
    if tag in ("RAZPETI_PETUK", "GOOD_FRIDAY"):
        return easter - timedelta(days=2)
    return None


def _namedays_names_for_date(dt: date) -> List[str]:
    cal = load_namedays_calendar()
    ddmm = dt.strftime("%d.%m")
    names: List[str] = []
    names += cal.get(ddmm, []) or []
    year = dt.year
    # movable keys
    for k, v in cal.items():
        if not isinstance(k, str) or not k.upper().startswith("MOVABLE:"):
            continue
        mdt = _movable_key_to_date(k, year)
        if mdt == dt:
            if isinstance(v, list):
                names += [str(x).strip() for x in v if str(x).strip()]
    # de-dup preserve order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _namedays_dates_for_name(name: str, year: int | None = None) -> List[str]:
    """Return list of human-readable dates/labels when name appears."""
    cal = load_namedays_calendar()
    target = _norm_name(name)
    out: List[str] = []
    # fixed dates
    for ddmm, names in cal.items():
        if not isinstance(ddmm, str):
            continue
        if ddmm.upper().startswith("MOVABLE:"):
            continue
        if any(_norm_name(n) == target for n in (names or [])):
            out.append(ddmm)
    # movable dates
    y = year or datetime.now(SOFIA_TZ).date().year
    for k, names in cal.items():
        if not isinstance(k, str) or not k.upper().startswith("MOVABLE:"):
            continue
        if any(_norm_name(n) == target for n in (names or [])):
            mdt = _movable_key_to_date(k, y)
            if mdt:
                label = k.split(":", 1)[1].title()
                out.append(f"{mdt.strftime('%d.%m')} ({label})")
            else:
                out.append(f"({k})")
    return out

def _norm_name(s: str) -> str:
    return (s or "").strip().lower()


def namedays_for_date(ddmm: str) -> List[str]:
    """Fixed-date lookup (DD.MM)."""
    cal = load_namedays_calendar()
    return cal.get(ddmm, []) or []


def namedays_for_today(dt: date) -> List[str]:
    """Includes movable holidays."""
    return _namedays_names_for_date(dt)


def find_nameday_dates(name: str) -> List[str]:
    """Return list of dates/labels when name appears (includes movable)."""
    return _namedays_dates_for_name(name)


def _get_user_namedays_favs(data: dict, user_id: int) -> List[str]:
    favs_map = (data.get("settings", {}) or {}).get("namedays_favorites", {}) or {}
    favs = favs_map.get(str(user_id), [])
    if not isinstance(favs, list):
        return []
    seen = set()
    out = []
    for x in favs:
        sx = str(x).strip()
        if sx and sx.lower() not in seen:
            out.append(sx)
            seen.add(sx.lower())
    return out


def _set_user_namedays_favs(data: dict, user_id: int, favs: List[str]) -> None:
    data.setdefault("settings", {})
    data["settings"].setdefault("namedays_favorites", {})
    data["settings"]["namedays_favorites"][str(user_id)] = favs


def _fmt_namedays_today(today: date, favs: List[str]) -> str:
    ddmm = today.strftime("%d.%m")
    names = namedays_for_today(today)
    if not names:
        return f"🎉 Имени дни днес ({ddmm})\n— няма данни —"

    fav_norm = {_norm_name(x) for x in (favs or [])}
    fav_hits = [n for n in names if _norm_name(n) in fav_norm]

    lines = [f"🎉 Имени дни днес ({ddmm})", ""]
    if fav_hits:
        lines.append("⭐ Твоите любими именници днес:")
        lines += [f"• {x}" for x in fav_hits]
        lines.append("")
    lines.append("📅 По календар:")
    lines += [f"• {x}" for x in names]
    return "\n".join(lines)

def _fmt_namedays_upcoming(today: date, days: int, user_id: int, data: dict) -> str:
    favs = _get_user_namedays_favs(data, user_id)
    fav_norm = {_norm_name(x) for x in (favs or [])}

    items: List[tuple[date, List[str]]] = []
    for i in range(0, days + 1):
        dt = today + timedelta(days=i)
        names = _namedays_names_for_date(dt)
        if names:
            items.append((dt, names))

    if not items:
        return f"🎉 Имени дни – следващи {days} дни\n— няма данни —"

    lines = [f"🎉 Имени дни – следващи {days} дни", ""]
    for dt, names in items:
        ddmm = dt.strftime("%d.%m")
        fav_hits = [n for n in names if _norm_name(n) in fav_norm]
        label = dt.strftime("%d.%m.%Y")
        if fav_hits:
            lines.append(f"⭐ {label}: " + ", ".join(fav_hits))
        lines.append(f"• {label}: " + ", ".join(names))
    return "\n".join(lines)



def namedays_menu(data: dict, user_id: int) -> InlineKeyboardMarkup:
    favs = _get_user_namedays_favs(data, user_id)
    fav_count = len(favs)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Днес кои имат имен ден?", callback_data="namedays:today")],
        [InlineKeyboardButton("⏭️ Следващи 7 дни", callback_data="namedays:next7"),
         InlineKeyboardButton("📆 Следващи 30 дни", callback_data="namedays:next30")],
        [InlineKeyboardButton("🔎 Търси по име", callback_data="namedays:search")],
        [InlineKeyboardButton(f"⭐ Любими имена ({fav_count})", callback_data="namedays:favs")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])

def namedays_favs_menu(data: dict, user_id: int) -> InlineKeyboardMarkup:
    favs = _get_user_namedays_favs(data, user_id)
    rows = [[InlineKeyboardButton("➕ Добави любимо име", callback_data="namedays:fav_add")]]
    if favs:
        for n in favs[:25]:
            rows.append([InlineKeyboardButton(f"➖ {n}", callback_data=f"namedays:fav_remove:{n}")])
        rows.append([InlineKeyboardButton("🗑️ Изчисти всички", callback_data="namedays:fav_clear")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:namedays")])
    return InlineKeyboardMarkup(rows)



# =========================
# DATE HELPERS
# =========================
def parse_bg_date_full(s: str) -> Optional[date]:
    s = (s or "").strip()
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        d = int(parts[0]); m = int(parts[1]); y = int(parts[2])
        return date(y, m, d)
    except Exception:
        return None


def _fmt(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def days_left_text(date_str: str) -> Optional[str]:
    dt = parse_bg_date_full(date_str)
    if not dt:
        return None
    today = datetime.now(SOFIA_TZ).date()
    diff = (dt - today).days
    if diff > 0:
        return f"⏳ Остават {diff} дни"
    if diff == 0:
        return "📌 Днес"
    return f"⚠️ Минало преди {-diff} дни"


def parse_bday(date_str: str) -> Optional[Tuple[int, int]]:
    try:
        parts = (date_str or "").strip().split(".")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
        if len(parts) == 3:
            return int(parts[0]), int(parts[1])
    except Exception:
        return None
    return None


def days_until_birthday(day: int, month: int) -> Tuple[int, date]:
    today = datetime.now(SOFIA_TZ).date()
    y = today.year
    nxt = date(y, month, day)
    if nxt < today:
        nxt = date(y + 1, month, day)
    return (nxt - today).days, nxt


def bday_is_today(date_str: str) -> bool:
    p = parse_bday(date_str)
    if not p:
        return False
    d, m = p
    t = datetime.now(SOFIA_TZ).date()
    return (t.day == d) and (t.month == m)


def _looks_like_bday(s: str) -> bool:
    s = (s or "").strip()
    return bool(parse_bday(s))


def _looks_like_full_date(s: str) -> bool:
    s = (s or "").strip()
    return parse_bg_date_full(s) is not None


# =========================
# ORDERS helpers (days)
# =========================
WEEKDAY_BG = {
    0: "Понеделник",
    1: "Вторник",
    2: "Сряда",
    3: "Четвъртък",
    4: "Петък",
    5: "Събота",
    6: "Неделя",
}

DAYS = [
    ("Пон", "Понеделник"),
    ("Вт", "Вторник"),
    ("Ср", "Сряда"),
    ("Чет", "Четвъртък"),
    ("Пет", "Петък"),
    ("Съб", "Събота"),
    ("Нед", "Неделя"),
]


def selected_days_text(selected_full_days):
    if not selected_full_days:
        return "—"
    ordered = [full for _, full in DAYS if full in selected_full_days]
    return ", ".join(ordered)


# =========================
# TASKS helpers (UI)
# =========================
def tasks_pick_keyboard(tasks):
    rows = []
    for i, t in enumerate(tasks[:30], 1):
        title = t.get("text", "—")
        d = t.get("date", "")
        label = f"{i}. {title}" + (f" ({d})" if d else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"tasks:done:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:tasks")])
    return InlineKeyboardMarkup(rows)


def _tasks_page_keyboard(total: int, offset: int, page_size: int):
    rows = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Предишни", callback_data=f"tasks:page:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton("Следващи ➡️", callback_data=f"tasks:page:{offset + page_size}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:tasks")])
    return InlineKeyboardMarkup(rows)


def _tasks_show_keyboard(tasks_page, offset, total, page_size=8):
    rows = []
    for i, t in enumerate(tasks_page, 1):
        abs_index = offset + (i - 1)
        d = t.get("date", "")
        label = f"{i}. {t.get('text', '—')}" + (f" ({d})" if d else "")
        rows.append([InlineKeyboardButton(f"✔️ Отметни: {label}"[:64], callback_data=f"tasks:done_abs:{abs_index}")])

    nav_kb = _tasks_page_keyboard(total=total, offset=offset, page_size=page_size)
    rows.extend(nav_kb.inline_keyboard)
    return InlineKeyboardMarkup(rows)


def tasks_confirm_clear_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, изчисти", callback_data="tasks:clear_yes"),
         InlineKeyboardButton("❌ Не", callback_data="tasks:clear_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:tasks")]
    ])


def tasks_confirm_history_clear_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, изчисти", callback_data="tasks:history_clear_yes"),
         InlineKeyboardButton("❌ Не", callback_data="tasks:history_clear_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:tasks")]
    ])


# =========================
# BIRTHDAYS helpers (UI)
# =========================
def bdays_confirm_delete_kb(abs_index):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, изтрий", callback_data=f"bdays:del_yes:{abs_index}"),
         InlineKeyboardButton("❌ Не", callback_data="bdays:del_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="bdays:show_buttons")]
    ])


def bdays_confirm_clear_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, изчисти", callback_data="bdays:clear_yes"),
         InlineKeyboardButton("❌ Не", callback_data="bdays:clear_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:bdays")]
    ])


def _bdays_page_keyboard(total: int, offset: int, page_size: int):
    rows = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Предишни", callback_data=f"bdays:page:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton("Следващи ➡️", callback_data=f"bdays:page:{offset + page_size}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:bdays")])
    return InlineKeyboardMarkup(rows)


def _bdays_list_keyboard(items, offset, total, page_size=8):
    rows = []
    for i, it in enumerate(items, 1):
        abs_index = offset + (i - 1)
        name = it.get("name", "—")
        d = it.get("date", "—")
        rows.append([
            InlineKeyboardButton(f"✏️ {i}", callback_data=f"bdays:edit_abs:{abs_index}"),
            InlineKeyboardButton(f"🗑️ {i}", callback_data=f"bdays:del_abs:{abs_index}"),
            InlineKeyboardButton(f"{name} ({d})"[:35], callback_data=f"bdays:view_abs:{abs_index}"),
        ])

    nav_kb = _bdays_page_keyboard(total=total, offset=offset, page_size=page_size)
    rows.extend(nav_kb.inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _bdays_view_kb(abs_index: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Име", callback_data=f"bdays:edit_name_abs:{abs_index}"),
         InlineKeyboardButton("✏️ Дата", callback_data=f"bdays:edit_date_abs:{abs_index}")],
        [InlineKeyboardButton("🗑️ Изтрий", callback_data=f"bdays:del_abs:{abs_index}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="bdays:show_buttons")]
    ])


# =========================
# 🐶 TIBO helpers / UI
# =========================
TIBO_LABELS = {
    "bday": "🎂 Рожден ден",
    "deworm": "🪱 Обезпаразитяване",
    "vaccine": "💉 Ваксинация",
}

def tibo_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Рожден ден", callback_data="tibo:show:bday"),
         InlineKeyboardButton("✏️ Промени", callback_data="tibo:set:bday")],

        [InlineKeyboardButton("🪱 Обезпаразитяване", callback_data="tibo:show:deworm"),
         InlineKeyboardButton("✏️ Промени", callback_data="tibo:set:deworm")],

        [InlineKeyboardButton("💉 Ваксинация", callback_data="tibo:show:vaccine"),
         InlineKeyboardButton("✏️ Промени", callback_data="tibo:set:vaccine")],

        [InlineKeyboardButton("👀 Покажи всички", callback_data="tibo:show_all")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


def tibo_summary(data):
    t = data["tibo"]

    bday_str = t.get("bday") or "—"
    bday_left = None
    p = parse_bday(bday_str)
    if p:
        left, nxt = days_until_birthday(p[0], p[1])
        bday_left = f"⏳ Остават {left} дни (на {nxt.strftime('%d.%m.%Y')})"

    deworm = t.get("deworm") or "—"
    vaccine = t.get("vaccine") or "—"

    deworm_left = days_left_text(t.get("deworm", ""))
    vaccine_left = days_left_text(t.get("vaccine", ""))

    lines = ["🐶 Тибо – записи:"]
    lines.append(f"🎂 Рожден ден: {bday_str}" + (f" • {bday_left}" if bday_left else ""))
    lines.append(f"🪱 Обезпаразитяване: {deworm}" + (f" • {deworm_left}" if deworm_left else ""))
    lines.append(f"💉 Ваксинация: {vaccine}" + (f" • {vaccine_left}" if vaccine_left else ""))
    return "\n".join(lines)


# =========================
# UI (menus)
# =========================
def main_menu():
    # ✅ Variant 5 (2 бутона на ред)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Днес", callback_data="today:show"),
         InlineKeyboardButton("☀️ Време", callback_data="weather:today")],

        [InlineKeyboardButton("✅ Задачи", callback_data="menu:tasks"),
         InlineKeyboardButton("🎂 Рождени дни", callback_data="menu:bdays")],

        [InlineKeyboardButton("🚗 Кола", callback_data="menu:car"),
         InlineKeyboardButton("🐶 Тибо", callback_data="menu:tibo")],

        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings"),
         InlineKeyboardButton("📦 Поръчки", callback_data="menu:orders")],

        [InlineKeyboardButton("🎉 Имени дни", callback_data="menu:namedays"), InlineKeyboardButton("🔎 Търсене", callback_data="menu:search")],
    ])


def settings_menu(data):
    city = data.get("settings", {}).get("city", "Sofia,BG")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏙️ Град: {city}", callback_data="settings:city_show")],
        [InlineKeyboardButton("✏️ Смени град", callback_data="settings:city_set")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


def car_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠️ ГТП", callback_data="car:show:gtp"),
         InlineKeyboardButton("✏️ Промени", callback_data="car:set:gtp")],

        [InlineKeyboardButton("🛣️ Винетка", callback_data="car:show:vinetka"),
         InlineKeyboardButton("✏️ Промени", callback_data="car:set:vinetka")],

        [InlineKeyboardButton("🛢️ Масло", callback_data="car:show:maslo"),
         InlineKeyboardButton("✏️ Промени", callback_data="car:set:maslo")],

        [InlineKeyboardButton("🔧 Обслужване", callback_data="car:show:obslujvane"),
         InlineKeyboardButton("✏️ Промени", callback_data="car:set:obslujvane")],

        [InlineKeyboardButton("👀 Покажи всички", callback_data="car:show_all")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


def bdays_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добави рожден ден", callback_data="bdays:add")],
        [InlineKeyboardButton("👀 Покажи всички (с бутони)", callback_data="bdays:show_buttons")],
        [InlineKeyboardButton("⭐ Следващ рожден ден", callback_data="bdays:next")],
        [InlineKeyboardButton("🧹 Изчисти всички", callback_data="bdays:clear")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


def tasks_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добави задача", callback_data="tasks:add")],
        [InlineKeyboardButton("👀 Покажи всички (с ✔️)", callback_data="tasks:show")],
        [InlineKeyboardButton("📅 Предстоящи", callback_data="tasks:upcoming")],
        [InlineKeyboardButton("✔️ Отметни изпълнена", callback_data="tasks:done_pick")],
        [InlineKeyboardButton("📜 История", callback_data="tasks:history")],
        [InlineKeyboardButton("🧹 Изчисти задачи", callback_data="tasks:clear"),
         InlineKeyboardButton("🧹 Изчисти история", callback_data="tasks:history_clear")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


def orders_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ (Поръчки: модулът е следващ)", callback_data="orders:todo")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ])


CAR_LABELS = {
    "gtp": "🛠️ ГТП",
    "vinetka": "🛣️ Винетка",
    "maslo": "🛢️ Смяна на масло",
    "obslujvane": "🔧 Обслужване",
}


def car_summary(data):
    c = data["car"]
    gtp_left = days_left_text(c.get("gtp", ""))
    vin_left = days_left_text(c.get("vinetka", ""))

    gtp_line = f"🛠️ ГТП: {c.get('gtp') or '—'}"
    if gtp_left:
        gtp_line += f"  •  {gtp_left}"

    vin_line = f"🛣️ Винетка: {c.get('vinetka') or '—'}"
    if vin_left:
        vin_line += f"  •  {vin_left}"

    return (
        "🚗 Данни за колата:\n"
        f"{gtp_line}\n"
        f"{vin_line}\n"
        f"🛢️ Масло: {c.get('maslo') or '—'}\n"
        f"🔧 Обслужване: {c.get('obslujvane') or '—'}"
    )


# =========================
# WEATHER
# =========================
async def get_weather_today(city: str) -> str:
    if not OPENWEATHER_API_KEY or OPENWEATHER_API_KEY.startswith("PASTE_"):
        return "❌ Нямаш зададен OPENWEATHER_API_KEY в кода."

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"q": city, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "bg"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            return f"❌ Не успях да взема времето за „{city}“. (код {r.status_code})"

        j = r.json()
        name = j.get("name", city)
        weather = (j.get("weather") or [{}])[0]
        desc = weather.get("description", "—")
        main = j.get("main") or {}
        wind = j.get("wind") or {}

        temp = main.get("temp")
        feels = main.get("feels_like")
        tmin = main.get("temp_min")
        tmax = main.get("temp_max")
        hum = main.get("humidity")
        ws = wind.get("speed")

        lines = [f"☀️ Времето днес – {name}", "────────────", f"☁️ {desc}"]
        if temp is not None: lines.append(f"🌡️ Температура: {temp:.0f}°C")
        if feels is not None: lines.append(f"🤒 Усеща се: {feels:.0f}°C")
        if tmin is not None and tmax is not None:
            lines.append(f"📉 Мин: {tmin:.0f}°C  |  📈 Макс: {tmax:.0f}°C")
        if hum is not None: lines.append(f"💧 Влажност: {hum}%")
        if ws is not None: lines.append(f"💨 Вятър: {ws:.1f} m/s")

        return "\n".join(lines)
    except Exception:
        return "❌ Грешка при връзката за времето. Опитай пак след малко."


# =========================
# INLINE MENU helper
# =========================
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text="📒 Меню"):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await _safe_edit(q, text, reply_markup=main_menu())
        return
    await update.message.reply_text(text, reply_markup=main_menu())



# =========================
# 📌 SMART START (Dashboard as part of start)
# =========================
def _fmt_dashboard(data: dict, base_day: date, user_id: int) -> str:
    today = base_day
    tomorrow = base_day + timedelta(days=1)

    tasks = data.get("tasks", []) or []
    def tasks_for_day(d: date):
        ds = d.strftime("%d.%m.%Y")
        out = []
        for t in tasks:
            td = (t.get("date") or "").strip()
            if td == ds:
                out.append(t.get("text") or "")
        return [x for x in out if x]

    tasks_today = tasks_for_day(today)
    tasks_tom = tasks_for_day(tomorrow)

    # birthdays (if stored as DD.MM)
    bdays = data.get("birthdays", []) or []
    def bdays_for_day(d: date):
        ddmm = d.strftime("%d.%m")
        hits=[]
        for b in bdays:
            if (b.get("date") or "").strip() == ddmm:
                hits.append(b.get("name") or "")
        return [x for x in hits if x]

    b_today = bdays_for_day(today)
    b_tom = bdays_for_day(tomorrow)

    # namedays
    n_today = _namedays_names_for_date(today)
    n_tom = _namedays_names_for_date(tomorrow)

    # favorites namedays hits
    favs = _get_user_namedays_favs(data, user_id)
    fav_norm = {_norm_name(x) for x in (favs or [])}
    n_today_fav = [n for n in n_today if _norm_name(n) in fav_norm]
    n_tom_fav = [n for n in n_tom if _norm_name(n) in fav_norm]

    lines = [f"📌 Важно сега ({today.strftime('%d.%m.%Y')})", ""]
    lines.append("📅 Днес")
    lines.append(f"✅ Задачи: {len(tasks_today)}" + (f" • {', '.join(tasks_today[:3])}" if tasks_today else ""))
    if b_today:
        lines.append("🎂 Рожден ден: " + ", ".join(b_today[:5]))
    if n_today_fav:
        lines.append("⭐ Любими именници: " + ", ".join(n_today_fav[:8]))
    elif n_today:
        lines.append("🎉 Имени дни: " + ", ".join(n_today[:8]))

    lines.append("")
    lines.append("⏭️ Утре")
    lines.append(f"✅ Задачи: {len(tasks_tom)}" + (f" • {', '.join(tasks_tom[:3])}" if tasks_tom else ""))
    if b_tom:
        lines.append("🎂 Рожден ден: " + ", ".join(b_tom[:5]))
    if n_tom_fav:
        lines.append("⭐ Любими именници: " + ", ".join(n_tom_fav[:8]))
    elif n_tom:
        lines.append("🎉 Имени дни: " + ", ".join(n_tom[:8]))

    return "\n".join(lines)


async def smart_start_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Smart start: morning -> today's focus, evening -> tomorrow's focus; otherwise -> main menu.
    Works both from /start and from inline Start button (callback query).
    """
    data = load_data()
    now = datetime.now(SOFIA_TZ)
    uid = update.effective_user.id if update.effective_user else 0

    hour = now.hour

    # Morning focus (today)
    if 6 <= hour <= 11:
        txt = _fmt_dashboard(data, now.date(), uid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Меню", callback_data="back:main"),
             InlineKeyboardButton("🔎 Търсене", callback_data="menu:search")],
        ])
        if update.callback_query:
            q = update.callback_query
            await q.answer()
            try:
                await _safe_edit(q, txt, reply_markup=kb)
            except Exception:
                # fallback if edit fails
                await update.effective_chat.send_message(txt, reply_markup=kb)
        else:
            await update.effective_message.reply_text(txt, reply_markup=kb)
        return

    # Evening focus (tomorrow)
    if hour >= 16:
        target = now.date() + timedelta(days=1)
        txt = _fmt_dashboard(data, target, uid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Меню", callback_data="back:main"),
             InlineKeyboardButton("🔎 Търсене", callback_data="menu:search")],
        ])
        if update.callback_query:
            q = update.callback_query
            await q.answer()
            try:
                await _safe_edit(q, txt, reply_markup=kb)
            except Exception:
                await update.effective_chat.send_message(txt, reply_markup=kb)
        else:
            await update.effective_message.reply_text(txt, reply_markup=kb)
        return

    # Default: just open the main menu
    await show_main_menu(update, context, text="📒 Меню")



# =========================
# 🔔 REMINDERS ENGINE (ALL DATED CATEGORIES)
# =========================
def _notify_log_cleanup(log: dict, keep_days: int = NOTIFY_LOG_KEEP_DAYS) -> dict:
    """Keep only last N days entries."""
    try:
        today = datetime.now(SOFIA_TZ).date()
        cutoff = today - timedelta(days=keep_days)
        new_log = {}
        for k, v in (log or {}).items():
            try:
                dt = datetime.strptime(k, "%Y-%m-%d").date()
            except Exception:
                continue
            if dt >= cutoff:
                new_log[k] = v if isinstance(v, list) else []
        return new_log
    except Exception:
        return {}


def _already_sent_today(data: dict, key: str, today: date) -> bool:
    s = data.setdefault("settings", {})
    log = s.setdefault("notify_log", {})
    day_key = today.strftime("%Y-%m-%d")
    sent = log.get(day_key, [])
    if not isinstance(sent, list):
        sent = []
        log[day_key] = sent
    return key in sent


def _mark_sent_today(data: dict, key: str, today: date):
    s = data.setdefault("settings", {})
    log = s.setdefault("notify_log", {})
    day_key = today.strftime("%Y-%m-%d")
    sent = log.get(day_key, [])
    if not isinstance(sent, list):
        sent = []
    if key not in sent:
        sent.append(key)
    log[day_key] = sent


def _lead_days(target: date, today: date) -> int:
    return (target - today).days


def _collect_task_summary(data: dict, today: date):
    tasks = data.get("tasks", []) or []
    overdue = []
    due_today = []
    due_tomorrow = []
    next7 = []

    tomorrow = today + timedelta(days=1)
    end7 = today + timedelta(days=7)

    for i, tsk in enumerate(tasks):
        dstr = (tsk.get("date") or "").strip()
        dt = parse_bg_date_full(dstr) if dstr else None
        if not dt:
            continue
        title = tsk.get("text", "—")
        if dt < today:
            overdue.append((dt, i, title))
        elif dt == today:
            due_today.append((dt, i, title))
        elif dt == tomorrow:
            due_tomorrow.append((dt, i, title))
        elif today < dt <= end7:
            next7.append((dt, i, title))

    overdue.sort(key=lambda x: x[0])
    due_today.sort(key=lambda x: x[0])
    due_tomorrow.sort(key=lambda x: x[0])
    next7.sort(key=lambda x: x[0])
    return overdue, due_today, due_tomorrow, next7


def _build_daily_notifications(data: dict, today: date) -> list[str]:
    msgs: list[str] = []

    # ---- CAR reminders (any field that parses as full date) ----
    car = data.get("car", {}) or {}
    for field, label in CAR_LABELS.items():
        v = (car.get(field) or "").strip()
        dt = parse_bg_date_full(v)
        if not dt:
            continue
        left = _lead_days(dt, today)
        for lead in CAR_REMIND_DAYS:
            if left == lead:
                key = f"car:{field}:{lead}:{_fmt(dt)}"
                if not _already_sent_today(data, key, today):
                    if lead == 0:
                        msgs.append(f"🚗 {label}: ДНЕС е срокът! ({_fmt(dt)})")
                    else:
                        msgs.append(f"🚗 {label}: след {lead} дни (на {_fmt(dt)})")
                    _mark_sent_today(data, key, today)

        # overdue info (only once per day)
        if left < 0:
            key = f"car:{field}:overdue:{_fmt(dt)}"
            if not _already_sent_today(data, key, today):
                msgs.append(f"🚗 {label}: ⚠️ Просрочено ({_fmt(dt)}) • преди {-left} дни")
                _mark_sent_today(data, key, today)

    # ---- TIBO reminders ----
    tibo = data.get("tibo", {}) or {}

    # bday (ДД.ММ)
    bday_str = (tibo.get("bday") or "").strip()
    p = parse_bday(bday_str)
    if p:
        left, nxt = days_until_birthday(p[0], p[1])
        for lead in BDAY_REMIND_DAYS:
            if left == lead:
                key = f"tibo:bday:{lead}:{_fmt(nxt)}"
                if not _already_sent_today(data, key, today):
                    if lead == 0:
                        msgs.append(f"🐶 Тибо: 🎂 Рожден ден ДНЕС! ({_fmt(nxt)})")
                    else:
                        msgs.append(f"🐶 Тибо: 🎂 Рожден ден след {lead} дни ({_fmt(nxt)})")
                    _mark_sent_today(data, key, today)

    # deworm/vaccine (ДД.ММ.ГГГГ)
    for field in ("deworm", "vaccine"):
        val = (tibo.get(field) or "").strip()
        dt = parse_bg_date_full(val)
        if not dt:
            continue
        left = _lead_days(dt, today)
        label = TIBO_LABELS.get(field, field)
        for lead in TIBO_REMIND_DAYS:
            if left == lead:
                key = f"tibo:{field}:{lead}:{_fmt(dt)}"
                if not _already_sent_today(data, key, today):
                    if lead == 0:
                        msgs.append(f"🐶 Тибо: {label} ДНЕС! ({_fmt(dt)})")
                    else:
                        msgs.append(f"🐶 Тибо: {label} след {lead} дни ({_fmt(dt)})")
                    _mark_sent_today(data, key, today)
        if left < 0:
            key = f"tibo:{field}:overdue:{_fmt(dt)}"
            if not _already_sent_today(data, key, today):
                msgs.append(f"🐶 Тибо: {label} ⚠️ Просрочено ({_fmt(dt)}) • преди {-left} дни")
                _mark_sent_today(data, key, today)

    # ---- BIRTHDAYS reminders (all) ----
    items = data.get("birthdays", []) or []
    for idx, it in enumerate(items):
        name = (it.get("name") or "").strip()
        dstr = (it.get("date") or "").strip()
        p = parse_bday(dstr)
        if not name or not p:
            continue
        left, nxt = days_until_birthday(p[0], p[1])
        for lead in BDAY_REMIND_DAYS:
            if left == lead:
                key = f"bdays:{idx}:{lead}:{name}:{_fmt(nxt)}"
                if not _already_sent_today(data, key, today):
                    if lead == 0:
                        msgs.append(f"🎂 Рожден ден ДНЕС: {name} ({_fmt(nxt)})")
                    else:
                        msgs.append(f"🎂 Рожден ден: {name} след {lead} дни ({_fmt(nxt)})")
                    _mark_sent_today(data, key, today)

    # ---- TASKS reminders (dated tasks) ----
    tasks = data.get("tasks", []) or []
    for idx, tsk in enumerate(tasks):
        dstr = (tsk.get("date") or "").strip()
        dt = parse_bg_date_full(dstr) if dstr else None
        if not dt:
            continue
        title = (tsk.get("text") or "—").strip()
        left = _lead_days(dt, today)
        for lead in TASK_REMIND_DAYS:
            if left == lead:
                key = f"task:{idx}:{lead}:{_fmt(dt)}"
                if not _already_sent_today(data, key, today):
                    if lead == 0:
                        msgs.append(f"✅ Задача ДНЕС: {title} ({_fmt(dt)})")
                    else:
                        msgs.append(f"✅ Задача след {lead} дни: {title} ({_fmt(dt)})")
                    _mark_sent_today(data, key, today)
        if left < 0:
            key = f"task:{idx}:overdue:{_fmt(dt)}"
            if not _already_sent_today(data, key, today):
                msgs.append(f"✅ Задача ⚠️ Просрочена: {title} ({_fmt(dt)}) • преди {-left} дни")
                _mark_sent_today(data, key, today)

    return msgs


def _build_morning_digest(data: dict, today: date) -> str:
    overdue, due_today, due_tomorrow, next7 = _collect_task_summary(data, today)

    lines = [f"🌅 Сутрешно резюме • {_fmt(today)}", "────────────"]

    # Tasks
    lines.append("✅ Задачи:")
    if overdue:
        lines.append("⚠️ Просрочени:")
        for dt, _, title in overdue[:10]:
            lines.append(f"• {_fmt(dt)} — {title}")
    if due_today:
        lines.append("📌 Днес:")
        for dt, _, title in due_today[:10]:
            lines.append(f"• {_fmt(dt)} — {title}")
    if due_tomorrow:
        lines.append("⏳ Утре:")
        for dt, _, title in due_tomorrow[:10]:
            lines.append(f"• {_fmt(dt)} — {title}")
    if next7:
        lines.append("📆 Следващи 7 дни:")
        for dt, _, title in next7[:10]:
            lines.append(f"• {_fmt(dt)} — {title}")
    if not (overdue or due_today or due_tomorrow or next7):
        lines.append("— няма задачи с дата —")

    # Birthdays today/tomorrow (quick)
    b_today = []
    b_tomorrow = []
    tom = today + timedelta(days=1)
    for it in (data.get("birthdays") or []):
        name = (it.get("name") or "").strip()
        p = parse_bday(it.get("date", ""))
        if not name or not p:
            continue
        _, nxt = days_until_birthday(p[0], p[1])
        if nxt == today:
            b_today.append(name)
        elif nxt == tom:
            b_tomorrow.append(name)

    lines.append("")
    lines.append("🎂 Рождени дни:")
    if b_today:
        lines.append("📌 Днес: " + ", ".join(b_today[:10]))
    if b_tomorrow:
        lines.append("⏳ Утре: " + ", ".join(b_tomorrow[:10]))
    if not b_today and not b_tomorrow:
        lines.append("— няма —")

    # Car quick
    lines.append("")
    lines.append("🚗 Кола (бързо):")
    c = data.get("car", {}) or {}
    for field in ("gtp", "vinetka"):
        val = (c.get(field) or "").strip()
        extra = days_left_text(val) if val else None
        lines.append(f"• {CAR_LABELS[field]}: {val or '—'}" + (f" ({extra})" if extra else ""))

    # Tibo quick
    lines.append("")
    lines.append("🐶 Тибо (бързо):")
    t = data.get("tibo", {}) or {}
    lines.append(f"• 🎂 Рожден ден: {t.get('bday') or '—'}")
    lines.append(f"• 🪱 Обезпаразитяване: {t.get('deworm') or '—'}")
    lines.append(f"• 💉 Ваксинация: {t.get('vaccine') or '—'}")

    return "\n".join(lines)


async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = data.get("settings", {}).get("owner_chat_id")
    if not chat_id:
        return

    # cleanup log
    data["settings"]["notify_log"] = _notify_log_cleanup(data["settings"].get("notify_log", {}))
    save_data(data)

    today = datetime.now(SOFIA_TZ).date()

    # 1) Morning digest (once per day)
    digest_key = f"digest:{today.strftime('%Y-%m-%d')}"
    if not _already_sent_today(data, digest_key, today):
        text = _build_morning_digest(data, today)
        await context.bot.send_message(chat_id=chat_id, text=text)
        _mark_sent_today(data, digest_key, today)
        save_data(data)

    # 2) Specific reminders (all categories)
    msgs = _build_daily_notifications(data, today)
    if msgs:
        await context.bot.send_message(chat_id=chat_id, text="🔔 Напомняния:\n" + "\n".join(msgs))
        save_data(data)



    # 5) Namedays favorites reminders (per user)
    try:
        ddmm = today.strftime("%d.%m")
        today_names = namedays_for_date(ddmm)
        if today_names:
            today_norm = {_norm_name(x) for x in today_names}

            # пращаме само на потребители, които имат favorites съвпадащи с днешните
            for cid in _get_broadcast_chat_ids(data):
                try:
                    uid = int(cid)
                    favs = _get_user_namedays_favs(data, uid)
                    if not favs:
                        continue
                    fav_norm = {_norm_name(x) for x in favs}
                    hits = [n for n in today_names if _norm_name(n) in fav_norm]
                    if not hits:
                        continue

                    key = f"namedays:{uid}:{today.strftime('%Y-%m-%d')}"
                    if _already_sent_today(data, key, today):
                        continue

                    msg = "🎉 Днес имен ден имат (от твоите любими):\n" + "\n".join([f"• {x}" for x in hits])
                    await context.bot.send_message(chat_id=uid, text=msg)
                    _mark_sent_today(data, key, today)
                except Exception:
                    continue
            save_data(data)
    except Exception:
        pass


# =========================
# COMMANDS
# =========================
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    cid = update.effective_chat.id if update.effective_chat else None
    await update.message.reply_text(
        f"👤 твоето user id: {uid}\n💬 chat id: {cid}\n\n"
        f"ADMIN_ID в кода трябва да е точно това user id (числото)."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()

    # 📜 log + 🔔 admin alert
    try:
        log_action(data, "start", update)
        save_data(data)
    except Exception:
        pass
    try:
        await _notify_admin(context, f"👋 /start: {_user_label(update)}\n💬 {_chat_label(update)}")
    except Exception:
        pass

    if _is_admin(update):
        data["settings"]["owner_chat_id"] = update.effective_chat.id
        _add_authorized_user(data, ADMIN_ID)
        save_data(data)
        context.user_data.clear()
        await smart_start_show(update, context)
        return

    if _is_authorized(update, data):
        context.user_data.clear()
        await smart_start_show(update, context)
        return

    context.user_data.clear()
    context.user_data["mode"] = "auth_password"
    context.user_data["auth_tries"] = 0
    await update.message.reply_text("🔒 За достъп въведи парола:")


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        await update.message.reply_text("👑 Админът не се logout-ва 🙂")
        return
    _remove_authorized_user(data, uid)
    save_data(data)
    context.user_data.clear()
    await update.message.reply_text("✅ Излязъл си. За вход: /start")


async def setpass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("⛔️ Само админ може да сменя паролата.")
        return

    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or len(parts[1].strip()) < 4:
        await update.message.reply_text("Ползвай: /setpass НОВАПАРОЛА (поне 4 символа)")
        return

    new_pass = parts[1].strip()
    data = load_data()
    salt = secrets.token_bytes(16)
    data["settings"]["password_salt_hex"] = salt.hex()
    data["settings"]["password_hash"] = _pbkdf2_hash(new_pass, salt)
    save_data(data)
    await update.message.reply_text("✅ Паролата е сменена.")


async def stat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# =========================
# ✅ SMART ANSWERS
# =========================
def _n(text: str) -> str:
    return (text or "").strip().lower()

def _contains_any(t: str, words) -> bool:
    return any(w in t for w in words)

def _extract_city_from_text(t: str) -> Optional[str]:
    if "в " not in t:
        return None
    after = t.split("в ", 1)[1].strip()
    if not after:
        return None
    after = after.strip(" ?!.,")
    if "," in after:
        return after
    parts = after.split()
    if not parts:
        return None
    city = parts[0]
    return city[0].upper() + city[1:] + ",BG"

def _car_field_from_text(t: str) -> Optional[str]:
    if "гтп" in t:
        return "gtp"
    if "винет" in t:
        return "vinetka"
    if "масло" in t:
        return "maslo"
    if _contains_any(t, ["обслуж", "сервиз"]):
        return "obslujvane"
    return None

def _tibo_field_from_text(t: str) -> Optional[str]:
    if "ваксин" in t:
        return "vaccine"
    if _contains_any(t, ["обезпараз", "глист"]):
        return "deworm"
    if _contains_any(t, ["рожден", "р.д", "рд"]):
        return "bday"
    return None

def _next_birthday_item(data: dict):
    items = data.get("birthdays", []) or []
    best = None
    for it in items:
        name = (it.get("name") or "").strip()
        dstr = (it.get("date") or "").strip()
        p = parse_bday(dstr)
        if not name or not p:
            continue
        left, nxt = days_until_birthday(p[0], p[1])
        cand = (left, nxt, it)
        if best is None or cand[0] < best[0]:
            best = cand
    return best



async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    is_auth = _is_admin(update) or _is_authorized(update, data)

    lines = ["📚 Команди и какво правят", ""]
    lines.append("/start – старт + умно начално показване (според часа)")
    lines.append("/help – този списък с команди")
    if is_auth:
        lines.append("/log – 📜 последни действия (само админ)")
        lines.append("/stat – 📊 статистика за задачи/данни")
        lines.append("/logout – изход (за потребители)")
    if _is_admin(update):
        lines.append("/export – 🧾 backup на data.json като файл (само админ)")
        lines.append("/setpass НОВАПАРОЛА – смени паролата (само админ)")
    lines.append("")
    lines.append("📌 Подсказка: можеш да пишеш и нормално, напр. „Утре да купя кафе“ – ботът ще предложи да добави задача.")
    await update.message.reply_text("\n".join(lines))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("⛔️ Само админ може да прави export.")
        return
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=DATA_FILE.open("rb"),
            filename="data.json",
            caption="🧾 Backup: data.json",
        )
        # 📜 log
        data = load_data()
        try:
            log_action(data, "export_data", update)
            save_data(data)
        except Exception:
            pass
    except Exception:
        await update.message.reply_text("❌ Не успях да изпратя файла. Провери дали data.json съществува.")

async def smart_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, text: str) -> bool:
    t = _n(text)


    # 🎉 ИМЕНИ ДНИ
    if _contains_any(t, ["имен ден", "имени дни", "именници"]) and _contains_any(t, ["днес", "сега"]):
        today = datetime.now(SOFIA_TZ).date()
        favs = _get_user_namedays_favs(data, update.effective_user.id)
        await update.message.reply_text(_fmt_namedays_today(today, favs))
        return True

    if _contains_any(t, ["кога"]) and _contains_any(t, ["имен ден", "именика", "именника"]):
        name = ""
        if " на " in t:
            name = t.split(" на ", 1)[1].strip()
        if name:
            dates = find_nameday_dates(name)
            if dates:
                await update.message.reply_text(f"🎉 Имен ден на „{name}“: {', '.join(dates)}")
            else:
                await update.message.reply_text(f"🔎 Не намерих „{name}“ в календара.")
            return True

    if _contains_any(t, ["времето", "време"]) and _contains_any(t, ["днес", "сега", "какво е"]):
        city = _extract_city_from_text(t) or data.get("settings", {}).get("city", "Sofia,BG")
        w = await get_weather_today(city)
        await update.message.reply_text(w)
        return True

    if _contains_any(t, ["следващ рожден", "кой има рожден", "рожден ден скоро", "кой е следващият рожден"]):
        best = _next_birthday_item(data)
        if not best:
            await update.message.reply_text("🎂 Няма записани рождени дни.")
            return True
        left, nxt, it = best
        name = it.get("name", "—")
        await update.message.reply_text(
            f"🎂 Следващ рожден ден:\n• {name}\n📅 {nxt.strftime('%d.%m.%Y')}\n⏳ Остават {left} дни"
        )
        return True

    if _contains_any(t, ["кога", "кога ми е", "покажи"]) and _contains_any(t, ["гтп", "винетка", "масло", "обслужване", "сервиз"]):
        field = _car_field_from_text(t)
        if field:
            val = data.get("car", {}).get(field) or "—"
            extra = days_left_text(val) if _looks_like_full_date(val) else None
            msg = f"{CAR_LABELS.get(field, field)}: {val}"
            if extra:
                msg += f"\n{extra}"
            await update.message.reply_text(msg)
            return True

    if _contains_any(t, ["кога", "покажи"]) and _contains_any(t, ["ваксина", "обезпараз", "глисти", "рожден"]):
        field = _tibo_field_from_text(t)
        if field:
            val = data.get("tibo", {}).get(field) or "—"
            await update.message.reply_text(f"{TIBO_LABELS.get(field, field)}: {val}")
            return True

    return False


# =========================
# BUTTONS
# =========================


# =========================
# 🪄 SMART FREE-TEXT PARSING (suggestions)
# =========================
def _parse_natural_task(text: str, now: date) -> Optional[tuple[str, str]]:
    """Try to parse a task from free text. Returns (task_text, date_str_ddmmYYYY_or_empty)."""
    t = (text or "").strip()
    if not t:
        return None

    low = t.lower()

    # detect date hints
    task_date: Optional[date] = None
    if "утре" in low:
        task_date = now + timedelta(days=1)
        low = low.replace("утре", "")
        t = t.replace("утре", "")
    elif "днес" in low:
        task_date = now
        low = low.replace("днес", "")
        t = t.replace("днес", "")
    else:
        # explicit dd.mm.yyyy
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t)
        if m:
            try:
                task_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                t = t.replace(m.group(0), "").strip()
            except Exception:
                task_date = None

    # clean leading phrases
    t2 = t.strip()
    for pref in ["да ", "трябва да ", "искам да ", "не забравяй да "]:
        if t2.lower().startswith(pref):
            t2 = t2[len(pref):].strip()
            break

    # avoid very short or question-like texts
    if len(t2) < 4:
        return None
    if any(qw in (text or "").lower() for qw in ["кога", "какво", "колко", "кой", "дали"]) and "да" not in (text or "").lower():
        return None

    dstr = ""
    if task_date:
        dstr = task_date.strftime("%d.%m.%Y")
    return (t2, dstr)


async def _send_task_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE, task_text: str, date_str: str) -> None:
    context.user_data["pending_suggestion"] = {
        "type": "task",
        "text": task_text,
        "date": date_str,
    }
    when = "без дата" if not date_str else date_str
    msg = (
        "🪄 Разпознах задача:\n"
        f"📝 {task_text}\n"
        f"📅 {when}\n\n"
        "Да я добавя ли?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data="smart:task:confirm"),
         InlineKeyboardButton("❌ Не", callback_data="smart:task:cancel")],
        [InlineKeyboardButton("🏠 Меню", callback_data="back:main")],
    ])
    await update.message.reply_text(msg, reply_markup=kb)

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not _is_authorized(update, data):
        await _deny_access(update)
        return

    q = update.callback_query
    await q.answer()

    # Startup 'Start' button
    if q.data == "startup:start":
        context.chat_data.clear()
        await smart_start_show(update, context)
        return



    # 🪄 SMART SUGGESTIONS (free text)
    if q.data == "smart:task:confirm":
        pend = context.user_data.get("pending_suggestion") or {}
        if pend.get("type") != "task":
            await q.answer("Няма активна заявка.")
            return
        ttxt = (pend.get("text") or "").strip()
        dstr = (pend.get("date") or "").strip()
        context.user_data.pop("pending_suggestion", None)

        data.setdefault("tasks", [])
        data["tasks"].append({"text": ttxt, "date": dstr})
        try:
            log_action(data, "task_add_smart", update, {"text": ttxt, "date": dstr})
        except Exception:
            pass
        save_data(data)
        try:
            await _broadcast_task_added(context, update, ttxt, dstr)
        except Exception:
            pass
        await _safe_edit(q, "✅ Добавих задачата.", reply_markup=tasks_menu())
        return

    if q.data == "smart:task:cancel":
        context.user_data.pop("pending_suggestion", None)
        await _safe_edit(q, "❌ Отказано.", reply_markup=main_menu())
        return

    # WEATHER
    if q.data == "weather:today":
        city = data["settings"].get("city", "Sofia,BG")
        text = await get_weather_today(city)
        await _safe_edit(q, text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]
        ]))
        return

    # SETTINGS
    if q.data == "menu:settings":
        context.chat_data.clear()
        await _safe_edit(q, "⚙️ Настройки", reply_markup=settings_menu(data))
        return

    if q.data == "settings:city_show":
        city = data["settings"].get("city", "Sofia,BG")
        await _safe_edit(
            q,
            f"🏙️ Текущ град: {city}\n\nМожеш да го смениш от „✏️ Смени град“.",
            reply_markup=settings_menu(data)
        )
        return

    if q.data == "settings:city_set":
        context.chat_data.clear()
        context.chat_data["mode"] = "set_city"
        await _safe_edit(
            q,
            "✏️ Смяна на град\n\nНапиши град така:\n"
            "• Sofia,BG\n"
            "• Plovdiv,BG\n"
            "• Varna,BG\n\n"
            "Може и само: Sofia",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:settings")]])
        )
        return

    # ---- Today ----
    if q.data == "today:show":
        today = datetime.now(SOFIA_TZ).date()
        weekday_name = WEEKDAY_BG[today.weekday()]

        suppliers_today = [
            s.get("name", "—")
            for s in data.get("orders", {}).get("suppliers", [])
            if weekday_name in (s.get("days", []) or [])
        ]

        tasks_today = []
        for t in data.get("tasks", []) or []:
            dt = parse_bg_date_full(t.get("date", "")) if t.get("date") else None
            if dt and dt == today:
                tasks_today.append(t.get("text", "—"))

        bdays_today = [
            b.get("name", "—")
            for b in data.get("birthdays", []) or []
            if bday_is_today(b.get("date", ""))
        ]

        lines = [
            f"📅 Днес: {today.strftime('%d.%m.%Y')} ({weekday_name})",
            "",
            "📦 Доставчици за днес:",
            *( [f"• {x}" for x in suppliers_today] if suppliers_today else ["— няма —"] ),
            "",
            "✅ Задачи за днес:",
            *( [f"• {x}" for x in tasks_today] if tasks_today else ["— няма —"] ),
            "",
            "🎂 Рождени дни днес:",
            *( [f"• {x}" for x in bdays_today] if bdays_today else ["— няма —"] ),
        ]
        await _safe_edit(q, "\n".join(lines), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]
        ]))
        return

    # back
    if q.data == "back:main":
        context.chat_data.clear()
        await smart_start_show(update, context)
        return

    # open menus
    if q.data == "menu:car":
        await _safe_edit(q, "🚗 Кола", reply_markup=car_menu())
        return

    if q.data == "menu:tibo":
        await _safe_edit(q, "🐶 Тибо", reply_markup=tibo_menu())
        return

    if q.data == "menu:bdays":
        context.chat_data.clear()
        await _safe_edit(q, "🎂 Рождени дни", reply_markup=bdays_menu())
        return

    if q.data == "menu:tasks":
        context.chat_data.clear()
        await _safe_edit(q, "✅ Лични задачи", reply_markup=tasks_menu())
        return

    if q.data == "menu:orders":
        await _safe_edit(q, "📦 Поръчки", reply_markup=orders_menu())
        return

    if q.data == "menu:search":
        context.chat_data.clear()
        context.chat_data["mode"] = "global_search"
        await _safe_edit(q, "🔎 Търсене (всичко)\n\nНапиши дума/име:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")]
        ]))
        return


    if q.data == "menu:namedays":
        await _safe_edit(q, "🎉 Имени дни", reply_markup=namedays_menu(data, update.effective_user.id))
        return

    if q.data == "namedays:today":
        today = datetime.now(SOFIA_TZ).date()
        favs = _get_user_namedays_favs(data, update.effective_user.id)
        await _safe_edit(q, _fmt_namedays_today(today, favs), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:namedays")]
        ]))
        return

    if q.data == "namedays:next7":
        today = datetime.now(SOFIA_TZ).date()
        days = 7
        txt = _fmt_namedays_upcoming(today, days, update.effective_user.id, data)
        await _safe_edit(q, txt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:namedays")]
        ]))
        return

    if q.data == "namedays:next30":
        today = datetime.now(SOFIA_TZ).date()
        days = 30
        txt = _fmt_namedays_upcoming(today, days, update.effective_user.id, data)
        await _safe_edit(q, txt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:namedays")]
        ]))
        return

    if q.data == "namedays:search":
        context.chat_data.clear()
        context.chat_data["mode"] = "namedays_search"
        await _safe_edit(q, "🔎 Напиши име (пример: Иван):", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:namedays")]
        ]))
        return

    if q.data == "namedays:favs":
        await _safe_edit(q, "⭐ Любими имена", reply_markup=namedays_favs_menu(data, update.effective_user.id))
        return

    if q.data == "namedays:fav_add":
        context.chat_data.clear()
        context.chat_data["mode"] = "namedays_fav_add"
        await _safe_edit(q, "➕ Напиши име за добавяне в любими:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="namedays:favs")]
        ]))
        return

    if q.data.startswith("namedays:fav_remove:"):
        name = q.data.split("namedays:fav_remove:", 1)[1].strip()
        uid = update.effective_user.id
        favs = _get_user_namedays_favs(data, uid)
        favs = [x for x in favs if _norm_name(x) != _norm_name(name)]
        _set_user_namedays_favs(data, uid, favs)
        try:
            log_action(data, "namedays_fav_remove", update, extra=f"name={name}")
            save_data(data)
        except Exception:
            pass
        await _safe_edit(q, "⭐ Любими имена (обновено)", reply_markup=namedays_favs_menu(data, uid))
        return

    if q.data == "namedays:fav_clear":
        uid = update.effective_user.id
        _set_user_namedays_favs(data, uid, [])
        try:
            log_action(data, "namedays_fav_clear", update)
            save_data(data)
        except Exception:
            pass
        await _safe_edit(q, "🗑️ Любимите имена са изчистени.", reply_markup=namedays_favs_menu(data, uid))
        return

    if q.data == "orders:todo":
        await q.answer("Този модул ще го довършим следващ.", show_alert=True)
        return

    # -------- CAR --------
    if q.data == "car:show_all":
        await _safe_edit(q, car_summary(data), reply_markup=car_menu())
        return

    if q.data.startswith("car:show:"):
        field = q.data.split(":")[2]
        value = data["car"].get(field) or "няма запис"
        extra = days_left_text(data["car"].get(field, "")) if field in ("gtp", "vinetka") else None

        text = f"{CAR_LABELS[field]}\n📅 Текущо: {value}"
        if extra:
            text += f"\n{extra}"

        await _safe_edit(q, text, reply_markup=car_menu())
        return

    if q.data.startswith("car:set:"):
        field = q.data.split(":")[2]
        context.chat_data["mode"] = "car_edit"
        context.chat_data["car_field"] = field

        current = data["car"].get(field) or "—"
        hint = "\n(за ГТП/Винетка: формат ДД.ММ.ГГГГ, пример 24.01.2026)" if field in ("gtp", "vinetka") else ""
        await _safe_edit(q, f"{CAR_LABELS[field]}\nТекущо: {current}\n\n✍️ Напиши нова стойност/дата:{hint}")
        return

    # -------- 🐶 TIBO --------
    if q.data == "tibo:show_all":
        await _safe_edit(q, tibo_summary(data), reply_markup=tibo_menu())
        return

    if q.data.startswith("tibo:show:"):
        field = q.data.split(":")[2]
        val = data.get("tibo", {}).get(field) or "—"

        extra = ""
        if field == "bday":
            p = parse_bday(val)
            if p:
                left, nxt = days_until_birthday(p[0], p[1])
                extra = f"\n⏳ Остават {left} дни (на {nxt.strftime('%d.%m.%Y')})"
        else:
            dl = days_left_text(val)
            if dl:
                extra = f"\n{dl}"

        await _safe_edit(q, f"{TIBO_LABELS[field]}\n📅 Текущо: {val}{extra}", reply_markup=tibo_menu())
        return

    if q.data.startswith("tibo:set:"):
        field = q.data.split(":")[2]
        context.chat_data["mode"] = "tibo_edit"
        context.chat_data["tibo_field"] = field

        current = data.get("tibo", {}).get(field) or "—"
        hint = "\n(формат: ДД.ММ, пример 24.01)" if field == "bday" else "\n(формат: ДД.ММ.ГГГГ, пример 24.01.2026)"
        await _safe_edit(q, f"{TIBO_LABELS[field]}\nТекущо: {current}\n\n✍️ Напиши нова дата:{hint}")
        return

    # =========================
    # 🎂 BIRTHDAYS FULL MODULE
    # =========================
    if q.data == "bdays:add":
        context.chat_data.clear()
        context.chat_data["mode"] = "bdays_add_name"
        await _safe_edit(q, "🎂 Добавяне на рожден ден\n\n✍️ Напиши ИМЕ:")
        return

    if q.data == "bdays:show_buttons":
        items = data.get("birthdays", []) or []
        if not items:
            await _safe_edit(q, "🎂 Няма записани рождени дни.", reply_markup=bdays_menu())
            return
        offset = 0
        page_size = 8
        page = items[offset:offset + page_size]
        kb = _bdays_list_keyboard(page, offset=offset, total=len(items), page_size=page_size)
        await _safe_edit(q, f"🎂 Рождени дни ({len(items)})", reply_markup=kb)
        return

    if q.data.startswith("bdays:page:"):
        items = data.get("birthdays", []) or []
        if not items:
            await _safe_edit(q, "🎂 Няма записани рождени дни.", reply_markup=bdays_menu())
            return
        try:
            offset = int(q.data.split(":")[2])
        except Exception:
            offset = 0
        offset = max(0, min(offset, max(0, len(items) - 1)))
        page_size = 8
        page = items[offset:offset + page_size]
        kb = _bdays_list_keyboard(page, offset=offset, total=len(items), page_size=page_size)
        await _safe_edit(q, f"🎂 Рождени дни ({len(items)})", reply_markup=kb)
        return

    if q.data.startswith("bdays:view_abs:"):
        items = data.get("birthdays", []) or []
        abs_index = int(q.data.split(":")[2])
        if abs_index < 0 or abs_index >= len(items):
            await q.answer("Невалиден запис.", show_alert=True)
            return
        it = items[abs_index]
        name = it.get("name", "—")
        dstr = it.get("date", "—")
        extra = ""
        p = parse_bday(dstr)
        if p:
            left, nxt = days_until_birthday(p[0], p[1])
            extra = f"\n⏳ Остават {left} дни (на {nxt.strftime('%d.%m.%Y')})"
        await _safe_edit(q, f"🎂 {name}\n📅 {dstr}{extra}", reply_markup=_bdays_view_kb(abs_index))
        return

    if q.data.startswith("bdays:edit_abs:"):
        abs_index = int(q.data.split(":")[2])
        context.chat_data["mode"] = "bdays_edit_choose"
        context.chat_data["bdays_index"] = abs_index
        await _safe_edit(q, "✏️ Редакция\nИзбери какво да промениш:", reply_markup=_bdays_view_kb(abs_index))
        return

    if q.data.startswith("bdays:edit_name_abs:"):
        abs_index = int(q.data.split(":")[2])
        context.chat_data.clear()
        context.chat_data["mode"] = "bdays_edit_name"
        context.chat_data["bdays_index"] = abs_index
        cur = (data.get("birthdays", []) or [])[abs_index].get("name", "—") if 0 <= abs_index < len(data.get("birthdays", [])) else "—"
        await _safe_edit(q, f"✏️ Смяна на име\nТекущо: {cur}\n\nНапиши ново ИМЕ:")
        return

    if q.data.startswith("bdays:edit_date_abs:"):
        abs_index = int(q.data.split(":")[2])
        context.chat_data.clear()
        context.chat_data["mode"] = "bdays_edit_date"
        context.chat_data["bdays_index"] = abs_index
        cur = (data.get("birthdays", []) or [])[abs_index].get("date", "—") if 0 <= abs_index < len(data.get("birthdays", [])) else "—"
        await _safe_edit(q, f"✏️ Смяна на дата\nТекущо: {cur}\n\nНапиши нова дата (ДД.ММ или ДД.ММ.ГГГГ):")
        return

    if q.data.startswith("bdays:del_abs:"):
        abs_index = int(q.data.split(":")[2])
        await _safe_edit(q, "🗑️ Сигурен ли си, че искаш да изтриеш?", reply_markup=bdays_confirm_delete_kb(abs_index))
        return

    if q.data.startswith("bdays:del_yes:"):
        abs_index = int(q.data.split(":")[2])
        items = data.get("birthdays", []) or []
        if 0 <= abs_index < len(items):
            deleted = items.pop(abs_index)
            # 📜 log (birthday delete)
            try:
                log_action(data, "bday_delete", update, {"name": deleted.get("name","—"), "date": deleted.get("date","")})
            except Exception:
                pass
            save_data(data)
            await _safe_edit(q, f"✅ Изтрито: {deleted.get('name','—')}", reply_markup=bdays_menu())
        else:
            await q.answer("Невалиден запис.", show_alert=True)
        return

    if q.data == "bdays:del_no":
        await _safe_edit(q, "🎂 Рождени дни", reply_markup=bdays_menu())
        return

    if q.data == "bdays:next":
        best = _next_birthday_item(data)
        if not best:
            await _safe_edit(q, "🎂 Няма записани рождени дни.", reply_markup=bdays_menu())
            return
        left, nxt, it = best
        name = it.get("name", "—")
        await _safe_edit(q, f"⭐ Следващ рожден ден:\n• {name}\n📅 {nxt.strftime('%d.%m.%Y')}\n⏳ Остават {left} дни",
                         reply_markup=bdays_menu())
        return

    if q.data == "bdays:clear":
        await _safe_edit(q, "🧹 Сигурен ли си, че искаш да изчистиш всички рождени дни?", reply_markup=bdays_confirm_clear_kb())
        return

    if q.data == "bdays:clear_yes":
        # 📜 log (birthday clear)
        try:
            log_action(data, "bday_clear", update)
        except Exception:
            pass
        data["birthdays"] = []
        save_data(data)
        await _safe_edit(q, "✅ Изчистих всички рождени дни.", reply_markup=bdays_menu())
        return

    if q.data == "bdays:clear_no":
        await _safe_edit(q, "🎂 Рождени дни", reply_markup=bdays_menu())
        return

    # =========================
    # ✅ TASKS FULL MODULE
    # =========================
    if q.data == "tasks:add":
        context.chat_data.clear()
        context.chat_data["mode"] = "tasks_add_text"
        await _safe_edit(q, "✅ Добавяне на задача\n\n✍️ Напиши текста на задачата:")
        return

    if q.data == "tasks:show":
        tasks = data.get("tasks", []) or []
        if not tasks:
            await _safe_edit(q, "✅ Нямаш текущи задачи.", reply_markup=tasks_menu())
            return
        offset = 0
        page_size = 8
        page = tasks[offset:offset + page_size]
        kb = _tasks_show_keyboard(page, offset=offset, total=len(tasks), page_size=page_size)
        await _safe_edit(q, f"✅ Задачи ({len(tasks)})\n(натискаш ✔️, за да отметнеш)", reply_markup=kb)
        return

    if q.data.startswith("tasks:page:"):
        tasks = data.get("tasks", []) or []
        if not tasks:
            await _safe_edit(q, "✅ Нямаш текущи задачи.", reply_markup=tasks_menu())
            return
        try:
            offset = int(q.data.split(":")[2])
        except Exception:
            offset = 0
        offset = max(0, min(offset, max(0, len(tasks) - 1)))
        page_size = 8
        page = tasks[offset:offset + page_size]
        kb = _tasks_show_keyboard(page, offset=offset, total=len(tasks), page_size=page_size)
        await _safe_edit(q, f"✅ Задачи ({len(tasks)})\n(натискаш ✔️, за да отметнеш)", reply_markup=kb)
        return

    if q.data == "tasks:upcoming":
        tasks = data.get("tasks", []) or []
        today = datetime.now(SOFIA_TZ).date()
        upcoming = []
        for tsk in tasks:
            dstr = (tsk.get("date") or "").strip()
            dt = parse_bg_date_full(dstr) if dstr else None
            if dt and dt >= today:
                upcoming.append((dt, tsk))
        if not upcoming:
            await _safe_edit(q, "📅 Нямаш предстоящи задачи с дата.", reply_markup=tasks_menu())
            return
        upcoming.sort(key=lambda x: x[0])
        lines = ["📅 Предстоящи задачи:"]
        for dt, tsk in upcoming[:20]:
            lines.append(f"• {dt.strftime('%d.%m.%Y')} — {tsk.get('text','—')}")
        await _safe_edit(q, "\n".join(lines), reply_markup=tasks_menu())
        return

    if q.data == "tasks:done_pick":
        tasks = data.get("tasks", []) or []
        if not tasks:
            await _safe_edit(q, "✅ Нямаш текущи задачи.", reply_markup=tasks_menu())
            return
        await _safe_edit(q, "✔️ Избери задача за отметка:", reply_markup=tasks_pick_keyboard(tasks))
        return

    def _mark_done(task_obj: dict):
        done = {
            "text": task_obj.get("text", "—"),
            "date": task_obj.get("date", ""),
            "done_at": datetime.now(SOFIA_TZ).date().strftime("%d.%m.%Y")
        }
        data.setdefault("tasks_done", [])
        data["tasks_done"].append(done)

    if q.data.startswith("tasks:done:"):
        idx = int(q.data.split(":")[2]) - 1
        tasks = data.get("tasks", []) or []
        if 0 <= idx < len(tasks):
            task_obj = tasks.pop(idx)
            _mark_done(task_obj)
            # 📜 log (task done)
            try:
                log_action(data, "task_done", update, {"text": task_obj.get("text","—"), "date": task_obj.get("date","")})
            except Exception:
                pass
            save_data(data)
            await _safe_edit(q, "✅ Отметнах задачата като изпълнена.", reply_markup=tasks_menu())
        else:
            await q.answer("Невалиден избор.", show_alert=True)
        return

    if q.data.startswith("tasks:done_abs:"):
        abs_index = int(q.data.split(":")[2])
        tasks = data.get("tasks", []) or []
        if 0 <= abs_index < len(tasks):
            task_obj = tasks.pop(abs_index)
            _mark_done(task_obj)
            save_data(data)
            await _safe_edit(q, "✅ Отметнах задачата като изпълнена.", reply_markup=tasks_menu())
        else:
            await q.answer("Невалиден избор.", show_alert=True)
        return

    if q.data == "tasks:history":
        hist = data.get("tasks_done", []) or []
        if not hist:
            await _safe_edit(q, "📜 Нямаш история (изпълнени задачи).", reply_markup=tasks_menu())
            return
        lines = ["📜 История (последни 25):"]
        for it in hist[-25:][::-1]:
            txt = it.get("text", "—")
            d = it.get("date", "")
            done_at = it.get("done_at", "")
            line = f"• {txt}"
            if d:
                line += f" ({d})"
            if done_at:
                line += f" ✅ {done_at}"
            lines.append(line)
        await _safe_edit(q, "\n".join(lines), reply_markup=tasks_menu())
        return

    if q.data == "tasks:clear":
        await _safe_edit(q, "🧹 Сигурен ли си, че искаш да изчистиш всички текущи задачи?", reply_markup=tasks_confirm_clear_kb())
        return

    if q.data == "tasks:clear_yes":
        # 📜 log (clear tasks)
        try:
            log_action(data, "tasks_clear", update)
        except Exception:
            pass
        data["tasks"] = []
        save_data(data)
        await _safe_edit(q, "✅ Изчистих текущите задачи.", reply_markup=tasks_menu())
        return

    if q.data == "tasks:clear_no":
        await _safe_edit(q, "✅ Лични задачи", reply_markup=tasks_menu())
        return

    if q.data == "tasks:history_clear":
        await _safe_edit(q, "🧹 Сигурен ли си, че искаш да изчистиш историята?", reply_markup=tasks_confirm_history_clear_kb())
        return

    if q.data == "tasks:history_clear_yes":
        # 📜 log (clear history)
        try:
            log_action(data, "tasks_history_clear", update)
        except Exception:
            pass
        data["tasks_done"] = []
        save_data(data)
        await _safe_edit(q, "✅ Изчистих историята.", reply_markup=tasks_menu())
        return

    if q.data == "tasks:history_clear_no":
        await _safe_edit(q, "✅ Лични задачи", reply_markup=tasks_menu())
        return

    # fallback
    await q.answer("Неразпознат бутон.", show_alert=False)


# =========================
# TEXT INPUT
# =========================
async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    data = load_data()

    # 🔒 AUTH flow
    if not _is_authorized(update, data):
        mode_u = context.user_data.get("mode")
        if mode_u != "auth_password":
            context.user_data["mode"] = "auth_password"
            context.user_data["auth_tries"] = 0
            await update.message.reply_text("🔒 За достъп въведи парола:")
            return

        tries = int(context.user_data.get("auth_tries", 0))
        if _check_password(data, text):
            uid = update.effective_user.id
            _add_authorized_user(data, uid)
            # 📜 log + 🔔 admin alert
            try:
                log_action(data, "auth_success", update)
            except Exception:
                pass
            save_data(data)
            try:
                await _notify_admin(context, f"✅ Достъп разрешен: {_user_label(update)}\n💬 {_chat_label(update)}")
            except Exception:
                pass
            context.user_data.clear()
            await update.message.reply_text("✅ Достъп разрешен!")
            await smart_start_show(update, context)
            return

        # 📜 log + 🔔 admin alert (грешна парола)
        try:
            log_action(data, "auth_fail", update, {"tries_next": int(context.user_data.get("auth_tries", 0)) + 1})
        except Exception:
            pass
        try:
            await _notify_admin(context, f"❌ Грешна парола: {_user_label(update)}\n💬 {_chat_label(update)}")
        except Exception:
            pass

        tries += 1
        context.user_data["auth_tries"] = tries
        if tries >= 5:
            context.user_data.clear()
            # 📜 log + 🔔 admin alert (blocked)
            try:
                log_action(data, "auth_blocked", update)
                save_data(data)
            except Exception:
                pass
            try:
                await _notify_admin(context, f"⛔️ Блокиран (5 опита): {_user_label(update)}\n💬 {_chat_label(update)}")
            except Exception:
                pass
            await update.message.reply_text("⛔️ Твърде много опити. Напиши /start и опитай пак.")
            return
        await update.message.reply_text(f"❌ Грешна парола. Опит {tries}/5. Опитай пак:")
        return

    # ако е authorized -> нормалната логика
    mode = context.chat_data.get("mode")

    # умно отговаряне (само ако НЕ редактираш)
    if not mode:
        answered = await smart_text_answer(update, context, data, text)
        if answered:
            return

        # 🪄 свободен текст -> предложение за задача
        sug = _parse_natural_task(text, datetime.now(SOFIA_TZ).date())
        if sug:
            task_text, date_str = sug
            await _send_task_suggestion(update, context, task_text, date_str)
            return




    # 🔎 GLOBAL SEARCH
    if mode == "global_search":
        qtxt = text.strip()
        context.chat_data.clear()

        # tasks
        tasks = data.get("tasks", []) or []
        task_hits = []
        for t in tasks:
            if qtxt.lower() in (t.get("text", "").lower()):
                task_hits.append(t)

        # birthdays
        bdays = data.get("birthdays", []) or []
        b_hits = []
        for b in bdays:
            if qtxt.lower() in (b.get("name", "").lower()):
                b_hits.append(b)

        # namedays
        nd_dates = find_nameday_dates(qtxt)

        # car fields
        car = (data.get("car") or {})
        car_hits = []
        for k, v in car.items():
            if qtxt.lower() in str(v).lower() or qtxt.lower() in str(k).lower():
                car_hits.append((k, v))

        # tibo fields
        tibo = (data.get("tibo") or {})
        tibo_hits = []
        for k, v in tibo.items():
            if qtxt.lower() in str(v).lower() or qtxt.lower() in str(k).lower():
                tibo_hits.append((k, v))

        lines = [f"🔎 Резултати за: {qtxt}", ""]
        if task_hits:
            lines.append("✅ Задачи:")
            for t in task_hits[:5]:
                d = (t.get("date") or "").strip()
                suffix = f" ({d})" if d else ""
                lines.append(f"• {t.get('text','')}{suffix}")
            lines.append("")
        if b_hits:
            lines.append("🎂 Рождени дни:")
            for b in b_hits[:5]:
                lines.append(f"• {b.get('name','')} – {b.get('date','')}")
            lines.append("")
        if nd_dates:
            lines.append("🎉 Имени дни (календар):")
            for d in nd_dates[:10]:
                lines.append(f"• {d}")
            lines.append("")
        if car_hits:
            lines.append("🚗 Кола:")
            for k, v in car_hits[:5]:
                lines.append(f"• {k}: {v}")
            lines.append("")
        if tibo_hits:
            lines.append("🐶 Тибо:")
            for k, v in tibo_hits[:5]:
                lines.append(f"• {k}: {v}")
            lines.append("")

        if len(lines) <= 2:
            lines.append("— няма съвпадения —")

        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Меню", callback_data="back:main")],
        ]))
        return

    # 🎉 NAMEDAYS: search / favorite add
    if mode == "namedays_search":
        name = text.strip()
        context.chat_data.clear()
        dates = find_nameday_dates(name)
        try:
            log_action(data, "namedays_search", update, extra=f"name={name}")
            save_data(data)
        except Exception:
            pass
        if not dates:
            await update.message.reply_text(f"🔎 Не намерих „{name}“ в календара.", reply_markup=namedays_menu(data, update.effective_user.id))
            return
        await update.message.reply_text(f"🎉 Имен ден на „{name}“: {', '.join(dates)}", reply_markup=namedays_menu(data, update.effective_user.id))
        return

    if mode == "namedays_fav_add":
        name = text.strip()
        uid = update.effective_user.id
        favs = _get_user_namedays_favs(data, uid)
        if _norm_name(name) not in {_norm_name(x) for x in favs}:
            favs.append(name)
        _set_user_namedays_favs(data, uid, favs)
        save_data(data)
        context.chat_data.clear()
        try:
            log_action(data, "namedays_fav_add", update, extra=f"name={name}")
            save_data(data)
        except Exception:
            pass

        dates = find_nameday_dates(name)
        if dates:
            await update.message.reply_text(f"⭐ Добавено в любими: {name}\n📅 По календар: {', '.join(dates)}", reply_markup=namedays_menu(data, uid))
        else:
            await update.message.reply_text(f"⭐ Добавено в любими: {name}\n⚠️ Не го намирам в календара (провери правопис или добави във namedays_bg.json).", reply_markup=namedays_menu(data, uid))
        return

    # SETTINGS: set city
    if mode == "set_city":
        city = text.strip()
        if len(city) < 2:
            await update.message.reply_text("❌ Невалиден град. Пример: Sofia,BG")
            return
        data["settings"]["city"] = city
        save_data(data)
        context.chat_data.clear()
        await update.message.reply_text("✅ Запаметих!", reply_markup=settings_menu(data))
        return

    # CAR edit
    if mode == "car_edit":
        field = context.chat_data.get("car_field")
        if field:
            data["car"][field] = text
            save_data(data)
        context.chat_data.clear()
        await update.message.reply_text("✅ Запаметено!\n\n" + car_summary(data), reply_markup=car_menu())
        return

    # 🐶 TIBO edit
    if mode == "tibo_edit":
        field = context.chat_data.get("tibo_field")
        if field:
            data["tibo"][field] = text
            save_data(data)
        context.chat_data.clear()
        await update.message.reply_text("✅ Запаметено!\n\n" + tibo_summary(data), reply_markup=tibo_menu())
        return

    # =========================
    # 🎂 BIRTHDAYS TEXT FLOWS
    # =========================
    if mode == "bdays_add_name":
        context.chat_data["bdays_tmp_name"] = text
        context.chat_data["mode"] = "bdays_add_date"
        await update.message.reply_text("📅 Супер. Сега напиши дата (ДД.ММ или ДД.ММ.ГГГГ):\nПример: 24.01")
        return

    if mode == "bdays_add_date":
        name = (context.chat_data.get("bdays_tmp_name") or "").strip()
        dstr = text.strip()
        if not name:
            context.chat_data.clear()
            await update.message.reply_text("❌ Грешка: липсва име. Започни пак от 🎂 Рождени дни → ➕ Добави.")
            return
        if not _looks_like_bday(dstr):
            await update.message.reply_text("❌ Невалиден формат.\nНапиши дата така: ДД.ММ (пример 24.01) или ДД.ММ.ГГГГ")
            return
        data.setdefault("birthdays", [])
        data["birthdays"].append({"name": name, "date": dstr})
        save_data(data)
        context.chat_data.clear()
        await update.message.reply_text(f"✅ Добавено: {name} — {dstr}", reply_markup=bdays_menu())
        return

    if mode == "bdays_edit_name":
        abs_index = int(context.chat_data.get("bdays_index", -1))
        items = data.get("birthdays", []) or []
        if not (0 <= abs_index < len(items)):
            context.chat_data.clear()
            await update.message.reply_text("❌ Невалиден запис.")
            return
        items[abs_index]["name"] = text.strip()
        save_data(data)
        context.chat_data.clear()
        await update.message.reply_text("✅ Името е променено.", reply_markup=bdays_menu())
        return

    if mode == "bdays_edit_date":
        abs_index = int(context.chat_data.get("bdays_index", -1))
        items = data.get("birthdays", []) or []
        if not (0 <= abs_index < len(items)):
            context.chat_data.clear()
            await update.message.reply_text("❌ Невалиден запис.")
            return
        if not _looks_like_bday(text.strip()):
            await update.message.reply_text("❌ Невалиден формат.\nНапиши: ДД.ММ (пример 24.01) или ДД.ММ.ГГГГ")
            return
        items[abs_index]["date"] = text.strip()
        save_data(data)
        context.chat_data.clear()
        await update.message.reply_text("✅ Датата е променена.", reply_markup=bdays_menu())
        return

    # =========================
    # ✅ TASKS TEXT FLOWS
    # =========================
    if mode == "tasks_add_text":
        context.chat_data["tasks_tmp_text"] = text
        context.chat_data["mode"] = "tasks_add_date"
        await update.message.reply_text(
            "📅 Искаш ли дата?\n"
            "Напиши ДД.ММ.ГГГГ (пример 24.01.2026)\n"
            "или напиши - (тире) ако е без дата."
        )
        return

    if mode == "tasks_add_date":
        ttxt = (context.chat_data.get("tasks_tmp_text") or "").strip()
        dstr = text.strip()
        if not ttxt:
            context.chat_data.clear()
            await update.message.reply_text("❌ Грешка: липсва текст. Започни пак от ✅ Задачи → ➕ Добави.")
            return

        final_date = ""
        if dstr not in ("-", "—", ""):
            if not _looks_like_full_date(dstr):
                await update.message.reply_text("❌ Невалидна дата.\nПолзвай ДД.ММ.ГГГГ (пример 24.01.2026) или - за без дата.")
                return
            final_date = dstr

        data.setdefault("tasks", [])
        data["tasks"].append({"text": ttxt, "date": final_date})
        # 📜 log
        try:
            log_action(data, "task_add", update, {"text": ttxt, "date": final_date})
        except Exception:
            pass
        save_data(data)
        # 🔔 broadcast към всички потребители
        try:
            await _broadcast_task_added(context, update, ttxt, final_date)
        except Exception:
            pass
        context.chat_data.clear()
        await update.message.reply_text("✅ Добавих задачата.", reply_markup=tasks_menu())
        return

    await update.message.reply_text("Напиши /start (или /stat) и използвай бутоните 🙂")


# =========================
# MAIN
# =========================
def main():
    # Ако нямаш JobQueue: pip install "python-telegram-bot[job-queue]"
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    if app.job_queue is None:
        print('❌ Няма JobQueue. Инсталирай: pip install "python-telegram-bot[job-queue]"')
    else:
        # ✅ 09:00 Europe/Sofia
        app.job_queue.run_daily(daily_check, time=dtime(hour=9, minute=0, tzinfo=SOFIA_TZ))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("stat", stat))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("setpass", setpass))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("log", show_last_actions))
    app.add_handler(CallbackQueryHandler(startup_start, pattern=r"^startup:start$"))

    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    app.run_polling()


if __name__ == "__main__":
    main()
