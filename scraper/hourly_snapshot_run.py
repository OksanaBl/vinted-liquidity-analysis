import os
from datetime import datetime, timezone

import psycopg
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

from vinted_scraper import (
    accept_cookies,
    get_game_ids_from_page,
    save_snapshots_to_db,
    save_run_log
)

load_dotenv()
connection_string = os.getenv("DATABASE_URL")

start_time = datetime.now(timezone.utc)
# Подключение к БД
conn = psycopg.connect(connection_string)
# Запуск и настройка браузера
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context()
    # передача в cookies данных для англоязычной локали для унификации данных из описательных полей
    # (состояние, категория, время публикации, стоимость доставки, рейтинг продавца)
    context.add_cookies([{
        "domain": "www.vinted.lt",
        "name": "anonymous-iso-locale",
        "value": "en-LT",
        "path": "/"
    }])
    page = context.new_page()
    page.goto("https://www.vinted.lt/catalog/4881-board-games?referrer=item-crumbs")
    # ждём, пока страница каталога полностью прогрузит JS-контент
    page.wait_for_timeout(3000)
    print(page.title())
    print(page.url)

    modal_layer = page.locator('[data-testid="domain-select-modal--overlay"]')
    if modal_layer.count() > 0:
        print(modal_layer.inner_text())

    accept_cookies(page)
    game_ids = get_game_ids_from_page(page)
    processed_game_count = len(game_ids)
    save_snapshots_to_db(conn, game_ids)
    browser.close()

end_time = datetime.now(timezone.utc)
log_parameters = {
    "run_type": "hourly_snapshot",
    "start_time": start_time,
    "end_time": end_time,
    "processed_game_count": processed_game_count
}
# Записываем только успешные запуски, так как в дальнейшем для выбора новых game_id используем дату последнего успешного запуска discover_new_items_run
# Ошибки запуска логируются в Github Actions
save_run_log(conn, log_parameters)
conn.close()