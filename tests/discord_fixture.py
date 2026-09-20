"""Resultados de crawl de ejemplo (usando make_diff real) para tests."""
import monitor

OLD = "\n".join([
    "イナズマイレブン 最新情報",
    "発売日は未定です",
    "公式サイトへようこそ",
    "お問い合わせ",
])
NEW = "\n".join([
    "イナズマイレブン 最新情報",
    "発売日は2026年12月です",
    "公式サイトへようこそ",
    "新キャラクター公開 🎉",
    "お問い合わせ",
])


def make_results():
    changed = monitor.PageChange(
        url="https://www.inazuma.jp/re/news/",
        title="ニュース",
        detail=monitor.make_diff(OLD, NEW),
        image_url="https://www.inazuma.jp/re/img/a.png",
    )
    new = monitor.PageChange(url="https://www.inazuma.jp/re/new/", title="新ページ",
                             detail=monitor._truncate(NEW))
    removed = monitor.PageChange(url="https://www.inazuma.jp/re/old/", title="旧ページ")
    r1 = monitor.CrawlResult(site_name="Inazuma Eleven RE (IERE)",
                             changed_pages=[changed], new_pages=[new],
                             removed_pages=[removed])
    r2 = monitor.CrawlResult(site_name="Sitio sin novedades")
    many = [monitor.PageChange(url=f"https://www.layton.jp/x/{i}", title=f"P{i}",
                               detail="texto") for i in range(20)]
    r3 = monitor.CrawlResult(site_name="Professor Layton", new_pages=many)
    return [r1, r2, r3]
