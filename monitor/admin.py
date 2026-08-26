"""Штатная админка: справочники ведёт менеджер, снимки только для чтения."""

from django.contrib import admin
from django.contrib.auth.models import Group, User

from monitor.models import Apartment, Competitor, PriceSnapshot

admin.site.site_header = 'Цены на посуточную аренду'
admin.site.site_title = 'Цены на посуточную аренду'
admin.site.index_title = 'Справочники и снимки цен'

# Пользователей в проекте двое, заводятся они из командной строки, а ролей и прав
# по постановке не будет вовсе. В списке эти разделы только мешают.
admin.site.unregister(Group)
admin.site.unregister(User)


class CompetitorInline(admin.TabularInline):
    model = Competitor
    verbose_name = 'конкурента'
    verbose_name_plural = 'конкуренты'
    extra = 1
    fields = ('url', 'title', 'min_nights', 'is_active')
    # Удаление конкурента запрещено: снимки на него ссылаются с PROTECT,
    # и убирать его из работы надо флагом is_active.
    can_delete = False
    show_change_link = True


class NoDeleteMixin:
    """Мягкое удаление — единственный способ убрать запись из работы."""

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Apartment)
class ApartmentAdmin(NoDeleteMixin, admin.ModelAdmin):
    list_display = ('title', 'address', 'min_nights', 'is_active', 'competitors_count')
    list_filter = ('is_active',)
    search_fields = ('title', 'address')
    inlines = [CompetitorInline]
    actions = ['mark_inactive', 'mark_active']

    @admin.display(description='конкурентов')
    def competitors_count(self, obj):
        return obj.competitors.filter(is_active=True).count()

    @admin.action(description='Убрать из работы')
    def mark_inactive(self, request, queryset):
        queryset.update(is_active=False)

    @admin.action(description='Вернуть в работу')
    def mark_active(self, request, queryset):
        queryset.update(is_active=True)


@admin.register(Competitor)
class CompetitorAdmin(NoDeleteMixin, admin.ModelAdmin):
    list_display = ('__str__', 'apartment', 'min_nights', 'is_active', 'created_at')
    list_filter = ('apartment', 'is_active')
    search_fields = ('title', 'url')
    actions = ['mark_inactive', 'mark_active']

    @admin.action(description='Убрать из работы')
    def mark_inactive(self, request, queryset):
        queryset.update(is_active=False)

    @admin.action(description='Вернуть в работу')
    def mark_active(self, request, queryset):
        queryset.update(is_active=True)


@admin.register(PriceSnapshot)
class PriceSnapshotAdmin(admin.ModelAdmin):
    """Только чтение. Снимки неизменяемы, а смотреть на них надо — в том числе
    на витринные, которых нет на странице сравнения."""

    list_display = (
        'collected_at',
        'owner',
        'price_kind',
        'check_in',
        'nights',
        'price_per_night',
        'total_price',
        'status',
    )
    list_filter = ('price_kind', 'status', 'source', 'check_in')
    search_fields = ('competitor__title', 'competitor__url', 'apartment__title')
    date_hierarchy = 'collected_at'

    @admin.display(description='объект')
    def owner(self, obj):
        return obj.competitor or obj.apartment

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
