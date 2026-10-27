import asyncio
import logging
import os
from datetime import timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from db import init_db, seen, mark_seen, get_stats
from sources import collect_all
from filters import matches
from date_utils import now_minsk, published_since_yesterday
from cards import card_text

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MAX_PRICE = int(os.getenv("MAX_PRICE_BYN", "1100"))
ROOMS = {int(x.strip()) for x in os.getenv("ROOMS", "1,2").split(",") if x.strip().isdigit()}
CITY = os.getenv("CITY", "Минск")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rent-bot")
dp = Dispatcher()
bot = None
paused = False
scan_lock = asyncio.Lock()
last_scan_stats = {
    "checked": 0,
    "with_date": 0,
    "without_date": 0,
    "not_today": 0,
    "matching": 0,
    "sent": 0,
}



async def scan(trigger="scheduled"):
    """Scan public search pages, sending only listings published since yesterday 00:00 Minsk time."""
    global paused, last_scan_stats
    if paused or scan_lock.locked():
        return
    if not CHAT_ID:
        log.info("TELEGRAM_CHAT_ID не задан: отправка выключена. Добавьте бота в целевую группу, вызовите /chatid, укажите ID в .env и перезапустите.")
        return
    async with scan_lock:
        scan_now = now_minsk()
        cutoff_date = (scan_now - timedelta(days=1)).date()
        stats = {
            "checked": 0, "with_date": 0, "without_date": 0, "not_today": 0,
            "within_window": 0, "missing_price": 0, "price_too_high": 0,
            "missing_rooms": 0, "rooms_outside": 0, "other_filter": 0,
            "matching": 0, "sent": 0,
        }
        source_stats = {}
        try:
            items = await collect_all(backfill=(trigger == "startup"))
            stats["checked"] = len(items)
            log.info("Проверка (%s): получено карточек-кандидатов %d; дата отсечения — %s 00:00 по Минску",
                     trigger, len(items), cutoff_date.strftime("%d.%m.%Y"))
            for item in items:
                source_stat = source_stats.setdefault(item.source, {"cards": 0, "without_date": 0, "date_ok": 0, "within_window": 0})
                source_stat["cards"] += 1
                # Kufar and Onliner: publication date is intentionally not required.
                # Domovita: keep the strict yesterday-00:00 Minsk freshness filter.
                if item.source == "Domovita":
                    if item.published_at is None:
                        stats["without_date"] += 1
                        source_stat["without_date"] += 1
                        continue
                    stats["with_date"] += 1
                    source_stat["date_ok"] += 1
                    if not published_since_yesterday(item.published_at, scan_now):
                        stats["not_today"] += 1
                        continue
                    stats["within_window"] += 1
                    source_stat["within_window"] += 1
                else:
                    # No date check for Kufar or Onliner, as requested.
                    if item.published_at is not None:
                        stats["with_date"] += 1
                        source_stat["date_ok"] += 1
                    else:
                        stats["without_date"] += 1
                        source_stat["without_date"] += 1
                    stats["within_window"] += 1
                    source_stat["within_window"] += 1

                # Log precisely why a date-qualified listing did not pass the user filters.
                if item.price is None or item.price <= 0:
                    stats["missing_price"] += 1
                    continue
                if item.price > MAX_PRICE:
                    stats["price_too_high"] += 1
                    continue
                if item.rooms is None:
                    stats["missing_rooms"] += 1
                    continue
                if item.rooms not in ROOMS:
                    stats["rooms_outside"] += 1
                    continue
                if not matches(item, CITY, MAX_PRICE, ROOMS):
                    stats["other_filter"] += 1
                    continue
                stats["matching"] += 1
                if await seen(item.url):
                    continue
                try:
                    if item.image_url:
                        try:
                            await bot.send_photo(
                                chat_id=CHAT_ID,
                                photo=item.image_url,
                                caption=card_text(item),
                                parse_mode="HTML",
                            )
                        except Exception:
                            # Some sites deny hotlinking; preserve the listing card without the image.
                            log.info("Не удалось приложить фото, отправляю текстовую карточку: %s", item.url)
                            await bot.send_message(
                                chat_id=CHAT_ID, text=card_text(item), parse_mode="HTML",
                                disable_web_page_preview=False,
                            )
                    else:
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=card_text(item),
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                        )
                    # Save only after successful Telegram delivery. Failed sends can retry next cycle.
                    await mark_seen(item.url, item.source, item.title)
                    stats["sent"] += 1
                except Exception:
                    log.exception("Не удалось отправить карточку: %s", item.url)
        except Exception:
            log.exception("Ошибка цикла проверки")
        finally:
            last_scan_stats = stats
            log.info(
                "Итог проверки: кандидатов=%(checked)s, без даты=%(without_date)s, не сегодня=%(not_today)s, "
                "в окне вчера+сегодня=%(within_window)s, без цены=%(missing_price)s, "
                "дороже лимита=%(price_too_high)s, без комнат=%(missing_rooms)s, "
                "комнаты не подходят=%(rooms_outside)s, прочие фильтры=%(other_filter)s, "
                "подходят по фильтрам=%(matching)s, отправлено=%(sent)s", stats
            )
            for source, source_stat in sorted(source_stats.items()):
                log.info(
                    "Диагностика дат [%s]: карточек=%s, без даты=%s, дата распознана=%s, в окне вчера+сегодня=%s",
                    source, source_stat["cards"], source_stat["without_date"],
                    source_stat["date_ok"], source_stat["within_window"]
                )


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Бот мониторинга аренды квартир в Минске.\n"
        "Kufar и Onliner: дата публикации не проверяется. Domovita: принимаются объявления со вчерашних 00:00 "
        "по времени Минска, если дата распознана.\n"
        "Onliner: бот пробует отдельный JSON-хост только после проверки его robots.txt; к запрещённому /sdapi/ не обращается.\n"
        "Фильтры: 1–2 комнаты, до 1100 BYN/месяц.\n\n"
        "/status — состояние\n/chatid — ID текущего чата\n/pause — приостановить\n/resume — продолжить\n/check — проверить сейчас"
    )


@dp.message(Command("chatid"))
async def chatid(message: Message):
    """Show the ID of the chat where this command was called (including groups)."""
    await message.answer(
        f"ID этого чата: <code>{message.chat.id}</code>\n"
        "Скопируйте это число в TELEGRAM_CHAT_ID файла .env, затем перезапустите бота.",
        parse_mode="HTML",
    )


@dp.message(Command("status"))
async def status(message: Message):
    count = await get_stats()
    s = last_scan_stats
    await message.answer(
        f"Состояние: {'пауза' if paused else 'работает'}\n"
        f"Интервал: {POLL_SECONDS} сек.\nМаксимальная цена: {MAX_PRICE} BYN\n"
        f"Комнаты: {', '.join(map(str, sorted(ROOMS)))}\n"
        f"Сохранено отправленных ссылок: {count}\n\n"
        f"Последняя проверка: карточек {s['checked']}; без даты {s['without_date']}; "
        f"старше окна {s['not_today']}; в окне вчера+сегодня {s.get('within_window', 0)}; "
        f"без цены {s.get('missing_price', 0)}; дороже лимита {s.get('price_too_high', 0)}; "
        f"без комнат {s.get('missing_rooms', 0)}; комнаты не подходят {s.get('rooms_outside', 0)}; "
        f"подходят фильтрам {s['matching']}; отправлено {s['sent']}."
    )


@dp.message(Command("pause"))
async def pause(message: Message):
    global paused
    paused = True
    await message.answer("Проверка приостановлена. Для продолжения: /resume")


@dp.message(Command("resume"))
async def resume(message: Message):
    global paused
    paused = False
    await message.answer("Проверка возобновлена.")
    asyncio.create_task(scan(trigger="resume"))


@dp.message(Command("check"))
async def check(message: Message):
    await message.answer("Проверяю источники: Kufar и Onliner без фильтра даты; Domovita — со вчерашних 00:00 по времени Минска.")
    asyncio.create_task(scan(trigger="manual"))


async def main():
    global bot
    if not TOKEN:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN в файле .env")
    if not CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID пуст: бот запустится для получения ID через /chatid, но отправка карточек отключена до перезапуска с заданным ID.")
    bot = Bot(TOKEN)
    await init_db()

    # Initial backfill: Kufar/Onliner ignore publication date; Domovita requires yesterday 00:00 Minsk time or later.
    # The SQLite URL index prevents duplicate deliveries when the process is restarted.
    await scan(trigger="startup")

    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    scheduler.add_job(scan, IntervalTrigger(seconds=POLL_SECONDS), max_instances=1, coalesce=True)
    scheduler.start()
    log.info("Бот запущен. Интервал %s сек.; Kufar и Onliner — без фильтра даты, Domovita — со вчерашних 00:00 по Минску. Страниц Domovita при старте: %s. Onliner API будет опрошен только если правила его robots.txt разрешают путь и параметры.", POLL_SECONDS, os.getenv("DOMOVITA_PAGES", "3"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
