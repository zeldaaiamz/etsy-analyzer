import datetime as dt
from analyzer.review_ocr import parse_reviews, parse_total, extract

T = dt.date(2026, 10, 15)


def test_english_dates():
    txt = "Reviews for this item (1,234)\nSally M  Oct 13, 2026\nLove it!\nbob_k Sep 2 2026\nJohn Sept 30, 2026"
    assert [d for _, d in parse_reviews(txt, T)] == [dt.date(2026, 10, 13), dt.date(2026, 9, 2), dt.date(2026, 9, 30)]


def test_ocr_noise_and_invalid():
    txt = "Amy Oct l3, 2O26\nX Feb 30, 2026\nFuture Dec 1, 2026"
    assert [d for _, d in parse_reviews(txt, T)] == [dt.date(2026, 10, 13)]


def test_chinese_dates_and_total():
    txt = "此商品的评论 (56)\n小王 2026年10月13日\n李 2026 年 8 月 1 日"
    assert [d for _, d in parse_reviews(txt, T)] == [dt.date(2026, 10, 13), dt.date(2026, 8, 1)]
    assert parse_total(txt) == 56
    assert parse_total("1,234 reviews") == 1234 and parse_total("nothing") is None


def test_extract_dedupes_overlapping_screenshots():
    a = "Reviews (20)\nSally Oct 13, 2026\nTom Oct 13, 2026\nAnn Sep 1, 2026"
    b = "Ann Sep 1, 2026\nZed Aug 5, 2026"
    dates, total = extract([a, b], T)
    assert dates == [dt.date(2026, 10, 13)] * 2 + [dt.date(2026, 9, 1), dt.date(2026, 8, 5)]
    assert total == 20
