from django.contrib import admin

from .models import (
    Account,
    AccountingEvent,
    AccountingSettings,
    InventoryMovement,
    JournalEntry,
    JournalLine,
    Refund,
    RefundLine,
)
from .services import retry_accounting_event


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    fields = ("account", "description", "debit", "credit", "metadata")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "tenant", "account_type", "parent", "normal_balance", "currency", "is_active")
    list_filter = ("account_type", "normal_balance", "is_active", "currency")
    search_fields = ("code", "name", "parent__code", "parent__name", "tenant__slug", "tenant__name")


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("entry_number", "tenant", "entry_date", "status", "total_debits", "total_credits", "posted_at")
    list_filter = ("status", "entry_date")
    search_fields = ("entry_number", "memo", "source_model", "source_id", "tenant__slug")
    inlines = [JournalLineInline]
    readonly_fields = ("posted_at", "posted_by", "reversed_at", "reversed_by", "created_at", "updated_at")


@admin.register(JournalLine)
class JournalLineAdmin(admin.ModelAdmin):
    list_display = ("journal_entry", "account", "tenant", "debit", "credit")
    list_filter = ("account__account_type",)
    search_fields = ("journal_entry__entry_number", "account__code", "account__name", "description")


@admin.register(AccountingSettings)
class AccountingSettingsAdmin(admin.ModelAdmin):
    list_display = ("tenant", "base_currency", "fiscal_year_start_month", "lock_posted_entries", "require_balanced_entries")
    search_fields = ("tenant__slug", "tenant__name")


@admin.register(AccountingEvent)
class AccountingEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "tenant", "source_model", "source_id", "status", "processed_at")
    list_filter = ("event_type", "status")
    search_fields = ("event_type", "source_model", "source_id", "idempotency_key", "tenant__slug")
    actions = ["retry_failed_events"]

    @admin.action(description="Retry selected failed accounting events")
    def retry_failed_events(self, request, queryset):
        retried_count = 0
        skipped_count = 0

        for event in queryset:
            if event.status != AccountingEvent.Status.FAILED:
                skipped_count += 1
                continue
            retry_accounting_event(event=event)
            retried_count += 1

        self.message_user(
            request,
            f"Retried {retried_count} failed accounting event(s); skipped {skipped_count}.",
        )


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = (
        "movement_type",
        "tenant",
        "variant",
        "quantity",
        "unit_cost",
        "total_cost",
        "source_model",
        "source_id",
        "created_at",
    )
    list_filter = ("movement_type", "created_at")
    search_fields = (
        "variant__sku",
        "variant__product__title",
        "source_model",
        "source_id",
        "note",
        "tenant__slug",
    )
    readonly_fields = ("total_cost", "created_at", "updated_at")


class RefundLineInline(admin.TabularInline):
    model = RefundLine
    extra = 0
    fields = ("order_item", "quantity", "amount", "tax_amount", "return_to_stock", "metadata")


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "order", "payment", "status", "amount", "tax_amount", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order__slug", "payment__reference", "reason", "idempotency_key", "tenant__slug")
    readonly_fields = ("approved_at", "approved_by", "completed_at", "completed_by", "created_at", "updated_at")
    inlines = [RefundLineInline]


@admin.register(RefundLine)
class RefundLineAdmin(admin.ModelAdmin):
    list_display = ("refund", "order_item", "tenant", "quantity", "amount", "tax_amount", "return_to_stock")
    list_filter = ("return_to_stock",)
    search_fields = ("refund__idempotency_key", "order_item__variant_sku", "order_item__product_title")
