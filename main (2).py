#!/usr/bin/env python3
"""
🌟 بوت استضافة بوتات Telegram الاحترافي - نسخة المطوّرين الفائقة (مُحسّن ومستقر)
المطوّر الأساسي: @banhi5
"""

import asyncio
import datetime as dt
import html
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================
# الإعدادات الأساسية
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9206735472:AAGRmgbaI1aU--IY3LiPU7t-7SmeXFNhPT4")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "8394163581"))

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploaded_bots"
DB_PATH = BASE_DIR / "database.db"

MAX_ZIP_MB = int(os.getenv("MAX_ZIP_MB", "100"))
MAX_ZIP_FILES = int(os.getenv("MAX_ZIP_FILES", "2000"))
MAX_EXTRACTED_MB = int(os.getenv("MAX_EXTRACTED_MB", "1024"))

TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

_running_processes: dict[int, subprocess.Popen] = {}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def now_iso() -> str:
    return utcnow().isoformat()


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:128]


def valid_main_file(name: str) -> bool:
    if not name or not name.endswith(".py"):
        return False
    p = Path(name)
    return len(p.parts) == 1 and p.name == name


# =========================
# قاعدة البيانات المتقدمة مع التحديث التلقائي
# =========================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    points INTEGER NOT NULL DEFAULT 0,
    vip INTEGER NOT NULL DEFAULT 0,
    referred_by INTEGER,
    last_daily TEXT,
    joined_at TEXT,
    is_banned INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS force_channels (
    chat_id TEXT PRIMARY KEY,
    title TEXT
);

CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    file_path TEXT,
    file_name TEXT,
    main_file TEXT,
    bot_name TEXT,
    bot_token TEXT,
    status TEXT DEFAULT 'pending',
    pid INTEGER,
    container_name TEXT,
    package_id INTEGER,
    price_points INTEGER NOT NULL DEFAULT 0,
    duration_days INTEGER NOT NULL DEFAULT 30,
    ram_mb INTEGER NOT NULL DEFAULT 256,
    cpu_percent INTEGER NOT NULL DEFAULT 50,
    disk_mb INTEGER NOT NULL DEFAULT 500,
    expires_at TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price_points INTEGER NOT NULL,
    duration_days INTEGER NOT NULL DEFAULT 30,
    ram_mb INTEGER NOT NULL,
    cpu_percent INTEGER NOT NULL,
    disk_mb INTEGER NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS points_links (
    code TEXT PRIMARY KEY,
    points INTEGER NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_by INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS points_link_redemptions (
    code TEXT,
    user_id INTEGER,
    redeemed_at TEXT,
    PRIMARY KEY (code, user_id)
);

CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    created_at TEXT
);
"""

DEFAULT_SETTINGS = {
    "daily_reward_enabled": "1",
    "daily_reward_amount": "5",
    "referral_reward": "10",
    "stars_per_point": "1",
    "dev_username": "banhi5",
    "bot_maintenance": "0"
}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)

        await db.execute(
            "INSERT OR IGNORE INTO admins(user_id, added_at) VALUES(?, ?)",
            (SUPER_ADMIN_ID, now_iso()),
        )

        user_cols = {row[1] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        if "is_banned" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")

        bot_cols = {row[1] for row in await (await db.execute("PRAGMA table_info(bots)")).fetchall()}
        if "duration_days" not in bot_cols:
            await db.execute("ALTER TABLE bots ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 30")
        if "container_name" not in bot_cols:
            await db.execute("ALTER TABLE bots ADD COLUMN container_name TEXT")

        pkg_cols = {row[1] for row in await (await db.execute("PRAGMA table_info(packages)")).fetchall()}
        if "duration_days" not in pkg_cols:
            await db.execute("ALTER TABLE packages ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 30")

        for key, value in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
        await db.commit()


async def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))).fetchone()
        return row is not None


class IsAdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id = event.from_user.id if event.from_user else 0
        return await is_admin(user_id)


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO settings(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        await db.commit()


async def get_or_create_user(user_id: int, username: str, referred_by: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT user_id, is_banned FROM users WHERE user_id=?", (user_id,))).fetchone()
        if row:
            await db.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            await db.commit()
            return False, bool(row[1])

        if referred_by == user_id:
            referred_by = None
        if referred_by is not None:
            exists = await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (referred_by,))).fetchone()
            if not exists:
                referred_by = None

        await db.execute(
            """INSERT INTO users(user_id,username,points,vip,referred_by,joined_at,is_banned)
               VALUES(?,?,?,?,?,?,0)""",
            (user_id, username, 0, 0, referred_by, now_iso()),
        )
        await db.commit()
        return True, False


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()


async def update_points(user_id: int, delta: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points=MAX(0, points+?) WHERE user_id=?", (delta, user_id))
        await db.commit()


async def claim_daily(user_id: int, amount: int) -> bool:
    today = dt.date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT last_daily FROM users WHERE user_id=?", (user_id,))).fetchone()
        if row and row[0] == today:
            return False
        await db.execute("UPDATE users SET points=points+?,last_daily=? WHERE user_id=?", (amount, today, user_id))
        await db.commit()
        return True


async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM users WHERE is_banned=0")).fetchall()
        return [r[0] for r in rows]


async def add_force_channel(chat_id: str, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO force_channels(chat_id,title) VALUES(?,?)", (chat_id, title))
        await db.commit()


async def remove_force_channel(chat_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM force_channels WHERE chat_id=?", (chat_id,))
        await db.commit()


async def list_force_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM force_channels ORDER BY title")).fetchall()


async def create_bot_upload(user_id, file_path, file_name, main_file, bot_name,
                            bot_token, package_id, price_points, duration_days,
                            ram_mb, cpu_percent, disk_mb):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO bots(
                user_id,file_path,file_name,main_file,bot_name,bot_token,status,
                package_id,price_points,duration_days,ram_mb,cpu_percent,disk_mb,created_at
            ) VALUES(?,?,?,?,?,?, 'pending',?,?,?,?,?,?,?)""",
            (
                user_id, str(file_path), file_name, main_file, bot_name, bot_token,
                package_id, price_points, duration_days, ram_mb, cpu_percent, disk_mb, now_iso()
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_bot(bot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM bots WHERE id=?", (bot_id,))).fetchone()


async def get_user_bots(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM bots WHERE user_id=? ORDER BY id DESC", (user_id,))).fetchall()


async def get_pending_bots():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM bots WHERE status='pending' ORDER BY id ASC")).fetchall()


async def set_bot_status(bot_id: int, status: str, pid=None, container_name=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET status=?, pid=?, container_name=? WHERE id=?",
            (status, pid, container_name, bot_id),
        )
        await db.commit()


async def activate_bot(bot_id: int):
    record = await get_bot(bot_id)
    days = int(record["duration_days"] or 30)
    expires = (utcnow() + dt.timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET status='approved', expires_at=?, reminder_sent=0 WHERE id=?",
            (expires, bot_id),
        )
        await db.commit()
    return expires


async def renew_bot(bot_id: int):
    record = await get_bot(bot_id)
    if not record:
        return None

    days = int(record["duration_days"] or 30)
    now = utcnow()
    try:
        current = dt.datetime.fromisoformat(record["expires_at"]) if record["expires_at"] else now
    except (TypeError, ValueError):
        current = now

    base = max(current, now)
    expires = (base + dt.timedelta(days=days)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET expires_at=?, reminder_sent=0 WHERE id=?",
            (expires, bot_id),
        )
        await db.commit()
    return expires


async def get_bots_needing_reminder(threshold_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            """SELECT * FROM bots
               WHERE status IN('approved','running','stopped','crashed')
               AND expires_at IS NOT NULL
               AND expires_at<=?
               AND reminder_sent=0""",
            (threshold_iso,),
        )).fetchall()


async def mark_reminder_sent(bot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bots SET reminder_sent=1 WHERE id=?", (bot_id,))
        await db.commit()


async def get_expired_bots(now_iso_value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            """SELECT * FROM bots
               WHERE expires_at IS NOT NULL
               AND expires_at<=?""",
            (now_iso_value,),
        )).fetchall()


async def delete_bot_record(bot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        await db.commit()


async def create_package(name, price_points, duration_days, ram_mb, cpu_percent, disk_mb):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO packages
               (name,price_points,duration_days,ram_mb,cpu_percent,disk_mb,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (name, price_points, duration_days, ram_mb, cpu_percent, disk_mb, now_iso()),
        )
        await db.commit()
        return cur.lastrowid


async def list_packages():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM packages ORDER BY price_points ASC")).fetchall()


async def get_package(package_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM packages WHERE id=?", (package_id,))).fetchone()


async def delete_package(package_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM packages WHERE id=?", (package_id,))
        await db.commit()


async def create_points_link(code, points, max_uses, expires_at, created_by):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO points_links 
               (code, points, max_uses, used_count, expires_at, created_by, created_at) 
               VALUES(?,?,?,?,?,?,?)""",
            (code, points, max_uses, 0, expires_at, created_by, now_iso()),
        )
        await db.commit()


async def redeem_points_link(code, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        link = await (await db.execute("SELECT * FROM points_links WHERE code=?", (code,))).fetchone()
        if not link:
            return "not_found"

        if link["expires_at"] and link["expires_at"] < now_iso():
            return "expired"

        if link["max_uses"] > 0 and link["used_count"] >= link["max_uses"]:
            return "max_reached"

        row = await (await db.execute(
            "SELECT 1 FROM points_link_redemptions WHERE code=? AND user_id=?",
            (code, user_id),
        )).fetchone()
        if row:
            return "already_used"

        await db.execute(
            "INSERT INTO points_link_redemptions(code,user_id,redeemed_at) VALUES(?,?,?)",
            (code, user_id, now_iso()),
        )
        await db.execute(
            "UPDATE points_links SET used_count = used_count + 1 WHERE code=?",
            (code,)
        )
        await db.execute("UPDATE users SET points=points+? WHERE user_id=?", (int(link["points"]), user_id))
        await db.commit()
        return int(link["points"])


def folder_size_mb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def safe_extract_zip(zip_path: Path, extract_to: Path, max_files: int, max_extracted_mb: int):
    extract_root = extract_to.resolve()
    total_uncompressed = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        if len(members) > max_files:
            raise ValueError("عدد ملفات ZIP أكبر من الحد المسموح.")

        for member in members:
            name = member.filename
            target = (extract_root / name).resolve()
            try:
                target.relative_to(extract_root)
            except ValueError:
                raise ValueError("ZIP يحتوي على مسار غير آمن.")

            if name.startswith("/") or name.startswith("\\"):
                raise ValueError("مسار غير مسموح.")

            total_uncompressed += member.file_size
            if total_uncompressed > max_extracted_mb * 1024 * 1024:
                raise ValueError("حجم الملفات بعد فك الضغط تجاوز حد الباقة.")

        zf.extractall(extract_root)


async def ensure_requirements(bot_dir: Path, log_path: Path) -> Path:
    bot_dir.mkdir(parents=True, exist_ok=True)
    req = bot_dir / "requirements.txt"
    marker = bot_dir / ".requirements_installed"

    if req.exists() and not marker.exists():
        with log_path.open("a", encoding="utf-8", errors="ignore") as log:
            log.write("\n[System] Installing requirements.txt...\n")
            log.flush()
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(req)],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError("فشل تثبيت requirements.txt. راجع ملف الـ Log.")
        try:
            marker.write_text("ok", encoding="utf-8")
        except OSError:
            pass

    return Path(sys.executable)


async def start_bot(bot_id: int):
    record = await get_bot(bot_id)
    if not record:
        return False, "❌ البوت غير موجود."

    if record["status"] == "running":
        alive = await is_bot_alive(record)
        if alive:
            return False, "⚠️ البوت يعمل بالفعل."
        await set_bot_status(bot_id, "crashed", None, None)

    if record["expires_at"]:
        try:
            if dt.datetime.fromisoformat(record["expires_at"]) <= utcnow():
                return False, "❌ انتهت مدة اشتراك البوت. جدد أولاً ليتم تشغيله."
        except ValueError:
            pass

    file_path = Path(record["file_path"]).resolve()
    if not file_path.exists():
        return False, "❌ الملف الرئيسي غير موجود."

    bot_dir = file_path.parent
    log_path = bot_dir / "bot.log"

    try:
        python_bin = await ensure_requirements(bot_dir, log_path)
    except Exception as exc:
        return False, f"❌ خطأ أثناء تجهيز البيئة: {exc}"

    env = os.environ.copy()
    env["BOT_TOKEN"] = record["bot_token"] or ""

    log = log_path.open("a", encoding="utf-8", errors="ignore")
    log.write(f"\n[{now_iso()}] Starting bot process\n")
    log.flush()

    try:
        process = subprocess.Popen(
            [str(python_bin), str(file_path)],
            cwd=str(bot_dir),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
    except Exception as e:
        log.close()
        return False, f"❌ فشل تشغيل السيرفر للبوت: {e}"
    finally:
        log.close()

    _running_processes[bot_id] = process
    await set_bot_status(bot_id, "running", process.pid, None)
    return True, "✅ تم تشغيل البوت بنجاح."


async def stop_bot(bot_id: int):
    record = await get_bot(bot_id)
    if not record:
        return False

    process = _running_processes.pop(bot_id, None)
    pid = process.pid if process else record["pid"]

    if process:
        try:
            if process.poll() is None:
                if os.name != "nt":
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
                await asyncio.sleep(1)
                if process.poll() is None:
                    process.kill()
        except Exception:
            pass
    elif pid and os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    await set_bot_status(bot_id, "stopped", None, None)
    return True


async def is_bot_alive(record) -> bool:
    process = _running_processes.get(record["id"])
    if process:
        return process.poll() is None
    if record["pid"]:
        try:
            os.kill(int(record["pid"]), 0)
            return True
        except OSError:
            return False
    return False


async def delete_bot_files(bot_id: int):
    record = await get_bot(bot_id)
    if not record:
        return

    await stop_bot(bot_id)

    if record["file_path"]:
        bot_dir = Path(record["file_path"]).parent
        try:
            bot_dir.resolve().relative_to(UPLOADS_DIR.resolve())
            if bot_dir.exists():
                await asyncio.to_thread(shutil.rmtree, bot_dir, True)
        except ValueError:
            logging.error("Refusing to delete path outside uploads: %s", bot_dir)


# =========================
# Background Workers (مُحسّنة لتقليل استهلاك الموارد)
# =========================
async def process_monitor_worker():
    while True:
        await asyncio.sleep(30)  # زيادة الفاصل الزمني لمنع الضغط على المعالج
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                records = await (await db.execute("SELECT * FROM bots WHERE status='running'")).fetchall()

            for record in records:
                alive = await is_bot_alive(record)
                if not alive:
                    logging.warning("Bot %s crashed/stopped.", record["id"])
                    await set_bot_status(record["id"], "crashed", None, None)
                    _running_processes.pop(record["id"], None)
        except Exception:
            pass


async def expiry_worker(bot: Bot):
    while True:
        await asyncio.sleep(120)
        try:
            now = utcnow()
            reminder_threshold = (now + dt.timedelta(days=1)).isoformat()
            for record in await get_bots_needing_reminder(reminder_threshold):
                try:
                    await bot.send_message(
                        record["user_id"],
                        f"⚠️ <b>تنبيه باقتراب انتهاء الاستضافة:</b>\n"
                        f"بوت «{html.escape(record['bot_name'])}» سينتهي اشتراكه في {record['expires_at'][:10]}.\n"
                        f"يرجى التجديد لتفادي إيقافه وحذفه نهائياً من السيرفر.",
                    )
                except Exception:
                    pass
                await mark_reminder_sent(record["id"])

            for record in await get_expired_bots(now.isoformat()):
                await delete_bot_files(record["id"])
                await delete_bot_record(record["id"])
                try:
                    await bot.send_message(
                        record["user_id"],
                        f"🚨 <b>تم حذف البوت نهائياً!</b>\n"
                        f"تم إيقاف وحذف بوت «{html.escape(record['bot_name'])}» وجميع ملفاته من السيرفر "
                        f"بسبب انتهاء فترة الاستضافة المحددة للباقة دون تجديد.",
                    )
                except Exception:
                    pass
        except Exception:
            logging.exception("Expiry worker error")


# =========================
# Subscription & UI
# =========================
async def check_subscription(bot: Bot, user_id: int):
    missing = []
    for channel in await list_force_channels():
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in {"left", "kicked"}:
                missing.append(channel)
        except Exception:
            continue
    return missing


def subscription_kb(missing):
    b = InlineKeyboardBuilder()
    for ch in missing:
        cid = str(ch["chat_id"])
        url = f"https://t.me/{cid[1:]}" if cid.startswith("@") else f"https://t.me/{cid}"
        b.button(text=f"📢 انضم إلى {ch['title']}", url=url)
    b.button(text="✅ تأكيد الاشتراك", callback_data="check_sub")
    b.adjust(1)
    return b.as_markup()


def user_menu(dev_username: str):
    b = InlineKeyboardBuilder()
    b.button(text="📦 رفع بوت جديد (ZIP)", callback_data="user_upload_bot")
    b.button(text="💻 رفع بوت عبر كود مباشر", callback_data="user_upload_code")
    b.button(text="📂 بوتاتي المرفوعة", callback_data="user_my_bots")
    b.button(text="🎁 الهدية اليومية", callback_data="user_daily")
    b.button(text="👥 نظام الإحالة", callback_data="user_referral")
    b.button(text="⭐ شحن نقاط", callback_data="user_buy_points")
    if dev_username:
        b.button(text="💬 تواصل مع المطور", url=f"https://t.me/{dev_username}")
    b.button(text="ℹ️ معلومات الحساب", callback_data="user_profile")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def back_user():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 العودة للقائمة الرئيسية", callback_data="user_menu")
    return b.as_markup()


def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📢 إذاعة عامة", callback_data="admin_broadcast")
    b.button(text="📥 طلبات الاستضافة المعلقة", callback_data="admin_pending_bots")
    b.button(text="📦 إدارة الباقات", callback_data="admin_packages")
    b.button(text="👑 إدارة المشرفين", callback_data="admin_manage_admins")
    b.button(text="📌 إضافة قناة إجبارية", callback_data="admin_add_channel")
    b.button(text="🗑 إزالة قناة إجبارية", callback_data="admin_remove_channel")
    b.button(text="🎁 إعدادات الهدية اليومية", callback_data="admin_daily_settings")
    b.button(text="👥 مكافأة الإحالة", callback_data="admin_set_referral_price")
    b.button(text="⭐ سعر النقاط (Stars)", callback_data="admin_set_points_price")
    b.button(text="🔗 إنشاء رابط نقاط هدية", callback_data="admin_create_points_link")
    b.button(text="➕ منح نقاط لمستخدم", callback_data="admin_give_points")
    b.button(text="⚙️ تعديل معرف المطور", callback_data="admin_set_dev")
    b.button(text="📊 إحصائيات السيرفر", callback_data="admin_stats")
    b.button(text="🔙 العودة لبوت المستخدم", callback_data="user_menu")
    b.adjust(1, 2, 2, 2, 2, 2, 2, 2, 1)
    return b.as_markup()


def back_admin():
    b = InlineKeyboardBuilder()
    b.button(text="🔙 العودة لوحة التحكم", callback_data="admin_menu")
    return b.as_markup()


def package_select(packages, action_prefix="select_package"):
    b = InlineKeyboardBuilder()
    for p in packages:
        b.button(
            text=f"📦 {p['name']} | 💰 {p['price_points']} نقطة | ⏳ {p['duration_days']} يوم",
            callback_data=f"{action_prefix}:{p['id']}",
        )
    b.button(text="🔙 رجوع", callback_data="user_menu")
    b.adjust(1)
    return b.as_markup()


def my_bots_kb(bots):
    b = InlineKeyboardBuilder()
    for bot in bots:
        emoji = {
            "running": "🟢",
            "stopped": "🔴",
            "approved": "🟡",
            "pending": "⏳",
            "crashed": "⚠️",
        }.get(bot["status"], "❔")
        b.button(text=f"{emoji} {bot['bot_name']}", callback_data=f"bot_manage:{bot['id']}")
    b.button(text="🔙 رجوع", callback_data="user_menu")
    b.adjust(1)
    return b.as_markup()


def bot_manage_kb(record):
    b = InlineKeyboardBuilder()
    if record["status"] == "running":
        b.button(text="🔴 إيقاف التشغيل", callback_data=f"bot_stop:{record['id']}")
        b.button(text="🔄 إعادة تشغيل", callback_data=f"bot_restart:{record['id']}")
    elif record["status"] in {"stopped", "approved", "crashed"}:
        b.button(text="🟢 تشغيل البوت", callback_data=f"bot_start:{record['id']}")
        if record["status"] == "crashed":
            b.button(text="🔄 إعادة تشغيل", callback_data=f"bot_restart:{record['id']}")

    if record["status"] in {"approved", "running", "stopped", "crashed"}:
        b.button(text="✏️ تعديل كود البوت", callback_data=f"bot_edit_code:{record['id']}")
        b.button(
            text=f"🔁 تجديد ({record['price_points']} نقطة)",
            callback_data=f"bot_renew:{record['id']}",
        )
        b.button(text="📄 سجل الأخطاء (Log)", callback_data=f"bot_log:{record['id']}")

    b.button(text="🗑 حذف نهائي", callback_data=f"bot_delete:{record['id']}")
    b.button(text="🔙 بوتاتي", callback_data="user_my_bots")
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


def approval_kb(bot_id):
    b = InlineKeyboardBuilder()
    b.button(text="✅ قبول الموافقة", callback_data=f"approve_bot:{bot_id}")
    b.button(text="❌ رفض الطلب", callback_data=f"reject_bot:{bot_id}")
    b.adjust(2)
    return b.as_markup()


def package_admin_kb(packages):
    b = InlineKeyboardBuilder()
    for p in packages:
        b.button(
            text=f"📦 {p['name']} ({p['price_points']} نقطة - {p['duration_days']} يوم)",
            callback_data=f"package_view:{p['id']}",
        )
    b.button(text="➕ إضافة باقة جديدة", callback_data="admin_add_package")
    b.button(text="🔙 العودة لوحة التحكم", callback_data="admin_menu")
    b.adjust(1)
    return b.as_markup()


# =========================
# حالات المحادثة (FSM States)
# =========================
class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_channel_add = State()
    waiting_channel_remove = State()
    waiting_daily_amount = State()
    waiting_referral_price = State()
    waiting_points_price = State()
    waiting_link_points = State()
    waiting_link_uses = State()
    waiting_link_days = State()
    waiting_package_name = State()
    waiting_package_price = State()
    waiting_package_days = State()
    waiting_package_limits = State()
    waiting_give_points_user = State()
    waiting_give_points_amount = State()
    waiting_dev_username = State()
    waiting_add_admin_id = State()
    waiting_remove_admin_id = State()


class UserStates(StatesGroup):
    waiting_bot_name = State()
    waiting_bot_token = State()
    waiting_main_filename = State()
    waiting_bot_file = State()
    waiting_code_content = State()
    waiting_code_requirements = State()
    waiting_buy_points_amount = State()
    waiting_edit_code_content = State()


# =========================
# راوتر لوحة تحكم الأدمن (Admin Router)
# =========================
admin_router = Router()
admin_router.message.filter(IsAdminFilter())
admin_router.callback_query.filter(IsAdminFilter())


@admin_router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🛠 <b>لوحة التحكم الاحترافية للإدارة العليا</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "مرحباً بك يا أدمن، يمكنك إدارة الباقات، المشرفين، الأعضاء، والبوتات بكل مرونة:",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data == "admin_menu")
async def admin_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🛠 <b>لوحة التحكم الاحترافية للإدارة العليا</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "اختر القسم المطلوب لإدارته:",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data == "admin_stats")
async def admin_stats_cb(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        users_count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        bots_count = (await (await db.execute("SELECT COUNT(*) FROM bots")).fetchone())[0]
        running_bots = (await (await db.execute("SELECT COUNT(*) FROM bots WHERE status='running'")).fetchone())[0]
        pending_bots = (await (await db.execute("SELECT COUNT(*) FROM bots WHERE status='pending'")).fetchone())[0]
        admins_count = (await (await db.execute("SELECT COUNT(*) FROM admins")).fetchone())[0]

    await callback.message.edit_text(
        f"📊 <b>إحصائيات السيرفر والبوت الشاملة:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 إجمالي المستخدمين: <code>{users_count}</code>\n"
        f"👑 إجمالي المشرفين: <code>{admins_count}</code>\n"
        f"🤖 إجمالي البوتات المرفوعة: <code>{bots_count}</code>\n"
        f"🟢 البوتات التي تعمل حالياً: <code>{running_bots}</code>\n"
        f"⏳ الطلبات المعلقة بانتظار الموافقة: <code>{pending_bots}</code>",
        reply_markup=back_admin(),
    )


@admin_router.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins_cb(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id, added_at FROM admins")).fetchall()
    
    txt = "👑 <b>قائمة المشرفين المعتمدين في البوت:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        txt += f"• الآيدي: <code>{r[0]}</code> (أضيف في: {r[1][:10]})\n"

    b = InlineKeyboardBuilder()
    b.button(text="➕ إضافة مشرف جديد", callback_data="admin_add_admin")
    b.button(text="🗑 إزالة مشرف", callback_data="admin_remove_admin")
    b.button(text="🔙 العودة لوحة التحكم", callback_data="admin_menu")
    b.adjust(1)
    await callback.message.edit_text(txt, reply_markup=b.as_markup())


@admin_router.callback_query(F.data == "admin_add_admin")
async def admin_add_admin_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_add_admin_id)
    await callback.message.edit_text("👑 أرسل آيدي (ID) المستخدم المراد تعيينه مشرفاً:", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_add_admin_id)
async def admin_add_admin_save(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("⚠️ أدخل آيدي صحيح بالأرقام.")
        return
    uid = int(message.text.strip())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins(user_id, added_at) VALUES(?, ?)", (uid, now_iso()))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ تم تعيين المستخدم <code>{uid}</code> مشرفاً جديداً بنجاح.", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_remove_admin")
async def admin_remove_admin_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_remove_admin_id)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM admins")).fetchall()
    txt = "🗑 أرسل آيدي المشرف المراد إزالته من القائمة التالية:\n" + "".join([f"• <code>{r[0]}</code>\n" for r in rows])
    await callback.message.edit_text(txt, reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_remove_admin_id)
async def admin_remove_admin_save(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("⚠️ أدخل آيدي صحيح بالأرقام.")
        return
    uid = int(message.text.strip())
    if uid == SUPER_ADMIN_ID:
        await message.answer("⚠️ لا يمكنك إزالة السوبر أدمن الأساسي.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ تم إزالة المشرف <code>{uid}</code> بنجاح.", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_create_points_link")
async def admin_points_link_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_link_points)
    await callback.message.edit_text("🎁 أرسل عدد النقاط التي سيحصل عليها من يستخدم الرابط:", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_link_points)
async def admin_link_points(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("⚠️ أدخل رقماً صحيحاً أكبر من صفر.")
        return
    await state.update_data(points=int(message.text))
    await state.set_state(AdminStates.waiting_link_uses)
    await message.answer("👥 أرسل أقصى عدد للمستخدمين المسموح لهم استخدام هذا الرابط (أرسل <code>0</code> إذا كان غير محدود):")


@admin_router.message(AdminStates.waiting_link_uses)
async def admin_link_uses(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("⚠️ أدخل رقماً صحيحاً.")
        return
    await state.update_data(max_uses=int(message.text))
    await state.set_state(AdminStates.waiting_link_days)
    await message.answer("⏳ أرسل مدة صلاحية الرابط بالأيام (أرسل <code>0</code> ليبقى صالحاً للأبد دون إنهاء):")


@admin_router.message(AdminStates.waiting_link_days)
async def admin_link_days(message: Message, state: FSMContext, bot: Bot):
    if not message.text.isdigit():
        await message.answer("⚠️ أدخل رقماً صحيحاً.")
        return
    days = int(message.text)
    data = await state.get_data()
    points = data["points"]
    max_uses = data["max_uses"]

    expires_at = None
    if days > 0:
        expires_at = (utcnow() + dt.timedelta(days=days)).isoformat()

    code = uuid.uuid4().hex[:12]
    await create_points_link(code, points, max_uses, expires_at, message.from_user.id)
    me = await bot.get_me()

    await state.clear()
    await message.answer(
        f"✅ <b>تم إنشاء رابط الهدية بنجاح باهر:</b>\n"
        f"<code>https://t.me/{me.username}?start=points_{code}</code>\n\n"
        f"⭐ القيمة: <b>{points}</b> نقطة\n"
        f"👥 الحد الأقصى للاستخدام: <b>{'غير محدود' if max_uses == 0 else max_uses}</b>\n"
        f"⏳ الصلاحية: <b>{f'{days} يوم' if days > 0 else 'دائم'}</b>",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data == "admin_set_dev")
async def admin_set_dev_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_dev_username)
    await callback.message.edit_text("⚙️ أرسل معرف المطور الجديد (بدون @):\nمثال: <code>banhi5</code>", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_dev_username)
async def admin_set_dev_save(message: Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    await set_setting("dev_username", username)
    await state.clear()
    await message.answer(f"✅ تم تحديث معرف المطور إلى: @{username}", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_give_points")
async def admin_give_points_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_give_points_user)
    await callback.message.edit_text("➕ أرسل آيدي (ID) المستخدم المراد منحه النقاط:", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_give_points_user)
async def admin_give_points_user(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("⚠️ أدخل آيدي صحيح بالأرقام.")
        return
    await state.update_data(target_user=int(message.text.strip()))
    await state.set_state(AdminStates.waiting_give_points_amount)
    await message.answer("⭐ أرسل عدد النقاط المراد إضافتها (سالب للخصم):")


@admin_router.message(AdminStates.waiting_give_points_amount)
async def admin_give_points_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ أدخل رقماً صحيحاً.")
        return
    data = await state.get_data()
    target_user = data["target_user"]
    user = await get_user(target_user)
    if not user:
        await message.answer("❌ المستخدم غير مسجل.", reply_markup=admin_menu())
        await state.clear()
        return
    await update_points(target_user, amount)
    await state.clear()
    await message.answer(f"✅ تم تعديل نقاط المستخدم <code>{target_user}</code> بمقدار {amount} نقطة.", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.edit_text("📢 أرسل الرسالة (صورة، نص، فيديو) لنشرها لكافة المستخدمين:", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_broadcast)
async def admin_broadcast(message: Message, state: FSMContext):
    await state.clear()
    sent = 0
    for uid in await get_all_user_ids():
        try:
            await message.copy_to(uid)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await message.answer(f"✅ تم إرسال الإذاعة بنجاح إلى {sent} مستخدم.", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_channel_add)
    await callback.message.edit_text("📌 أرسل معرف القناة الإجبارية (مثال: @mychannel):", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_channel_add)
async def admin_add_channel(message: Message, state: FSMContext, bot: Bot):
    cid = message.text.strip()
    try:
        chat = await bot.get_chat(cid)
        title = chat.title or cid
    except Exception:
        title = cid
    await add_force_channel(cid, title)
    await state.clear()
    await message.answer(f"✅ تمت إضافة قناة الاشتراك: {html.escape(title)}", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_remove_channel")
async def admin_remove_channel_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_channel_remove)
    channels = await list_force_channels()
    if not channels:
        await callback.answer("لا توجد قنوات مسجلة.", show_alert=True)
        return
    txt = "🗑 أرسل معرف القناة المراد حذفها:\n" + "\n".join([f"• {c['chat_id']} ({c['title']})" for c in channels])
    await callback.message.edit_text(txt, reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_channel_remove)
async def admin_remove_channel(message: Message, state: FSMContext):
    await remove_force_channel(message.text.strip())
    await state.clear()
    await message.answer("✅ تم إزالة القناة بنجاح.", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin_packages")
async def admin_packages_cb(callback: CallbackQuery):
    packages = await list_packages()
    await callback.message.edit_text("📦 <b>إدارة باقات الاستضافة:</b>", reply_markup=package_admin_kb(packages))


@admin_router.callback_query(F.data == "admin_add_package")
async def admin_add_package_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_package_name)
    await callback.message.edit_text("📦 أرسل اسم الباقة الجديدة:", reply_markup=back_admin())


@admin_router.message(AdminStates.waiting_package_name)
async def admin_pkg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:64])
    await state.set_state(AdminStates.waiting_package_price)
    await message.answer("💰 أرسل سعر الباقة بالنقاط:")


@admin_router.message(AdminStates.waiting_package_price)
async def admin_pkg_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("⚠️ أدخل رقماً صحيحاً.")
        return
    await state.update_data(price=int(message.text))
    await state.set_state(AdminStates.waiting_package_days)
    await message.answer("📅 أرسل مدة الباقة بالأيام:")


@admin_router.message(AdminStates.waiting_package_days)
async def admin_pkg_days(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("⚠️ أدخل رقماً صحيحاً.")
        return
    await state.update_data(duration_days=int(message.text))
    await state.set_state(AdminStates.waiting_package_limits)
    await message.answer("📊 أرسل حدود الموارد بالترتيب مفصولة بمسافة:\n<code>RAM_MB CPU_PERCENT DISK_MB</code>\nمثال: <code>256 50 500</code>")


@admin_router.message(AdminStates.waiting_package_limits)
async def admin_pkg_limits(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        await message.answer("⚠️ صيغة غير صحيحة. أرسل 3 أرقام مفصولة بمسافات.")
        return
    ram, cpu, disk = map(int, parts)
    data = await state.get_data()
    await create_package(data["name"], data["price"], data["duration_days"], ram, cpu, disk)
    await state.clear()
    await message.answer("✅ تمت إضافة الباقة بنجاح.", reply_markup=admin_menu())


@admin_router.callback_query(F.data.startswith("package_view:"))
async def package_view(callback: CallbackQuery):
    package_id = int(callback.data.split(":")[1])
    package = await get_package(package_id)
    if not package:
        await callback.answer("❌ غير موجودة.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="🗑 حذف هذه الباقة", callback_data=f"package_delete:{package_id}")
    b.button(text="🔙 العودة للباقات", callback_data="admin_packages")
    await callback.message.edit_text(
        f"📦 <b>تفاصيل الباقة: {html.escape(package['name'])}</b>\n"
        f"💰 السعر: {package['price_points']} نقطة | 📅 المدة: {package['duration_days']} يوم\n"
        f"🧠 RAM: {package['ram_mb']}MB | ⚙️ CPU: {package['cpu_percent']}% | 💾 Disk: {package['disk_mb']}MB",
        reply_markup=b.as_markup(),
    )


@admin_router.callback_query(F.data.startswith("package_delete:"))
async def package_delete(callback: CallbackQuery):
    await delete_package(int(callback.data.split(":")[1]))
    await callback.answer("🗑 تم الحذف.")
    await admin_packages_cb(callback)


@admin_router.callback_query(F.data == "admin_pending_bots")
async def admin_pending(callback: CallbackQuery):
    records = await get_pending_bots()
    if not records:
        await callback.answer("لا توجد طلبات معلقة.", show_alert=True)
        return
    await callback.message.edit_text(f"📥 يوجد <b>{len(records)}</b> طلب معلق:")
    for r in records:
        await callback.message.answer(
            f"📥 <b>طلب استضافة #{r['id']}</b>\n👤 المستخدم: <code>{r['user_id']}</code>\n🤖 البوت: {html.escape(r['bot_name'])}\n💰 السعر: {r['price_points']} نقطة",
            reply_markup=approval_kb(r["id"]),
        )


@admin_router.callback_query(F.data.startswith("approve_bot:"))
async def approve_bot(callback: CallbackQuery, bot: Bot):
    bot_id = int(callback.data.split(":")[1])
    record = await get_bot(bot_id)
    if not record or record["status"] != "pending":
        await callback.answer("❌ غير متاح.", show_alert=True)
        return
    user = await get_user(record["user_id"])
    if not user or user["points"] < record["price_points"]:
        await set_bot_status(bot_id, "rejected", None, None)
        await callback.answer("❌ رصيد المستخدم غير كافٍ.", show_alert=True)
        return

    await update_points(record["user_id"], -record["price_points"])
    expires = await activate_bot(bot_id)
    await callback.message.edit_text(f"✅ تمت الموافقة على البوت #{bot_id} بنجاح.")
    try:
        await bot.send_message(
            record["user_id"],
            f"🎉 <b>تمت الموافقة على بوتك «{html.escape(record['bot_name'])}» بنجاح!</b>\n📅 ينتهي في: <b>{expires[:10]}</b>",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("reject_bot:"))
async def reject_bot(callback: CallbackQuery, bot: Bot):
    bot_id = int(callback.data.split(":")[1])
    record = await get_bot(bot_id)
    if record and record["status"] == "pending":
        await set_bot_status(bot_id, "rejected", None, None)
        try:
            await bot.send_message(record["user_id"], f"❌ عذراً، تم رفض استضافة بوتك: «{html.escape(record['bot_name'])}».")
        except Exception:
            pass
    await callback.message.edit_text("❌ تم رفض الطلب.")


# =========================
# راوتر المستخدم (User Router)
# =========================
user_router = Router()


@user_router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    payload = None
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2:
        payload = parts[1].strip()

    referred_by = None
    if payload and payload.startswith("ref_"):
        try:
            referred_by = int(payload[4:])
            if referred_by == uid:
                referred_by = None
        except ValueError:
            referred_by = None

    is_new, is_banned = await get_or_create_user(uid, username, referred_by)
    if is_banned:
        await message.answer("🚫 تم حظرك من استخدام هذا البوت.")
        return

    if is_new and referred_by:
        reward = int(await get_setting("referral_reward", "10"))
        await update_points(referred_by, reward)
        try:
            await bot.send_message(referred_by, f"👥 انضم مستخدم جديد عبر رابط إحالتك وحصلت على {reward} نقطة!")
        except Exception:
            pass

    if payload and payload.startswith("points_"):
        code = payload[7:]
        res = await redeem_points_link(code, uid)
        if res == "not_found":
            await message.answer("❌ رابط الهدية غير صالح.")
        elif res == "expired":
            await message.answer("⏳ انتهت صلاحية رابط الهدية.")
        elif res == "max_reached":
            await message.answer("⚠️ لقد استنفذ هذا الرابط الحد الأقصى للاستخدام.")
        elif res == "already_used":
            await message.answer("⚠️ لقد استهلكت هذا الرابط مسبقاً.")
        else:
            await message.answer(f"🎉 مبروك! تمت إضافة {res} نقطة إلى رصيدك.")

    missing = await check_subscription(bot, uid)
    if missing:
        await message.answer("⚠️ يجب عليك الاشتراك في قنوات البوت أولاً:", reply_markup=subscription_kb(missing))
        return

    user = await get_user(uid)
    dev_username = await get_setting("dev_username", "banhi5")
    await message.answer(
        f"👋 أهلاً بك يا <b>{html.escape(message.from_user.full_name)}</b> في منصة الاستضافة الاحترافية 🌟\n"
        f"⭐ رصيد نقاطك: <b>{user['points']}</b> نقطة\n\nاختر ما تحب من القائمة أدناه:",
        reply_markup=user_menu(dev_username),
    )


@user_router.callback_query(F.data == "check_sub")
async def check_sub_cb(callback: CallbackQuery, bot: Bot):
    missing = await check_subscription(bot, callback.from_user.id)
    if missing:
        await callback.answer("❌ لم تقم بالاشتراك في جميع القنوات بعد.", show_alert=True)
        return
    dev_username = await get_setting("dev_username", "banhi5")
    await callback.message.edit_text("✅ تم التحقق بنجاح!", reply_markup=user_menu(dev_username))


@user_router.callback_query(F.data == "user_menu")
async def user_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(callback.from_user.id)
    dev_username = await get_setting("dev_username", "banhi5")
    await callback.message.edit_text(
        f"👋 القائمة الرئيسية 🌟\n⭐ رصيد نقاطك: <b>{user['points']}</b> نقطة\n\nاختر الخدمة المطلوبة:",
        reply_markup=user_menu(dev_username),
    )


@user_router.callback_query(F.data == "user_profile")
async def user_profile_cb(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    bots = await get_user_bots(callback.from_user.id)
    await callback.message.edit_text(
        f"ℹ️ <b>معلومات حسابك:</b>\n🆔 الآيدي: <code>{user['user_id']}</code>\n⭐ الرصيد: {user['points']} نقطة\n🤖 بوتاتك: {len(bots)} بوت",
        reply_markup=back_user(),
    )


@user_router.callback_query(F.data == "user_daily")
async def user_daily(callback: CallbackQuery):
    if await get_setting("daily_reward_enabled", "1") != "1":
        await callback.answer("⚠️ الهدية اليومية معطلة حالياً.", show_alert=True)
        return
    amount = int(await get_setting("daily_reward_amount", "5"))
    if await claim_daily(callback.from_user.id, amount):
        await callback.answer(f"🎁 حصلت على {amount} نقطة كهدية يومية!", show_alert=True)
    else:
        await callback.answer("⏳ استلمت هديتك اليومية مسبقاً، عد غداً.", show_alert=True)


@user_router.callback_query(F.data == "user_referral")
async def user_referral(callback: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    reward = await get_setting("referral_reward", "10")
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.message.edit_text(
        f"👥 <b>نظام الإحالة والدعوات:</b>\nاحصل على <b>{reward}</b> نقطة لكل صديق يدعو البوت!\n\n🔗 رابطك:\n<code>{link}</code>",
        reply_markup=back_user(),
    )


@user_router.callback_query(F.data == "user_my_bots")
async def user_my_bots(callback: CallbackQuery):
    bots = await get_user_bots(callback.from_user.id)
    if not bots:
        await callback.message.edit_text("📂 ليس لديك أي بوتات مرفوعة.", reply_markup=back_user())
        return
    await callback.message.edit_text("📂 <b>قائمة بوتاتك المرفوعة:</b>", reply_markup=my_bots_kb(bots))


@user_router.callback_query(F.data.startswith("bot_manage:"))
async def bot_manage(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    record = await get_bot(bot_id)
    if not record or record["user_id"] != callback.from_user.id:
        await callback.answer("❌ البوت غير موجود.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🤖 <b>إدارة بوت: {html.escape(record['bot_name'])}</b>\n📌 الحالة: <b>{record['status']}</b>\n📅 ينتهي في: {record['expires_at'][:10] if record['expires_at'] else 'بانتظار التفعيل'}",
        reply_markup=bot_manage_kb(record),
    )


@user_router.callback_query(F.data.startswith("bot_start:"))
async def bot_start_cb(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    ok, msg = await start_bot(bot_id)
    await callback.answer(msg[:180], show_alert=not ok)
    new_record = await get_bot(bot_id)
    if new_record:
        await callback.message.edit_reply_markup(reply_markup=bot_manage_kb(new_record))


@user_router.callback_query(F.data.startswith("bot_stop:"))
async def bot_stop_cb(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    await stop_bot(bot_id)
    await callback.answer("⏹ تم إيقاف البوت.")
    new_record = await get_bot(bot_id)
    if new_record:
        await callback.message.edit_reply_markup(reply_markup=bot_manage_kb(new_record))


@user_router.callback_query(F.data.startswith("bot_restart:"))
async def bot_restart_cb(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    await stop_bot(bot_id)
    await asyncio.sleep(1)
    await start_bot(bot_id)
    await callback.answer("🔄 تم إعادة تشغيل البوت.")
    new_record = await get_bot(bot_id)
    if new_record:
        await callback.message.edit_reply_markup(reply_markup=bot_manage_kb(new_record))


@user_router.callback_query(F.data.startswith("bot_renew:"))
async def bot_renew_cb(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    record = await get_bot(bot_id)
    user = await get_user(callback.from_user.id)
    price = int(record["price_points"])
    if user["points"] < price:
        await callback.answer(f"❌ رصيدك لا يكفي. تحتاج إلى {price} نقطة.", show_alert=True)
        return
    await update_points(callback.from_user.id, -price)
    expires = await renew_bot(bot_id)
    await callback.answer("✅ تم التجديد بنجاح!", show_alert=True)
    new_record = await get_bot(bot_id)
    await callback.message.edit_text(f"🤖 <b>{html.escape(new_record['bot_name'])}</b>\n✅ تم التجديد حتى: {expires[:10]}", reply_markup=bot_manage_kb(new_record))


@user_router.callback_query(F.data.startswith("bot_log:"))
async def bot_log_cb(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    record = await get_bot(bot_id)
    log_path = Path(record["file_path"]).parent / "bot.log"
    if not log_path.exists():
        await callback.answer("📄 السجل فارغ.", show_alert=True)
        return
    content = log_path.read_text(encoding="utf-8", errors="ignore")
    tail = html.escape(content[-3500:] or "السجل فارغ.")
    await callback.message.answer(f"📄 <b>آخر أسطر السجل:</b>\n<pre>{tail}</pre>")


@user_router.callback_query(F.data.startswith("bot_delete:"))
async def bot_delete_cb(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    await delete_bot_files(bot_id)
    await delete_bot_record(bot_id)
    await callback.answer("🗑 تم الحذف.", show_alert=True)
    await callback.message.edit_text("🗑 تم حذف البوت نهائياً.", reply_markup=back_user())


@user_router.callback_query(F.data.startswith("bot_edit_code:"))
async def bot_edit_code_cb(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split(":")[1])
    record = await get_bot(bot_id)
    if not record or record["user_id"] != callback.from_user.id:
        await callback.answer("❌ غير متاح.", show_alert=True)
        return
    await state.update_data(edit_bot_id=bot_id, main_file_path=record["file_path"])
    await state.set_state(UserStates.waiting_edit_code_content)
    await callback.message.edit_text(
        "✏️ <b>تعديل كود البوت مباشرة:</b>\nأرسل الكود الجديد كاملاً (للملف الرئيسي)، وسيتم استبدال الكود القديم فوراً وحل الأخطاء:",
        reply_markup=back_user(),
    )


@user_router.message(UserStates.waiting_edit_code_content)
async def bot_edit_code_save(message: Message, state: FSMContext):
    data = await state.get_data()
    file_path = Path(data["main_file_path"])
    bot_id = data["edit_bot_id"]

    try:
        file_path.write_text(message.text, encoding="utf-8")
        await stop_bot(bot_id)
        await start_bot(bot_id)
        await state.clear()
        await message.answer("✅ تم تحديث كود البوت وإعادة تشغيله بنجاح وإصلاح الخطأ!", reply_markup=back_user())
    except Exception as e:
        await message.answer(f"❌ حدث خطأ أثناء حفظ التعديل: {e}")


@user_router.callback_query(F.data == "user_upload_bot")
async def user_upload_cb(callback: CallbackQuery):
    packages = await list_packages()
    if not packages:
        await callback.message.edit_text("⚠️ لا توجد باقات متاحة حالياً.", reply_markup=back_user())
        return
    await callback.message.edit_text("📦 <b>اختر باقة الاستضافة:</b>", reply_markup=package_select(packages, "select_package"))


@user_router.callback_query(F.data.startswith("select_package:"))
async def select_package_cb(callback: CallbackQuery, state: FSMContext):
    package_id = int(callback.data.split(":")[1])
    package = await get_package(package_id)
    user = await get_user(callback.from_user.id)
    if user["points"] < package["price_points"]:
        await callback.answer(f"❌ رصيدك لا يكفي. تحتاج إلى {package['price_points']} نقطة.", show_alert=True)
        return
    await state.update_data(pkg=dict(package), upload_type="zip")
    await state.set_state(UserStates.waiting_bot_name)
    await callback.message.edit_text("🏷 أرسل اسماً مميزاً للبوت:", reply_markup=back_user())


@user_router.callback_query(F.data == "user_upload_code")
async def user_upload_code_cb(callback: CallbackQuery):
    packages = await list_packages()
    if not packages:
        await callback.message.edit_text("⚠️ لا توجد باقات متاحة حالياً.", reply_markup=back_user())
        return
    await callback.message.edit_text("📦 <b>اختر باقة الاستضافة لرفع كود مباشر:</b>", reply_markup=package_select(packages, "select_code_pkg"))


@user_router.callback_query(F.data.startswith("select_code_pkg:"))
async def select_code_pkg_cb(callback: CallbackQuery, state: FSMContext):
    package_id = int(callback.data.split(":")[1])
    package = await get_package(package_id)
    user = await get_user(callback.from_user.id)
    if user["points"] < package["price_points"]:
        await callback.answer(f"❌ رصيدك لا يكفي.", show_alert=True)
        return
    await state.update_data(pkg=dict(package), upload_type="code")
    await state.set_state(UserStates.waiting_bot_name)
    await callback.message.edit_text("🏷 أرسل اسماً مميزاً للبوت:", reply_markup=back_user())


@user_router.message(UserStates.waiting_bot_name)
async def user_bot_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:64])
    await state.set_state(UserStates.waiting_bot_token)
    await message.answer("🔑 أرسل توكن البوت الخاص بك (من @BotFather):")


@user_router.message(UserStates.waiting_bot_token)
async def user_bot_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if not TOKEN_RE.match(token):
        await message.answer("⚠️ صيغة التوكن غير صحيحة.")
        return
    await state.update_data(token=token)
    data = await state.get_data()
    
    if data.get("upload_type") == "code":
        await state.set_state(UserStates.waiting_code_content)
        await message.answer("💻 أرسل الكود البرمجي لبوتك (ملف بايثون كامل):")
    else:
        await state.set_state(UserStates.waiting_main_filename)
        await message.answer("📄 أرسل اسم ملف التشغيل الرئيسي داخل ملف الـ ZIP (مثال: <code>main.py</code>):")


@user_router.message(UserStates.waiting_main_filename)
async def user_main_filename(message: Message, state: FSMContext):
    fn = message.text.strip()
    if not valid_main_file(fn):
        await message.answer("⚠️ يجب أن ينتهي بـ .py وفي جذر المشروع:")
        return
    await state.update_data(main_file=fn)
    await state.set_state(UserStates.waiting_bot_file)
    await message.answer("📁 أرسل ملف مشروع البوت مضغوطاً بصيغة <b>ZIP</b> فقط:")


@user_router.message(UserStates.waiting_bot_file, F.document)
async def user_bot_zip(message: Message, state: FSMContext, bot: Bot):
    if not (message.document.file_name or "").lower().endswith(".zip"):
        await message.answer("⚠️ أرسل ملف ZIP فقط.")
        return

    data = await state.get_data()
    pkg = data["pkg"]
    folder = UPLOADS_DIR / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / "upload.zip"

    try:
        tg_file = await bot.get_file(message.document.file_id)
        await bot.download_file(tg_file.file_path, destination=str(zip_path))
        await asyncio.to_thread(safe_extract_zip, zip_path, folder, MAX_ZIP_FILES, min(MAX_EXTRACTED_MB, int(pkg["disk_mb"])))

        entry = folder / data["main_file"]
        if not entry.exists() or not entry.is_file():
            raise ValueError(f"الملف الرئيسي «{data['main_file']}» غير موجود.")
        try:
            zip_path.unlink()
        except OSError:
            pass

        bot_id = await create_bot_upload(
            message.from_user.id, entry, safe_filename(message.document.file_name),
            data["main_file"], data["name"], data["token"], pkg["id"],
            pkg["price_points"], pkg["duration_days"], pkg["ram_mb"], pkg["cpu_percent"], pkg["disk_mb"]
        )
        await state.clear()
        await message.answer("✅ تم رفع مشروعك بنجاح وبانتظار موافقة الأدمن.", reply_markup=back_user())
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        await message.answer(f"❌ خطأ: {html.escape(str(exc))}")


@user_router.message(UserStates.waiting_code_content)
async def user_code_content(message: Message, state: FSMContext):
    await state.update_data(code_content=message.text)
    await state.set_state(UserStates.waiting_code_requirements)
    await message.answer("📦 أكتب أسماء المكتبات المطلوبة لتشغيل بوتك مفصولة بمسافات (مثال: <code>aiogram aiosqlite requests</code>)\nأو أرسل <code>لا</code> إذا لم تكن بحاجة لمكتبات إضافية:")


@user_router.message(UserStates.waiting_code_requirements)
async def user_code_requirements(message: Message, state: FSMContext, bot: Bot):
    req_text = message.text.strip()
    data = await state.get_data()
    pkg = data["pkg"]

    folder = UPLOADS_DIR / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    main_path = folder / "main.py"
    main_path.write_text(data["code_content"], encoding="utf-8")

    if req_text.lower() not in {"لا", "none", "no"}:
        req_path = folder / "requirements.txt"
        req_path.write_text("\n".join(req_text.split()), encoding="utf-8")

    try:
        bot_id = await create_bot_upload(
            message.from_user.id, main_path, "direct_code.py",
            "main.py", data["name"], data["token"], pkg["id"],
            pkg["price_points"], pkg["duration_days"], pkg["ram_mb"], pkg["cpu_percent"], pkg["disk_mb"]
        )
        await state.clear()
        await message.answer("✅ تم رفع الكود المباشر بنجاح وبانتظار موافقة الأدمن للتفعيل!", reply_markup=back_user())
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        await message.answer(f"❌ خطأ: {html.escape(str(exc))}")


@user_router.callback_query(F.data == "user_buy_points")
async def buy_points_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_buy_points_amount)
    await callback.message.edit_text("⭐ أرسل عدد النقاط التي ترغب بشرائها:", reply_markup=back_user())


@user_router.message(UserStates.waiting_buy_points_amount)
async def buy_points(message: Message, state: FSMContext, bot: Bot):
    if not message.text.isdigit():
        await message.answer("⚠️ أدخل رقماً صحيحاً.")
        return
    points = int(message.text)
    rate = float(await get_setting("stars_per_point", "1"))
    stars = max(1, round(points * rate))
    await state.clear()
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=f"شراء {points} نقطة استضافة",
        description=f"إضافة {points} نقطة إلى رصيدك.",
        payload=f"pts:{points}:{message.from_user.id}:{uuid.uuid4().hex}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{points} نقطة", amount=stars)],
    )


@user_router.pre_checkout_query()
async def pre_checkout(pre_checkout_q: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@user_router.message(F.successful_payment)
async def payment_success(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload.split(":")
    points = int(payload[1])
    await update_points(message.from_user.id, points)
    await message.answer(f"✅ تم الدفع بنجاح وإضافة <b>{points}</b> نقطة إلى رصيدك!", reply_markup=back_user())


async def on_shutdown():
    for bot_id in list(_running_processes):
        try:
            await stop_bot(bot_id)
        except Exception:
            pass


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("يرجى تعيين BOT_TOKEN.")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(user_router)

    workers = [
        asyncio.create_task(expiry_worker(bot)),
        asyncio.create_task(process_monitor_worker()),
    ]
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        for t in workers:
            t.cancel()
        await on_shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
