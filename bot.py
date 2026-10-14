
import os
import random
import logging
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

app = Flask(__name__)

# Demo qofaa: odeeffannoon kun server restart yoo godhe ni bada.
games = {
    "100": {"price": 100, "prizes": [1000, 500], "players": {}},
    "200": {"price": 200, "prizes": [2000, 1000], "players": {}},
}
results = []

@app.get("/")
def home():
    return "Koshkoshe Bot is running!"

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎮 Tapha 100 Birr", callback_data="game_100")],
        [InlineKeyboardButton("🎮 Tapha 200 Birr", callback_data="game_200")],
        [InlineKeyboardButton("📜 Seenaa Taphaa", callback_data="history")],
    ]
    await update.message.reply_text(
        "🎯 Baga gara Koshkoshe Bot dhuftan!\n\n"
        "Kun demo tapha carraa ti. Kaffaltii dhugaa hin fudhatu.\n"
        "Tapha filadhu:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data.startswith("game_"):
        kind = query.data.split("_")[1]
        game = games[kind]

        keyboard = [
            [
                InlineKeyboardButton(
                    str(n),
                    callback_data=f"pick_{kind}_{n}"
                )
            ]
            for n in range(1, 101)
        ]

        # Telegram inline keyboards allow a maximum of 8 buttons per row.
        keyboard = [
            [InlineKeyboardButton(str(n), callback_data=f"pick_{kind}_{n}")
             for n in range(start, min(start + 5, 101))]
            for start in range(1, 101, 5)
        ]

        await query.edit_message_text(
            f"🎮 Tapha {kind} Birr (DEMO)\n"
            f"👥 Hirmaattota: {len(game['players'])}/20\n"
            "Lakkoofsa 1–100 keessaa tokko filadhu.\n"
            "Kaffaltii dhugaa hin jiru.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if query.data.startswith("pick_"):
        _, kind, number = query.data.split("_")
        game = games[kind]
        uid = str(user.id)

        if uid in game["players"]:
            await query.answer("Tapha kana keessatti lakkoofsa duraan filatteetta.", show_alert=True)
            return

        if len(game["players"]) >= 20:
            await query.answer("Taphaan kun guutameera.", show_alert=True)
            return

        game["players"][uid] = {
            "name": user.full_name,
            "number": int(number),
        }

        await query.edit_message_text(
            f"✅ Lakkoofsa {number} filatteetta.\n"
            f"Tapha {kind} Birr (DEMO)\n"
            f"Hirmaattota: {len(game['players'])}/20"
        )

        if len(game["players"]) == 20:
            await finish_game(kind, context)
        return

    if query.data == "history":
        if not results:
            text = "Seenaa taphaa ammaaf hin jiru."
        else:
            text = "\n".join(results[-10:])
        await query.edit_message_text(text)

async def finish_game(kind: str, context: ContextTypes.DEFAULT_TYPE):
    game = games[kind]
    chosen = random.sample(range(1, 101), 2)
    winners = []

    for place, number in enumerate(chosen, start=1):
        player = next(
            (p for p in game["players"].values() if p["number"] == number),
            None
        )
        prize = game["prizes"][place - 1]

        if player:
            winners.append(f"🏆 Mo'ataa {place}: {player['name']} — {prize} birr (DEMO)")
        else:
            winners.append(
                f"🔢 Lakkoofsi {number} hin bitamne; badhaasni {prize} birr bulchiinsaaf hafa (DEMO)."
            )

    result = f"🎯 Bu'aa Tapha {kind} Birr:\n" + "\n".join(winners)
    results.append(result)

    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=result)
        except Exception:
            logging.exception("Admin message failed")

    # Demo: tapha itti aanuuf hirmaattota haaraa eegi.
    game["players"] = {}

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable hin jiru.")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
