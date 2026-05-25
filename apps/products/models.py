from decimal import Decimal

from cloudinary.models import CloudinaryField
from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import TimeStampedModel
from apps.tenants.models import Tenant


class Category(TimeStampedModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="categories",
        null=True,
        blank=True,
    )

    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, db_index=True)

    image = CloudinaryField(
        "category_image",
        folder="gocart/categories",
        transformation={
            "width": 800,
            "height": 800,
            "crop": "fill",
            "quality": "auto",
            "fetch_format": "auto",
        },
        blank=True,
        null=True,
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Category"
        verbose_name_plural = "Categories"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="unique_category_name_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "slug"], name="unique_category_slug_per_tenant"),
        ]
        indexes = [
            models.Index(fields=["tenant", "name"]),
            models.Index(fields=["tenant", "slug"]),
            models.Index(fields=["tenant", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant.slug}: {self.name}" if self.tenant else self.name

    @property
    def image_url(self) -> str | None:
        return self.image.url if self.image else None


class Product(TimeStampedModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="products",
        null=True,
        blank=True,
    )

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="products",
        db_index=True,
    )

    title = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, db_index=True)

    description = models.TextField(blank=True)
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        db_index=True,
    )

    hero_image = CloudinaryField(
        "product_hero_image",
        folder="gocart/products/hero",
        transformation={
            "width": 1200,
            "height": 1200,
            "crop": "fill",
            "quality": "auto",
            "fetch_format": "auto",
        },
        blank=True,
        null=True,
    )

    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Product"
        verbose_name_plural = "Products"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "title"], name="unique_product_title_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "slug"], name="unique_product_slug_per_tenant"),
        ]
        indexes = [
            models.Index(fields=["tenant", "title"]),
            models.Index(fields=["tenant", "slug"]),
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "is_featured", "is_active"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["category", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def hero_image_url(self) -> str | None:
        return self.hero_image.url if self.hero_image else None

    @property
    def primary_image(self) -> str | None:
        if self.hero_image:
            return self.hero_image.url

        first_image = self.images.filter(is_active=True).order_by("sort_order", "id").first()  # type: ignore[attr-defined]
        if first_image and first_image.image:
            return first_image.image.url

        return None

    @property
    def image_urls(self) -> list[str]:
        """Return list of all active product image URLs, with hero image first if present."""
        
        # Allow tests to override/mock image URLs
        if hasattr(self, "_image_urls_override"):
            return self._image_urls_override

        urls = []

        if self.hero_image:
            urls.append(self.hero_image.url)

        gallery_urls = [
            img.image.url
            for img in self.images.filter(is_active=True).order_by("sort_order", "id")
            if img.image
        ]

        urls.extend(gallery_urls)

        return urls


    @image_urls.setter
    def image_urls(self, value):
        self._image_urls_override = value


    @property
    def base_price(self):
        if self.selling_price:
            return self.selling_price
        first_variant = self.variants.filter(is_active=True).order_by("price").first()  # type: ignore[attr-defined]
        return first_variant.price if first_variant else Decimal("0.00")

    @property
    def gross_profit_amount(self) -> Decimal:
        return (self.selling_price or Decimal("0.00")) - (self.cost_price or Decimal("0.00"))

    @property
    def gross_margin_percent(self) -> Decimal:
        if not self.selling_price:
            return Decimal("0.00")
        return (self.gross_profit_amount / self.selling_price * Decimal("100.00")).quantize(Decimal("0.01"))

    @property
    def is_in_stock(self) -> bool:
        return self.variants.filter(is_active=True, stock_quantity__gt=0).exists()  # type: ignore[attr-defined]


class ProductImage(TimeStampedModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="product_images",
        null=True,
        blank=True,
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="images",
        db_index=True,
    )

    image = CloudinaryField(
        "product_gallery_image",
        folder="gocart/products/gallery",
        transformation={
            "width": 1200,
            "height": 1200,
            "crop": "limit",
            "quality": "auto",
            "fetch_format": "auto",
        },
    )

    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Product Image"
        verbose_name_plural = "Product Images"
        indexes = [
            models.Index(fields=["tenant", "product"]),
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["product", "sort_order"]),
        ]

    def __str__(self) -> str:
        return f"{self.product.title} image"

    @property
    def image_url(self) -> str | None:
        return self.image.url if self.image else None

    def save(self, *args, **kwargs):
        if self.product_id and not self.tenant_id:
            self.tenant = self.product.tenant
        super().save(*args, **kwargs)


class ProductVariant(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="product_variants", null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants", db_index=True)

    name = models.CharField(max_length=100)
    sku = models.CharField(max_length=100, db_index=True)

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        db_index=True,
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    stock_quantity = models.PositiveIntegerField(default=0)
    max_quantity_per_order = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price", "id"]
        constraints = [
            models.UniqueConstraint(fields=["product", "name"], name="unique_variant_name_per_product"),
            models.UniqueConstraint(fields=["tenant", "sku"], name="unique_variant_sku_per_tenant"),
        ]
        indexes = [
            models.Index(fields=["tenant", "product"]),
            models.Index(fields=["tenant", "sku"]),
            models.Index(fields=["tenant", "price"]),
            models.Index(fields=["tenant", "stock_quantity"]),
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["product", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.product.title} - {self.name}"

    @property
    def is_in_stock(self) -> bool:
        return self.stock_quantity > 0

    @property
    def selling_price(self) -> Decimal:
        return self.price

    @selling_price.setter
    def selling_price(self, value) -> None:
        self.price = value

    @property
    def cost_price(self) -> Decimal:
        return self.unit_cost

    @cost_price.setter
    def cost_price(self, value) -> None:
        self.unit_cost = value

    @property
    def gross_profit_amount(self) -> Decimal:
        return (self.price or Decimal("0.00")) - (self.unit_cost or Decimal("0.00"))

    @property
    def gross_margin_percent(self) -> Decimal:
        if not self.price:
            return Decimal("0.00")
        return (self.gross_profit_amount / self.price * Decimal("100.00")).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        if self.product_id and not self.tenant_id:
            self.tenant = self.product.tenant
        super().save(*args, **kwargs)
