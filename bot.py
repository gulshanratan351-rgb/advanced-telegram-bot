from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pymongo import MongoClient
import datetime

from config import API_ID, API_HASH, BOT_TOKEN, MONGO_URI

# MongoDB setup
mongo = MongoClient(MONGO_URI)
db = mongo["telegram_bot"]
users = db["users"]
channels = db["channels"]
schedules = db["schedules"]

# Bot setup
app = Client("modifier_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
scheduler = AsyncIOScheduler()

# Auto Forward Posts
@app.on_message(filters.channel)
async def auto_forward(client, message):
    targets = [c["target"] for c in channels.find({"active": True})]
    for target in targets:
        header = users.find_one({"id": message.from_user.id}, {"header": 1})
        footer = users.find_one({"id": message.from_user.id}, {"footer": 1})
        text = f"{header.get('header','')}\n{message.text}\n{footer.get('footer','')}"
        await client.send_message(target, text)

# Multi Channel Posting
@app.on_message(filters.command("post"))
async def multi_post(client, message):
    _, text = message.text.split(" ", 1)
    targets = [c["target"] for c in channels.find({"active": True})]
    for target in targets:
        await client.send_message(target, text)

# Scheduled Posting
@app.on_message(filters.command("schedule"))
async def schedule_post(client, message):
    # Example: /schedule @channel1 2026-05-24 15:00 Hello World
    _, channel, date, time, text = message.text.split(" ", 4)
    run_date = datetime.datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    schedules.insert_one({"channel": channel, "text": text, "time": run_date, "status": "pending"})
    scheduler.add_job(lambda: client.send_message(channel, text), "date", run_date=run_date)
    await message.reply("✅ Scheduled successfully!")

# Header/Footer System
@app.on_message(filters.command("setheader"))
async def set_header(client, message):
    header = message.text.split(" ", 1)[1]
    users.update_one({"id": message.from_user.id}, {"$set": {"header": header}}, upsert=True)
    await message.reply("✅ Header set!")

@app.on_message(filters.command("setfooter"))
async def set_footer(client, message):
    footer = message.text.split(" ", 1)[1]
    users.update_one({"id": message.from_user.id}, {"$set": {"footer": footer}}, upsert=True)
    await message.reply("✅ Footer set!")

# Spoiler Hidden Images
@app.on_message(filters.command("spoiler"))
async def spoiler_image(client, message):
    if message.reply_to_message and message.reply_to_message.photo:
        await client.send_photo(message.chat.id, message.reply_to_message.photo.file_id, has_spoiler=True)

# Inline Buttons
@app.on_message(filters.command("button"))
async def inline_button(client, message):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Click Me", url="https://t.me/yourchannel")]]
    )
    await message.reply("Here’s your button:", reply_markup=keyboard)

# Auto Join Request Accept
@app.on_chat_join_request()
async def auto_accept(client, join_request):
    await client.approve_chat_join_request(join_request.chat.id, join_request.from_user.id)

# Service Message Delete
@app.on_message(filters.service)
async def delete_service(client, message):
    await message.delete()

scheduler.start()
app.run()
