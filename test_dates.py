import unittest
from datetime import datetime
from bs4 import BeautifulSoup
from date_utils import MINSK_TZ, parse_datetime_value, extract_published_at, published_since_yesterday, start_of_today

class PublicationDateTests(unittest.TestCase):
    def test_numeric_date(self):
        dt = parse_datetime_value("27.10.2026", datetime(2026, 10, 27, 12, 0, tzinfo=MINSK_TZ))
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 27)

    def test_russian_month_date(self):
        dt = parse_datetime_value("27 октября 2026 в 10:40", datetime(2026, 10, 27, 12, 0, tzinfo=MINSK_TZ))
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute), (2026, 10, 27, 10, 40))

    def test_today_and_yesterday_relative_labels(self):
        now = datetime(2026, 10, 27, 12, 30, tzinfo=MINSK_TZ)
        self.assertEqual(parse_datetime_value("сегодня", now).date(), now.date())
        self.assertEqual(parse_datetime_value("вчера", now).date().isoformat(), "2026-10-26")

    def test_card_uses_last_absolute_date_as_footer_date(self):
        now = datetime(2026, 10, 27, 12, 30, tzinfo=MINSK_TZ)
        html = '''<div class="card"><a>2-комнатная квартира в Минске</a>
        <p>Заехать можно с 30.10.2026, описание квартиры.</p><div>27.10.2026</div></div>'''
        soup = BeautifulSoup(html, "html.parser")
        dt = extract_published_at(soup.div, now)
        self.assertEqual(dt.date().isoformat(), "2026-10-27")

    def test_freshness_is_yesterday_and_today_in_minsk(self):
        now = datetime(2026, 10, 27, 0, 30, tzinfo=MINSK_TZ)
        self.assertTrue(published_since_yesterday(start_of_today(now), now))
        yesterday_midnight = datetime(2026, 10, 26, 0, 0, tzinfo=MINSK_TZ)
        yesterday_late = datetime(2026, 10, 26, 23, 59, tzinfo=MINSK_TZ)
        two_days_ago = datetime(2026, 10, 25, 23, 59, tzinfo=MINSK_TZ)
        self.assertTrue(published_since_yesterday(yesterday_midnight, now))
        self.assertTrue(published_since_yesterday(yesterday_late, now))
        self.assertFalse(published_since_yesterday(two_days_ago, now))
        self.assertFalse(published_since_yesterday(None, now))

if __name__ == "__main__":
    unittest.main()
