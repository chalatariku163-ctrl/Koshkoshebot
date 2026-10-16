import os
import random
import logging
import threading
import asyncio
import sqlite3

from datetime import datetime, timezone
from flask import Flask, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_PATH = os.getenv("DATABASE_PATH", "/var/data/koshkoshe.db")

if not os.path.isdir(os.path.dirname(DATABASE_PATH) or "."):
    DATABASE_PATH = "koshkoshe.db"

app = Flask(__name__)

MAX_PLAYERS = 20
game_lock = threading.RLock()

GAMES = {
    "100": {"price": 100, "prizes": [1000, 500]},
    "200": {"price": 200, "prizes": [2000, 1000]},
}

PAYMENT_METHODS = {
    "telebirr1": {
        "name": "Telebirr - Chala",
        "details": "0902640434",
        "owner": "Chala",
    },
    "telebirr2": {
        "name": "Telebirr - Sanyi",
        "details": "0945311726",
        "owner": "Sanyi",
    },
    "cbe": {
        "name": "CBE Bank",
        "details": "1000193661991",
        "owner": "Chala",
    },
    "awash": {
        "name": "Awash Bank",
        "details": "01320058936200",
        "owner": "Chala",
    },
}


# =========================
# DATABASE
# =========================

def get_db():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS game_state (
                kind TEXT PRIMARY KEY,
                finished INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                kind TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                group_start INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, user_id),
                UNIQUE (kind, group_start),
                CHECK (group_start >= 1),
                CHECK (group_start <= 96),
                CHECK ((group_start - 1) % 5 = 0)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS game_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                status TEXT NOT NULL DEFAULT 'awaiting_receipt',
                receipt_file_id TEXT NOT NULL DEFAULT '',
                receipt_type TEXT NOT NULL DEFAULT '',
                receipt_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT '',
                reviewed_by INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS balance_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        for kind in GAMES:
            db.execute("""
                INSERT OR IGNORE INTO game_state (kind, finished)
                VALUES (?, 0)
            """, (kind,))


def get_balance(user_id):
    with get_db() as db:
        row = db.execute(
            "SELECT amount FROM balances WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return row["amount"] if row else 0


def ensure_balance_user(db, user_id, name):
    db.execute("""
        INSERT OR IGNORE INTO balances (user_id, name, amount, updated_at)
        VALUES (?, ?, 0, ?)
    """, (user_id, name, now_text()))

    db.execute("""
        UPDATE balances SET name = ?, updated_at = ?
        WHERE user_id = ?
    """, (name, now_text(), user_id))


def get_game_state(kind):
    with get_db() as db:
        row = db.execute(
            "SELECT finished FROM game_state WHERE kind = ?",
            (kind,),
        ).fetchone()

    return bool(row["finished"]) if row else False


def get_players(kind):
    with get_db() as db:
        rows = db.execute("""
            SELECT user_id, name, group_start, created_at
            FROM players
            WHERE kind = ?
            ORDER BY group_start
        """, (kind,)).fetchall()

    return [dict(row) for row in rows]


def get_player_count(kind):
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS total FROM players WHERE kind = ?",
            (kind,),
        ).fetchone()

    return row["total"]


def get_user_player(kind, user_id):
    with get_db() as db:
        row = db.execute("""
            SELECT * FROM players
            WHERE kind = ? AND user_id = ?
        """, (kind, user_id)).fetchone()

    return dict(row) if row else None


def get_occupied_groups(kind):
    with get_db() as db:
        rows = db.execute("""
            SELECT group_start, user_id, name
            FROM players
            WHERE kind = ?
        """, (kind,)).fetchall()

    return {row["group_start"]: dict(row) for row in rows}


# =========================
# FLASK
# =========================

@app.get("/")
def home():
    return "Koshkoshe Bot is running!"


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "database": os.path.exists(DATABASE_PATH),
    })


# =========================
# TELEGRAM MENUS
# =========================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Tapha Abba 100 Birr", callback_data="game_100")],
        [InlineKeyboardButton("🎮 Tapha Abba 200 Birr", callback_data="game_200")],
        [InlineKeyboardButton("💰 My Balance", callback_data="my_balance")],
        [InlineKeyboardButton("➕ Deposit", callback_data="deposit")],
        [InlineKeyboardButton("📜 Seenaa Taphaa", callback_data="history")],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Gara Menu", callback_data="menu")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    user = update.effective_user

    with get_db() as db:
        ensure_balance_user(db, user.id, user.full_name)

    await update.effective_message.reply_text(
        "🎯 BAGA GARA KOSHKOSHE BOT DHUFTAN!\n\n"
        "🎮 TAPHA ABBA 100\n"
        "💵 Gatii garee: 100 birr\n"
        "🥇 Badhaasa 1ffaa: 1,000 birr\n"
        "🥈 Badhaasa 2ffaa: 500 birr\n\n"
        "🎮 TAPHA ABBA 200\n"
        "💵 Gatii garee: 200 birr\n"
        "🥇 Badhaasa 1ffaa: 2,000 birr\n"
        "🥈 Badhaasa 2ffaa: 1,000 birr\n\n"
        "📌 Lakkoofsota 1–100 keessaa garee filadhu.\n"
        "📌 Garee tokko lakkoofsota 5 qaba.\n"
        "📌 Garee tokko namni tokko qofa qabaata.\n"
        "📌 Deposit Adminiin erga mirkanaa'ee booda balance dabala.\n\n"
        "👇 Tapha kee filadhu:",
        reply_markup=main_menu(),
    )


# =========================
# BALANCE
# =========================

async def show_balance(query):
    user = query.from_user

    with get_db() as db:
        ensure_balance_user(db, user.id, user.full_name)

    balance = get_balance(user.id)

    await query.edit_message_text(
        "💰 MY BALANCE\n\n"
        f"👤 Maqaa: {user.full_name}\n"
        f"🆔 User ID: {user.id}\n"
        f"💵 Balance: {balance:,} birr\n\n"
        "Deposit gochuuf button Deposit tuqi.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Deposit", callback_data="deposit")],
            [InlineKeyboardButton("⬅️ Gara Menu", callback_data="menu")],
        ]),
    )


# =========================
# DEPOSIT
# =========================

def deposit_methods_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Telebirr - Chala", callback_data="method_telebirr1")],
        [InlineKeyboardButton("📱 Telebirr - Sanyi", callback_data="method_telebirr2")],
        [InlineKeyboardButton("🏦 CBE Bank", callback_data="method_cbe")],
        [InlineKeyboardButton("🏦 Awash Bank", callback_data="method_awash")],
        [InlineKeyboardButton("⬅️ Gara Menu", callback_data="menu")],
    ])


async def start_deposit(query, context):
    context.user_data.pop("deposit_method", None)
    context.user_data.pop("deposit_amount", None)
    context.user_data.pop("deposit_id", None)
    context.user_data["deposit_step"] = None

    await query.edit_message_text(
        "➕ DEPOSIT\n\nKaraa ittiin maallaqa galchitu filadhu:",
        reply_markup=deposit_methods_keyboard(),
    )


async def choose_payment_method(query, context, method_key):
    if method_key not in PAYMENT_METHODS:
        await query.answer("Karaan kaffaltii kun hin jiru.", show_alert=True)
        return

    method = PAYMENT_METHODS[method_key]
    context.user_data["deposit_method"] = method_key
    context.user_data["deposit_step"] = "amount"

    await query.edit_message_text(
        "💳 ODEEFFANNOO KAFFALTII\n\n"
        f"🏦 Karaa: {method['name']}\n"
        f"🔢 Account/lakkoofsa: {method['details']}\n"
        f"👤 Maqaa: {method['owner']}\n\n"
        "Maallaqa galchuu barbaaddu birriidhaan ergi.\n"
        "Fakkeenya: 500\n\n"
        "⚠️ Kaffaltii erguun qofti balance hin dabalu. "
        "Admin kaffaltii kee mirkaneessuu qaba.",
        reply_markup=back_menu(),
    )


async def handle_user_message(update, context):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    step = context.user_data.get("deposit_step")

    if step == "amount":
        if not message.text:
            await message.reply_text("Maallaqa lakkoofsaan ergi. Fakkeenya: 500")
            return

        try:
            amount = int(message.text.strip().replace(",", ""))
        except ValueError:
            await message.reply_text("Lakkoofsa sirrii galchi. Fakkeenya: 500")
            return

        if amount < 1 or amount > 1_000_000:
            await message.reply_text("Maallaqa 1 hanga 1,000,000 birr galchi.")
            return

        method_key = context.user_data.get("deposit_method")

        if method_key not in PAYMENT_METHODS:
            context.user_data.pop("deposit_step", None)
            await message.reply_text(
                "Karaa deposit irra deebi'ii filadhu.",
                reply_markup=deposit_methods_keyboard(),
            )
            return

        with get_db() as db:
            cursor = db.execute("""
                INSERT INTO deposits (
                    user_id, username, method, amount, status, created_at
                )
                VALUES (?, ?, ?, ?, 'awaiting_receipt', ?)
            """, (user.id, user.full_name, method_key, amount, now_text()))

            deposit_id = cursor.lastrowid

        context.user_data["deposit_id"] = deposit_id
        context.user_data["deposit_amount"] = amount
        context.user_data["deposit_step"] = "receipt"

        await message.reply_text(
            "✅ DEPOSIT REQUEST UUMAMEERA\n\n"
            f"🧾 Request ID: #{deposit_id}\n"
            f"💵 Maallaqa: {amount:,} birr\n\n"
            "Kaffaltii raawwadhu; booda ragaa kaffaltii ergi.\n"
            "Ragaan suuraa, screenshot, document ykn lakkoofsa "
            "transaction ta'uu danda'a.\n\n"
            "Balance kee Admin erga mirkaneessee booda dabala."
        )
        return

    if step == "receipt":
        deposit_id = context.user_data.get("deposit_id")

        if not deposit_id:
            context.user_data.pop("deposit_step", None)
            await message.reply_text("Request deposit hin argamne. Irra deebi'i.")
            return

        receipt_file_id = ""
        receipt_type = ""
        receipt_text = ""

        if message.photo:
            receipt_file_id = message.photo[-1].file_id
            receipt_type = "photo"
        elif message.document:
            receipt_file_id = message.document.file_id
            receipt_type = "document"
        elif message.text:
            receipt_text = message.text.strip()
            receipt_type = "text"

            if len(receipt_text) < 4:
                await message.reply_text("Ragaa kaffaltii sirrii ergi.")
                return
        else:
            await message.reply_text(
                "Ragaa kaffaltii suuraan, document ykn transaction IDn ergi."
            )
            return

        with get_db() as db:
            db.execute("""
                UPDATE deposits
                SET status = 'pending',
                    receipt_file_id = ?,
                    receipt_type = ?,
                    receipt_text = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'awaiting_receipt'
            """, (
                receipt_file_id,
                receipt_type,
                receipt_text,
                deposit_id,
                user.id,
            ))

            row = db.execute(
                "SELECT * FROM deposits WHERE id = ? AND user_id = ?",
                (deposit_id, user.id),
            ).fetchone()

        context.user_data.pop("deposit_step", None)
        context.user_data.pop("deposit_method", None)
        context.user_data.pop("deposit_amount", None)
        context.user_data.pop("deposit_id", None)

        if not row or row["status"] != "pending":
            await message.reply_text("Request kun hin jijjiiramne. Admin qunnami.")
            return

        method = PAYMENT_METHODS[row["method"]]

        admin_text = (
            "🔔 DEPOSIT HAARAA\n\n"
            f"🧾 Request ID: #{row['id']}\n"
            f"👤 User: {row['username']}\n"
            f"🆔 User ID: {row['user_id']}\n"
            f"💵 Maallaqa: {row['amount']:,} birr\n"
            f"🏦 Karaa: {method['name']}\n"
            f"📅 Yeroo: {row['created_at']}\n\n"
            "Ragaa kaffaltii ilaali. Yoo sirrii ta'e mirkaneessi; "
            "yoo sirrii hin taane didi."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Mirkaneessi",
                    callback_data=f"approve_{deposit_id}",
                ),
                InlineKeyboardButton(
                    "❌ Didi",
                    callback_data=f"reject_{deposit_id}",
                ),
            ]
        ])

        if not ADMIN_ID:
            await message.reply_text("ADMIN_ID hin qindaa'in.")
            return

        try:
            if receipt_type == "photo":
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=receipt_file_id,
                    caption=admin_text,
                    reply_markup=keyboard,
                )
            elif receipt_type == "document":
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=receipt_file_id,
                    caption=admin_text,
                    reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=admin_text + "\n\nRagaa user erge:\n" + receipt_text,
                    reply_markup=keyboard,
                )
        except Exception:
            logger.exception("Deposit request gara admin erguun hin milkoofne")
            await message.reply_text(
                "Request kee kuufameera; Adminitti erguun hin milkoofne. "
                "Admin qunnamuuf yaali."
            )
            return

        await message.reply_text(
            "📨 RAGAAN KAFFALTII ERGAMEERA!\n\n"
            f"🧾 Request ID: #{deposit_id}\n"
            f"💵 Maallaqa: {row['amount']:,} birr\n\n"
            "Admin erga mirkaneessee booda balance kee ni dabala.",
            reply_markup=main_menu(),
        )


# =========================
# GAME GROUPS
# =========================

def make_group_keyboard(kind):
    occupied = get_occupied_groups(kind)
    keyboard = []

    for group_start in range(1, 101, 5):
        group_end = group_start + 4

        if group_start in occupied:
            label = f"🔒 {group_start}–{group_end}"
            callback = f"taken_{kind}_{group_start}"
        else:
            label = f"🎯 {group_start}–{group_end}"
            callback = f"pick_{kind}_{group_start}"

        keyboard.append([
            InlineKeyboardButton(label, callback_data=callback)
        ])

    keyboard.append([
        InlineKeyboardButton("⬅️ Gara Menu", callback_data="menu")
    ])

    return InlineKeyboardMarkup(keyboard)


async def show_game(query, kind):
    user_id = query.from_user.id

    with game_lock:
        finished = get_game_state(kind)
        player_count = get_player_count(kind)
        user_player = get_user_player(kind, user_id)

    balance = get_balance(user_id)

    if finished:
        await query.edit_message_text(
            f"🎮 Tapha Abba {kind}\n\nTaphaan kun xumurameera.",
            reply_markup=back_menu(),
        )
        return

    if user_player:
        group_start = user_player["group_start"]

        await query.edit_message_text(
            "✅ GAREE KEE GALMAA'EERA!\n\n"
            f"🎮 Tapha Abba {kind}\n"
            f"🔢 Garee: {group_start}–{group_start + 4}\n"
            f"💰 Balance amma: {balance:,} birr\n\n"
            "Bu'aa taphaa eegi.",
            reply_markup=back_menu(),
        )
        return

    if player_count >= MAX_PLAYERS:
        await query.edit_message_text(
            "Taphaan kun guutameera.",
            reply_markup=back_menu(),
        )
        return

    await query.edit_message_text(
        f"🎮 TAPHA ABBA {kind}\n\n"
        f"💵 Gatii garee: {GAMES[kind]['price']:,} birr\n"
        f"💰 Balance kee: {balance:,} birr\n"
        f"👥 Gareewwan fudhataman: {player_count}/{MAX_PLAYERS}\n\n"
        "Garee tokko filadhu. Garee tokko lakkoofsota 5 qaba.\n"
        "Fakkeenya: 1–5, 6–10, 11–15 ... 96–100.\n\n"
        "Garee filachuuf balance gahaa qabaachuu qabda.",
        reply_markup=make_group_keyboard(kind),
    )


# =========================
# GAME HISTORY
# =========================

def get_history():
    with get_db() as db:
        rows = db.execute("""
            SELECT kind, result, created_at
            FROM game_results
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()

    return [dict(row) for row in rows]


# =========================
# APPROVE / REJECT DEPOSIT
# =========================

async def review_deposit(query, approve, deposit_id):
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer(
            "⛔ Admin qofa kana gochuu danda'a.",
            show_alert=True,
        )
        return

    db = get_db()

    try:
        db.execute("BEGIN IMMEDIATE")

        deposit = db.execute(
            "SELECT * FROM deposits WHERE id = ?",
            (deposit_id,),
        ).fetchone()

        if not deposit:
            db.rollback()
            await query.answer("Request hin argamne.", show_alert=True)
            return

        if deposit["status"] != "pending":
            db.rollback()
            await query.answer(
                "Request kun duraan ilaalameera.",
                show_alert=True,
            )
            return

        user_id = deposit["user_id"]
        amount = deposit["amount"]

        if approve:
            db.execute("""
                INSERT OR IGNORE INTO balances
                    (user_id, name, amount, updated_at)
                VALUES (?, ?, 0, ?)
            """, (user_id, deposit["username"], now_text()))

            db.execute("""
                UPDATE balances
                SET amount = amount + ?, updated_at = ?
                WHERE user_id = ?
            """, (amount, now_text(), user_id))

            db.execute("""
                INSERT INTO balance_transactions
                    (user_id, amount, transaction_type, description, created_at)
                VALUES (?, ?, 'deposit', ?, ?)
            """, (
                user_id,
                amount,
                f"Deposit #{deposit_id} mirkanaa'e",
                now_text(),
            ))

            db.execute("""
                UPDATE deposits
                SET status = 'approved',
                    reviewed_at = ?,
                    reviewed_by = ?
                WHERE id = ?
            """, (now_text(), query.from_user.id, deposit_id))

        else:
            db.execute("""
                UPDATE deposits
                SET status = 'rejected',
                    reviewed_at = ?,
                    reviewed_by = ?
                WHERE id = ?
            """, (now_text(), query.from_user.id, deposit_id))

        db.commit()

    except Exception:
        db.rollback()
        logger.exception("Deposit review failed")
        await query.answer("Dogoggorri uumame. Irra deebi'i.", show_alert=True)
        return

    finally:
        db.close()

    if approve:
        user_text = (
            "✅ DEPOSIT KEE MIRKANAA'EEERA!\n\n"
            f"💵 {amount:,} birr balance kee irratti dabalameera.\n"
            f"💰 Balance kee amma: {get_balance(user_id):,} birr."
        )
        admin_text = f"✅ Deposit #{deposit_id} mirkanaa'e."
    else:
        user_text = (
            "❌ DEPOSIT REQUEST KEE DIDAMEERA.\n\n"
            "Ragaa kaffaltii kee mirkaneessi. Yoo dogoggora ta'e "
            "Admin qunnami."
        )
        admin_text = f"❌ Deposit #{deposit_id} didame."

    try:
        if query.message.caption:
            await query.edit_message_caption(
                caption=query.message.caption + "\n\n" + admin_text,
                reply_markup=None,
            )
        else:
            await query.edit_message_text(
                text=query.message.text + "\n\n" + admin_text,
                reply_markup=None,
            )
    except Exception:
        pass

    try:
        await context_send_user(query, user_id, user_text)
    except Exception:
        logger.exception("Useritti bu'aa deposit erguun hin milkoofne")


async def context_send_user(query, user_id, text):
    await query.get_bot().send_message(
        chat_id=user_id,
        text=text,
        reply_markup=main_menu(),
    )


# =========================
# BUTTON HANDLER
# =========================

async def buttons(update, context):
    query = update.callback_query

    if query is None:
        return

    await query.answer()
    data = query.data or ""
    user = query.from_user

    if data == "menu":
        await query.edit_message_text(
            "🎯 KOSHKOSHE BOT\n\nFilannoo kee filadhu:",
            reply_markup=main_menu(),
        )
        return

    if data == "my_balance":
        await show_balance(query)
        return

    if data == "deposit":
        await start_deposit(query, context)
        return

    if data.startswith("method_"):
        await choose_payment_method(query, context, data[len("method_"):])
        return

    if data.startswith("approve_") or data.startswith("reject_"):
        try:
            deposit_id = int(data.split("_", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Request ID sirrii miti.", show_alert=True)
            return

        await review_deposit(
            query,
            approve=data.startswith("approve_"),
            deposit_id=deposit_id,
        )
        return

    if data == "history":
        history = get_history()

        if not history:
            text = "📜 Seenaa taphaa ammaaf hin jiru."
        else:
            text = "📜 SEENAA TAPHAA\n\n" + "\n\n".join(
                f"🕒 {item['created_at']}\n{item['result']}"
                for item in history
            )

        await query.edit_message_text(text[:4000], reply_markup=back_menu())
        return

    if data.startswith("game_"):
        kind = data.split("_", 1)[1]

        if kind not in GAMES:
            await query.answer("Taphaan kun hin jiru.", show_alert=True)
            return

        await show_game(query, kind)
        return

    if data.startswith("taken_"):
        parts = data.split("_")

        if len(parts) != 3:
            return

        _, kind, group_text = parts

        if kind not in GAMES:
            return

        try:
            group_start = int(group_text)
        except ValueError:
            return

        if group_start in get_occupied_groups(kind):
            await query.answer("🔒 Gareen kun duraan fudhatameera!", show_alert=True)

        await show_game(query, kind)
        return

    if not data.startswith("pick_"):
        return

    parts = data.split("_")

    if len(parts) != 3:
        await query.answer("Filannoon sirrii miti.", show_alert=True)
        return

    _, kind, group_text = parts

    if kind not in GAMES:
        await query.answer("Taphaan kun hin jiru.", show_alert=True)
        return

    try:
        group_start = int(group_text)
    except ValueError:
        await query.answer("Lakkoofsi sirrii miti.", show_alert=True)
        return

    if group_start not in range(1, 101, 5):
        await query.answer("Garee sirrii filadhu.", show_alert=True)
        return

    price = GAMES[kind]["price"]
    group_end = group_start + 4
    error = None
    should_finish = False

    with game_lock:
        db = get_db()

        try:
            db.execute("BEGIN IMMEDIATE")

            state = db.execute(
                "SELECT finished FROM game_state WHERE kind = ?",
                (kind,),
            ).fetchone()

            if state is None or state["finished"]:
                error = "Taphaan kun xumurameera."
            else:
                existing_user = db.execute("""
                    SELECT group_start FROM players
                    WHERE kind = ? AND user_id = ?
                """, (kind, user.id)).fetchone()

                if existing_user:
                    error = (
                        "Ati garee duraan qabda: "
                        f"{existing_user['group_start']}–"
                        f"{existing_user['group_start'] + 4}."
                    )
                else:
                    existing_group = db.execute("""
                        SELECT user_id FROM players
                        WHERE kind = ? AND group_start = ?
                    """, (kind, group_start)).fetchone()

                    if existing_group:
                        error = "Gareen kun duraan fudhatameera."
                    else:
                        count_row = db.execute("""
                            SELECT COUNT(*) AS total
                            FROM players
                            WHERE kind = ?
                        """, (kind,)).fetchone()

                        if count_row["total"] >= MAX_PLAYERS:
                            error = "Taphaan kun guutameera."
                        else:
                            balance_row = db.execute(
                                "SELECT amount FROM balances WHERE user_id = ?",
                                (user.id,),
                            ).fetchone()

                            balance = balance_row["amount"] if balance_row else 0

                            if balance < price:
                                error = (
                                    f"💰 Balance kee {balance:,} birr qofa. "
                                    f"Garee kanaaf {price:,} birr barbaachisa. "
                                    "Maaloo deposit godhi."
                                )
                            else:
                                db.execute("""
                                    UPDATE balances
                                    SET amount = amount - ?, name = ?, updated_at = ?
                                    WHERE user_id = ?
                                """, (price, user.full_name, now_text(), user.id))

                                db.execute("""
                                    INSERT INTO balance_transactions
                                        (user_id, amount, transaction_type, description, created_at)
                                    VALUES (?, ?, 'game_entry', ?, ?)
                                """, (
                                    user.id,
                                    -price,
                                    f"Tapha Abba {kind}, garee {group_start}-{group_end}",
                                    now_text(),
                                ))

                                db.execute("""
                                    INSERT INTO players
                                        (kind, user_id, name, group_start, created_at)
                                    VALUES (?, ?, ?, ?, ?)
                                """, (
                                    kind,
                                    user.id,
                                    user.full_name,
                                    group_start,
                                    now_text(),
                                ))

                                new_count = count_row["total"] + 1

                                if new_count == MAX_PLAYERS:
                                    db.execute("""
                                        UPDATE game_state
                                        SET finished = 1
                                        WHERE kind = ?
                                    """, (kind,))
                                    should_finish = True

            db.commit()

        except sqlite3.IntegrityError:
            db.rollback()
            error = "Gareen kun ykn galmeen kee duraan jira."
        except Exception:
            db.rollback()
            logger.exception("Dogoggora garee galmeessuu")
            error = "Dogoggorri uumame. Irra deebi'i."
        finally:
            db.close()

    if error:
        await query.answer(error, show_alert=True)
        await show_game(query, kind)
        return

    await query.edit_message_text(
        "✅ GAREEN KEE MILKAA'EERA!\n\n"
        f"🎮 Tapha Abba {kind}\n"
        f"🔢 Lakkoofsota kee: {group_start}–{group_end}\n"
        f"💵 Kaffaltii taphaa: {price:,} birr\n"
        f"💰 Balance hafe: {get_balance(user.id):,} birr\n\n"
        "🔒 Gareen kun siif galmaa'eera.\n"
        "🏆 Bu'aa taphaa eegi.",
        reply_markup=back_menu(),
    )

    if should_finish:
        await finish_game(kind, context)


# =========================
# FINISH GAME
# =========================

async def finish_game(kind, context):
    with game_lock:
        players = get_players(kind)
        prizes = list(GAMES[kind]["prizes"])

    if len(players) != MAX_PLAYERS:
        logger.warning("Tapha %s hirmaattota 20 hin guunne.", kind)
        return

    drawn_numbers = random.sample(range(1, 101), 2)
    winner_lines = []

    for place, number in enumerate(drawn_numbers):
        prize = prizes[place]

        player = next(
            (
                p for p in players
                if p["group_start"] <= number <= p["group_start"] + 4
            ),
            None,
        )

        if player:
            winner_lines.append(
                f"🏆 MO'ATAA {place + 1}\n"
                f"👤 Maqaa: {player['name']}\n"
                f"🔢 Lakkoofsa mo'ataa: {number}\n"
                f"🎟️ Garee: {player['group_start']}–"
                f"{player['group_start'] + 4}\n"
                f"💰 Badhaasa: {prize:,} birr"
            )
        else:
            winner_lines.append(
                f"🔢 Lakkoofsa mo'ataa {place + 1}: {number}\n"
                "❌ Garee lakkoofsa kana qabu hin jiru."
            )

    result = (
        f"🎯 BU'AA TAPHA ABBA {kind}\n\n"
        + "\n\n".join(winner_lines)
        + "\n\n⚠️ Badhaasni balance irratti ofumaan hin dabalamu."
    )

    with get_db() as db:
        db.execute("""
            INSERT INTO game_results (kind, result, created_at)
            VALUES (?, ?, ?)
        """, (kind, result, now_text()))

    for player in players:
        try:
            await context.bot.send_message(
                chat_id=player["user_id"],
                text=result,
            )
        except Exception:
            logger.exception(
                "Bu'aa gara user %s erguun hin milkoofne",
                player["user_id"],
            )

    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=result)
        except Exception:
            logger.exception("Bu'aa gara admin erguun hin milkoofne")


# =========================
# ADMIN COMMANDS
# =========================

async def addbalance(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user or user.id != ADMIN_ID:
        await message.reply_text("⛔ Admin qofa ajaja kana fayyadamuu danda'a.")
        return

    if len(context.args) != 2:
        await message.reply_text(
            "Fayyadama:\n/addbalance USER_ID AMOUNT\n"
            "Fakkeenya: /addbalance 123456789 500"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await message.reply_text("User ID fi amount lakkoofsa ta'uu qabu.")
        return

    if amount <= 0:
        await message.reply_text("Amount 0 ol ta'uu qaba.")
        return

    with get_db() as db:
        db.execute("""
            INSERT OR IGNORE INTO balances (user_id, name, amount, updated_at)
            VALUES (?, ?, 0, ?)
        """, (target_id, str(target_id), now_text()))

        db.execute("""
            UPDATE balances SET amount = amount + ?, updated_at = ?
            WHERE user_id = ?
        """, (amount, now_text(), target_id))

        db.execute("""
            INSERT INTO balance_transactions
                (user_id, amount, transaction_type, description, created_at)
            VALUES (?, ?, 'admin_credit', ?, ?)
        """, (target_id, amount, "Adminiin balance dabale", now_text()))

    await message.reply_text(
        f"✅ User {target_id} balance irratti {amount:,} birr dabalame.\n"
        f"💰 Balance amma: {get_balance(target_id):,} birr"
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"💰 Admin balance kee irratti {amount:,} birr dabalera.\n"
                f"Balance kee amma: {get_balance(target_id):,} birr"
            ),
        )
    except Exception:
        pass


async def balance_command(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user:
        return

    if context.args and user.id == ADMIN_ID:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await message.reply_text("User ID sirrii galchi.")
            return

        await message.reply_text(
            f"User ID: {target_id}\n"
            f"Balance: {get_balance(target_id):,} birr"
        )
        return

    await message.reply_text(
        f"💰 Balance kee: {get_balance(user.id):,} birr"
    )


async def newgame(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user or user.id != ADMIN_ID:
        await message.reply_text("⛔ Admin qofa ajaja kana fayyadamuu danda'a.")
        return

    if not context.args or context.args[0] not in GAMES:
        await message.reply_text(
            "Fayyadama:\n/newgame 100\n/newgame 200\n\n"
            "Ajajni kun hirmaattota tapha filatame ni haqa; "
            "balance isaanii garuu hin jijjiiru."
        )
        return

    kind = context.args[0]

    with game_lock:
        with get_db() as db:
            db.execute("DELETE FROM players WHERE kind = ?", (kind,))
            db.execute(
                "UPDATE game_state SET finished = 0 WHERE kind = ?",
                (kind,),
            )

    await message.reply_text(f"✅ Tapha Abba {kind} haaromfameera.")


# =========================
# ERROR HANDLER
# =========================

async def error_handler(update, context):
    logger.error(
        "Dogoggora Telegram keessatti uumame:",
        exc_info=context.error,
    )


# =========================
# FLASK SERVER
# =========================

def run_flask():
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )


# =========================
# STARTUP
# =========================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN hin jiru. Render Environment keessatti galchi."
        )

    if not ADMIN_ID:
        logger.warning(
            "ADMIN_ID hin qindaa'in. Deposit approval hin hojjetu."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("addbalance", addbalance))
    application.add_handler(CommandHandler("newgame", newgame))

    application.add_handler(CallbackQueryHandler(buttons))

    application.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.ALL | filters.TEXT)
            & ~filters.COMMAND,
            handle_user_message,
        )
    )

    application.add_error_handler(error_handler)

    threading.Thread(
        target=run_flask,
        daemon=True,
    ).start()

    logger.info("Koshkoshe Bot jalqabuuf jira.")

    async with application:
        await application.start()

        if application.updater is None:
            raise RuntimeError("Telegram updater hin argamne.")

        await application.updater.start_polling(
            drop_pending_updates=False,
        )

        logger.info("Koshkoshe Bot hojiirra jira.")

        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
