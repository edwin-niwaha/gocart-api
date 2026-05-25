from django.urls import include, path
from rest_framework import status
from rest_framework.routers import DefaultRouter
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.addresses import views as address_views
from apps.analytics import views as analytics_views
from apps.accounting import views as accounting_views
from apps.cart import views as cart_views
from apps.common import views as common_views
from apps.notifications import views as notification_views
from apps.orders import views as order_views
from apps.payments import views as payment_views
from apps.products import views as product_views
from apps.promotions import views as promotions_views
from apps.reviews import views as review_views
from apps.shipping import views as shipping_views
from apps.tenants import views as tenant_views
from apps.users import views as user_views
from apps.wishlist import views as wishlist_views

router = DefaultRouter()
router.register("tenants", tenant_views.TenantViewSet, basename="tenants")
router.register("users", user_views.UserViewSet, basename="users")
router.register("products", product_views.ProductViewSet, basename="products")
router.register("variants", product_views.ProductVariantViewSet, basename="variants")
router.register("categories", product_views.CategoryViewSet, basename="categories")
router.register("cart", cart_views.CartViewSet, basename="cart")
router.register("cart-items", cart_views.CartItemViewSet, basename="cart-items")
router.register("orders", order_views.OrderViewSet, basename="orders")
router.register("order-items", order_views.OrderItemViewSet, basename="order-items")
router.register("reviews", review_views.ReviewViewSet, basename="reviews")
router.register("product-reviews", review_views.ProductReviewViewSet, basename="product-reviews")
router.register("ratings", review_views.ProductRatingViewSet, basename="ratings")
router.register("wishlist", wishlist_views.WishlistViewSet, basename="wishlist")
router.register("wishlist-items", wishlist_views.WishlistItemViewSet, basename="wishlist-items")
router.register("addresses", address_views.CustomerAddressViewSet, basename="addresses")
router.register("pickup-stations", shipping_views.PickupStationViewSet, basename="pickup-stations")
router.register("coupons", promotions_views.CouponViewSet, basename="coupons")
router.register("notifications", notification_views.NotificationViewSet, basename="notifications")
router.register("device-tokens", notification_views.DeviceTokenViewSet, basename="device-tokens")
router.register("contact", common_views.ContactMessageViewSet, basename="contact-message")
router.register("newsletter", common_views.NewsletterSubscribeViewSet, basename="newsletter")
router.register("support-messages", common_views.SupportMessageViewSet, basename="support-messages")
router.register("audit-logs", common_views.AuditLogViewSet, basename="audit-logs")
router.register("chart-of-accounts", accounting_views.ChartOfAccountViewSet, basename="chart-of-accounts")


class RemovedShippingEndpointView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, *args, **kwargs):
        return Response(
            {
                "count": 0,
                "next": None,
                "previous": None,
                "results": [],
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, *args, **kwargs):
        return Response(
            {"detail": "Shipping configuration has been removed. GoCart uses road delivery only."},
            status=status.HTTP_410_GONE,
        )


urlpatterns = [
    path("tenants/current/branding/", tenant_views.CurrentTenantBrandingView.as_view(), name="tenant-branding"),
    path("tenants/current/settings/", tenant_views.CurrentTenantSettingsView.as_view(), name="tenant-settings"),
    path("tenants/current/feature-flags/", tenant_views.CurrentTenantFeatureFlagView.as_view(), name="tenant-feature-flags"),
    path("tenants/current/memberships/", tenant_views.CurrentTenantMembershipListCreateView.as_view(), name="tenant-memberships"),
    path("tenants/current/memberships/<int:membership_id>/", tenant_views.CurrentTenantMembershipDetailView.as_view(), name="tenant-membership-detail"),
    path("auth/", include("apps.users.urls")),
    path("rest-auth/", include("dj_rest_auth.urls")),
    path("rest-auth/registration/", include("dj_rest_auth.registration.urls")),
    path("payments/", include("apps.payments.urls")),
    path("admin/payments/", include("apps.payments.admin_urls")),
    path("admin/dashboard/summary/", analytics_views.AdminDashboardSummaryView.as_view(), name="admin-dashboard-summary"),
    path("accounting/", include("apps.accounting.urls")),
    path("reports/trial-balance/", accounting_views.TrialBalanceReportView.as_view(), name="report-trial-balance"),
    path("reports/profit-loss/", accounting_views.ProfitAndLossReportView.as_view(), name="report-profit-loss"),
    path("reports/balance-sheet/", accounting_views.BalanceSheetReportView.as_view(), name="report-balance-sheet"),
    path("reports/cash-flow/", accounting_views.CashFlowReportView.as_view(), name="report-cash-flow"),
    path(
        "chart-of-accounts/template/",
        accounting_views.ChartOfAccountsTemplateView.as_view(),
        name="chart-of-accounts-template",
    ),
    path(
        "chart-of-accounts/export/",
        accounting_views.ChartOfAccountsExportView.as_view(),
        name="chart-of-accounts-export",
    ),
    path(
        "chart-of-accounts/preview/",
        accounting_views.ChartOfAccountsPdfPreviewView.as_view(),
        name="chart-of-accounts-preview",
    ),
    path("shipping-methods/", RemovedShippingEndpointView.as_view(), name="removed-shipping-methods"),
    path("delivery-rates/", RemovedShippingEndpointView.as_view(), name="removed-delivery-rates"),
    path("checkout/summary/", order_views.CheckoutSummaryView.as_view(), name="checkout-summary"),
    path("checkout/validate/", order_views.CheckoutSummaryView.as_view(), name="checkout-validate"),
    path("", include(router.urls)),
]
