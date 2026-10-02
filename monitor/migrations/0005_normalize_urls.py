"""Привести уже заведённые ссылки к каноническому виду.

Ссылки копировались из адресной строки и тащат за собой `checkIn`, `checkOut`
и `guestsDetailed`. Без этой миграции одно и то же объявление остаётся в базе
дважды и даёт в таблице дубль.

Столкнувшиеся строки не удаляются: снимки на них ссылаются с PROTECT, а мягкое
удаление — принятый в проекте способ убрать запись из работы.

Из пары остаётся та, что **уже** записана канонически: тогда победителю не надо
менять ссылку, и уникальный констрейнт не встаёт поперёк. Если канонической нет
ни у одной — остаётся та, у которой больше собранной истории.

Оговорка, которую надо знать: у проигравшей строки снимки остаются при ней.
История одного объявления оказывается разложена по двум строкам, и старые снимки
в таблицу не попадут. Переносить их нельзя — снимки неизменяемы.
"""

from django.db import migrations

from monitor.validators import normalize_avito_url


def normalize(apps, schema_editor):
    Competitor = apps.get_model('monitor', 'Competitor')
    Apartment = apps.get_model('monitor', 'Apartment')

    for apartment in Apartment.objects.exclude(avito_url='').iterator():
        canonical = normalize_avito_url(apartment.avito_url)
        if canonical != apartment.avito_url:
            apartment.avito_url = canonical
            apartment.save(update_fields=['avito_url'])

    groups = {}
    for competitor in Competitor.objects.all():
        key = (competitor.apartment_id, normalize_avito_url(competitor.url))
        groups.setdefault(key, []).append(competitor)

    for (_, canonical), rows in groups.items():
        already_canonical = [row for row in rows if row.url == canonical]
        if already_canonical:
            winner = min(already_canonical, key=lambda row: row.pk)
        else:
            winner = max(rows, key=lambda row: (row.snapshots.count(), -row.pk))

        for row in rows:
            if row is winner:
                continue
            if row.is_active:
                row.is_active = False
                row.save(update_fields=['is_active'])
                print(
                    f'  дубль убран из работы: #{row.pk} {row.url}\n'
                    f'    то же объявление, что #{winner.pk}; '
                    f'снимки остаются при #{row.pk}'
                )

        if winner.url != canonical:
            winner.url = canonical
            winner.save(update_fields=['url'])


class Migration(migrations.Migration):

    dependencies = [
        ('monitor', '0004_remove_pricesnapshot_snapshot_competitor_idx_and_more'),
    ]

    # Обратной операции нет: чужие параметры запроса восстановить неоткуда,
    # да и восстанавливать их незачем — к объявлению они не относятся.
    operations = [migrations.RunPython(normalize, migrations.RunPython.noop)]
