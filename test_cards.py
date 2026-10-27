import unittest
from cards import card_text
from models import Listing


class TelegramCardTests(unittest.TestCase):
    def test_card_contains_requested_fields(self):
        item = Listing(
            source="Onliner",
            url="https://r.onliner.by/ak/flat/246810",
            title="Квартира в аренду",
            price=950,
            rooms=1,
            location="ул. Ленина, 5",
            metro="Площадь Победы (7 мин пешком)",
            area="54 м²",
        )
        text = card_text(item)
        for label in ("Квартира в аренду", "Цена:", "Комнаты:", "Адрес:", "Метро рядом:", "Площадь:", "Источник:", "Ссылка:", "Найдено ботом:"):
            self.assertIn(label, text)
        self.assertIn("950 BYN/мес.", text)
        self.assertIn("54 м²", text)
        self.assertIn("https://r.onliner.by/ak/flat/246810", text)

    def test_card_shows_fallback_if_address_or_area_unknown(self):
        item = Listing(source="Kufar", url="https://re.kufar.by/example", title="Квартира", price=1000, rooms=2)
        text = card_text(item)
        self.assertIn("Не указан в доступной карточке", text)
        self.assertIn("Площадь:</b> Не указана", text)
        self.assertNotIn("Метро рядом", text)


if __name__ == "__main__":
    unittest.main()
