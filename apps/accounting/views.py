from __future__ import annotations

from django.http import HttpResponse
from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.permissions import IsTenantManager

from .models import Account, AccountingEvent, AccountingSettings, InventoryMovement, JournalEntry, Refund
from .serializers import (
    AccountSerializer,
    AccountingEventSerializer,
    AccountingSettingsSerializer,
    InventoryMovementSerializer,
    JournalEntryReverseSerializer,
    JournalEntrySerializer,
    ReportAsOfDateSerializer,
    ReportDateRangeSerializer,
    RefundSerializer,
)
from .reports import balance_sheet, cash_flow, general_ledger, profit_and_loss, sales_report, trial_balance
from .services import (
    approve_refund,
    chart_of_accounts_template_csv,
    chart_of_accounts_template_xlsx,
    chart_of_accounts_pdf,
    complete_refund,
    export_chart_of_accounts_csv,
    export_chart_of_accounts_xlsx,
    import_chart_of_accounts,
    retry_accounting_event,
)


CHART_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def as_drf_validation_error(exc: DjangoValidationError) -> ValidationError:
    detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
    return ValidationError(detail)


class AccountViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsTenantManager]
    serializer_class = AccountSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["account_type", "normal_balance", "is_active", "parent"]
    search_fields = ["code", "name", "description", "parent__code", "parent__name"]
    ordering_fields = ["code", "name", "account_type", "created_at"]
    ordering = ["code", "name"]

    def get_queryset(self):
        return Account.objects.filter(tenant=self.request.tenant).select_related("parent")

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant)

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        export_format = request.query_params.get("format", "csv").strip().lower()
        accounts = self.filter_queryset(self.get_queryset())
        if export_format == "xlsx":
            response = HttpResponse(
                export_chart_of_accounts_xlsx(accounts=accounts),
                content_type=CHART_XLSX_CONTENT_TYPE,
            )
            response["Content-Disposition"] = 'attachment; filename="chart-of-accounts.xlsx"'
            return response
        if export_format != "csv":
            raise ValidationError({"format": "format must be csv or xlsx."})
        response = HttpResponse(
            export_chart_of_accounts_csv(accounts=accounts),
            content_type="text/csv",
        )
        response["Content-Disposition"] = 'attachment; filename="chart-of-accounts.csv"'
        return response

    @action(detail=False, methods=["get"], url_path="template")
    def template(self, request):
        template_format = request.query_params.get("format", "xlsx").strip().lower()
        if template_format == "xlsx":
            response = HttpResponse(
                chart_of_accounts_template_xlsx(),
                content_type=CHART_XLSX_CONTENT_TYPE,
            )
            response["Content-Disposition"] = 'attachment; filename="chart_of_accounts_template.xlsx"'
            return response
        if template_format != "csv":
            raise ValidationError({"format": "format must be csv or xlsx."})
        response = HttpResponse(
            chart_of_accounts_template_csv(),
            content_type="text/csv",
        )
        response["Content-Disposition"] = 'attachment; filename="chart_of_accounts_template.csv"'
        return response

    @action(detail=False, methods=["get"], url_path="preview")
    def preview(self, request):
        accounts = self.filter_queryset(self.get_queryset()).order_by("code", "name")
        response = HttpResponse(chart_of_accounts_pdf(accounts=accounts), content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="chart-of-accounts.pdf"'
        return response

    @action(
        detail=False,
        methods=["post"],
        url_path="import",
        parser_classes=[MultiPartParser, FormParser],
    )
    def import_accounts(self, request):
        uploaded_file = request.FILES.get("file")
        if uploaded_file is None:
            raise ValidationError({"file": "CSV or XLSX file is required."})
        result = import_chart_of_accounts(
            tenant=request.tenant,
            uploaded_file=uploaded_file,
            update_existing=str(request.data.get("update_existing", "")).strip().lower()
            in {"1", "true", "yes", "y"},
        )
        return Response(result, status=status.HTTP_200_OK)


class ChartOfAccountViewSet(AccountViewSet):
    """Root chart-of-accounts API.

    Kept separate from the accounting/accounts route so DRF exposes the exact
    dashboard URLs under /api/v1/chart-of-accounts/.
    """

    pass


class ChartOfAccountsTemplateView(APIView):
    permission_classes = [IsAuthenticated, IsTenantManager]

    def get(self, request):
        template_format = request.query_params.get("format", "xlsx").strip().lower()
        if template_format == "xlsx":
            response = HttpResponse(
                chart_of_accounts_template_xlsx(),
                content_type=CHART_XLSX_CONTENT_TYPE,
            )
            response["Content-Disposition"] = 'attachment; filename="chart_of_accounts_template.xlsx"'
            return response
        if template_format != "csv":
            raise ValidationError({"format": "format must be csv or xlsx."})
        response = HttpResponse(chart_of_accounts_template_csv(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="chart_of_accounts_template.csv"'
        return response


class ChartOfAccountsExportView(APIView):
    permission_classes = [IsAuthenticated, IsTenantManager]

    def get_queryset(self, request):
        return Account.objects.filter(tenant=request.tenant).select_related("parent").order_by("code", "name")

    def get(self, request):
        export_format = request.query_params.get("format", "csv").strip().lower()
        accounts = self.get_queryset(request)
        if export_format == "xlsx":
            response = HttpResponse(
                export_chart_of_accounts_xlsx(accounts=accounts),
                content_type=CHART_XLSX_CONTENT_TYPE,
            )
            response["Content-Disposition"] = 'attachment; filename="chart-of-accounts.xlsx"'
            return response
        if export_format == "pdf":
            response = HttpResponse(chart_of_accounts_pdf(accounts=accounts), content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="chart-of-accounts.pdf"'
            return response
        if export_format != "csv":
            raise ValidationError({"format": "format must be csv, xlsx, or pdf."})
        response = HttpResponse(export_chart_of_accounts_csv(accounts=accounts), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="chart-of-accounts.csv"'
        return response


class ChartOfAccountsPdfPreviewView(APIView):
    permission_classes = [IsAuthenticated, IsTenantManager]

    def get(self, request):
        accounts = Account.objects.filter(tenant=request.tenant).select_related("parent").order_by("code", "name")
        response = HttpResponse(chart_of_accounts_pdf(accounts=accounts), content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="chart-of-accounts.pdf"'
        return response


class JournalEntryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsTenantManager]
    serializer_class = JournalEntrySerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "entry_date", "source_model"]
    search_fields = ["entry_number", "memo", "source_model", "source_id"]
    ordering_fields = ["entry_date", "created_at", "entry_number", "status"]
    ordering = ["-entry_date", "-created_at"]

    def get_queryset(self):
        return (
            JournalEntry.objects.filter(tenant=self.request.tenant)
            .select_related("posted_by", "reversed_by", "reversed_entry")
            .prefetch_related("lines", "lines__account")
        )

    def perform_destroy(self, instance):
        if instance.is_finalized:
            raise ValidationError("Finalized journal entries cannot be deleted.")
        instance.delete()

    @action(detail=True, methods=["post"], url_path="post")
    def post_entry(self, request, pk=None):
        journal_entry = self.get_object()
        try:
            posted = journal_entry.post(user=request.user)
        except DjangoValidationError as exc:
            raise as_drf_validation_error(exc) from exc
        return Response(self.get_serializer(posted).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_entry(self, request, pk=None):
        journal_entry = self.get_object()
        serializer = JournalEntryReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reversal = journal_entry.reverse(
                user=request.user,
                memo=serializer.validated_data.get("memo", ""),
            )
        except DjangoValidationError as exc:
            raise as_drf_validation_error(exc) from exc
        return Response(self.get_serializer(reversal).data, status=status.HTTP_201_CREATED)


class AccountingEventViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsTenantManager]
    serializer_class = AccountingEventSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["event_type", "status", "source_model"]
    search_fields = ["event_type", "source_model", "source_id", "idempotency_key"]
    ordering_fields = ["created_at", "updated_at", "status", "event_type"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return AccountingEvent.objects.filter(tenant=self.request.tenant).select_related("journal_entry")

    @action(detail=True, methods=["post"], url_path="retry")
    def retry(self, request, pk=None):
        event = self.get_object()
        try:
            retried = retry_accounting_event(event=event)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(
            self.get_serializer(retried).data,
            status=status.HTTP_200_OK,
        )


class InventoryMovementViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsTenantManager]
    serializer_class = InventoryMovementSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["movement_type", "variant", "source_model"]
    search_fields = ["variant__sku", "variant__product__title", "source_model", "source_id", "note"]
    ordering_fields = ["created_at", "quantity", "unit_cost", "total_cost", "movement_type"]
    ordering = ["-created_at", "-id"]

    def get_queryset(self):
        return (
            InventoryMovement.objects.filter(tenant=self.request.tenant)
            .select_related("tenant", "variant", "variant__product")
        )


class RefundViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsTenantManager]
    serializer_class = RefundSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "order", "payment"]
    search_fields = ["order__slug", "payment__reference", "reason", "idempotency_key"]
    ordering_fields = ["created_at", "updated_at", "amount", "status"]
    ordering = ["-created_at", "-id"]

    def get_queryset(self):
        return (
            Refund.objects.filter(tenant=self.request.tenant)
            .select_related("tenant", "order", "payment", "approved_by", "completed_by")
            .prefetch_related("lines", "lines__order_item")
        )

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        refund = self.get_object()
        try:
            approved = approve_refund(refund=refund, user=request.user)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(approved).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        refund = self.get_object()
        try:
            completed = complete_refund(refund=refund, user=request.user)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(completed).data, status=status.HTTP_200_OK)


class AccountingSettingsView(APIView):
    permission_classes = [IsAuthenticated, IsTenantManager]

    def get_object(self, request):
        settings, _created = AccountingSettings.objects.get_or_create(
            tenant=request.tenant,
            defaults={"base_currency": getattr(request.tenant, "currency", "UGX") or "UGX"},
        )
        return settings

    def get(self, request):
        serializer = AccountingSettingsSerializer(self.get_object(request), context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request):
        instance = self.get_object(request)
        serializer = AccountingSettingsSerializer(
            instance,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class AccountingReportView(APIView):
    permission_classes = [IsAuthenticated, IsTenantManager]

    def get_date_range(self, request):
        serializer = ReportDateRangeSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def get_as_of_date(self, request):
        serializer = ReportAsOfDateSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class GeneralLedgerReportView(AccountingReportView):
    def get(self, request):
        params = self.get_date_range(request)
        return Response(
            general_ledger(tenant=request.tenant, **params),
            status=status.HTTP_200_OK,
        )


class TrialBalanceReportView(AccountingReportView):
    def get(self, request):
        params = self.get_date_range(request)
        return Response(
            trial_balance(tenant=request.tenant, **params),
            status=status.HTTP_200_OK,
        )


class ProfitAndLossReportView(AccountingReportView):
    def get(self, request):
        params = self.get_date_range(request)
        return Response(
            profit_and_loss(tenant=request.tenant, **params),
            status=status.HTTP_200_OK,
        )


class BalanceSheetReportView(AccountingReportView):
    def get(self, request):
        params = self.get_as_of_date(request)
        return Response(
            balance_sheet(tenant=request.tenant, **params),
            status=status.HTTP_200_OK,
        )


class CashFlowReportView(AccountingReportView):
    def get(self, request):
        params = self.get_date_range(request)
        return Response(
            cash_flow(tenant=request.tenant, **params),
            status=status.HTTP_200_OK,
        )


class SalesReportView(AccountingReportView):
    def get(self, request):
        params = self.get_date_range(request)
        return Response(
            sales_report(tenant=request.tenant, **params),
            status=status.HTTP_200_OK,
        )
