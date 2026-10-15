from datetime import datetime, timezone
import os

from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

from vinted_scraper import (
    accept_cookies,
    check_and_rewrite_game_properties,
    choose_country,
    extract_current_game_properties_from_page,
    get_current_game_properties_from_db,
    get_unsold_game_ids_from_db,
    save_run_log
)

load_dotenv()
connection_string = os.getenv("DATABASE_URL")

start_time = datetime.now(timezone.utc)
# Подключение к БД Postgres
conn = psycopg.connect(connection_string, row_factory=dict_row)
# Запуск скрипта
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

    choose_country(page)
    accept_cookies(page)
    unsold_game_ids = get_unsold_game_ids_from_db(conn)

    processed_game_count = 0
    for game_id in unsold_game_ids:
        try:
            game_properties, seller_properties = get_current_game_properties_from_db(conn, game_id)
        except Exception as e:
            conn.rollback()  # откатываем транзакцию, чтобы ошибка чтения одного товара не заблокировала следующие
            print(f"Внимание! Возникла ошибка получения свойств для {game_id}. Ошибка {e}")
            continue  # не прерываем проверку из-за недоступности одного game_id

        try:
            item_status, game_parameters_from_page, seller_parameters_from_page = extract_current_game_properties_from_page(page, game_id)
        except Exception as e:
            print(f"Внимание! Для {game_id} не удалось получить характеристики с сайта, ошибка парсинга {e}")
            continue  # не прерываем проверку из-за ошибки парсинга одного game_id

        try:
            check_and_rewrite_game_properties(
                conn,
                game_properties,
                seller_properties,
                item_status,
                game_parameters_from_page,
                seller_parameters_from_page
            )
            processed_game_count += 1
        except Exception as e:
            conn.rollback()  # откатываем транзакцию, чтобы ошибка сохранения одного товара не заблокировала следующие
            print(f"Внимание! Для {game_id} не удалось обновить данные в БД, ошибка {e}")

    browser.close()

end_time = datetime.now(timezone.utc)
log_parameters = {
    "run_type": "revisit",
    "start_time": start_time,
    "end_time": end_time,
    "processed_game_count": processed_game_count
}
# Записываем только успешные запуски, так как в дальнейшем для выбора новых game_id используем дату последнего успешного запуска discover_new_items_run
# Ошибки запуска логируются в Github Actions
save_run_log(conn, log_parameters)

conn.close()