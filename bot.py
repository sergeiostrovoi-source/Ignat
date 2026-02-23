import os
import random
import asyncio
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from openai import OpenAI

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# --- поведение ---
REPLY_CHANCE = 0.30        # 30% шанс влезть самому
ACTIVE_WINDOW = 120        # 2 минуты поддерживает беседу после включения
MUTE_SECONDS = 180         # 3 минуты молчит если "послали"

active_until = 0
mute_until = 0

SYSTEM_PROMPT = """
Ты — участник дружеского группового чата. Ведёшь себя как живой человек.

ЯЗЫК:
- Всегда отвечай на языке последнего сообщения.
- Если языки смешаны — используй доминирующий.

ХАРАКТЕР:
- Ты циничный, саркастичный, токсичный комментатор (5/5).
- Мат допустим.
- Поддеваешь умно, не тупо.
- 1–3 коротких сообщения, без лекций.
- Не говори, что ты ИИ.

ПОВЕДЕНИЕ:
- Иногда одна фраза, иногда две, редко три.
- Иногда короткий колкий вопрос.
- Иногда съезжай с темы, чтобы звучать как человек.

ЕСЛИ ТЕБЯ ПОСЛАЛИ:
- Одной короткой фразой признай и заткнись.
"""

PUSH_WORDS = [
    "заткнись", "перегнул", "хватит", "тормози", "заебал",
    "иди нах", "пошел нах", "пішов нах", "відвали"
]

def is_push(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in PUSH_WORDS)

async def generate_reply(user_text: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Ответь на том же языке и кратко (1–3 строки): {user_text}"}
        ],
        temperature=1.1,
        max_tokens=200,
    )
    return resp.choices[0].message.content.strip()

@dp.message()
async def handle_message(message: Message):
    global active_until, mute_until

    if message.chat.type not in ["group", "supergroup"]:
        return
    if not message.text:
        return

    now = time.time()
    text = message.text
    low = text.lower()

    if now < mute_until:
        return

    # если его послали — сдаёт назад и молчит
    if is_push(low):
        await asyncio.sleep(random.randint(1, 2))
        await message.reply(random.choice(["Ок, торможу.", "Ладно.", "Понял."]))
        mute_until = now + MUTE_SECONDS
        active_until = 0
        return

    # если явно позвали
    if "бот" in low or "@ignathui_bot" in low:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        active_until = now + ACTIVE_WINDOW
        return

    # если он уже активен — поддерживает разговор
    if now < active_until:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        return

    # самовключение
    if random.random() < REPLY_CHANCE:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        active_until = now + ACTIVE_WINDOW

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
