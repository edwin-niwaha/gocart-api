from django.db import transaction
from django.utils.text import slugify

from apps.tenants.models import Tenant
from .models import Category, Product, ProductImage, ProductVariant


def generate_unique_slug(
    model_class,
    value: str,
    *,
    tenant: Tenant,
    slug_field: str = "slug",
    exclude_id: int | None = None,
) -> str:
    base_slug = slugify(value).strip("-") or "item"
    slug = base_slug
    counter = 1

    queryset = model_class.objects.filter(tenant=tenant)
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)

    while queryset.filter(**{slug_field: slug}).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    return slug


def generate_unique_sku(
    product_title: str,
    variant_name: str,
    *,
    tenant: Tenant,
    exclude_id: int | None = None,
    reserved_skus: set[str] | None = None,
) -> str:
    base = slugify(f"{product_title}-{variant_name}").replace("-", "").upper()[:24] or "SKU"
    return normalize_unique_sku(
        base,
        tenant=tenant,
        exclude_id=exclude_id,
        reserved_skus=reserved_skus,
    )


def normalize_unique_sku(
    value: str,
    *,
    tenant: Tenant,
    exclude_id: int | None = None,
    reserved_skus: set[str] | None = None,
) -> str:
    base = slugify(value).replace("-", "").upper()[:24] or "SKU"
    sku = base
    counter = 1
    reserved = reserved_skus if reserved_skus is not None else set()

    queryset = ProductVariant.objects.filter(tenant=tenant)
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)

    while sku.lower() in reserved or queryset.filter(sku=sku).exists():
        sku = f"{base}{counter}"
        counter += 1

    reserved.add(sku.lower())
    return sku



def _build_variant_defaults(
    product: Product,
    item: dict,
    *,
    reserved_skus: set[str] | None = None,
    exclude_id: int | None = None,
) -> dict:
    name = item["name"].strip()
    raw_sku = (item.get("sku") or "").strip()
    unit_cost = item.get("unit_cost", item.get("cost_price", None))
    if unit_cost is None:
        unit_cost = product.cost_price or 0
    price = item.get("price", item.get("selling_price", None))
    if price is None:
        price = product.selling_price or 0

    return {
        "tenant": product.tenant,
        "product": product,
        "name": name,
        "sku": normalize_unique_sku(
            raw_sku or f"{product.title}-{name}",
            tenant=product.tenant,
            exclude_id=exclude_id,
            reserved_skus=reserved_skus,
        ),
        "price": price,
        "unit_cost": unit_cost,
        "stock_quantity": item.get("stock_quantity", 0),
        "max_quantity_per_order": item.get("max_quantity_per_order"),
        "is_active": item.get("is_active", True),
        "sort_order": item.get("sort_order", 0),
    }


def _build_image_defaults(product: Product, item: dict) -> dict:
    return {
        "tenant": product.tenant,
        "product": product,
        "image": item["image"],
        "alt_text": (item.get("alt_text") or "").strip(),
        "sort_order": item.get("sort_order", 0),
        "is_active": item.get("is_active", True),
    }


@transaction.atomic
def create_category(*, tenant: Tenant, **validated_data) -> Category:
    name = validated_data.get("name", "").strip()
    validated_data["name"] = name
    validated_data["tenant"] = tenant

    slug = (validated_data.get("slug") or "").strip()
    if not slug:
        validated_data["slug"] = generate_unique_slug(Category, name, tenant=tenant)
    else:
        validated_data["slug"] = slug

    return Category.objects.create(**validated_data)


@transaction.atomic
def update_category(*, instance: Category, **validated_data) -> Category:
    if "name" in validated_data and validated_data["name"]:
        validated_data["name"] = validated_data["name"].strip()

    if "slug" in validated_data and validated_data["slug"]:
        validated_data["slug"] = validated_data["slug"].strip()

    for attr, value in validated_data.items():
        setattr(instance, attr, value)

    if not (instance.slug or "").strip():
        instance.slug = generate_unique_slug(
            Category,
            instance.name,
            tenant=instance.tenant,
            exclude_id=instance.id,
        )

    instance.save()
    return instance


@transaction.atomic
def create_product(*, tenant: Tenant, **validated_data) -> Product:
    variants_data = validated_data.pop("variants", [])
    images_data = validated_data.pop("images", [])

    title = validated_data.get("title", "").strip()
    validated_data["title"] = title
    validated_data["tenant"] = tenant

    if "description" in validated_data and validated_data["description"] is not None:
        validated_data["description"] = validated_data["description"].strip()


    slug = (validated_data.get("slug") or "").strip()
    if not slug:
        validated_data["slug"] = generate_unique_slug(Product, title, tenant=tenant)
    else:
        validated_data["slug"] = slug

    product = Product.objects.create(**validated_data)

    for item in images_data:
        ProductImage.objects.create(**_build_image_defaults(product, item))

    reserved_skus: set[str] = set()
    for item in variants_data:
        ProductVariant.objects.create(
            **_build_variant_defaults(product, item, reserved_skus=reserved_skus)
        )

    sync_product_default_prices_from_first_variant(product)

    return product


@transaction.atomic
def update_product(*, instance: Product, **validated_data) -> Product:
    variants_data = validated_data.pop("variants", None)
    images_data = validated_data.pop("images", None)

    if "title" in validated_data and validated_data["title"]:
        validated_data["title"] = validated_data["title"].strip()

    if "description" in validated_data and validated_data["description"] is not None:
        validated_data["description"] = validated_data["description"].strip()


    if "slug" in validated_data and validated_data["slug"]:
        validated_data["slug"] = validated_data["slug"].strip()

    for attr, value in validated_data.items():
        setattr(instance, attr, value)

    if instance.category.tenant_id != instance.tenant_id:
        instance.category = Category.objects.get(id=instance.category_id, tenant=instance.tenant)

    if not (instance.slug or "").strip():
        instance.slug = generate_unique_slug(
            Product,
            instance.title,
            tenant=instance.tenant,
            exclude_id=instance.id,
        )

    instance.save()

    if images_data is not None:
        kept_image_ids: list[int] = []
        existing_images = {image.id: image for image in instance.images.all()}

        for item in images_data:
            image_id = item.get("id")

            if image_id and image_id in existing_images:
                product_image = existing_images[image_id]

                if "image" in item:
                    product_image.image = item["image"]

                product_image.alt_text = (item.get("alt_text") or "").strip()
                product_image.sort_order = item.get("sort_order", 0)
                product_image.is_active = item.get("is_active", True)
                product_image.tenant = instance.tenant
                product_image.save()

                kept_image_ids.append(product_image.id)
            else:
                product_image = ProductImage.objects.create(
                    tenant=instance.tenant,
                    product=instance,
                    image=item["image"],
                    alt_text=(item.get("alt_text") or "").strip(),
                    sort_order=item.get("sort_order", 0),
                    is_active=item.get("is_active", True),
                )
                kept_image_ids.append(product_image.id)

        instance.images.exclude(id__in=kept_image_ids).delete()

    if variants_data is not None:
        kept_variant_ids: list[int] = []
        existing_variants = {variant.id: variant for variant in instance.variants.all()}
        incoming_variant_ids = {
            item.get("id")
            for item in variants_data
            if item.get("id") and item.get("id") in existing_variants
        }

        if incoming_variant_ids:
            instance.variants.exclude(id__in=incoming_variant_ids).delete()
        else:
            instance.variants.all().delete()

        reserved_skus: set[str] = set()

        for item in variants_data:
            variant_id = item.get("id")

            if variant_id and variant_id in existing_variants:
                variant = existing_variants[variant_id]
                defaults = _build_variant_defaults(
                    instance,
                    item,
                    reserved_skus=reserved_skus,
                    exclude_id=variant.id,
                )

                variant.name = defaults["name"]
                variant.sku = defaults["sku"]
                variant.price = defaults["price"]
                variant.unit_cost = defaults["unit_cost"]
                variant.stock_quantity = defaults["stock_quantity"]
                variant.max_quantity_per_order = defaults["max_quantity_per_order"]
                variant.is_active = defaults["is_active"]
                variant.sort_order = defaults["sort_order"]
                variant.tenant = defaults["tenant"]
                variant.save()
                kept_variant_ids.append(variant.id)
            else:
                variant = ProductVariant.objects.create(
                    **_build_variant_defaults(
                        instance,
                        item,
                        reserved_skus=reserved_skus,
                    )
                )
                kept_variant_ids.append(variant.id)

        sync_product_default_prices_from_first_variant(instance)

    return instance


def sync_product_default_prices_from_first_variant(product: Product) -> None:
    first_variant = product.variants.order_by("sort_order", "price", "id").first()
    if first_variant is None:
        return

    updates: list[str] = []
    if product.selling_price != first_variant.price:
        product.selling_price = first_variant.price
        updates.append("selling_price")
    if product.cost_price != first_variant.unit_cost:
        product.cost_price = first_variant.unit_cost
        updates.append("cost_price")

    if updates:
        updates.append("updated_at")
        product.save(update_fields=updates)
