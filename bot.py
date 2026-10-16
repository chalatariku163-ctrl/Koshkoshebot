import os
import random
import logging
import threading
import asyncio
from datetime import datetime, timezone

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

app = Flask(__name__)

MAX_PLAYERS = 20
results = []
game_lock = threading.Lock()

games = {
    "100": {
        "price": 100,
        "prizes": [1000, 500],
        "players": {},
        "finished": False,
    },
    "200": {
        "price": 200,
        "prizes": [2000, 1000],
        "players": {},
        "finished": False,
    },
}


@app.get("/")
def home():
    return "Koshkoshe Bot is running!"


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


def main_menu():
    keyboard = [
        [InlineKeyboardButton(
            "🎮 Tapha Abba 100 Birr",
            callback_data="game_100",
        )],
        [InlineKeyboardButton(
            "🎮 Tapha Abba 200 Birr",
            callback_data="game_200",
        )],
        [InlineKeyboardButton(
            "📜 Seenaa Taphaa",
            callback_data="history",
        )],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message is None:
        return

    await update.effective_message.reply_text(
        "🎯 BAGA GARA KOSHKOSHE BOT DHUFTAN!\n\n"
        "🎮 TAPHA ABBA 100\n"
        "👥 Hirmaattota: 20\n"
        "🥇 Badhaasa 1ffaa: 1,000 birr\n"
        "🥈 Badhaasa 2ffaa: 500 birr\n\n"
        "🎮 TAPHA ABBA 200\n"
        "👥 Hirmaattota: 20\n"
        "🥇 Badhaasa 1ffaa: 2,000 birr\n"
        "🥈 Badhaasa 2ffaa: 1,000 birr\n\n"
        "⚠️ Kun demo dha. Kaffaltii dhugaa hin fudhatu.\n\n"
        "👇 Tapha kee filadhu:",
        reply_markup=main_menu(),
    )


async def show_game(query, kind):
    with game_lock:
        game = games[kind]
        player_count = len(game["players"])
        finished = game["finished"]

    if finished:
        await query.edit_message_text(
            "Taphaan kun xumurameera.\n/start tuqi."
        )
        return

    if player_count >= MAX_PLAYERS:
        await query.edit_message_text(
            "Taphaan kun guutameera. Bu'aa eegi."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                str(n),
                callback_data=f"pick_{kind}_{n}",
            )
            for n in range(start, min(start + 5, 101))
        ]
        for start in range(1, 101, 5)
    ]

    await query.edit_message_text(
        f"🎮 TAPHA ABBA {kind} — DEMO\n\n"
        f"💵 Gatii taphaa: {kind} birr\n"
        f"👥 Hirmaattota: {player_count}/{MAX_PLAYERS}\n\n"
        "Lakkoofsa 1–100 keessaa tokko filadhu.\n"
        "Nama tokkoof lakkoofsi tokko qofa.\n\n"
        "⚠️ Kaffaltii dhugaa hin jiru.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    user = query.from_user
    data = query.data or ""

    if data == "menu":
        await query.edit_message_text(
            "🎯 KOSHKOSHE BOT\n\nTapha filadhu:",
            reply_markup=main_menu(),
        )
        return

    if data == "history":
        with game_lock:
            history = list(results[-10:])

        text = (
            "📜 Seenaa taphaa ammaaf hin jiru."
            if not history
            else "📜 SEENAA TAPHAA\n\n" + "\n\n".join(history)
        )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Gara Menu",
                    callback_data="menu",
                )]
            ]),
        )
        return

    if data.startswith("game_"):
        kind = data.split("_", 1)[1]

        if kind not in games:
            await query.answer("Taphaan kun hin jiru.", show_alert=True)
            return

        await show_game(query, kind)
        return

    if not data.startswith("pick_"):
        return

    parts = data.split("_")
    if len(parts) != 3:
        await query.answer("Filannoon sirrii miti.", show_alert=True)
        return

    _, kind, number_text = parts

    if kind not in games:
        await query.answer("Taphaan kun hin jiru.", show_alert=True)
        return

    try:
        number = int(number_text)
    except ValueError:
        await query.answer("Lakkoofsi sirrii miti.", show_alert=True)
        return

    if not 1 <= number <= 100:
        await query.answer("Lakkoofsa 1–100 filadhu.", show_alert=True)
        return

    should_finish = False

    with game_lock:
        game = games[kind]
        uid = str(user.id)

        if game["finished"]:
            error = "Taphaan kun xumurameera."
        elif uid in game["players"]:
            error = "Lakkoofsa duraan filatteetta."
        elif len(game["players"]) >= MAX_PLAYERS:
            error = "Taphaan kun guutameera."
        elif any(
            player["number"] == number
            for player in game["players"].values()
        ):
            error = "Lakkoofsi kun duraan filatameera."
        else:
            error = None
            game["players"][uid] = {
                "name": user.full_name,
                "number": number,
                "user_id": user.id,
            }

        player_count = len(game["players"])

        if error is None and player_count == MAX_PLAYERS:
            game["finished"] = True
            should_finish = True

    if error:
        await query.answer(error, show_alert=True)
        return

    await query.edit_message_text(
        f"✅ Lakkoofsa {number} filatteetta!\n\n"
        f"🎮 Tapha Abba {kind} — DEMO\n"
        f"👥 Hirmaattota: {player_count}/{MAX_PLAYERS}\n\n"
        "Bu'aa taphaa eegi.\n"
        "/start jechuun gara menu deebi'i."
    )

    if should_finish:
        await finish_game(kind, context)


async def finish_game(kind: str, context: ContextTypes.DEFAULT_TYPE):
    with game_lock:
        game = games[kind]
        players = list(game["players"].values())
        prizes = list(game["prizes"])

    drawn_numbers = random.sample(range(1, 101), 2)
    winner_lines = []

    for place, number in enumerate(drawn_numbers):
        prize = prizes[place]

        player = next(
            (p for p in players if p["number"] == number),
            None,
        )

        if player:
            winner_lines.append(
                f"🏆 Mo'ataa {place + 1}: {player['name']}\n"
                f"🔢 Lakkoofsa: {number}\n"
                f"💰 Badhaasa: {prize:,} birr (DEMO)"
            )
        else:
            winner_lines.append(
                f"🔢 Lakkoofsa mo'ataa {place + 1}: {number}\n"
                "❌ Lakkoofsi kun hin bitamne.\n"
                f"💰 {prize:,} birr: badhaasni demo keessatti hin kennamu."
            )

    result = (
        f"🎯 BU'AA TAPHA ABBA {kind}\n\n"
        + "\n\n".join(winner_lines)
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    with game_lock:
        results.append(f"{timestamp}\n{result}")
        if len(results) > 100:
            del results[:-100]

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
            logger.exception("Bu'aa gara admin erguun hin milkoofne")

    logger.info("Tapha Abba %s xumurame.", kind)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Dogoggora Telegram keessatti uumame:",
        exc_info=context.error,
    )


def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN hin jiru. Render Environment keessatti galchi."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons))
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
