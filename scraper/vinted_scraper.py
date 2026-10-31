import hashlib
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from psycopg.rows import dict_row

REJECTED = "rejected"

def accept_cookies(page):
    policy_button = page.locator("#onetrust-accept-btn-handler")
    policy_button_count = policy_button.count()
    if policy_button_count > 0:
        policy_button.click()
        page.wait_for_timeout(5000)

# Закрывает окно выбора страны, если Vinted его показал.
# Окно появляется при нелитовском IP (например, на раннере Actions)
# и перекрывает баннер cookies. Если окна нет, ничего не делает.
def choose_country(page):
    modal_layer = page.locator('[data-testid="domain-select-modal--overlay"]')
    if modal_layer.count() > 0:
        modal_layer.get_by_text("Lietuva", exact=True).click()
        modal_layer.wait_for(state="hidden")

def get_game_ids_from_page(page):
    game_ids = set()
    next_button = page.locator('[data-testid="catalog-pagination--next-page"]')

    while True:
        item_links = page.locator('[data-testid*="--overlay-link"]').all()

        for link in item_links:
            game_id = link.get_attribute("href").split("/")[-1].split("-")[0]
            game_ids.add(game_id)

        if next_button.get_attribute("aria-disabled") != "false":
            break
        else:
            print(f"Страница обработана, найдено {len(game_ids)} game_id")
            next_button.click()
            page.wait_for_timeout(10000)
    return list(game_ids)

def save_snapshots_to_db(conn, game_ids):
    cur = conn.cursor()
    snapshot_datetime = datetime.now(timezone.utc)
    snapshot_parameters = [(game_id, snapshot_datetime) for game_id in game_ids]
    cur.executemany("""
        INSERT INTO game_id_snapshots (game_id, snapshot_datetime)
        VALUES (%s, %s) ON CONFLICT (game_id, snapshot_datetime) DO NOTHING
    """, snapshot_parameters)
    conn.commit()

def get_new_game_ids(conn):
    cur = conn.cursor()
    cur.execute("SELECT game_id FROM goods")
    existing_game_ids = [row["game_id"] for row in cur.fetchall()]
    cur.execute("""
        SELECT DISTINCT game_id
        FROM game_id_snapshots
        WHERE snapshot_datetime > COALESCE(
                (SELECT MAX(start_time) - INTERVAL '1 hour'
                FROM run_log
                WHERE run_type = 'discovery'),
                'epoch'
        )
    """)
    snapshot_game_ids = [row["game_id"] for row in cur.fetchall()]
    cur.execute("SELECT item_id FROM rejected_ids")
    rejected_item_ids = [row["item_id"] for row in cur.fetchall()]
    new_game_ids = list(set(snapshot_game_ids) - set(existing_game_ids) - set(rejected_item_ids))
    return new_game_ids

def extract_game_properties_from_page(page, game_id):
    url = f"https://www.vinted.lt/items/{game_id}"
    response = page.goto(url, wait_until="domcontentloaded")
    page_status = response.status
    breadcrumb_selector = '[data-testid="breadcrumb"]'
    not_found_page_selector = '[data-testid="page-not-found"]'
    start = time.perf_counter()
    try:
        page.locator(breadcrumb_selector).first.or_(page.locator(not_found_page_selector)).wait_for(timeout=15000)
        elapsed = time.perf_counter() - start
        if elapsed > 3:
            print(f'Загрузка страницы для товара {game_id} заняла {elapsed:.2f} сек.')
    except PlaywrightTimeoutError:
        page_title = page.title()
        print(f"Страница для товара {game_id} не загрузилась. Статус загрузки: {page_status}, заголовок страницы {page_title}")
        return None
    page.wait_for_timeout(2000)

    not_found_marker_count = page.locator(not_found_page_selector).count()
    if not_found_marker_count > 0:
        print(f"Товар {game_id} удалён. Его характеристики не извлекаем.")
        return None

    board_games_marker_count = page.locator(f'{breadcrumb_selector}[href^="/catalog/4881-board-games"]').count()
    if board_games_marker_count == 0:
        print(f"Внимание! Товар {game_id} не относится к категории Board games. Его характеристики не извлекаем.")
        return REJECTED

    title = page.locator('[data-testid="item-page-summary-plugin"] h1').text_content()
    description = page.locator('div[itemprop="description"]').text_content()

    brand_tag = page.locator('a[href^="/brand/"] [itemprop="name"]')
    brand_tag_count = brand_tag.count()
    if brand_tag_count == 1:
        brand = brand_tag.text_content().strip()
    elif brand_tag_count == 0:
        # Бренд может отсутствовать, в этой ситуации селектор не будет обнаружен
        brand = None
    else:
        print(f"Внимание! Для товара {game_id} найдено {brand_tag_count} тегов бренда.")
        brand = None

    photo_count = page.locator("[data-photoid]").count()
    if photo_count == 0:
        print(f"Внимание! Не найдено ни одного фото для game_id {game_id}")
        photo_count = None
    elif page.locator('[data-testid="item-photo-5--label"]').count() > 0:
        editional_photo_mark = int(page.locator('[data-testid="item-photo-5--label"]').text_content().strip().split(" ")[-1])
        photo_count += editional_photo_mark

    condition = page.locator('div[itemprop="status"]').text_content()
    category = "Board games"

    likes_button = page.locator('[data-testid="favourite-button"]')
    likes_button_count = likes_button.count()
    if likes_button_count != 1:
        print(f"Внимание! Найдено {likes_button_count} кнопок 'Лайки' для game_id {game_id}.")
        likes_count = None
    else:
        likes_counter = likes_button.locator("span.web_ui__Text__text")
        likes_counter_count = likes_counter.count()
        if likes_counter_count == 1:
            likes_count = int(likes_counter.text_content().strip())
        elif likes_counter_count == 0:
            likes_count = 0
        else:
            print(f"Внимание! Найдено {likes_counter_count} полей текст в блоке 'Лайки' для game_id {game_id}.")
            likes_count = None

    try:
        raw_listed_text = page.locator('div[itemprop="upload_date"]').text_content().strip()
        listed_text = raw_listed_text

        # обработка разных вариантов написания времени публликации объявление
        if listed_text.lower() == 'just now':
            listed_date = datetime.now().date()
        else:
            listed_text = listed_text.split(" ")
            try:
                listed_unit_ago = listed_text[-2]

                if listed_text[-3].isdigit():
                    listed_number_ago = int(listed_text[-3])
                else:
                    listed_number_ago = 1

                if listed_unit_ago.startswith('min'):
                    listed_date = (datetime.now() - timedelta(minutes=listed_number_ago)).date()
                elif listed_unit_ago.startswith('hour'):
                    listed_date = (datetime.now() - timedelta(hours=listed_number_ago)).date()
                elif listed_unit_ago.startswith('day'):
                    listed_date = (datetime.now() - timedelta(days=listed_number_ago)).date()
                elif listed_unit_ago.startswith('week'):
                    listed_date = (datetime.now() - timedelta(weeks=listed_number_ago)).date()
                elif listed_unit_ago.startswith('month'):
                    listed_date = (datetime.now() - timedelta(days=listed_number_ago)*30).date()
                elif listed_unit_ago.startswith('year'):
                    listed_date = (datetime.now() - timedelta(days=listed_number_ago)*365).date()
                else:
                    print(f"Внимание! Обнаружено новое написание времени публикации: {raw_listed_text}. Для товара {game_id} не установлена дата публикации.")
                    listed_date = None
            except IndexError:
                print(f"Внимание! Обнаружено новое написание времени публикации: {raw_listed_text}. Для товара {game_id} не установлена дата публикации.")
                listed_date = None
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Дата размещения' для game_id {game_id}")
        listed_date = None

    try:
        price = Decimal(page.locator('[data-testid="item-price"]').text_content().strip()[1:])
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Цена' для game_id {game_id}")
        price = None

    try:
        price_with_protection = Decimal(page.locator('[data-testid="item-sidebar-price-container"] [data-testid="total-combined-price"]').text_content().strip()[1:])
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Цена с защитой' для game_id {game_id}")
        price_with_protection = None

    try:
        shipping_cost = Decimal(page.locator('[data-testid="item-shipping-banner--suffix"]').text_content().strip().split(" ")[-1][1:])
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Стоимость доставки' для game_id {game_id}")
        shipping_cost = None
    except InvalidOperation:
        shipping_cost = page.locator('[data-testid="item-shipping-banner--suffix"]').text_content().strip()
        if shipping_cost.lower().find("free") != -1:
            shipping_cost = Decimal(0.0)
        elif page.locator('[data-testid="item-shipping-banner-title"]').text_content().lower().find("free") != -1:
            shipping_cost = Decimal(0.0)
        else:
            print(f"Внимание! Обнаружена неизвестная стоимость доставки: {shipping_cost} для game_id {game_id}")
            shipping_cost = None

    currency_code = page.locator('[data-testid="item-price"]').text_content().strip()[0]
    if currency_code == "€":
        currency_code = "EUR"
    else:
        print(f"Внимание! Обнаружена неизвестная валюта: {currency_code} для game_id {game_id}")
        currency_code = None

    seller_id_mark = page.locator('a:has([data-testid="profile-username"])')
    seller_id_mark_count = seller_id_mark.count()
    if seller_id_mark_count == 1:
        seller_id = hashlib.sha256(seller_id_mark.get_attribute("href").split("/")[-1].encode('utf-8')).hexdigest()
    else:
        raise ValueError(f"Ссылка на продавца не найдена или найдена не одна: {seller_id_mark_count} шт.")



    # На проверенных страницах блок с числом отзывов есть всегда, в том
    # числе при 0 отзывов (отражается 0). Если блок исчезнет, поиск упадёт
    # и товар будет пропущен.
    review_count = int(page.locator('a[href^="/member/"] .web_ui__Rating__label span').text_content())

    seller_rating_mark = page.locator('div[aria-label^="Member rated"]')
    if seller_rating_mark.count() == 1:
        seller_rating = Decimal(seller_rating_mark.get_attribute("aria-label").split(" ")[2].strip())
    elif seller_rating_mark.count() == 0 and review_count == 0:
        seller_rating = None
    else:
        print(f"""Внимание! Обнаружены проблемы с полем 'Рейтинг продавца' для game_id {game_id}
    - Найдено {seller_rating_mark.count()} блоков. Если больше 1 - изменилась структура страницы.
    - Рейтинг продавца не обнаружен, но при этом обнаружены отзывы ({review_count} шт.)""")
        seller_rating = None

    sold_badge = page.locator("#sidebar .web_ui__Cell__success", has_text="Sold").count()
    if sold_badge > 0:
        status = "sold"
        sold_date = datetime.today().date()
    else:
        status = 'active'
        sold_date = None

    game_parameters = {
        "game_id": game_id,
        "title": title,
        "description": description,
        "brand": brand,
        "photo_count": photo_count,
        "condition": condition,
        "category": category,
        "likes_count": likes_count,
        "listed_date": listed_date,
        "price": price,
        "price_with_protection": price_with_protection,
        "shipping_cost": shipping_cost,
        "currency_code": currency_code,
        "status": status,
        "sold_date": sold_date,
        "seller_id": seller_id,
        "seller_rating": seller_rating,
        "review_count": review_count
    }

    return game_parameters

def save_to_db(conn, game_parameters):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sellers (seller_id, seller_rating, review_count)
        VALUES (%(seller_id)s, %(seller_rating)s, %(review_count)s)
        ON CONFLICT (seller_id) DO NOTHING
    """, game_parameters)

    cur.execute("""
        INSERT INTO goods (
            game_id, title, description, brand, photo_count, condition,
            category, likes_count, listed_date, price, price_with_protection,
            shipping_cost, currency_code, status, sold_date, seller_id
        )
        VALUES (
            %(game_id)s, %(title)s, %(description)s, %(brand)s, %(photo_count)s,
            %(condition)s, %(category)s, %(likes_count)s, %(listed_date)s, %(price)s,
            %(price_with_protection)s, %(shipping_cost)s, %(currency_code)s, %(status)s, %(sold_date)s, %(seller_id)s
        )
    """, game_parameters)

    conn.commit()

def save_rejected_ids_to_db(conn, game_id):
    cur = conn.cursor()
    rejected_datetime = datetime.now(timezone.utc)
    item_data = {
        'item_id': game_id,
        'rejected_datetime': rejected_datetime
    }
    cur.execute("""
        INSERT INTO rejected_ids (item_id, rejected_datetime)
        VALUES (%(item_id)s, %(rejected_datetime)s)
        ON CONFLICT (item_id) DO NOTHING
    """, item_data)
    conn.commit()

def get_unsold_game_ids_from_db(conn):
    cur = conn.cursor()
    cur.execute("SELECT game_id FROM goods WHERE sold_date IS NULL")
    unsold_game_ids = [row["game_id"] for row in cur.fetchall()]

    return unsold_game_ids

def get_current_game_properties_from_db(conn, game_id):
    cur = conn.cursor()

    cur.execute("""
        SELECT game_id, price, price_with_protection, shipping_cost, likes_count
        FROM item_metrics_history
        WHERE game_id = %s
        ORDER BY date
        DESC LIMIT 1
    """, (game_id,))
    game_properties = cur.fetchone()

    # Если для товара ещё нет истории изменений — берём его
    # характеристики напрямую из таблицы goods
    if game_properties is None:
        cur.execute("""
            SELECT game_id, price, price_with_protection, shipping_cost, likes_count
            FROM goods
            WHERE game_id = %s
        """, (game_id,))
        game_properties = cur.fetchone()

    cur.execute("""
        SELECT seller_id, seller_rating, review_count
        FROM rating_changes
        WHERE seller_id = (
                SELECT seller_id
                FROM goods
                WHERE game_id = %s)
        ORDER BY date
        DESC LIMIT 1
    """, (game_id,))
    seller_properties = cur.fetchone()

    # Если для продавца ещё нет истории изменений рейтинга — берём его
    # текущий рейтинг напрямую из таблицы sellers
    if seller_properties is None:
        cur.execute("""
            SELECT seller_id, seller_rating, review_count
            FROM sellers
            WHERE seller_id = (
                    SELECT seller_id
                    FROM goods
                    WHERE game_id = %s)
        """, (game_id,))
        seller_properties = cur.fetchone()

    return game_properties, seller_properties

def extract_current_game_properties_from_page(page, game_id):
    url = f"https://www.vinted.lt/items/{game_id}"
    response = page.goto(url, wait_until="domcontentloaded")
    page_status = response.status
    breadcrumb_selector = '[data-testid="breadcrumb"]'
    not_found_page_selector = '[data-testid="page-not-found"]'
    start = time.perf_counter()
    try:
        page.locator(breadcrumb_selector).first.or_(page.locator(not_found_page_selector)).wait_for(timeout=15000)
        elapsed = time.perf_counter() - start
        if elapsed > 3:
            print(f'Загрузка страницы для товара {game_id} заняла {elapsed:.2f} сек.')
    except PlaywrightTimeoutError:
        page_title = page.title()
        raise ValueError(f"Страница для товара {game_id} не загрузилась за 15 секунд. Статус загрузки: {page_status}, заголовок страницы {page_title}")
    page.wait_for_timeout(2000)

    not_found_marker_count = page.locator(not_found_page_selector).count()
    if not_found_marker_count > 0:
        item_status = {
            "game_id": game_id,
            "status": "removed",
            "sold_date": datetime.today().date()
        }
        print(f"Обнаружен удаленный товар {game_id}. В БД будет проставлен статус 'removed' и дата продажи будет установлена как текущая.")
        return item_status, None, None
    else:
        sold_badge = page.locator("#sidebar .web_ui__Cell__success", has_text="Sold").count()
        if sold_badge > 0:
            item_status = {
                "game_id": game_id,
                "status": "sold",
                "sold_date": datetime.today().date()
            }
        else:
            item_status = {
                "game_id": game_id,
                "status": "active",
                "sold_date": None
            }

    # Если seller_id нет на странице, то данные о товаре неполные и их
    # не записываем: поиск элемента падает с исключением, а раннер
    # ловит его в try/except по товару и пропускает только этот товар.
    # Псевдонимизация: в БД записываем хеш seller_id, а не сам id.
    seller_id_mark = page.locator('a:has([data-testid="profile-username"])')
    seller_id_mark_count = seller_id_mark.count()
    if seller_id_mark_count == 1:
        seller_id = hashlib.sha256(seller_id_mark.get_attribute("href").split("/")[-1].encode('utf-8')).hexdigest()
    else:
        raise ValueError(f"Ссылка на продавца не найдена или найдена не одна: {seller_id_mark_count} шт.")

    try:
        price = Decimal(page.locator('[data-testid="item-price"]').text_content().strip()[1:])
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Цена' для game_id {game_id}")
        price = None

    try:
        price_with_protection = Decimal(page.locator('[data-testid="item-sidebar-price-container"] [data-testid="total-combined-price"]').text_content().strip()[1:])
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Цена с защитой' для game_id {game_id}")
        price_with_protection = None

    try:
        shipping_cost = Decimal(page.locator('[data-testid="item-shipping-banner--suffix"]').text_content().strip().split(" ")[-1][1:])
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Стоимость доставки' для game_id {game_id}")
        shipping_cost = None
    except InvalidOperation:
        shipping_cost = page.locator('[data-testid="item-shipping-banner--suffix"]').text_content().strip()
        if shipping_cost.lower().find("free") != -1:
            shipping_cost = Decimal(0.0)
        elif page.locator('[data-testid="item-shipping-banner-title"]').text_content().lower().find("free") != -1:
            shipping_cost = Decimal(0.0)
        else:
            print(f"Внимание! Обнаружена неизвестная стоимость доставки: {shipping_cost} для game_id {game_id}")
            shipping_cost = None

    likes_button = page.locator('[data-testid="favourite-button"]')
    likes_button_count = likes_button.count()
    if likes_button_count != 1:
        print(f"Внимание! Найдено {likes_button_count} кнопок 'Лайки' для game_id {game_id}.")
        likes_count = None
    else:
        likes_counter = likes_button.locator("span.web_ui__Text__text")
        likes_counter_count = likes_counter.count()
        if likes_counter_count == 1:
            likes_count = int(likes_counter.text_content().strip())
        elif likes_counter_count == 0:
            likes_count = 0
        else:
            print(f"Внимание! Найдено {likes_counter_count} полей текст в блоке 'Лайки' для game_id {game_id}.")
            likes_count = None

    # На проверенных страницах блок с числом отзывов есть всегда, в том
    # числе при 0 отзывов (отражается 0). Если блок исчезнет, поиск упадёт
    # и товар будет пропущен.
    review_count = int(page.locator('a[href^="/member/"] .web_ui__Rating__label span').text_content().strip())

    seller_rating_mark = page.locator('div[aria-label^="Member rated"]')
    if seller_rating_mark.count() == 1:
        seller_rating = Decimal(seller_rating_mark.get_attribute("aria-label").split(" ")[2].strip())
    elif seller_rating_mark.count() == 0 and review_count == 0:
        seller_rating = None
    else:
        print(f"""Внимание! Обнаружены проблемы с полем 'Рейтинг продавца' для game_id {game_id}
    - Найдено {seller_rating_mark.count()} блоков. Если больше 1 - изменилась структура страницы.
    - Рейтинг продавца не обнаружен, но при этом обнаружены отзывы ({review_count} шт.)""")
        seller_rating = None

    game_parameters_from_page = {
            'game_id': game_id,
            'likes_count': likes_count,
            'price': price,
            'price_with_protection': price_with_protection,
            'shipping_cost': shipping_cost
    }

    seller_parameters_from_page = {
            'seller_id': seller_id,
            'seller_rating': seller_rating,
            'review_count': review_count
        }

    return item_status, game_parameters_from_page, seller_parameters_from_page

def check_and_rewrite_game_properties(conn, game_properties, seller_properties, item_status, game_parameters_from_page, seller_parameters_from_page):
    cur = conn.cursor()
    current_date = datetime.today().date()

    if item_status["status"] != "active":
        cur.execute("UPDATE goods SET status = %(status)s, sold_date = %(sold_date)s WHERE game_id = %(game_id)s", item_status)

    if item_status["status"] != "removed":
        if game_properties != game_parameters_from_page:
            changed_game_parameters = game_parameters_from_page | {"date": current_date}
            cur.execute("""
            INSERT INTO item_metrics_history (game_id, date, price, price_with_protection, shipping_cost, likes_count)
            VALUES (%(game_id)s, %(date)s, %(price)s, %(price_with_protection)s, %(shipping_cost)s, %(likes_count)s)
            ON CONFLICT (game_id, date) DO UPDATE
            SET price = EXCLUDED.price,
                price_with_protection = EXCLUDED.price_with_protection,
                shipping_cost = EXCLUDED.shipping_cost,
                likes_count = EXCLUDED.likes_count
            """, changed_game_parameters)

        if seller_properties != seller_parameters_from_page:
            changed_seller_parameters = seller_parameters_from_page | {"date": current_date}
            cur.execute("""
            INSERT INTO rating_changes (seller_id, date, seller_rating, review_count)
            VALUES (%(seller_id)s, %(date)s, %(seller_rating)s, %(review_count)s)
            ON CONFLICT (seller_id, date) DO UPDATE
            SET seller_rating = EXCLUDED.seller_rating,
                review_count = EXCLUDED.review_count
            """, changed_seller_parameters)

    conn.commit()

# Журнал успешных запусков раннеров (discovery, revisit, hourly_snapshot).
# Зачем: (1) discovery берёт из game_id_snapshots только снимки новее
# MAX(start_time) последнего успешного discovery минус 1 час (запас
# на случай задержки записи и запуска по cron); (2) start_time и end_time дают
# длительность запусков для выбора времени cron.
# Пишем только успешные запуски: упавшие видны в истории GitHub Actions.
def save_run_log(conn, log_parameters):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO run_log (run_type, start_time, end_time, processed_game_count)
        VALUES (%(run_type)s, %(start_time)s, %(end_time)s, %(processed_game_count)s)
    """, log_parameters)
    conn.commit()