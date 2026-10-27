"""Formatting of Telegram listing cards; kept separate for simple unit tests."""
from date_utils import now_minsk


def esc(value: str) -> str:
    """Escape plain text for Telegram HTML."""
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def card_text(item) -> str:
    price = f'{item.price:,}'.replace(",", " ") + " BYN/мес." if item.price else "Не распознана"
    rooms = str(item.rooms) if item.rooms else "Не указано"
    address = esc(item.location or "Не указан в доступной карточке")
    area = esc(item.area or "Не указана")
    lines = [
        "🏠 <b>Квартира в аренду</b>",
        f"<b>{esc(item.title or 'Квартира')}</b>",
        f"💰 <b>Цена:</b> {price}",
        f"🚪 <b>Комнаты:</b> {rooms}",
        f"📍 <b>Адрес:</b> {address}",
    ]
    if item.metro:
        lines.append(f"🚇 <b>Метро рядом:</b> {esc(item.metro)}")
    lines.append(f"📐 <b>Площадь:</b> {area}")
    lines.extend([
        f"🌐 <b>Источник:</b> {esc(item.source)}",
        f'🔗 <b>Ссылка:</b> <a href="{esc(item.url)}">Открыть объявление</a>',
        f"🕒 <b>Найдено ботом:</b> {now_minsk().strftime('%d.%m.%Y %H:%M')} (Минск)",
    ])
    return "\n".join(lines)
