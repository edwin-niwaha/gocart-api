from apps.tenants.admin_mixins import TenantScopedAdminMixin
from django.contrib import admin, messages
from django.utils.html import format_html

from .models import Category, Product, ProductImage, ProductVariant


def cloudinary_preview(field, size: int = 60):
    if not field:
        return "—"

    try:
        url = field.url
    except Exception:
        return "—"

    return format_html(
        '<img src="{}" width="{}" height="{}" '
        'style="object-fit:cover;border-radius:8px;" />',
        url,
        size,
        size,
    )


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = (
        "tenant",
        "preview",
        "image",
        "alt_text",
        "sort_order",
        "is_active",
    )
    readonly_fields = ("tenant", "preview")

    def preview(self, obj):
        return cloudinary_preview(getattr(obj, "image", None))

    preview.short_description = "Preview"


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1
    fields = (
        "tenant",
        "name",
        "sku",
        "price",
        "stock_quantity",
        "max_quantity_per_order",
        "is_active",
        "sort_order",
    )
    readonly_fields = ("tenant",)


@admin.register(Category)
class CategoryAdmin(TenantScopedAdminMixin):
    list_display = (
        "name",
        "tenant",
        "slug",
        "image_preview",
        "is_active",
        "created_at",
    )
    list_filter = ("tenant", "is_active", "created_at")
    search_fields = ("name", "slug", "tenant__name", "tenant__slug")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("tenant",)
    readonly_fields = ("image_preview", "created_at", "updated_at")
    ordering = ("tenant", "name")

    fieldsets = (
        (
            "Basic Information",
            {
                "fields": (
                    "tenant",
                    "name",
                    "slug",
                    "image",
                    "image_preview",
                )
            },
        ),
        ("Status", {"fields": ("is_active",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def image_preview(self, obj):
        return cloudinary_preview(getattr(obj, "image", None))

    image_preview.short_description = "Preview"


@admin.register(Product)
class ProductAdmin(TenantScopedAdminMixin):
    inlines = [ProductImageInline, ProductVariantInline]

    def hero_preview(self, obj):
        return cloudinary_preview(getattr(obj, "hero_image", None))

    hero_preview.short_description = "Hero"

    def featured_badge(self, obj):
        return "⭐ Featured" if obj.is_featured else "—"

    featured_badge.short_description = "Featured"
    featured_badge.admin_order_field = "is_featured"

    def variant_count(self, obj):
        return obj.variants.count()

    variant_count.short_description = "Variants"

    def image_count(self, obj):
        return obj.images.count()

    image_count.short_description = "Images"

    def base_price_display(self, obj):
        return obj.base_price

    base_price_display.short_description = "Base price"

    def gross_margin_display(self, obj):
        return f"{obj.gross_margin_percent}%"

    gross_margin_display.short_description = "Margin"

    list_display = (
        "title",
        "tenant",
        "category",
        "hero_preview",
        "base_price_display",
        "cost_price",
        "selling_price",
        "gross_margin_display",
        "variant_count",
        "image_count",
        "featured_badge",
        "is_active",
        "created_at",
    )

    list_filter = (
        "tenant",
        "category",
        "is_active",
        "is_featured",
        "created_at",
    )

    list_editable = ("is_active",)

    search_fields = (
        "title",
        "slug",
        "description",
        "tenant__name",
        "variants__name",
        "variants__sku",
    )

    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("tenant", "category")
    readonly_fields = ("hero_preview", "created_at", "updated_at")

    fieldsets = (
        (
            "Basic Information",
            {
                "fields": (
                    "tenant",
                    "title",
                    "slug",
                    "category",
                    "description",
                    "cost_price",
                    "selling_price",
                ),
                "description": (
                    "Enter a product description manually."
                ),
            },
        ),
        (
            "Hero Image",
            {
                "fields": (
                    "hero_image",
                    "hero_preview",
                )
            },
        ),
        ("Status", {"fields": ("is_active", "is_featured")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    ordering = ("tenant", "-created_at")




@admin.register(ProductImage)
class ProductImageAdmin(TenantScopedAdminMixin):
    list_display = (
        "product",
        "tenant",
        "preview",
        "alt_text",
        "sort_order",
        "is_active",
        "created_at",
    )
    list_filter = ("tenant", "is_active", "product__category", "created_at")
    search_fields = ("product__title", "tenant__name", "alt_text")
    autocomplete_fields = ("tenant", "product")
    readonly_fields = ("preview", "created_at", "updated_at")
    ordering = ("tenant", "product", "sort_order", "id")

    def preview(self, obj):
        return cloudinary_preview(getattr(obj, "image", None))

    preview.short_description = "Preview"


@admin.register(ProductVariant)
class ProductVariantAdmin(TenantScopedAdminMixin):
    list_display = (
        "product",
        "tenant",
        "name",
        "sku",
        "price",
        "unit_cost",
        "gross_margin_display",
        "stock_quantity",
        "is_active",
        "sort_order",
        "created_at",
    )
    list_filter = ("tenant", "is_active", "product__category", "created_at")
    search_fields = ("product__title", "tenant__name", "name", "sku")
    autocomplete_fields = ("tenant", "product")
    ordering = ("tenant", "product", "sort_order", "price")

    def gross_margin_display(self, obj):
        return f"{obj.gross_margin_percent}%"

    gross_margin_display.short_description = "Margin"
