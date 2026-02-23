import os
import random
import asyncio
import time
from collections import deque
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from openai import OpenAI

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_KEY = os.environ["OPENAI_API_KEY"]

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_KEY)

last_bot_time = 0
mute_until = 0

# Храним последние сообщения для анализа
recent_messages = deque(maxlen=20)

NEGATIVE_TRIGGERS = [
    "перегнул", "заткнись", "не смешно",
    "охренел", "заебал", "отвали", "хватит",
    "иди нах", "ты че", "тормози"
]

SYSTEM_PROMPT = """
Ты чат-бот в дружеской группе.

Стиль: токсичный, саркастичный, провокационный комментатор.
Уровень жесткости высокий (5/5). Можно материться.
Можно персонально поддевать участников.
Сообщения короткие. Максимум 2–3 подряд.

Пишешь на том языке, на котором общается чат.
Не объясняешь, что ты ИИ.
Не призываешь к реальному вреду.

Если несколько участников подряд негативно реагируют на тебя,
признай, что перегнул, и уйди в паузу.
"""

def group_push_detected():
    negative_count = 0
    for msg in list(recent_messages)[-5:]:
        low = msg.lower()
        if any(word in low for word in NEGATIVE_TRIGGERS):
            negative_count += 1
    return negative_count >= 3

@dp.message()
async def handle_message(message: Message):
    global last_bot_time, mute_until

    if message.chat.type not in ["group", "supergroup"]:
        return

    if not message.text:
        return

    recent_messages.append(message.text)

    now = time.time()

    # Если бот в паузе
    if now < mute_until:
        return

    # Если группа начала пушить
    if group_push_detected():
        await message.answer(random.choice([
            "Окей, перегнул. Бывает.",
            "Ладно, сегодня без огня.",
            "Понял, снимаю обороты."
        ]))
        mute_until = now + 3600  # 1 час паузы
        return

    # Не чаще чем раз в 8–15 минут
    if now - last_bot_time < random.randint(480, 900):
        return

    # Непредсказуемый шанс вмешательства
    if random.random() > 0.18:
        return

    await bot.send_chat_action(message.chat.id, "typing")
    await asyncio.sleep(random.randint(5, 15))

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message.text}
        ],
        temperature=1.0,
        max_tokens=200
    )

    reply = response.choices[0].message.content

    # Разбиваем на 1–3 сообщения
    parts = reply.split("\n")
    parts = [p.strip() for p in parts if p.strip()]

    for part in parts[:3]:
        await message.answer(part)
        await asyncio.sleep(random.randint(2, 5))

    last_bot_time = now


async def main():
    await dp.start_polling(bot)

asyncio.run(main())
