import os
import random
import logging
import threading
import asyncio
import sqlite3

from datetime import datetime, timezone
from flask import Flask, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# CONFIGURATION
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/var/data/koshkoshe.db"
)

# Yoo persistent disk hin qabne, local path fayyadami.
if not os.path.isdir(os.path.dirname(DATABASE_PATH)):
    DATABASE_PATH = "koshkoshe.db"

app = Flask(__name__)

MAX_PLAYERS = 20

game_lock = threading.RLock()


GAMES = {
    "100": {
        "price": 100,
        "prizes": [1000, 500],
    },
    "200": {
        "price": 200,
        "prizes": [2000, 1000],
    },
}


# =========================
# DATABASE
# =========================

def get_db():
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA busy_timeout = 30000")

    return connection


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

        for kind in GAMES:
            db.execute("""
                INSERT OR IGNORE INTO game_state
                (kind, finished)
                VALUES (?, 0)
            """, (kind,))


def get_game_state(kind):
    with get_db() as db:
        row = db.execute("""
            SELECT finished
            FROM game_state
            WHERE kind = ?
        """, (kind,)).fetchone()

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
        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM players
            WHERE kind = ?
        """, (kind,)).fetchone()

    return row["total"]


def get_user_player(kind, user_id):
    with get_db() as db:
        row = db.execute("""
            SELECT *
            FROM players
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

    return {
        row["group_start"]: dict(row)
        for row in rows
    }


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
# TELEGRAM MENU
# =========================

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "🎮 Tapha Abba 100 Birr",
                callback_data="game_100",
            )
        ],
        [
            InlineKeyboardButton(
                "🎮 Tapha Abba 200 Birr",
                callback_data="game_200",
            )
        ],
        [
            InlineKeyboardButton(
                "📜 Seenaa Taphaa",
                callback_data="history",
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message is None:
        return

    await update.effective_message.reply_text(
        "🎯 BAGA GARA KOSHKOSHE BOT DHUFTAN!\n\n"

        "🎮 TAPHA ABBA 100\n"
        "💵 Gatii: 100 birr\n"
        "👥 Gareewwan: 20\n"
        "🥇 Badhaasa 1ffaa: 1,000 birr\n"
        "🥈 Badhaasa 2ffaa: 500 birr\n\n"

        "🎮 TAPHA ABBA 200\n"
        "💵 Gatii: 200 birr\n"
        "👥 Gareewwan: 20\n"
        "🥇 Badhaasa 1ffaa: 2,000 birr\n"
        "🥈 Badhaasa 2ffaa: 1,000 birr\n\n"

        "📌 Lakkoofsota 1–100 keessaa garee filadhu.\n"
        "📌 Garee tokko lakkoofsota 5 qaba.\n"
        "📌 Garee tokko namni tokko qofa qabaata.\n"
        "📌 Fayyadamaan kaffaltii demo tokkoon garee tokko argata.\n\n"

        "⚠️ Kun demo dha. Kaffaltii dhugaa hin fudhatu.\n\n"

        "👇 Tapha kee filadhu:",
        reply_markup=main_menu(),
    )


# =========================
# GROUP SELECTION
# =========================

def make_group_keyboard(kind):
    occupied = get_occupied_groups(kind)

    keyboard = []

    # Gareewwan 20:
    # 1–5, 6–10, ... 96–100

    for group_start in range(1, 101, 5):
        group_end = group_start + 4

        if group_start in occupied:
            label = f"🔒 {group_start}–{group_end}"

            callback = f"taken_{kind}_{group_start}"

        else:
            label = f"🎯 {group_start}–{group_end}"

            callback = f"pick_{kind}_{group_start}"

        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=callback,
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ Gara Menu",
            callback_data="menu",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


async def show_game(query, kind):

    with game_lock:
        finished = get_game_state(kind)
        player_count = get_player_count(kind)
        user_id = query.from_user.id

        user_player = get_user_player(kind, user_id)

    if finished:
        await query.edit_message_text(
            f"🎮 Tapha Abba {kind}\n\n"
            "Taphaan kun xumurameera.\n"
            "Bu'aa taphaa ilaali ykn menu deebi'i.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📜 Seenaa Taphaa",
                        callback_data="history",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Gara Menu",
                        callback_data="menu",
                    )
                ],
            ]),
        )
        return

    if user_player:
        group_start = user_player["group_start"]

        await query.edit_message_text(
            "✅ GAREE KEE GALMAA'EERA!\n\n"
            f"🎮 Tapha Abba {kind}\n"
            f"🔢 Lakkoofsota kee: "
            f"{group_start}–{group_start + 4}\n\n"
            "🔒 Garee kana nama biraatu hin qabu.\n"
            "🏆 Bu'aa taphaa eegi.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Gara Menu",
                        callback_data="menu",
                    )
                ]
            ]),
        )
        return

    if player_count >= MAX_PLAYERS:
        await query.edit_message_text(
            "Taphaan kun guutameera.\n"
            "Bu'aa taphaa eegi.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Gara Menu",
                        callback_data="menu",
                    )
                ]
            ]),
        )
        return

    keyboard = make_group_keyboard(kind)

    await query.edit_message_text(
        f"🎮 TAPHA ABBA {kind} — DEMO\n\n"

        f"💵 Gatii garee tokkoo: {kind} birr\n"
        f"👥 Gareewwan fudhataman: "
        f"{player_count}/{MAX_PLAYERS}\n\n"

        "👇 Garee kee filadhu:\n\n"

        "🎯 1–5 filachuun lakkoofsota 1, 2, 3, 4, 5 argatta.\n"
        "🎯 6–10 filachuun lakkoofsota 6, 7, 8, 9, 10 argatta.\n\n"

        "🔒 Gareen fudhatame nama biraaf hin kennamu.\n"
        "⚠️ Kun demo dha.",
        reply_markup=keyboard,
    )


# =========================
# HISTORY
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
# BUTTON HANDLER
# =========================

async def buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    user = query.from_user
    data = query.data or ""

    # MENU

    if data == "menu":
        await query.edit_message_text(
            "🎯 KOSHKOSHE BOT\n\nTapha filadhu:",
            reply_markup=main_menu(),
        )
        return

    # HISTORY

    if data == "history":
        history = get_history()

        if not history:
            text = "📜 Seenaa taphaa ammaaf hin jiru."

        else:
            text = "📜 SEENAA TAPHAA\n\n"

            text += "\n\n".join(
                f"🕒 {item['created_at']}\n"
                f"{item['result']}"
                for item in history
            )

        await query.edit_message_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Gara Menu",
                        callback_data="menu",
                    )
                ]
            ]),
        )
        return

    # OPEN GAME

    if data.startswith("game_"):
        kind = data.split("_", 1)[1]

        if kind not in GAMES:
            await query.answer(
                "Taphaan kun hin jiru.",
                show_alert=True,
            )
            return

        await show_game(query, kind)
        return

    # ALREADY OCCUPIED GROUP

    if data.startswith("taken_"):
        parts = data.split("_")

        if len(parts) != 3:
            return

        _, kind, group_text = parts

        try:
            group_start = int(group_text)
        except ValueError:
            return

        if kind not in GAMES:
            return

        occupied = get_occupied_groups(kind)

        if group_start in occupied:
            await query.answer(
                "🔒 Gareen kun duraan fudhatameera!",
                show_alert=True,
            )
        else:
            await query.answer(
                "Gareen kun bilisa ta'uu danda'a. Irra deebi'i.",
                show_alert=True,
            )

        await show_game(query, kind)
        return

    # PICK GROUP

    if not data.startswith("pick_"):
        return

    parts = data.split("_")

    if len(parts) != 3:
        await query.answer(
            "Filannoon sirrii miti.",
            show_alert=True,
        )
        return

    _, kind, group_text = parts

    if kind not in GAMES:
        await query.answer(
            "Taphaan kun hin jiru.",
            show_alert=True,
        )
        return

    try:
        group_start = int(group_text)
    except ValueError:
        await query.answer(
            "Lakkoofsi sirrii miti.",
            show_alert=True,
        )
        return

    if group_start not in range(1, 101, 5):
        await query.answer(
            "Garee sirrii filadhu.",
            show_alert=True,
        )
        return

    group_end = group_start + 4

    should_finish = False
    error = None

    # TRANSACTION:
    # Gareen tokko yeroo tokko qofa kennama.

    with game_lock:
        db = get_db()

        try:
            db.execute("BEGIN IMMEDIATE")

            state = db.execute("""
                SELECT finished
                FROM game_state
                WHERE kind = ?
            """, (kind,)).fetchone()

            if state is None or state["finished"]:
                error = "Taphaan kun xumurameera."

            else:
                existing_user = db.execute("""
                    SELECT group_start
                    FROM players
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
                        SELECT user_id
                        FROM players
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
                            timestamp = datetime.now(
                                timezone.utc
                            ).strftime("%Y-%m-%d %H:%M:%S UTC")

                            db.execute("""
                                INSERT INTO players (
                                    kind,
                                    user_id,
                                    name,
                                    group_start,
                                    created_at
                                )
                                VALUES (?, ?, ?, ?, ?)
                            """, (
                                kind,
                                user.id,
                                user.full_name,
                                group_start,
                                timestamp,
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
        await query.answer(
            error,
            show_alert=True,
        )

        await show_game(query, kind)
        return

    await query.edit_message_text(
        "✅ GAREEN KEE MILKAA'EERA!\n\n"

        f"🎮 Tapha Abba {kind}\n"
        f"🔢 Lakkoofsota kee: "
        f"{group_start}–{group_end}\n"
        f"💵 Gatii taphaa: {GAMES[kind]['price']} birr (DEMO)\n\n"

        "🔒 Gareen kun siif galmaa'eera.\n"
        "💾 Odeeffannoon kee database keessatti kuufameera.\n"
        "🏆 Bu'aa taphaa eegi.\n\n"

        "⚠️ Kaffaltii dhugaa hin jiru.",

        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Gara Menu",
                    callback_data="menu",
                )
            ]
        ]),
    )

    if should_finish:
        await finish_game(kind, context)


# =========================
# FINISH GAME
# =========================

async def finish_game(
    kind: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    with game_lock:
        players = get_players(kind)

        prizes = list(GAMES[kind]["prizes"])

    if len(players) != MAX_PLAYERS:
        logger.warning(
            "Tapha %s hirmaattota 20 hin guunne.",
            kind,
        )
        return

    drawn_numbers = random.sample(
        range(1, 101),
        2,
    )

    winner_lines = []

    for place, number in enumerate(drawn_numbers):
        prize = prizes[place]

        player = next(
            (
                p for p in players
                if p["group_start"] <= number
                <= p["group_start"] + 4
            ),
            None,
        )

        if player:
            winner_lines.append(
                f"🏆 MO'ATAA {place + 1}\n"
                f"👤 Maqaa: {player['name']}\n"
                f"🔢 Lakkoofsa mo'ataa: {number}\n"
                f"🎟️ Garee: "
                f"{player['group_start']}–"
                f"{player['group_start'] + 4}\n"
                f"💰 Badhaasa: {prize:,} birr (DEMO)"
            )

        else:
            winner_lines.append(
                f"🔢 Lakkoofsa mo'ataa {place + 1}: {number}\n"
                "❌ Gareen lakkoofsa kana qabu hin jiru.\n"
                f"💰 Badhaasa: {prize:,} birr\n"
                "Demo keessatti kaffaltiin hin kennamu."
            )

    result = (
        f"🎯 BU'AA TAPHA ABBA {kind}\n\n"
        + "\n\n".join(winner_lines)
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    with get_db() as db:
        db.execute("""
            INSERT INTO game_results (
                kind,
                result,
                created_at
            )
            VALUES (?, ?, ?)
        """, (
            kind,
            result,
            timestamp,
        ))

    # Bu'aa gara hirmaattota hundaatti ergi.

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
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=result,
            )

        except Exception:
            logger.exception(
                "Bu'aa gara admin erguun hin milkoofne"
            )

    logger.info(
        "Tapha Abba %s xumurame.",
        kind,
    )


# =========================
# ADMIN: RESET GAME
# =========================

async def newgame(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user is None:
        return

    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text(
            "⛔ Admin qofa ajaja kana fayyadamuu danda'a."
        )
        return

    if not context.args or context.args[0] not in GAMES:
        await update.effective_message.reply_text(
            "Fayyadama:\n"
            "/newgame 100\n"
            "/newgame 200\n\n"
            "Hubachiisa: ajajni kun tapha filatame "
            "irraa galmee hirmaattotaa ni haqa."
        )
        return

    kind = context.args[0]

    with game_lock:
        with get_db() as db:
            db.execute("""
                DELETE FROM players
                WHERE kind = ?
            """, (kind,))

            db.execute("""
                UPDATE game_state
                SET finished = 0
                WHERE kind = ?
            """, (kind,))

    await update.effective_message.reply_text(
        f"✅ Tapha Abba {kind} haaromfameera.\n"
        "Gareewwan 20 bilisa ta'aniiru."
    )


# =========================
# ERROR HANDLER
# =========================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
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
# BOT STARTUP
# =========================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN hin jiru. "
            "Render Environment keessatti galchi."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("newgame", newgame)
    )

    application.add_handler(
        CallbackQueryHandler(buttons)
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
            raise RuntimeError(
                "Telegram updater hin argamne."
            )

        await application.updater.start_polling(
            drop_pending_updates=False,
        )

        logger.info(
            "Koshkoshe Bot hojiirra jira."
        )

        try:
            await asyncio.Event().wait()

        finally:
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
