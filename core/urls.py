from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.accounting import views as accounting_views
from .health import live, ready

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live", live, name="health-live"),
    path("health/ready", ready, name="health-ready"),
    path(
        "api/v1/chart-of-accounts/template/",
        accounting_views.ChartOfAccountsTemplateView.as_view(),
        name="chart-of-accounts-template-direct",
    ),
    path(
        "api/v1/chart-of-accounts/export/",
        accounting_views.ChartOfAccountsExportView.as_view(),
        name="chart-of-accounts-export-direct",
    ),
    path(
        "api/v1/chart-of-accounts/preview/",
        accounting_views.ChartOfAccountsPdfPreviewView.as_view(),
        name="chart-of-accounts-preview-direct",
    ),
    path("api/v1/", include("api.v1.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
