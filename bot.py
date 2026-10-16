import os
import random
import logging
import threading
import asyncio
import sqlite3
import hmac
import hashlib
import json
import urllib.parse

from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
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
    "telebirr1": {"name": "Telebirr - Chala", "details": "0902640434", "owner": "Chala"},
    "telebirr2": {"name": "Telebirr - Sanyi", "details": "0945311726", "owner": "Sanyi"},
    "cbe": {"name": "CBE Bank", "details": "1000193661991", "owner": "Chala"},
    "awash": {"name": "Awash Bank", "details": "01320058936200", "owner": "Chala"},
}

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def get_db():
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    return db

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS game_state (
            kind TEXT PRIMARY KEY, finished INTEGER NOT NULL DEFAULT 0,
            round_no INTEGER NOT NULL DEFAULT 1)""")
        # Migrate an older database made by the previous bot version.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(game_state)")}
        if "round_no" not in cols:
            db.execute("ALTER TABLE game_state ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1")
        db.execute("""CREATE TABLE IF NOT EXISTS players (
            kind TEXT NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL,
            group_start INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(kind,user_id), UNIQUE(kind,group_start),
            CHECK(group_start >= 1), CHECK(group_start <= 96),
            CHECK((group_start - 1) % 5 = 0))""")
        db.execute("""CREATE TABLE IF NOT EXISTS game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            result TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            amount INTEGER NOT NULL DEFAULT 0 CHECK(amount >= 0),
            updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '', method TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            status TEXT NOT NULL DEFAULT 'awaiting_receipt',
            receipt_file_id TEXT NOT NULL DEFAULT '', receipt_type TEXT NOT NULL DEFAULT '',
            receipt_text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            reviewed_at TEXT NOT NULL DEFAULT '', reviewed_by INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS balance_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, transaction_type TEXT NOT NULL,
            description TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS game_views (
            kind TEXT NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(kind,user_id))""")
        # Unique per round: prevents a second spin/payout for the same round.
        db.execute("""CREATE TABLE IF NOT EXISTS game_draws (
            kind TEXT NOT NULL, round_no INTEGER NOT NULL, numbers TEXT NOT NULL,
            result TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(kind,round_no))""")
        for kind in GAMES:
            db.execute("""INSERT OR IGNORE INTO game_state(kind,finished,round_no)
                          VALUES (?,0,1)""", (kind,))

def ensure_balance_user(db, user_id, name):
    db.execute("""INSERT OR IGNORE INTO balances(user_id,name,amount,updated_at)
                  VALUES(?,?,0,?)""", (user_id, name, now_text()))
    db.execute("UPDATE balances SET name=?,updated_at=? WHERE user_id=?",
               (name, now_text(), user_id))

def get_balance(user_id):
    with get_db() as db:
        row = db.execute("SELECT amount FROM balances WHERE user_id=?", (user_id,)).fetchone()
    return row["amount"] if row else 0

def get_game_info(kind):
    with get_db() as db:
        row = db.execute("SELECT finished,round_no FROM game_state WHERE kind=?", (kind,)).fetchone()
    return dict(row) if row else {"finished": False, "round_no": 1}

def get_players(kind):
    with get_db() as db:
        rows = db.execute("SELECT user_id,name,group_start,created_at FROM players WHERE kind=? ORDER BY group_start", (kind,)).fetchall()
    return [dict(r) for r in rows]

def get_player_count(kind):
    with get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM players WHERE kind=?", (kind,)).fetchone()["n"]

def get_user_player(kind, user_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM players WHERE kind=? AND user_id=?", (kind,user_id)).fetchone()
    return dict(row) if row else None

def get_occupied_groups(kind):
    with get_db() as db:
        rows = db.execute("SELECT group_start,user_id,name FROM players WHERE kind=?", (kind,)).fetchall()
    return {r["group_start"]: dict(r) for r in rows}

@app.get("/")
def home():
    return "Koshkoshe Bot is running!"

@app.get("/health")
def health():
    return jsonify({"status":"ok","database":os.path.exists(DATABASE_PATH)})

@app.get("/wheel")
def wheel_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

def verify_telegram_init_data(init_data):
    """Verify Telegram Web App initData; never trust a user ID supplied by browser alone."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if abs(int(datetime.now(timezone.utc).timestamp()) - auth_date) > 86400:
            return None
        user_data = json.loads(parsed.get("user", "{}"))
        return int(user_data.get("id", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

@app.get("/api/wheel-status")
def wheel_status():
    kind = request.args.get("kind", "")
    if kind not in GAMES:
        return jsonify({"ok":False,"error":"Tapha sirrii miti."}), 400
    info = get_game_info(kind)
    count = get_player_count(kind)
    with get_db() as db:
        draw = db.execute("SELECT numbers,result FROM game_draws WHERE kind=? AND round_no=?",
                          (kind, info["round_no"])).fetchone()
    return jsonify({
        "ok": True, "kind":kind, "round_no":info["round_no"],
        "players":count, "max_players":MAX_PLAYERS,
        "ready": count == MAX_PLAYERS and bool(info["finished"]),
        "already_drawn": draw is not None,
        "result": draw["result"] if draw else ""
    })

@app.post("/api/spin")
def spin_api():
    payload = request.get_json(silent=True) or {}
    user_id = verify_telegram_init_data(payload.get("initData", ""))
    if not user_id or user_id != ADMIN_ID:
        return jsonify({"ok":False,"error":"Admin qofa geengoo naannessuu danda'a."}), 403
    kind = str(payload.get("kind", ""))
    if kind not in GAMES:
        return jsonify({"ok":False,"error":"Tapha sirrii filadhu."}), 400
    try:
        result = draw_and_pay(kind)
    except ValueError as exc:
        return jsonify({"ok":False,"error":str(exc)}), 400
    except Exception:
        logger.exception("Spin/payout failed")
        return jsonify({"ok":False,"error":"Dogoggorri uumame. Admin log ilaalu qaba."}), 500
    # Send the completed result to players after the database transaction has committed.
    try:
        loop = asyncio.run_coroutine_threadsafe(
            broadcast_result(kind, result["result"], result["players"]),
            BOT_LOOP
        )
        loop.result(timeout=30)
    except Exception:
        logger.exception("Bu'aa taphaa erguun hin milkoofne")
    return jsonify({"ok":True,"numbers":result["numbers"],"result":result["result"]})

BOT_LOOP = None

def draw_and_pay(kind):
    """One atomic, idempotent draw and payout per round."""
    with game_lock:
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT finished,round_no FROM game_state WHERE kind=?", (kind,)).fetchone()
            if not state:
                raise ValueError("Taphaan hin argamne.")
            round_no = state["round_no"]
            existing = db.execute("SELECT numbers,result FROM game_draws WHERE kind=? AND round_no=?",
                                   (kind,round_no)).fetchone()
            if existing:
                raise ValueError("Geengoon marsaa kanaaf duraan naanna'eera.")
            players_rows = db.execute("SELECT user_id,name,group_start FROM players WHERE kind=? ORDER BY group_start",
                                      (kind,)).fetchall()
            if len(players_rows) != MAX_PLAYERS or not state["finished"]:
                raise ValueError(f"Taphaan guutuu qaba: hirmaattota {len(players_rows)}/{MAX_PLAYERS}.")
            players = [dict(r) for r in players_rows]
            numbers = random.sample(range(1,101), 2)
            winner_lines = []
            for place, number in enumerate(numbers):
                prize = GAMES[kind]["prizes"][place]
                winner = next((p for p in players if p["group_start"] <= number <= p["group_start"] + 4), None)
                if winner:
                    # A unique round record prevents duplicate payout after retries/restarts.
                    db.execute("""UPDATE balances SET amount=amount+?,name=?,updated_at=? WHERE user_id=?""",
                               (prize,winner["name"],now_text(),winner["user_id"]))
                    db.execute("""INSERT INTO balance_transactions(user_id,amount,transaction_type,description,created_at)
                                   VALUES(?,?,'game_prize',?,?)""",
                               (winner["user_id"],prize,f"Abba {kind}, marsaa {round_no}, mo'ataa {place+1}, lakkoofsa {number}",now_text()))
                    winner_lines.append(
                        f"🏆 MO'ATAA {place+1}\n👤 Maqaa: {winner['name']}\n"
                        f"🔢 Lakkoofsa: {number}\n🎟️ Garee: {winner['group_start']}–{winner['group_start']+4}\n"
                        f"💰 Badhaasa: {prize:,} birr\n✅ Balance irratti dabalameera."
                    )
                else:
                    winner_lines.append(f"🎯 Lakkoofsa mo'ataa {place+1}: {number}\n❌ Gareen lakkoofsa kana qabu hin jiru.\n💰 Badhaasni manaaf hafa.")
            result_text = f"🎡 BU'AA GEENGOO — TAPHA ABBA {kind}\nMarsaa: {round_no}\n\n" + "\n\n".join(winner_lines)
            db.execute("INSERT INTO game_draws(kind,round_no,numbers,result,created_at) VALUES(?,?,?,?,?)",
                       (kind,round_no,json.dumps(numbers),result_text,now_text()))
            db.execute("INSERT INTO game_results(kind,result,created_at) VALUES(?,?,?)",
                       (kind,result_text,now_text()))
            db.commit()
            return {"numbers":numbers,"result":result_text,"players":players}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

async def broadcast_result(kind, result, players):
    for player in players:
        try:
            await application_bot_send(player["user_id"], result)
        except Exception:
            logger.exception("Bu'aa gara user %s erguun hin milkoofne", player["user_id"])
    if ADMIN_ID:
        try:
            await application_bot_send(ADMIN_ID, result)
        except Exception:
            logger.exception("Bu'aa adminitti erguun hin milkoofne")
    with get_db() as db:
        db.execute("DELETE FROM game_views WHERE kind=?", (kind,))

# This callback is assigned once the Telegram Application starts.
BOT_SEND = None
async def application_bot_send(chat_id, text):
    if BOT_SEND is None:
        raise RuntimeError("Telegram bot hin qindaa'in.")
    await BOT_SEND(chat_id=chat_id, text=text)

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Tapha Abba 100 Birr", callback_data="game_100")],
        [InlineKeyboardButton("🎮 Tapha Abba 200 Birr", callback_data="game_200")],
        [InlineKeyboardButton("💰 My Balance", callback_data="my_balance")],
        [InlineKeyboardButton("➕ Deposit", callback_data="deposit")],
        [InlineKeyboardButton("📜 Seenaa Taphaa", callback_data="history")],
    ])

def back_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Gara Menu", callback_data="menu")]])

def deposit_methods_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Telebirr - Chala", callback_data="method_telebirr1")],
        [InlineKeyboardButton("📱 Telebirr - Sanyi", callback_data="method_telebirr2")],
        [InlineKeyboardButton("🏦 CBE Bank", callback_data="method_cbe")],
        [InlineKeyboardButton("🏦 Awash Bank", callback_data="method_awash")],
        [InlineKeyboardButton("⬅️ Gara Menu", callback_data="menu")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    user = update.effective_user
    with get_db() as db:
        ensure_balance_user(db,user.id,user.full_name)
    await update.effective_message.reply_text(
        "🎯 BAGA GARA KOSHKOSHE BOT DHUFTAN!\n\n"
        "🎮 TAPHA ABBA 100\n💵 Gatii garee: 100 birr\n🥇 Badhaasa 1ffaa: 1,000 birr\n🥈 Badhaasa 2ffaa: 500 birr\n\n"
        "🎮 TAPHA ABBA 200\n💵 Gatii garee: 200 birr\n🥇 Badhaasa 1ffaa: 2,000 birr\n🥈 Badhaasa 2ffaa: 1,000 birr\n\n"
        "📌 Garee tokko lakkoofsota 5 qaba; garee tokko namni tokko qofa qabaata.\n"
        "📌 Deposit Adminiin erga mirkanaa'ee booda balance dabala.\n👇 Tapha kee filadhu:",
        reply_markup=main_menu())

async def show_balance(query):
    user=query.from_user
    with get_db() as db: ensure_balance_user(db,user.id,user.full_name)
    await query.edit_message_text(
        f"💰 MY BALANCE\n\n👤 Maqaa: {user.full_name}\n🆔 User ID: {user.id}\n💵 Balance: {get_balance(user.id):,} birr",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Deposit",callback_data="deposit")],[InlineKeyboardButton("⬅️ Gara Menu",callback_data="menu")]]))

async def start_deposit(query, context):
    for key in ("deposit_method","deposit_amount","deposit_id","deposit_step"): context.user_data.pop(key,None)
    await query.edit_message_text("➕ DEPOSIT\n\nKaraa ittiin maallaqa galchitu filadhu:",reply_markup=deposit_methods_keyboard())

async def choose_payment_method(query, context, method_key):
    if method_key not in PAYMENT_METHODS:
        await query.answer("Karaan kaffaltii kun hin jiru.",show_alert=True); return
    method=PAYMENT_METHODS[method_key]
    context.user_data["deposit_method"]=method_key; context.user_data["deposit_step"]="amount"
    await query.edit_message_text(
        f"💳 ODEEFFANNOO KAFFALTII\n\n🏦 Karaa: {method['name']}\n🔢 Account/lakkoofsa: {method['details']}\n👤 Maqaa: {method['owner']}\n\n"
        "Maallaqa birriidhaan ergi. Fakkeenya: 500\n⚠️ Admin mirkaneessuu qaba.",reply_markup=back_menu())

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message=update.effective_message; user=update.effective_user
    if not message or not user: return
    step=context.user_data.get("deposit_step")
    if step=="amount":
        if not message.text:
            await message.reply_text("Maallaqa lakkoofsaan ergi. Fakkeenya: 500"); return
        try: amount=int(message.text.strip().replace(",",""))
        except ValueError:
            await message.reply_text("Lakkoofsa sirrii galchi. Fakkeenya: 500"); return
        if amount<1 or amount>1_000_000:
            await message.reply_text("Maallaqa 1 hanga 1,000,000 birr galchi."); return
        method=context.user_data.get("deposit_method")
        if method not in PAYMENT_METHODS:
            context.user_data.pop("deposit_step",None)
            await message.reply_text("Karaa deposit irra deebi'ii filadhu.",reply_markup=deposit_methods_keyboard()); return
        with get_db() as db:
            cur=db.execute("""INSERT INTO deposits(user_id,username,method,amount,status,created_at)
                              VALUES(?,?,?,?,'awaiting_receipt',?)""",(user.id,user.full_name,method,amount,now_text()))
            did=cur.lastrowid
        context.user_data["deposit_id"]=did; context.user_data["deposit_amount"]=amount; context.user_data["deposit_step"]="receipt"
        await message.reply_text(f"✅ DEPOSIT REQUEST UUMAMEERA\n🧾 Request ID: #{did}\n💵 {amount:,} birr\nKaffaltii raawwadhu; booda ragaa kaffaltii ergi.")
        return
    if step=="receipt":
        did=context.user_data.get("deposit_id")
        if not did:
            context.user_data.pop("deposit_step",None); await message.reply_text("Request deposit hin argamne. Irra deebi'i."); return
        file_id=""; typ=""; receipt_text=""
        if message.photo: file_id=message.photo[-1].file_id; typ="photo"
        elif message.document: file_id=message.document.file_id; typ="document"
        elif message.text:
            receipt_text=message.text.strip(); typ="text"
            if len(receipt_text)<4: await message.reply_text("Ragaa kaffaltii sirrii ergi."); return
        else:
            await message.reply_text("Ragaa kaffaltii suuraa, document ykn transaction IDn ergi."); return
        with get_db() as db:
            db.execute("""UPDATE deposits SET status='pending',receipt_file_id=?,receipt_type=?,receipt_text=?
                          WHERE id=? AND user_id=? AND status='awaiting_receipt'""",(file_id,typ,receipt_text,did,user.id))
            row=db.execute("SELECT * FROM deposits WHERE id=? AND user_id=?",(did,user.id)).fetchone()
        for key in ("deposit_step","deposit_method","deposit_amount","deposit_id"): context.user_data.pop(key,None)
        if not row or row["status"]!="pending":
            await message.reply_text("Request kun hin jijjiiramne. Admin qunnami."); return
        if not ADMIN_ID:
            await message.reply_text("ADMIN_ID hin qindaa'in. Admin qunnami."); return
        method=PAYMENT_METHODS[row["method"]]
        caption=(f"🔔 DEPOSIT HAARAA\n🧾 Request ID: #{row['id']}\n👤 User: {row['username']}\n🆔 User ID: {row['user_id']}\n"
                 f"💵 Maallaqa: {row['amount']:,} birr\n🏦 Karaa: {method['name']}\n📅 Yeroo: {row['created_at']}")
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Mirkaneessi",callback_data=f"approve_{did}"),
                                  InlineKeyboardButton("❌ Didi",callback_data=f"reject_{did}")]])
        try:
            if typ=="photo": await context.bot.send_photo(ADMIN_ID,file_id,caption=caption,reply_markup=kb)
            elif typ=="document": await context.bot.send_document(ADMIN_ID,file_id,caption=caption,reply_markup=kb)
            else: await context.bot.send_message(ADMIN_ID,caption+"\n\nRagaa: "+receipt_text,reply_markup=kb)
        except Exception:
            logger.exception("Deposit adminitti erguun hin milkoofne")
            await message.reply_text("Request kee kuufameera; Adminitti erguun hin milkoofne."); return
        await message.reply_text(f"📨 Ragaan ergameera. Request #{did}; Admin mirkaneessuu eegi.",reply_markup=main_menu())

def make_group_keyboard(kind):
    occupied=get_occupied_groups(kind); keyboard=[]
    for start in range(1,101,5):
        end=start+4
        if start in occupied: label=f"🟢 ✅ {start}–{end}"; cb=f"taken_{kind}_{start}"
        else: label=f"⚪ 🎯 {start}–{end}"; cb=f"pick_{kind}_{start}"
        keyboard.append([InlineKeyboardButton(label,callback_data=cb)])
    keyboard.extend([[InlineKeyboardButton("🔄 Haaromsi",callback_data=f"game_{kind}")],
                     [InlineKeyboardButton("⬅️ Gara Menu",callback_data="menu")]])
    return InlineKeyboardMarkup(keyboard)

async def save_game_view(query,kind):
    if not query.message:return
    with get_db() as db:
        db.execute("""INSERT INTO game_views(kind,user_id,chat_id,message_id,updated_at) VALUES(?,?,?,?,?)
                      ON CONFLICT(kind,user_id) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id,updated_at=excluded.updated_at""",
                   (kind,query.from_user.id,query.message.chat_id,query.message.message_id,now_text()))

async def refresh_group_views(context,kind):
    with get_db() as db: rows=db.execute("SELECT user_id,chat_id,message_id FROM game_views WHERE kind=?",(kind,)).fetchall()
    keyboard=make_group_keyboard(kind)
    for row in rows:
        try: await context.bot.edit_message_reply_markup(chat_id=row["chat_id"],message_id=row["message_id"],reply_markup=keyboard)
        except Exception as exc: logger.info("Fuula user %s haaromsuun hin milkoofne: %s",row["user_id"],exc)

async def show_game(query,kind):
    user=query.from_user; info=get_game_info(kind); count=get_player_count(kind); player=get_user_player(kind,user.id)
    if info["finished"]:
        await query.edit_message_text(f"🎮 Tapha Abba {kind}\n\nTaphaan kun guutameera. Admin geengoo naannessuu eegi.",reply_markup=back_menu()); return
    balance=get_balance(user.id)
    text=(f"🎮 TAPHA ABBA {kind}\n\n💵 Gatii garee: {GAMES[kind]['price']:,} birr\n💰 Balance kee: {balance:,} birr\n"
          f"👥 Gareewwan fudhataman: {count}/{MAX_PLAYERS}\n\n🟢 = Garee bitame\n⚪ = Garee bilisaa\n\nGaree tokko filadhu. Gareen tokko lakkoofsota 5 qaba.")
    if player:text+=f"\n\n✅ Garee kee: {player['group_start']}–{player['group_start']+4}. Garee biraa hin filachuu dandeessu."
    await query.edit_message_text(text,reply_markup=make_group_keyboard(kind))
    await save_game_view(query,kind)

def get_history():
    with get_db() as db: rows=db.execute("SELECT kind,result,created_at FROM game_results ORDER BY id DESC LIMIT 10").fetchall()
    return [dict(r) for r in rows]

async def review_deposit(query,approve,did):
    if not ADMIN_ID or query.from_user.id!=ADMIN_ID:
        await query.answer("⛔ Admin qofa kana gochuu danda'a.",show_alert=True);return
    db=get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        dep=db.execute("SELECT * FROM deposits WHERE id=?",(did,)).fetchone()
        if not dep or dep["status"]!="pending":
            db.rollback();await query.answer("Request hin jiru ykn duraan ilaalameera.",show_alert=True);return
        uid=dep["user_id"]; amount=dep["amount"]
        if approve:
            db.execute("INSERT OR IGNORE INTO balances(user_id,name,amount,updated_at) VALUES(?,?,0,?)",(uid,dep["username"],now_text()))
            db.execute("UPDATE balances SET amount=amount+?,updated_at=? WHERE user_id=?",(amount,now_text(),uid))
            db.execute("INSERT INTO balance_transactions(user_id,amount,transaction_type,description,created_at) VALUES(?,?,'deposit',?,?)",(uid,amount,f"Deposit #{did} mirkanaa'e",now_text()))
        db.execute("UPDATE deposits SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?",
                   ("approved" if approve else "rejected",now_text(),query.from_user.id,did))
        db.commit()
    except Exception:
        db.rollback();logger.exception("Deposit review failed");await query.answer("Dogoggorri uumame.",show_alert=True);return
    finally:db.close()
    note=f"✅ Deposit #{did} mirkanaa'e." if approve else f"❌ Deposit #{did} didame."
    try:
        if query.message.caption: await query.edit_message_caption(caption=(query.message.caption or "")+"\n\n"+note,reply_markup=None)
        else: await query.edit_message_text((query.message.text or "")+"\n\n"+note,reply_markup=None)
    except Exception:pass
    try:
        msg=(f"✅ Deposit kee mirkanaa'eera. {amount:,} birr dabalameera.\nBalance: {get_balance(uid):,} birr" if approve
             else "❌ Deposit request kee didameera. Admin qunnami.")
        await query.get_bot().send_message(uid,msg,reply_markup=main_menu())
    except Exception:logger.exception("Deposit result useritti erguun hin milkoofne")

async def buttons(update:Update,context:ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not query:return
    await query.answer(); data=query.data or ""; user=query.from_user
    if data=="menu":
        await query.edit_message_text("🎯 KOSHKOSHE BOT\n\nFilannoo kee filadhu:",reply_markup=main_menu());return
    if data=="my_balance":await show_balance(query);return
    if data=="deposit":await start_deposit(query,context);return
    if data.startswith("method_"):await choose_payment_method(query,context,data[7:]);return
    if data.startswith(("approve_","reject_")):
        try:did=int(data.split("_",1)[1])
        except ValueError:return
        await review_deposit(query,data.startswith("approve_"),did);return
    if data=="history":
        items=get_history()
        text="📜 Seenaa taphaa hin jiru." if not items else "📜 SEENAA TAPHAA\n\n"+"\n\n".join(f"🕒 {x['created_at']}\n{x['result']}" for x in items)
        await query.edit_message_text(text[:4000],reply_markup=back_menu());return
    if data.startswith("game_"):
        kind=data.split("_",1)[1]
        if kind in GAMES:await show_game(query,kind)
        return
    if data.startswith("taken_"):
        parts=data.split("_")
        if len(parts)==3 and parts[1] in GAMES:
            await query.answer("🔒 Gareen kun duraan fudhatameera!",show_alert=True)
            await show_game(query,parts[1])
        return
    if not data.startswith("pick_"):return
    parts=data.split("_")
    if len(parts)!=3:return
    _,kind,group_text=parts
    if kind not in GAMES:return
    try:start=int(group_text)
    except ValueError:return
    if start not in range(1,101,5):
        await query.answer("Garee sirrii filadhu.",show_alert=True);return
    price=GAMES[kind]["price"]; end=start+4; error=None
    with game_lock:
        db=get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            state=db.execute("SELECT finished FROM game_state WHERE kind=?",(kind,)).fetchone()
            count=db.execute("SELECT COUNT(*) AS n FROM players WHERE kind=?",(kind,)).fetchone()["n"]
            if not state or state["finished"]:error="Taphaan kun guutameera."
            elif db.execute("SELECT 1 FROM players WHERE kind=? AND user_id=?",(kind,user.id)).fetchone():error="Garee tokko duraan qabda."
            elif db.execute("SELECT 1 FROM players WHERE kind=? AND group_start=?",(kind,start)).fetchone():error="Gareen kun duraan fudhatameera."
            elif count>=MAX_PLAYERS:error="Taphaan kun guutameera."
            else:
                bal=db.execute("SELECT amount FROM balances WHERE user_id=?",(user.id,)).fetchone()
                if not bal or bal["amount"]<price:error=f"Balance kee gahaa miti. {price:,} birr barbaachisa."
                else:
                    db.execute("UPDATE balances SET amount=amount-?,name=?,updated_at=? WHERE user_id=?",(price,user.full_name,now_text(),user.id))
                    db.execute("INSERT INTO balance_transactions(user_id,amount,transaction_type,description,created_at) VALUES(?,?,'game_entry',?,?)",(user.id,-price,f"Abba {kind}, garee {start}-{end}",now_text()))
                    db.execute("INSERT INTO players(kind,user_id,name,group_start,created_at) VALUES(?,?,?,?,?)",(kind,user.id,user.full_name,start,now_text()))
                    if count+1==MAX_PLAYERS:db.execute("UPDATE game_state SET finished=1 WHERE kind=?",(kind,))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback();error="Gareen kun ykn galmeen kee duraan jira."
        except Exception:
            db.rollback();logger.exception("Garee galmeessuun hin milkoofne");error="Dogoggorri uumame."
        finally:db.close()
    if error:
        await query.answer(error,show_alert=True);await show_game(query,kind);return
    try:
        await query.edit_message_text(
            f"🎮 TAPHA ABBA {kind}\n\nGareen kee galmaa'eera: {start}–{end}\n"
            f"💰 Balance: {get_balance(user.id):,} birr\n👥 Hirmaattota: {get_player_count(kind)}/{MAX_PLAYERS}\n\n"
            "Geengoon adminiin erga naanna'ee booda bu'aan ni dhufa.",
            reply_markup=make_group_keyboard(kind))
        await save_game_view(query,kind)
    except Exception:pass
    await refresh_group_views(context,kind)
    if get_game_info(kind)["finished"] and ADMIN_ID:
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"🔔 TAPHA ABBA {kind} GUUTAMEERA!\nHirmaattota 20 galmaa'aniiru. Geengoo naannessuuf /wheel {kind} fayyadami.")
        except Exception:pass

async def wheel_command(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; msg=update.effective_message
    if not user or user.id!=ADMIN_ID:
        await msg.reply_text("⛔ Admin qofa geengoo naannessuu danda'a.");return
    if not context.args or context.args[0] not in GAMES:
        await msg.reply_text("Fayyadama: /wheel 100 ykn /wheel 200");return
    kind=context.args[0]
    if not WEBAPP_URL:
        await msg.reply_text("WEBAPP_URL Render Environment keessatti galchi (fakkeenya: https://your-app.onrender.com).");return
    info=get_game_info(kind); count=get_player_count(kind)
    if info["finished"] and count==MAX_PLAYERS:
        with get_db() as db:
            done=db.execute("SELECT 1 FROM game_draws WHERE kind=? AND round_no=?",(kind,info["round_no"])).fetchone()
        if done:
            await msg.reply_text("Geengoon marsaa kanaaf duraan naanna'eera.");return
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton(
        f"🎡 Geengoo Abba {kind} Naannessi",
        web_app=WebAppInfo(url=f"{WEBAPP_URL}/wheel?kind={kind}"))]])
    await msg.reply_text(
        f"🎡 GEENGOO ABBA {kind}\n\nHirmaattota: {count}/{MAX_PLAYERS}\n"
        "Geengoon hirmaattota 20 erga guutamanii booda qofa naanna'a.\n"
        "Button armaan gadii tuqi. Admin qofa button kana argata.",
        reply_markup=keyboard)

async def addbalance(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; msg=update.effective_message
    if not user or user.id!=ADMIN_ID:await msg.reply_text("⛔ Admin qofa.");return
    if len(context.args)!=2:
        await msg.reply_text("Fayyadama: /addbalance USER_ID AMOUNT");return
    try:uid=int(context.args[0]);amount=int(context.args[1])
    except ValueError:await msg.reply_text("User ID fi amount lakkoofsa ta'uu qabu.");return
    if amount<=0:await msg.reply_text("Amount 0 ol ta'uu qaba.");return
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO balances(user_id,name,amount,updated_at) VALUES(?,?,0,?)",(uid,str(uid),now_text()))
        db.execute("UPDATE balances SET amount=amount+?,updated_at=? WHERE user_id=?",(amount,now_text(),uid))
        db.execute("INSERT INTO balance_transactions(user_id,amount,transaction_type,description,created_at) VALUES(?,?,'admin_credit',?,?)",(uid,amount,"Adminiin balance dabale",now_text()))
    await msg.reply_text(f"✅ User {uid}: {amount:,} birr dabalame. Balance: {get_balance(uid):,} birr")
    try:await context.bot.send_message(uid,f"💰 Balance kee irratti {amount:,} birr dabalameera.\nBalance: {get_balance(uid):,} birr")
    except Exception:pass

async def balance_command(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; msg=update.effective_message
    if not user:return
    if context.args and user.id==ADMIN_ID:
        try:uid=int(context.args[0])
        except ValueError:await msg.reply_text("User ID sirrii galchi.");return
        await msg.reply_text(f"User ID: {uid}\nBalance: {get_balance(uid):,} birr");return
    await msg.reply_text(f"💰 Balance kee: {get_balance(user.id):,} birr")

async def newgame(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; msg=update.effective_message
    if not user or user.id!=ADMIN_ID:await msg.reply_text("⛔ Admin qofa.");return
    if not context.args or context.args[0] not in GAMES:
        await msg.reply_text("Fayyadama: /newgame 100 ykn /newgame 200");return
    kind=context.args[0]
    with game_lock:
        db=get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            # Keep previous result/history and increase the round identifier.
            db.execute("DELETE FROM players WHERE kind=?",(kind,))
            db.execute("UPDATE game_state SET finished=0,round_no=round_no+1 WHERE kind=?",(kind,))
            db.execute("DELETE FROM game_views WHERE kind=?",(kind,))
            db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
    await msg.reply_text(f"✅ Tapha Abba {kind} haaromfameera. Balance hin jijjiiramne.")

async def error_handler(update,context):
    logger.error("Dogoggora Telegram keessatti uumame:",exc_info=context.error)

def run_flask():
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),use_reloader=False)

async def main():
    global BOT_LOOP, BOT_SEND
    if not BOT_TOKEN:raise RuntimeError("BOT_TOKEN hin jiru. Render Environment keessatti galchi.")
    if not ADMIN_ID:logger.warning("ADMIN_ID hin qindaa'in.")
    init_db()
    application=Application.builder().token(BOT_TOKEN).build()
    BOT_LOOP=asyncio.get_running_loop()
    BOT_SEND=application.bot.send_message
    application.add_handler(CommandHandler("start",start))
    application.add_handler(CommandHandler("balance",balance_command))
    application.add_handler(CommandHandler("addbalance",addbalance))
    application.add_handler(CommandHandler("newgame",newgame))
    application.add_handler(CommandHandler("wheel",wheel_command))
    application.add_handler(CallbackQueryHandler(buttons))
    application.add_handler(MessageHandler((filters.PHOTO|filters.Document.ALL|filters.TEXT)&~filters.COMMAND,handle_user_message))
    application.add_error_handler(error_handler)
    threading.Thread(target=run_flask,daemon=True).start()
    logger.info("Koshkoshe Bot jalqabuuf jira.")
    async with application:
        await application.start()
        if application.updater is None:raise RuntimeError("Telegram updater hin argamne.")
        await application.updater.start_polling(drop_pending_updates=False)
        logger.info("Koshkoshe Bot hojiirra jira.")
        try:await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()

if __name__=="__main__":
    asyncio.run(main())
