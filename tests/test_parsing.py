"""Тесты парсера.

Две части. Первая гоняет парсер по дампам фазы 0 и сверяет с `expected.json`,
который заполнил человек по скриншотам — это единственная защита от ошибки,
которую не видно по поведению системы: парсер, стабильно берущий витринную
цену вместо цены на дату, проходит любые синтетические тесты.

Вторая — синтетика на случаи, которые в дампах не встретились или которые
вживую воспроизводить дорого.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from monitor.parsing import (
    ParsedPage,
    ParseStatus,
    normalize,
    parse_item_page,
    visible_text,
)

FIXTURES = Path(__file__).parent / 'fixtures'
EXPECTED_PATH = FIXTURES / 'expected.json'

TODAY = date(2026, 8, 25)


def load_expected():
    return json.loads(EXPECTED_PATH.read_text(encoding='utf-8'))['dumps']


EXPECTED = load_expected()
IDS = [entry['file'] for entry in EXPECTED]


@dataclass(frozen=True)
class RequestedInterval:
    """Период для парсера. Свой, а не monitor.services.Interval: парсер обязан
    оставаться чистым, и тесты не должны тянуть в него модели."""

    check_in: date
    nights: int


def interval_of(entry):
    raw = entry.get('requested_interval')
    if not raw:
        return None
    return RequestedInterval(
        check_in=date.fromisoformat(raw['check_in']), nights=raw['nights']
    )


def parse_dump(entry) -> ParsedPage:
    html = (FIXTURES / 'dumps' / entry['file']).read_text(
        encoding='utf-8', errors='replace'
    )
    return parse_item_page(
        html,
        today=date.fromisoformat(entry['collected_on']),
        interval=interval_of(entry),
    )


# ------------------------------------------------------------------ фикстуры


@pytest.mark.parametrize('entry', EXPECTED, ids=IDS)
def test_fixture_status(entry):
    """Исход разбора совпадает с тем, что человек увидел на скриншоте."""
    assert parse_dump(entry).status == entry['expected']['status']


@pytest.mark.parametrize('entry', EXPECTED, ids=IDS)
def test_fixture_listing_price(entry):
    """Витринная цена. Пропуск, если человек ещё не сверил — не тихий проход."""
    want = entry['expected']['listing_price']
    if want is None:
        pytest.skip(f'{entry["file"]}: listing_price в expected.json не заполнен')
    got = parse_dump(entry).listing_price
    assert got == (Decimal(str(want)) if want else None)


@pytest.mark.parametrize('entry', EXPECTED, ids=IDS)
def test_fixture_title(entry):
    want = entry['expected']['title']
    if want is None:
        pytest.skip(f'{entry["file"]}: title в expected.json не заполнен')
    assert parse_dump(entry).title == want


@pytest.mark.parametrize('entry', EXPECTED, ids=IDS)
def test_fixture_periods(entry):
    """Периоды: дата заезда, число ночей, сумма и цена за ночь — все четыре."""
    want = entry['expected']['periods']
    unfilled = [period for period in want if period['check_in'] is None]
    if want and unfilled:
        pytest.skip(f'{entry["file"]}: периоды в expected.json не заполнены')

    got = parse_dump(entry).periods
    assert len(got) == len(want), 'число найденных периодов не совпало'
    for period, expected in zip(got, want):
        assert period.check_in == date.fromisoformat(expected['check_in'])
        assert period.nights == expected['nights']
        assert period.total_price == Decimal(str(expected['total_price']))
        assert period.price_per_night == Decimal(str(expected['price_per_night']))


@pytest.mark.parametrize('entry', EXPECTED, ids=IDS)
def test_fixture_requested_period(entry):
    """Блок цены на запрошенный период — сверка с тем, что подтвердил человек.

    Здесь ловится главная ошибка этой фазы: датированная цена, взятая
    из состояния приложения. В состоянии на этом дампе 3 350, на странице 3 807.
    """
    want = entry['expected'].get('requested_period')
    if want is None:
        pytest.skip(f'{entry["file"]}: период адресом не запрашивали')
    if want['total_price'] is None:
        pytest.skip(f'{entry["file"]}: requested_period в expected.json не заполнен')

    got = parse_dump(entry).requested_period
    assert got is not None, 'блок цены на период не разобрался'
    assert got.total_price == Decimal(str(want['total_price']))
    assert got.price_per_night == Decimal(str(want['price_per_night']))
    if want['base_price'] is not None:
        assert got.base_price == Decimal(str(want['base_price']))
    if want['discount_label'] is not None:
        assert got.discount_label == want['discount_label']


@pytest.mark.parametrize('entry', EXPECTED, ids=IDS)
def test_listing_price_never_becomes_a_period(entry):
    """Витринное число не попадает в периоды ни на одном дампе.

    Главная ошибка проекта: витринная цена ниже реальной в 1,25–1,9 раза,
    а система с такой подменой выглядит полностью рабочей.
    """
    page = parse_dump(entry)
    if page.listing_price is None:
        pytest.skip('витринной цены на этом дампе нет')
    assert all(period.total_price != page.listing_price for period in page.periods)
    assert all(period.price_per_night != page.listing_price for period in page.periods)


def test_page_without_dates_block_yields_no_prices():
    """Ключевая фикстура: страница цела, витринная есть, карусели нет.

    Ровно тот случай, когда парсер обязан вернуть отсутствие датированной цены,
    а не подставить витринную.
    """
    entry = next(e for e in EXPECTED if e['file'].endswith('u1_http_ck.html'))
    page = parse_dump(entry)
    assert page.status == ParseStatus.NO_DATED_PRICES
    assert page.periods == []
    assert page.listing_price == Decimal('3800')
    assert page.error_note


def test_captcha_overlay_keeps_listing_price_and_loses_dates():
    entry = next(e for e in EXPECTED if e['expected']['status'] == 'captcha_overlay')
    page = parse_dump(entry)
    assert page.status == ParseStatus.CAPTCHA_OVERLAY
    assert page.listing_price is not None
    assert page.periods == []


def test_blocked_page_has_no_prices_at_all():
    entry = next(e for e in EXPECTED if e['expected']['status'] == 'blocked')
    page = parse_dump(entry)
    assert page.status == ParseStatus.BLOCKED
    assert page.listing_price is None
    assert page.periods == []


def test_all_money_is_decimal():
    """Ни одного float в результатах разбора — правило CLAUDE.md."""
    for entry in EXPECTED:
        page = parse_dump(entry)
        assert page.listing_price is None or isinstance(page.listing_price, Decimal)
        for period in page.periods:
            assert isinstance(period.total_price, Decimal)
            assert isinstance(period.price_per_night, Decimal)


# ----------------------------------------------------------------- синтетика


def build_page(*, buyer_item=None, body='', state_broken=False):
    """Минимальная страница объявления: состояние в экранированном литерале."""
    parts = ['<html><head><title>Тест</title></head><body>', body]
    if buyer_item is not None:
        state = {'loaderData': {'catalog-or-main-or-item': {'buyerItem': buyer_item}}}
        literal = json.dumps(json.dumps(state, ensure_ascii=False))
        if state_broken:
            literal = literal[:-1] + 'мусор"'
        parts.append(
            f'<script>window.__staticRouterHydrationData = JSON.parse({literal});</script>'
        )
    parts.append('</body></html>')
    return ''.join(parts)


def item_state(price=3350, *, ga_price=None, title='Квартира-студия, 25 м²', **flags):
    """Состояние объявления с ценой в обоих независимых источниках."""
    return {
        'priceString': f'{price:,}&nbsp;₽ за\xa0сутки'.replace(',', '\xa0'),
        'ga': [{'itemPrice': price if ga_price is None else ga_price}],
        'item': {
            'title': title,
            'price': price,
            'formattedPrice': {'value': price, 'formatedString': f'{price}&nbsp;₽'},
            'isActive': True,
            'shortTermRent': {'isShortTermRentCategory': True},
            **flags,
        },
    }


def card(total, label, data_id=None):
    """Карточка периода. `data-id` — точные даты в ISO, как их отдаёт площадка."""
    attr = f' data-id="{data_id}"' if data_id else ''
    return (
        f'<li{attr} role="option"><div>'
        f'<span>{total}&nbsp;₽</span><span>{label}</span></div></li>'
    )


def dates_block(pairs):
    """Карусель без `data-id` — запасной путь разбора, по русским подписям."""
    cards = ''.join(card(total, label) for total, label in pairs)
    return f'<ul data-marker="nearest-dates" role="listbox">{cards}</ul>'


def dates_block_with_ids(triples):
    """Карусель как на живой странице: у каждой карточки есть `data-id`."""
    cards = ''.join(card(total, label, data_id) for total, label, data_id in triples)
    return f'<ul data-marker="nearest-dates" role="listbox">{cards}</ul>'


def test_empty_page():
    page = parse_item_page('', today=TODAY)
    assert page.status == ParseStatus.FAILED
    assert page.error_note


def test_page_without_state():
    page = parse_item_page('<html><body>Ничего тут нет</body></html>', today=TODAY)
    assert page.status == ParseStatus.FAILED
    assert 'состояние' in page.error_note.lower()


def test_state_without_expected_keys():
    """Состояние есть, но buyerItem в нём нет — не падаем, а честно сообщаем."""
    literal = json.dumps(json.dumps({'loaderData': {'что-то': {'иное': 1}}}))
    html = f'<html><body><script>window.__staticRouterHydrationData = JSON.parse({literal});</script></body></html>'
    page = parse_item_page(html, today=TODAY)
    assert page.status == ParseStatus.FAILED


def test_broken_state_json_does_not_crash():
    page = parse_item_page(build_page(buyer_item=item_state(), state_broken=True),
                           today=TODAY)
    assert page.status in (ParseStatus.FAILED, ParseStatus.PRICE_UNRELIABLE)


def test_state_conflict_between_price_object_and_analytics():
    """Ценовой объект и аналитика разошлись — не берём ни одно число."""
    page = parse_item_page(
        build_page(buyer_item=item_state(3350, ga_price=4900),
                   body=dates_block([('5 670', '26—27 августа')])),
        today=TODAY,
    )
    assert page.status == ParseStatus.STATE_CONFLICT
    assert page.listing_price is None
    assert page.periods == []
    assert 'разошлись' in page.error_note


def test_price_without_analytics_still_parses():
    """Один источник — не повод отказываться: расхождения нет, значит нет и спора."""
    state = item_state()
    state.pop('ga')
    page = parse_item_page(
        build_page(buyer_item=state, body=dates_block([('5 670', '26—27 августа')])),
        today=TODAY,
    )
    assert page.status == ParseStatus.OK
    assert page.listing_price == Decimal('3350')


def test_closed_item():
    page = parse_item_page(
        build_page(buyer_item=item_state(isActive=False)), today=TODAY
    )
    assert page.status == ParseStatus.NOT_AVAILABLE


def test_archived_item():
    page = parse_item_page(
        build_page(buyer_item=item_state(isArchived=True)), today=TODAY
    )
    assert page.status == ParseStatus.NOT_AVAILABLE


def test_dates_block_present_but_unreadable():
    """Блок есть, подписи не разбираются — это поломка парсера, а не решение площадки."""
    body = (
        '<ul data-marker="nearest-dates">'
        '<li><span>5 670&nbsp;₽</span><span>с 26 по 27</span></li></ul>'
    )
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    assert page.status == ParseStatus.DATES_BLOCK_UNREADABLE
    assert page.periods == []
    assert page.listing_price == Decimal('3350')


def test_missing_dates_block_differs_from_unreadable_one():
    """Два разных исхода: причины разные и действия по ним разные."""
    without = parse_item_page(build_page(buyer_item=item_state()), today=TODAY)
    unreadable = parse_item_page(
        build_page(buyer_item=item_state(),
                   body='<ul data-marker="nearest-dates">пусто</ul>'),
        today=TODAY,
    )
    assert without.status == ParseStatus.NO_DATED_PRICES
    assert unreadable.status == ParseStatus.DATES_BLOCK_UNREADABLE
    assert without.status != unreadable.status


def test_partial_unreadable_labels_do_not_hide_good_periods():
    body = dates_block([('5 670', '26—27 августа'), ('4 000', 'нет подписи')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    assert page.status == ParseStatus.OK
    assert len(page.periods) == 1
    assert page.error_note, 'о неразобранной карточке парсер обязан сказать'


def test_multi_night_period_divides_into_price_per_night():
    body = dates_block([('11 452', '3—5 сентября')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    period = page.periods[0]
    assert period.check_in == date(2026, 9, 3)
    assert period.nights == 2
    assert period.total_price == Decimal('11452.00')
    assert period.price_per_night == Decimal('5726.00')
    assert period.check_out == date(2026, 9, 5)


def test_period_across_month_boundary():
    body = dates_block([('4 125', '31 авг—1 сент')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    period = page.periods[0]
    assert period.check_in == date(2026, 8, 31)
    assert period.nights == 1


def test_period_across_new_year():
    """«30 дек—2 янв» в конце декабря — заезд в этом году, выезд в следующем."""
    body = dates_block([('30 000', '30 дек—2 янв')])
    page = parse_item_page(
        build_page(buyer_item=item_state(), body=body), today=date(2026, 12, 20)
    )
    period = page.periods[0]
    assert period.check_in == date(2026, 12, 30)
    assert period.nights == 3
    assert period.check_out == date(2027, 1, 2)


def test_year_is_taken_from_collection_day_not_from_clock():
    """Один и тот же дамп, разные дни сбора — разные годы. Часы тут ни при чём."""
    body = dates_block([('5 670', '26—27 января')])
    first = parse_item_page(build_page(buyer_item=item_state(), body=body),
                            today=date(2026, 1, 20))
    second = parse_item_page(build_page(buyer_item=item_state(), body=body),
                             today=date(2026, 8, 25))
    assert first.periods[0].check_in == date(2026, 1, 26)
    assert second.periods[0].check_in == date(2027, 1, 26)


def test_visible_text_only_is_unreliable():
    """Числа из видимого текста в цену не идут: там залог, уборка и бельё."""
    html = '<html><body><div>Залог 5 000 ₽</div><div>Уборка 1 500 ₽</div></body></html>'
    page = parse_item_page(html, today=TODAY)
    assert page.status == ParseStatus.PRICE_UNRELIABLE
    assert page.listing_price is None
    assert page.periods == []
    assert '5000' in page.error_note


def test_blocked_stub_without_state():
    html = '<html><head><title>Доступ ограничен: проблема с IP</title></head><body>Доступ ограничен</body></html>'
    page = parse_item_page(html, today=TODAY)
    assert page.status == ParseStatus.BLOCKED


def test_captcha_overlay_needs_loaded_item():
    """Та же надпись, но объявление загрузилось — это другой исход и другое лечение."""
    page = parse_item_page(
        build_page(buyer_item=item_state(),
                   body='<div>Сервис недоступен. Попробуйте позже</div>'),
        today=TODAY,
    )
    assert page.status == ParseStatus.CAPTCHA_OVERLAY
    assert page.listing_price == Decimal('3350')


def test_ab_flags_in_state_do_not_look_like_a_block():
    """В состоянии сотни флагов со словом captcha — блокировкой это не является."""
    state = item_state()
    state['toggles'] = {'recaptcha_enabled': True, 'captcha_v3': 'доступ ограничен'}
    page = parse_item_page(
        build_page(buyer_item=state, body=dates_block([('5 670', '26—27 августа')])),
        today=TODAY,
    )
    assert page.status == ParseStatus.OK


def test_gone_marker_without_state():
    html = '<html><body>Объявление снято с публикации</body></html>'
    assert parse_item_page(html, today=TODAY).status == ParseStatus.NOT_AVAILABLE


# ------------------------------------------------------------- нормализация


def test_normalization_makes_escaped_and_plain_pages_equal():
    """Экранирование и &nbsp; не должны менять результат — на них разбор и ломался."""
    plain = build_page(buyer_item=item_state(),
                       body='<ul data-marker="nearest-dates"><li>'
                            '<span>5 670 ₽</span><span>26—27 августа</span></li></ul>')
    escaped = build_page(buyer_item=item_state(),
                         body='<ul data-marker="nearest-dates"><li>'
                              '<span>5&nbsp;670&nbsp;₽</span>'
                              '<span>26⁠—⁠27 августа</span></li></ul>')
    first = parse_item_page(plain, today=TODAY)
    second = parse_item_page(escaped, today=TODAY)
    assert first.status == second.status == ParseStatus.OK
    assert first.periods == second.periods


def test_normalize_strips_escaping_and_invisible_characters():
    assert normalize('\\"priceString\\"') == '"priceString"'
    assert normalize('5&nbsp;670') == '5 670'
    assert normalize('26⁠—⁠27') == '26—27'


def test_visible_text_drops_scripts():
    html = '<html><body><script>var x = "доступ ограничен";</script><p>Цена</p></body></html>'
    text = visible_text(html)
    assert 'Цена' in text
    assert 'доступ ограничен' not in text


def test_unmatched_card_is_counted_not_swallowed():
    """Карточка, подпись которой не подошла, обязана оставить след.

    Иначе смена вёрстки блока выглядит как «площадка показала меньше периодов»
    и парсер продолжает считаться исправным.
    """
    body = dates_block([('5 670', '26—27 августа'), ('4 000', 'на выходные')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    assert page.status == ParseStatus.OK
    assert len(page.periods) == 1
    assert 'Не разобраны' in page.error_note


def test_clean_fixtures_have_no_unparsed_cards():
    """На дампах фазы 0 разбирается всё: пустая жалоба — признак, что счёт сходится."""
    for entry in EXPECTED:
        if entry['expected']['status'] != 'ok':
            continue
        assert parse_dump(entry).error_note == '', entry['file']


def test_dates_come_from_data_id_when_present():
    """Точные даты из data-id надёжнее подписи: в подписи нет года."""
    body = dates_block_with_ids([('11 452', '3—5 сентября', '2026-09-03--2026-09-05')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    period = page.periods[0]
    assert period.check_in == date(2026, 9, 3)
    assert period.nights == 2
    assert period.source == 'nearest_dates:data-id'


def test_data_id_wins_over_a_far_away_collection_day():
    """Год берётся из разметки, а не из арифметики: день сбора на него не влияет."""
    body = dates_block_with_ids([('30 000', '30 дек—2 янв', '2026-12-30--2027-01-02')])
    page = parse_item_page(
        build_page(buyer_item=item_state(), body=body), today=date(2026, 3, 1)
    )
    period = page.periods[0]
    assert period.check_in == date(2026, 12, 30)
    assert period.check_out == date(2027, 1, 2)


def test_label_disagreeing_with_data_id_is_not_taken():
    """Расхождение подписи и data-id — не выбор одного из двух, а отказ от карточки."""
    body = dates_block_with_ids([('11 452', '3—5 сентября', '2026-10-11--2026-10-13')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    assert page.status == ParseStatus.DATES_BLOCK_UNREADABLE
    assert page.periods == []
    assert 'расходится' in page.error_note


def test_fixtures_use_data_id_as_the_source():
    """На настоящих дампах даты берутся из разметки карточки, а не из подписи."""
    for entry in EXPECTED:
        if entry['expected']['status'] != 'ok':
            continue
        page = parse_dump(entry)
        assert page.periods
        sources = {p.source for p in page.periods}
        # У дампа с запрошенным периодом одна карточка приходит из блока цены —
        # он и есть ответ площадки на наш вопрос.
        assert sources <= {'nearest_dates:data-id', 'str-price-info'}, entry['file']
        assert 'nearest_dates:data-id' in sources, entry['file']


def test_label_path_still_works_without_data_id():
    """Если площадка перестанет отдавать data-id, разбор не встанет насмерть."""
    body = dates_block([('5 670', '26—27 августа')])
    page = parse_item_page(build_page(buyer_item=item_state(), body=body), today=TODAY)
    assert page.periods[0].source == 'nearest_dates:label'
    assert page.periods[0].check_in == date(2026, 8, 26)


# ------------------------------------------- цена на запрошенный период


MANUAL = '7628611904_2026-09-20_2026-09-23_g2.html'
REQUESTED = RequestedInterval(check_in=date(2026, 9, 20), nights=3)


def manual_dump() -> str:
    return (FIXTURES / 'dumps' / MANUAL).read_text(encoding='utf-8', errors='replace')


def parse_manual(interval=REQUESTED):
    return parse_item_page(manual_dump(), today=date(2026, 8, 29), interval=interval)


def test_period_price_comes_from_the_price_block():
    page = parse_manual()
    period = page.requested_period
    assert period is not None
    assert period.source == 'str-price-info'
    assert period.total_price == Decimal('11421.00')
    assert period.price_per_night == Decimal('3807.00')
    assert period.check_in == date(2026, 9, 20)
    assert period.nights == 3


def test_discounted_total_is_taken_not_the_base_price():
    """В снимок идут те деньги, которые платит гость, а не цена до скидки."""
    period = parse_manual().requested_period
    assert period.total_price == Decimal('11421.00')
    assert period.base_price == Decimal('12690.00')
    assert period.discount_label == '−10%'


def test_state_price_is_stale_and_never_used_for_the_period():
    """Подтверждённый факт: витринная цена в состоянии не меняется при выборе дат.

    На этом дампе в состоянии 3 350, а в разметке 3 807. Датированную цену
    брать из состояния нельзя ни при каких условиях.
    """
    page = parse_manual()
    assert page.listing_price == Decimal('3350')
    assert all(p.price_per_night != Decimal('3350') for p in page.periods)
    assert all(p.total_price != Decimal('3350') for p in page.periods)
    assert page.requested_period.price_per_night == Decimal('3807.00')


def test_requested_period_replaces_the_carousel_card():
    """Карусель содержит тот же период; в снимок идёт один — из блока цены."""
    page = parse_manual()
    same = [p for p in page.periods
            if (p.check_in, p.nights) == (REQUESTED.check_in, REQUESTED.nights)]
    assert len(same) == 1
    assert same[0].source == 'str-price-info'


def test_carousel_rebuilds_under_the_requested_duration():
    """Наблюдение заказчика: после выбора дат все карточки стали по три ночи."""
    page = parse_manual()
    assert page.cards_found == 7
    assert {p.nights for p in page.periods} == {3}


def test_nbsp_between_digits_does_not_hide_the_price():
    """Разряды в блоке отбиты &nbsp; — на этом ломались ручные greps."""
    from monitor.parsing import _marker_text

    assert _marker_text(manual_dump(), 'str-price-info/final-price') == (
        '11 421 ₽ за весь период'
    )


def test_integrity_check_rejects_a_block_that_disagrees_with_itself():
    """Сумма / ночи против цены за сутки в той же разметке: не сошлось — ошибка."""
    broken = manual_dump().replace(
        'data-marker="item-view/item-price">3&nbsp;807',
        'data-marker="item-view/item-price">9&nbsp;999',
    )
    page = parse_item_page(broken, today=date(2026, 8, 29), interval=REQUESTED)
    assert page.status == ParseStatus.PRICE_CONFLICT
    assert page.periods == []
    assert page.requested_period is None
    assert 'не сходится' in page.error_note


def test_rounding_of_one_rouble_is_not_a_disagreement():
    """11 420 на три ночи это 3806,67, а на странице будет 3 807. Это округление.

    Рубль расхождения между делением и подписью — механика площадки, а не сбой.
    Карусель правится вместе с блоком: между двумя суммами деления нет, и там
    расхождение осталось бы настоящим.
    """
    page = parse_item_page(
        manual_dump()
        .replace('data-marker="str-price-info/final-price">11&nbsp;421',
                 'data-marker="str-price-info/final-price">11&nbsp;420')
        .replace('11&nbsp;421&nbsp;₽</span>', '11&nbsp;420&nbsp;₽</span>'),
        today=date(2026, 8, 29),
        interval=REQUESTED,
    )
    assert page.status == ParseStatus.OK
    assert page.requested_period.total_price == Decimal('11420.00')
    assert page.requested_period.price_per_night == Decimal('3806.67')


def test_price_block_disagreeing_with_the_carousel_is_rejected():
    """Два независимых источника про один период должны говорить одно."""
    broken = manual_dump().replace(
        'data-marker="str-price-info/final-price">11&nbsp;421',
        'data-marker="str-price-info/final-price">15&nbsp;000',
    ).replace(
        'data-marker="item-view/item-price">3&nbsp;807',
        'data-marker="item-view/item-price">5&nbsp;000',
    )
    page = parse_item_page(broken, today=date(2026, 8, 29), interval=REQUESTED)
    assert page.status == ParseStatus.PRICE_CONFLICT
    assert 'разошлись' in page.error_note


def test_without_interval_the_price_block_is_not_read():
    """Без запрошенного периода блок читать не по чему: ночей взять неоткуда."""
    page = parse_manual(interval=None)
    assert page.status == ParseStatus.OK
    assert page.requested_period is None
    assert all(p.source == 'nearest_dates:data-id' for p in page.periods)


def test_missing_price_block_is_reported_not_swallowed():
    """Период запросили, а блока нет — об этом надо сказать."""
    without = manual_dump().replace('str-price-info/final-price', 'str-price-info/gone')
    page = parse_item_page(without, today=date(2026, 8, 29), interval=REQUESTED)
    assert page.status == ParseStatus.OK  # карусель всё ещё даёт периоды
    assert page.requested_period is None
    assert 'Блока цены на запрошенный период' in page.error_note


def test_page_with_only_the_price_block_still_works():
    """Карусель под A/B-флагом и может исчезнуть; блок периода самодостаточен."""
    without_carousel = manual_dump().replace('data-marker="nearest-dates"',
                                             'data-marker="gone"')
    page = parse_item_page(without_carousel, today=date(2026, 8, 29),
                           interval=REQUESTED)
    assert page.status == ParseStatus.OK
    assert len(page.periods) == 1
    assert page.periods[0].source == 'str-price-info'
