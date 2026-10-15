import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from psycopg.rows import dict_row


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
    new_game_ids = list(set(snapshot_game_ids) - set(existing_game_ids))
    return new_game_ids

def extract_game_properties_from_page(page, game_id):
    url = f"https://www.vinted.lt/items/{game_id}"
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    title = page.locator("h1").text_content()
    description = page.locator('div[itemprop="description"]').text_content()

    try:
        brand = page.locator('a[href^="/brand/"] [itemprop="name"]').text_content().strip()
    except PlaywrightTimeoutError:
        # Бренд может отсутствовать, в этой ситуации селектор не будет обнаружен
        brand = None

    photo_count = int(
        page.locator("#content > section > section > div > figure._item-photo-box-module-scss-module__KFJIwq__item-description.u-flexbox._item-photo-box-module-scss-module__KFJIwq__item-photo._item-photo-box-module-scss-module__KFJIwq__item-photo")
        .count()
    )
    condition = page.locator('div[itemprop="status"]').text_content()
    category = "Board games"

    try:
        likes_count = int(page.locator("#content > section > section > div > div > button > span.web_ui__Text__text.web_ui__Text__body.web_ui__Text__left").text_content().strip())
    except PlaywrightTimeoutError:
        # При отсутствии лайков, поля с количеством на странице отсутствует
        likes_count = 0

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
        else:
            print(f"Внимание! Обнаружена неизвестная стоимость доставки: {shipping_cost} для game_id {game_id}")
            shipping_cost = None

    currency_code = page.locator('[data-testid="item-price"]').text_content().strip()[0]
    if currency_code == "€":
        currency_code = "EUR"
    else:
        print(f"Внимание! Обнаружена неизвестная валюта: {currency_code} для game_id {game_id}")
        currency_code = None

    # Если seller_id нет на странице, то данные о товаре неполные и их
    # не записываем: поиск элемента падает с исключением, а раннер
    # ловит его в try/except по товару и пропускает только этот товар.
    # Псевдонимизация: в БД записываем хеш seller_id, а не сам id.
    seller_id = hashlib.sha256(page.locator('a:has([data-testid="profile-username"])').get_attribute("href").split("/")[-1].encode('utf-8')).hexdigest()

    try:
        seller_rating = Decimal(page.locator('div[aria-label^="Member rated"]').get_attribute("aria-label").split(" ")[2].strip())
    except PlaywrightTimeoutError:
        print(f"Внимание! Не найдено поле 'Рейтинг продавца' для game_id {game_id}")
        seller_rating = None

    # На проверенных страницах блок с числом отзывов есть всегда, в том
    # числе при 0 отзывов (отражается 0). Если блок исчезнет, поиск упадёт
    # и товар будет пропущен.
    review_count = int(page.locator("#sidebar > div._item-details-module-scss-module__ymptpG__item-page-sidebar-content > div:nth-child(3) > div > a > div.web_ui__Cell__content > div.web_ui__Cell__body > div > div.web_ui__Rating__label > span").text_content())

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
            shipping_cost, currency_code, seller_id
        )
        VALUES (
            %(game_id)s, %(title)s, %(description)s, %(brand)s, %(photo_count)s,
            %(condition)s, %(category)s, %(likes_count)s, %(listed_date)s, %(price)s,
            %(price_with_protection)s, %(shipping_cost)s, %(currency_code)s, %(seller_id)s
        )
    """, game_parameters)

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

    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    not_found_marker_count = page.locator('[data-testid="page-not-found"]').count()
    if not_found_marker_count > 0:
        item_status = {
            "game_id": game_id,
            "status": "removed",
            "sold_date": datetime.today().date()
        }
        print(f"Обнаружен удаленный товар {game_id}. В БД будет проставлен статус 'removed' и дата продажи будет установлена как текущая.")
        return item_status, None, None

    else:
        sold_badge = page.locator("#sidebar > div._item-details-module-scss-module__ymptpG__item-page-sidebar-content > div:nth-child(1) > div > div.web_ui__Cell__cell.web_ui__Cell__default.web_ui__Cell__success > div > div").count()
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
            else:
                print(f"Внимание! Обнаружена неизвестная стоимость доставки: {shipping_cost} для game_id {game_id}")
                shipping_cost = None

        try:
            likes_count = int(page.locator("#content > section > section > div > div > button > span.web_ui__Text__text.web_ui__Text__body.web_ui__Text__left").text_content().strip())
        except PlaywrightTimeoutError:
            # При отсутствии лайков, поля с количеством на странице отсутствует
            likes_count = 0

        # Если seller_id нет на странице, то данные о товаре неполные и их
        # не записываем: поиск элемента падает с исключением, а раннер
        # ловит его в try/except по товару и пропускает только этот товар.
        # Псевдонимизация: в БД записываем хеш seller_id, а не сам id.
        seller_id = hashlib.sha256(page.locator('a:has([data-testid="profile-username"])').get_attribute("href").split("/")[-1].encode('utf-8')).hexdigest()

        try:
            seller_rating = Decimal(page.locator('div[aria-label^="Member rated"]').get_attribute("aria-label").split(" ")[2].strip())
        except PlaywrightTimeoutError:
            print(f"Внимание! Не найдено поле 'Рейтинг продавца' для товара {game_id}")
            seller_rating = None

        # На проверенных страницах блок с числом отзывов есть всегда, в том
        # числе при 0 отзывов (отражается 0). Если блок исчезнет, поиск упадёт
        # и товар будет пропущен.
        review_count = int(page.locator("#sidebar > div._item-details-module-scss-module__ymptpG__item-page-sidebar-content > div:nth-child(3) > div > a > div.web_ui__Cell__content > div.web_ui__Cell__body > div > div.web_ui__Rating__label > span").text_content().strip())

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
            """, changed_game_parameters)

        if seller_properties != seller_parameters_from_page:
            changed_seller_parameters = seller_parameters_from_page | {"date": current_date}
            cur.execute("""
            INSERT INTO rating_changes (seller_id, date, seller_rating, review_count)
            VALUES (%(seller_id)s, %(date)s, %(seller_rating)s, %(review_count)s)
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