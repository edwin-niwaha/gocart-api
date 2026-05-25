from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AccountViewSet,
    AccountingEventViewSet,
    AccountingSettingsView,
    BalanceSheetReportView,
    CashFlowReportView,
    GeneralLedgerReportView,
    InventoryMovementViewSet,
    JournalEntryViewSet,
    ProfitAndLossReportView,
    RefundViewSet,
    SalesReportView,
    TrialBalanceReportView,
)

router = DefaultRouter()
router.register("accounts", AccountViewSet, basename="accounting-accounts")
router.register("journal-entries", JournalEntryViewSet, basename="accounting-journal-entries")
router.register("events", AccountingEventViewSet, basename="accounting-events")
router.register("inventory-movements", InventoryMovementViewSet, basename="accounting-inventory-movements")
router.register("refunds", RefundViewSet, basename="accounting-refunds")

urlpatterns = [
    path("settings/", AccountingSettingsView.as_view(), name="accounting-settings"),
    path("reports/general-ledger/", GeneralLedgerReportView.as_view(), name="accounting-report-general-ledger"),
    path("reports/trial-balance/", TrialBalanceReportView.as_view(), name="accounting-report-trial-balance"),
    path("reports/profit-and-loss/", ProfitAndLossReportView.as_view(), name="accounting-report-profit-and-loss"),
    path("reports/sales/", SalesReportView.as_view(), name="accounting-report-sales"),
    path("reports/balance-sheet/", BalanceSheetReportView.as_view(), name="accounting-report-balance-sheet"),
    path("reports/cash-flow/", CashFlowReportView.as_view(), name="accounting-report-cash-flow"),
    path("", include(router.urls)),
]
