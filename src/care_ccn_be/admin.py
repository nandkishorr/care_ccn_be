from django.contrib import admin, messages

from care_ccn_be import store
from care_ccn_be.models import BecknMessage, BecknTransaction, Delivery


class BecknMessageInline(admin.TabularInline):
    model = BecknMessage
    extra = 0
    can_delete = False
    fields = ("created_date", "direction", "action", "message_id", "delivery_status", "processing_status", "attempts")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BecknTransaction)
class BecknTransactionAdmin(ReadOnlyAdmin):
    list_display = ("transaction_id", "flow", "side", "status", "counterparty_id", "created_date")
    list_filter = ("flow", "side", "status")
    search_fields = ("transaction_id", "contract_id", "coordination_id")
    inlines = [BecknMessageInline]


@admin.register(BecknMessage)
class BecknMessageAdmin(ReadOnlyAdmin):
    list_display = (
        "message_id",
        "action",
        "direction",
        "delivery_status",
        "processing_status",
        "attempts",
        "created_date",
    )
    list_filter = ("direction", "action", "delivery_status", "processing_status")
    search_fields = ("message_id", "transaction__transaction_id")
    actions = ["redeliver"]

    @admin.action(description="Redeliver failed or nacked outbound messages")
    def redeliver(self, request, queryset):
        count = 0
        for msg in queryset.filter(direction="out", delivery_status__in=[Delivery.FAILED, Delivery.NACKED]):
            msg.delivery_status = Delivery.QUEUED
            msg.save(update_fields=["delivery_status", "modified_date"])
            store.enqueue_delivery(msg)
            count += 1
        self.message_user(request, f"Queued {count} message(s)", messages.SUCCESS)
