from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from decimal import Decimal
from rest_framework.test import APIClient

from apps.products.models import Category, Product, ProductImage, ProductVariant
from apps.tenants.models import Tenant, TenantMembership

User = get_user_model()


class ProductTenantIsolationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.tenant_a = Tenant.objects.create(
            name="Tenant A",
            slug="tenant-a",
            is_active=True,
            is_default=True,
        )
        self.tenant_b = Tenant.objects.create(
            name="Tenant B",
            slug="tenant-b",
            is_active=True,
        )

        self.category_a = Category.objects.create(
            tenant=self.tenant_a,
            name="Fruits",
            slug="fruits",
        )
        self.category_b = Category.objects.create(
            tenant=self.tenant_b,
            name="Fruits",
            slug="fruits",
        )

        self.product_a = Product.objects.create(
            tenant=self.tenant_a,
            category=self.category_a,
            title="Apple",
            slug="apple",
        )
        self.product_b = Product.objects.create(
            tenant=self.tenant_b,
            category=self.category_b,
            title="Apple",
            slug="apple",
        )

        ProductImage.objects.create(
            tenant=self.tenant_a,
            product=self.product_a,
            image="gocart/products/gallery/apple-a",
            alt_text="Apple A",
        )
        ProductImage.objects.create(
            tenant=self.tenant_b,
            product=self.product_b,
            image="gocart/products/gallery/apple-b",
            alt_text="Apple B",
        )

        ProductVariant.objects.create(
            tenant=self.tenant_a,
            product=self.product_a,
            name="1kg",
            sku="A1",
            price="1000.00",
            stock_quantity=10,
        )
        ProductVariant.objects.create(
            tenant=self.tenant_b,
            product=self.product_b,
            name="1kg",
            sku="B1",
            price="2000.00",
            stock_quantity=10,
        )

    def test_list_products_returns_active_tenant_only(self):
        response = self.client.get(
            "/api/v1/products/",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["slug"], self.product_a.slug)





class ProductRolePermissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.tenant = Tenant.objects.create(
            name="Tenant A",
            slug="tenant-a-role",
            is_active=True,
            is_default=True,
        )
        self.category = Category.objects.create(
            tenant=self.tenant,
            name="Fruits",
            slug="fruits-role",
        )

    def test_manager_cannot_create_product(self):
        manager = User.objects.create_user(
            email="manager2@example.com",
            username="manager2",
            password="secret123",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=manager,
            role=TenantMembership.Role.MANAGER,
        )

        self.client.force_authenticate(user=manager)

        response = self.client.post(
            "/api/v1/products/",
            {
                "title": "Pear",
                "description": "Fresh",
                "category_id": self.category.id,
                "variants": [
                    {
                        "name": "Pack",
                        "price": "1000.00",
                        "stock_quantity": 3,
                    }
                ],
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_product_with_multipart_gallery_image(self):
        admin = User.objects.create_user(
            email="admin-products@example.com",
            username="admin-products",
            password="secret123",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        self.client.force_authenticate(user=admin)

        image = SimpleUploadedFile(
            "product.gif",
            b"GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
            content_type="image/gif",
        )

        response = self.client.post(
            "/api/v1/products/",
            {
                "title": "Pear",
                "category_id": str(self.category.id),
                "is_active": "true",
                "is_featured": "false",
                "images_payload": '[{"alt_text":"Pear image","sort_order":0,"is_active":true}]',
                "image_file_0": image,
            },
            format="multipart",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201)
        product = Product.objects.get(slug="pear")
        self.assertEqual(product.images.count(), 1)

    def test_admin_can_create_category_without_slug(self):
        admin = User.objects.create_user(
            email="admin-categories@example.com",
            username="admin-categories",
            password="secret123",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            "/api/v1/categories/",
            {
                "name": "Beauty Health",
                "is_active": "true",
            },
            format="multipart",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["slug"], "beauty-health")

    def test_admin_create_product_resolves_duplicate_variant_skus(self):
        admin = User.objects.create_user(
            email="admin-product-skus@example.com",
            username="admin-product-skus",
            password="secret123",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        self.client.force_authenticate(user=admin)

        existing_product = Product.objects.create(
            tenant=self.tenant,
            category=self.category,
            title="Existing pear",
            slug="existing-pear",
        )
        ProductVariant.objects.create(
            tenant=self.tenant,
            product=existing_product,
            name="Pack",
            sku="PEARPACK",
            price="1000.00",
            stock_quantity=3,
        )

        response = self.client.post(
            "/api/v1/products/",
            {
                "title": "Pear bundle",
                "category_id": self.category.id,
                "variants": [
                    {
                        "name": "Pack",
                        "sku": "PEARPACK",
                        "price": "1000.00",
                        "stock_quantity": 3,
                    },
                    {
                        "name": "Box",
                        "price": "2500.00",
                        "stock_quantity": 4,
                    },
                    {
                        "name": "Very long option alpha",
                        "price": "3000.00",
                        "stock_quantity": 2,
                    },
                    {
                        "name": "Very long option alpine",
                        "price": "3500.00",
                        "stock_quantity": 2,
                    },
                ],
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201)
        product = Product.objects.get(slug="pear-bundle")
        skus = list(product.variants.order_by("sort_order", "id").values_list("sku", flat=True))
        self.assertEqual(len(skus), len(set(skus)))
        self.assertNotEqual(skus[0], "PEARPACK")

    def test_admin_can_create_variants_with_different_prices_and_costs(self):
        admin = User.objects.create_user(
            email="admin-product-variant-prices@example.com",
            username="admin-product-variant-prices",
            password="secret123",
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=admin,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            "/api/v1/products/",
            {
                "title": "Rice",
                "category_id": self.category.id,
                "variants": [
                    {
                        "name": "1kg",
                        "sku": "RICE-1KG",
                        "price": "6000.00",
                        "unit_cost": "4200.00",
                        "stock_quantity": 20,
                    },
                    {
                        "name": "5kg",
                        "sku": "RICE-5KG",
                        "selling_price": "28000.00",
                        "cost_price": "21000.00",
                        "stock_quantity": 10,
                    },
                ],
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        product = Product.objects.get(slug="rice")
        one_kg = product.variants.get(sku="RICE-1KG")
        five_kg = product.variants.get(sku="RICE-5KG")
        self.assertEqual(one_kg.price, Decimal("6000.00"))
        self.assertEqual(one_kg.unit_cost, Decimal("4200.00"))
        self.assertEqual(five_kg.price, Decimal("28000.00"))
        self.assertEqual(five_kg.unit_cost, Decimal("21000.00"))
        self.assertEqual(product.selling_price, one_kg.price)
        self.assertEqual(product.cost_price, one_kg.unit_cost)
