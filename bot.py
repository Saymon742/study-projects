import asyncio
import os
import logging
import traceback
from datetime import datetime, timedelta, timezone, time
from typing import Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import dateparser
from dateparser.search import search_dates

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN", "7639908563:AAFrKwzQ4T_DwhGbK9pZ2f910EyS3W-0Tmo")
DB_PATH = "reminders.db"

# ==================== БАЗА ДАННЫХ ====================
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  tz_offset_minutes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  due_at_utc TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  FOREIGN KEY(user_id) REFERENCES users(user_id)
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()

async def get_tz_offset_minutes(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tz_offset_minutes FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def set_tz_offset_minutes(user_id: int, minutes: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, tz_offset_minutes) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET tz_offset_minutes=excluded.tz_offset_minutes", 
            (user_id, minutes)
        )
        await db.commit()

async def insert_reminder(user_id: int, text: str, due_at_utc: datetime) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reminders(user_id, text, due_at_utc, created_at_utc, status) VALUES(?, ?, ?, ?, 'active')",
            (user_id, text, due_at_utc.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            return (await cur.fetchone())[0]

async def get_user_active_reminders(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, due_at_utc FROM reminders WHERE user_id=? AND status='active' ORDER BY due_at_utc ASC",
            (user_id,),
        ) as cur:
            return await cur.fetchall()

async def get_reminder(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, text, due_at_utc, status FROM reminders WHERE id=?",
            (reminder_id,),
        ) as cur:
            return await cur.fetchone()

async def update_reminder(reminder_id: int, *, text: Optional[str] = None, due_at_utc: Optional[datetime] = None):
    fields, params = [], []
    if text is not None:
        fields.append("text=?")
        params.append(text)
    if due_at_utc is not None:
        fields.append("due_at_utc=?")
        params.append(due_at_utc.isoformat())
    if fields:
        params.append(reminder_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE reminders SET {', '.join(fields)} WHERE id=?", params)
            await db.commit()

async def delete_reminder(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET status='deleted' WHERE id=?", (reminder_id,))
        await db.commit()

async def list_all_future_active_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT r.id, r.user_id, r.text, r.due_at_utc, u.tz_offset_minutes FROM reminders r JOIN users u ON r.user_id=u.user_id WHERE r.status='active'"
        ) as cur:
            return await cur.fetchall()

# ==================== РАБОТА С ВРЕМЕНЕМ ====================
def parse_tz_offset_str(offset_str: str) -> Optional[int]:
    try:
        sign = 1 if offset_str.strip()[0] == '+' else -1
        hh, mm = offset_str.strip()[1:].split(':')
        return sign * (int(hh) * 60 + int(mm))
    except Exception:
        return None

def offset_tz(minutes: int) -> timezone:
    return timezone(timedelta(minutes=minutes))

def format_dt_for_user(dt_utc: datetime, tz_minutes: int) -> str:
    return dt_utc.astimezone(offset_tz(tz_minutes)).strftime("%d.%m.%Y, %H:%M")

def compute_morning_dt(dt_utc: datetime, tz_minutes: int) -> datetime:
    tz = offset_tz(tz_minutes)
    local_dt = dt_utc.astimezone(tz)
    morning_local = datetime.combine(local_dt.date(), time(8, 0), tz)
    return morning_local.astimezone(timezone.utc)

# ==================== ПАРСИНГ ДАТ ====================
LANGS = ["ru", "uk", "en"]

def extract_datetime_and_text(raw: str, base_dt_utc: datetime, tz_minutes: int) -> Tuple[Optional[datetime], str]:
    tz = offset_tz(tz_minutes)
    base_local = base_dt_utc.astimezone(tz)
    current_year = base_local.year
    
    settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": str(tz),
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": base_local,
        "PARSERS": ["absolute-time", "relative-time", "timestamp", "custom-formats"],
        "DEFAULT_LANGUAGES": LANGS,
    }

    try:
        result = search_dates(raw, languages=LANGS, settings=settings)
        if not result:
            return None, raw.strip()

        chosen_text, chosen_dt = None, None
        for fragment, dt in result:
            if dt.year == 2000:
                dt = dt.replace(year=current_year)
                if dt < base_local:
                    dt = dt.replace(year=current_year + 1)
            
            chosen_text, chosen_dt = fragment, dt
            if dt.hour != 0 or dt.minute != 0:
                break

        if not chosen_dt or chosen_dt < base_local:
            return None, raw.strip()

        due_utc = chosen_dt.astimezone(timezone.utc)
        
        cleaned = raw
        for fragment, _ in result:
            cleaned = cleaned.replace(fragment, "", 1).strip()
        
        return due_utc, cleaned.strip(" ,.-") or "Напоминание"

    except Exception as e:
        logger.warning(f"Ошибка парсинга даты: {e}")
        return None, raw.strip()

# ==================== ПЛАНИРОВЩИК ====================
scheduler = None
bot_instance = None

async def schedule_all_existing_jobs():
    rows = await list_all_future_active_reminders()
    now = datetime.now(timezone.utc)
    for row in rows:
        rid, user_id, text, due_at_utc_str, tz_minutes = row
        due_at_utc = datetime.fromisoformat(due_at_utc_str).astimezone(timezone.utc)
        await schedule_reminder_jobs(rid, user_id, text, due_at_utc, tz_minutes, now)

async def schedule_reminder_jobs(reminder_id: int, user_id: int, text: str, due_at_utc: datetime, tz_minutes: int, now: Optional[datetime] = None):
    if now is None:
        now = datetime.now(timezone.utc)

    if due_at_utc > now:
        scheduler.add_job(
            send_due_notification, 
            trigger=DateTrigger(run_date=due_at_utc), 
            id=f"due:{reminder_id}", 
            args=[user_id, reminder_id, text]
        )

    pre30 = due_at_utc - timedelta(minutes=30)
    if pre30 > now:
        scheduler.add_job(
            send_pre30_notification, 
            trigger=DateTrigger(run_date=pre30), 
            id=f"pre30:{reminder_id}", 
            args=[user_id, reminder_id, text]
        )

    morning_utc = compute_morning_dt(due_at_utc, tz_minutes)
    if morning_utc > now:
        scheduler.add_job(
            send_morning_notification, 
            trigger=DateTrigger(run_date=morning_utc), 
            id=f"morning:{reminder_id}", 
            args=[user_id, reminder_id, text, due_at_utc, tz_minutes]
        )

async def remove_jobs_for(reminder_id: int):
    for prefix in ("due", "pre30", "morning"):
        try:
            scheduler.remove_job(f"{prefix}:{reminder_id}")
        except Exception:
            pass

# ==================== УВЕДОМЛЕНИЯ ====================
async def send_morning_notification(user_id: int, reminder_id: int, text: str, due_at_utc: datetime, tz_minutes: int):
    try:
        when = format_dt_for_user(due_at_utc, tz_minutes)
        await bot_instance.send_message(user_id, f"🌅 Напоминание на сегодня: {text}\nКогда: {when}")
    except Exception as e:
        logger.error(f"Ошибка отправки утреннего уведомления: {e}")

async def send_pre30_notification(user_id: int, reminder_id: int, text: str):
    try:
        await bot_instance.send_message(user_id, f"⏳ Через 30 минут: {text}")
    except Exception as e:
        logger.error(f"Ошибка отправки предупреждения: {e}")

async def send_due_notification(user_id: int, reminder_id: int, text: str):
    try:
        await bot_instance.send_message(user_id, f"🔔 Сейчас: {text}")
        logger.info(f"Напоминание {reminder_id} отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания: {e}")

# ==================== FSM СОСТОЯНИЯ ====================
class CreateReminder(StatesGroup):
    waiting_for_datetime = State()
    waiting_for_text = State()

class EditReminder(StatesGroup):
    choosing_field = State()
    entering_text = State()
    entering_datetime = State()

# ==================== ОСНОВНОЙ КОД БОТА ====================
dp = Dispatcher()

async def bot_heartbeat():
    """Отправка heartbeat в консоль каждые 2 минуты"""
    logger.info("💓 Бот активен и работает")
    jobs = scheduler.get_jobs()
    active_jobs = len([job for job in jobs if not job.id.startswith('heartbeat')])
    logger.info(f"📊 Активных напоминаний в планировщике: {active_jobs}")

@dp.startup()
async def on_startup(bot: Bot):
    global scheduler, bot_instance
    bot_instance = bot
    
    logger.info("🚀 Бот запускается...")
    await init_db()
    
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.start()
    
    # Добавляем heartbeat задачу
    scheduler.add_job(
        bot_heartbeat,
        trigger=IntervalTrigger(minutes=2),
        id="heartbeat",
        replace_existing=True
    )
    
    await schedule_all_existing_jobs()
    logger.info("✅ Бот успешно запущен и планировщик инициализирован")

# ==================== КОМАНДЫ БОТА ====================

@dp.message()
async def handle_unknown(message: Message):
    """Обработчик неизвестных команд"""
    if message.text.startswith('/'):
        await message.answer(
            "🤔 Неизвестная команда. Используйте /help для просмотра доступных команд"
        )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот-напоминатель\n\n"
        "Я помогу вам не забывать о важных делах и событиях.\n\n"
        "📋 *Основные команды:*\n"
        "• /remind - создать напоминание\n"
        "• /my_reminders - ваши напоминания\n"
        "• /set_tz - установить часовой пояс\n"
        "• /help - подробная справка\n\n"
        "🚀 *Начните с установки часового пояса:*\n"
        "/set_tz +03:00 (для Москвы)\n\n"
        "❓ Нужна помощь? Напишите /help",
        parse_mode="Markdown"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды help"""
    help_text = """
🤖 Помощь по командам бота-напоминателя

/start - начать работу с ботом
/help - показать эту справку
/remind - создать новое напоминание
/my_reminders - посмотреть ваши напоминания
/set_tz - установить часовой пояс

📋 Примеры использования:
• /remind завтра в 15:00 совещание
• /remind 25 декабря в 18:30 купить торт
• /remind через 2 часа позвонить маме
• /set_tz +02:00 - для киевского времени

⚙️ Как это работает:
1. Установите часовой пояс командой /set_tz
2. Создавайте напоминания командой /remind
3. Получайте уведомления в указанное время
4. Управляйте напоминаниями через /my_reminders

🔔 Бот напомнит вам:
- Утром в 8:00 о событиях дня
- За 30 минут до события
- В точное время события
"""
    await message.answer(help_text)


@dp.message(Command("set_tz"))
async def cmd_set_tz(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Укажите офсет в формате +03:00")
        return
        
    minutes = parse_tz_offset_str(command.args.strip())
    if minutes is None:
        await message.answer("Неверный формат. Пример: +03:00 или -05:30")
        return
        
    await set_tz_offset_minutes(message.from_user.id, minutes)
    await message.answer(f"Часовой пояс установлен: UTC{command.args.strip()}")

@dp.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    tz_minutes = await get_tz_offset_minutes(user_id)
    args_text = (command.args or "").strip()

    if not args_text:
        await state.set_state(CreateReminder.waiting_for_datetime)
        await state.update_data(tz_minutes=tz_minutes)
        await message.answer("На когда напомнить? Например: 'завтра в 15:00'")
        return

    due_utc, task_text = extract_datetime_and_text(args_text, message.date.replace(tzinfo=timezone.utc), tz_minutes)
    
    if due_utc is None:
        await state.set_state(CreateReminder.waiting_for_datetime)
        await state.update_data(partial_text=args_text, tz_minutes=tz_minutes)
        await message.answer("Не удалось распознать дату. На когда напомнить?")
        return
        
    if due_utc <= datetime.now(timezone.utc):
        await message.answer("Укажите будущее время")
        return
        
    if not task_text:
        await state.set_state(CreateReminder.waiting_for_text)
        await state.update_data(due_utc=due_utc.isoformat(), tz_minutes=tz_minutes)
        await message.answer("О чём напомнить?")
        return
        
    rid = await insert_reminder(user_id, task_text, due_utc)
    await schedule_reminder_jobs(rid, user_id, task_text, due_utc, tz_minutes)
    when = format_dt_for_user(due_utc, tz_minutes)
    await message.answer(f"✅ Напоминание создано:\n• {task_text}\n• Когда: {when}")

@dp.message(CreateReminder.waiting_for_datetime)
async def cr_get_datetime(message: Message, state: FSMContext):
    data = await state.get_data()
    tz_minutes = data.get("tz_minutes", 0)
    
    due_utc, _ = extract_datetime_and_text(message.text, message.date.replace(tzinfo=timezone.utc), tz_minutes)
    
    if due_utc is None or due_utc <= datetime.now(timezone.utc):
        await message.answer("Неверная дата или время. Попробуйте снова:")
        return
        
    await state.update_data(due_utc=due_utc.isoformat())
    await state.set_state(CreateReminder.waiting_for_text)
    await message.answer("О чём напомнить?")

@dp.message(CreateReminder.waiting_for_text)
async def cr_get_text(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    tz_minutes = data.get("tz_minutes", 0)
    due_utc = datetime.fromisoformat(data["due_utc"]).astimezone(timezone.utc)
    text = message.text.strip()
    
    if not text:
        await message.answer("Текст не может быть пустым")
        return
        
    rid = await insert_reminder(user_id, text, due_utc)
    await schedule_reminder_jobs(rid, user_id, text, due_utc, tz_minutes)
    when = format_dt_for_user(due_utc, tz_minutes)
    await state.clear()
    await message.answer(f"✅ Напоминание создано:\n• {text}\n• Когда: {when}")

@dp.message(Command("my_reminders"))
async def cmd_my_reminders(message: Message):
    user_id = message.from_user.id
    tz_minutes = await get_tz_offset_minutes(user_id)
    rows = await get_user_active_reminders(user_id)
    
    if not rows:
        await message.answer("Нет активных напоминаний")
        return

    parts = ["Ваши напоминания:"]
    for row in rows:
        rid, text, due_at_utc_str = row
        due_at_utc = datetime.fromisoformat(due_at_utc_str).astimezone(timezone.utc)
        when = format_dt_for_user(due_at_utc, tz_minutes)
        parts.append(f"ID {rid}: {when} - {text}")

    await message.answer("\n".join(parts))
    
    for row in rows:
        rid, _, _ = row
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"del:{rid}"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{rid}")
        ]])
        await message.answer(f"Управление напоминанием ID {rid}", reply_markup=kb)

@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_confirm(call: CallbackQuery):
    rid = int(call.data.split(":")[1])
    r = await get_reminder(rid)
    
    if not r or r[4] != 'active' or r[1] != call.from_user.id:
        await call.answer("Не найдено")
        return
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"del_yes:{rid}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="noop")
    ]])
    await call.message.answer(f"Удалить напоминание: \"{r[2]}\"?", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer("Отменено")

@dp.callback_query(F.data.startswith("del_yes:"))
async def cb_delete_yes(call: CallbackQuery):
    rid = int(call.data.split(":")[1])
    r = await get_reminder(rid)
    
    if not r or r[4] != 'active' or r[1] != call.from_user.id:
        await call.answer("Не найдено")
        return
        
    await delete_reminder(rid)
    await remove_jobs_for(rid)
    await call.message.answer("🗑 Напоминание удалено")
    await call.answer()

# ==================== ЗАПУСК БОТА ====================
async def main():
    if not TOKEN:
        logger.error("Токен бота не установлен")
        return
        
    try:
        logger.info("Запуск бота...")
        bot = Bot(TOKEN)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")

if __name__ == "__main__":
    asyncio.run(main())