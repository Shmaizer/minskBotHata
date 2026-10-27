import unittest
from bs4 import BeautifulSoup
from sources import (extract_price, extract_rooms, extract_area, parse_domovita, parse_kufar, parse_onliner, onliner_public_url, parse_onliner_api_payload)


class ParserTests(unittest.TestCase):
    def test_price_is_monthly_byn_only(self):
        self.assertEqual(extract_price("1 050 р./мес."), 1050)
        self.assertEqual(extract_price("992 р. / мес."), 992)
        self.assertEqual(extract_price("450 $/мес."), None)

    def test_room_count_patterns(self):
        self.assertEqual(extract_rooms("2-комнатная квартира"), 2)
        self.assertEqual(extract_rooms("Аренда: 1-к квартира 38 м²"), 1)
        self.assertEqual(extract_rooms("3-комн. 50 м²"), 3)

    def test_domovita_public_card(self):
        html = '''<html><body><div class="card">
        <a href="/minsk/flats/rent/2-komnatnaa-kvartira-test">2-комнатная квартира, Минск, ул. Тестовая, д. 1</a>
        <div>1 050 р./мес. 54 / 31 / 7м2</div><img src="/photo.jpg"><div class="published">27.10.2026</div>
        </div></body></html>'''
        items = parse_domovita(BeautifulSoup(html, "html.parser"), "https://domovita.by/minsk/flats/rent")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 1050)
        self.assertEqual(items[0].rooms, 2)
        self.assertTrue(items[0].image_url.endswith("/photo.jpg"))
        self.assertIsNotNone(items[0].published_at)
        self.assertEqual(items[0].published_at.date().isoformat(), "2026-10-27")

    def test_domovita_date_in_sibling_footer_is_not_lost(self):
        html = """<html><body><div class="listing-card">
        <div class="listing-main">
          <a href="/minsk/flats/rent/test-flat-987">1-комнатная квартира, Минск, тестовая улица</a>
          <div class="price-and-features">1 050 р./мес. 38 / 20 / 8 м2</div>
        </div>
        <div class="listing-footer"><span>26.10.2026</span></div>
        </div></body></html>"""
        items = parse_domovita(BeautifulSoup(html, "html.parser"), "https://domovita.by/minsk/flats/rent")
        self.assertEqual(len(items), 1)
        self.assertIsNotNone(items[0].published_at)
        self.assertEqual(items[0].published_at.date().isoformat(), "2026-10-26")

    def test_kufar_public_card(self):
        html = '''<html><body><div class="card">
        <a href="/vi/minsk/snyat/kvartiru/123456">Аренда: 1-к квартира 38 м², Минск</a>
        <div>992 р. / мес. 1-к квартира 38 м²</div>
        <time datetime="2026-10-27T08:15:00+03:00">сегодня</time>
        </div></body></html>'''
        items = parse_kufar(BeautifulSoup(html, "html.parser"), "https://re.kufar.by/l/minsk/snyat/kvartiru/1k")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 992)
        self.assertEqual(items[0].rooms, 1)
        self.assertIsNotNone(items[0].published_at)
        self.assertEqual(items[0].published_at.hour, 8)

    def test_kufar_card_anchor_and_tracking_query(self):
        html = '''<html><body><div class="card">
        <a href="/vi/minsk/snyat/kvartiru/v-novostrojke/123456?rank=7&amp;searchId=demo">
        1 031 р./мес. 1-комн.29 м²3/5 эт. Артиллеристов ул., 11, Минск Ковальская Слобода Сдается квартира
        </a></div></body></html>'''
        items = parse_kufar(BeautifulSoup(html, "html.parser"), "https://re.kufar.by/l/minsk/snyat/kvartiru/1k")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 1031)
        self.assertEqual(items[0].rooms, 1)
        self.assertNotIn("searchId", items[0].url)

    def test_onliner_public_card(self):
        html = '''<html><body><div class="card">
        <a href="/ak/flat/123456">1-комнатная квартира Минск, тест</a>
        <div>900 Br / мес. 1-комнатная квартира</div>
        <time datetime="2026-10-27T07:45:00+03:00">сегодня</time>
        </div></body></html>'''
        items = parse_onliner(BeautifulSoup(html, "html.parser"), "https://r.onliner.by/ak/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 900)
        self.assertEqual(items[0].rooms, 1)
        self.assertIsNotNone(items[0].published_at)

    def test_area_uses_total_not_kitchen_area(self):
        self.assertEqual(extract_area("54 / 31 / 7 м²"), "54 м²")
        self.assertEqual(extract_area("38 м2"), "38 м²")

    def test_onliner_card_metadata(self):
        html = """<html><body><article class="apartment-card">
        <a class="apartment-card__title" href="/ak/flat/246810">Квартира в аренду</a>
        <div class="apartment-card__location">ул. Ленина, 5</div>
        <div class="apartment-card__metro">Площадь Победы, 7 мин пешком</div>
        <div class="price">950 Br / мес.</div>
        <div class="features">1-комнатная квартира 54 / 31 / 7 м²</div>
        </article></body></html>"""
        items = parse_onliner(BeautifulSoup(html, "html.parser"), "https://r.onliner.by/ak/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, 950)
        self.assertEqual(items[0].rooms, 1)
        self.assertEqual(items[0].location, "ул. Ленина, 5")
        self.assertIn("Площадь Победы", items[0].metro)
        self.assertEqual(items[0].area, "54 м²")
        self.assertEqual(items[0].url, "https://r.onliner.by/ak/flat/246810")

    def test_onliner_api_payload_maps_fields(self):
        payload = {
            "total": 1,
            "page": {"last": 1},
            "apartments": [{
                "id": 765432,
                "url": "https://ak.onliner.by/apartments/765432",
                "rent_type": "2_rooms",
                "price": {"amount": "800.00", "currency": "USD", "converted": {"BYN": {"amount": "1090.00", "currency": "BYN"}}},
                "location": {
                    "address": "Беларусь, Минск, ул. Ленина, 5",
                    "user_address": "ул. Ленина, 5",
                    "latitude": 53.9000,
                    "longitude": 27.6000,
                },
                "photo": "https://content.onliner.by/apartment/photo.jpg",
                "created_at": "2026-10-27T08:30:00+03:00",
                "area": {"total": 54, "living": 31, "kitchen": 7},
                "metro": [{"name": "Площадь Победы"}],
            }]
        }
        items = parse_onliner_api_payload(payload)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.source, "Onliner")
        self.assertEqual(item.price, 1090)
        self.assertEqual(item.rooms, 2)
        self.assertEqual(item.location, "ул. Ленина, 5")
        self.assertEqual(item.area, "54 м²")
        self.assertEqual(item.metro, "Площадь Победы")
        self.assertTrue(item.image_url.endswith("photo.jpg"))
        self.assertIn("apartments/765432", item.url)

    def test_onliner_api_drops_out_of_bounds_listing(self):
        payload = {"apartments": [{
            "id": 765433,
            "url": "https://ak.onliner.by/apartments/765433",
            "rent_type": "1_room",
            "price": {"amount": "500", "currency": "BYN"},
            "location": {"address": "Беларусь, Гомель", "latitude": 52.4, "longitude": 30.9},
        }]}
        self.assertEqual(parse_onliner_api_payload(payload), [])

    def test_onliner_url_removes_robots_disallowed_filter_params(self):
        url = onliner_public_url(
            "https://r.onliner.by/ak/?currency=BYN&price%5Bmin%5D=50&price%5Bmax%5D=1100"
            "&rent_type%5B%5D=1_room&rent_type%5B%5D=2_rooms#bounds%5Blb%5D%5Blat%5D=53.7"
        )
        self.assertIn("rent_type%5B%5D=1_room", url)
        self.assertIn("rent_type%5B%5D=2_rooms", url)
        self.assertNotIn("price", url.lower())
        self.assertNotIn("bounds", url.lower())
        self.assertNotIn("#", url)


if __name__ == "__main__":
    unittest.main()
