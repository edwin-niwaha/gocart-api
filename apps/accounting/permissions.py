from apps.tenants.permissions import IsTenantManager


class IsAccountingManager(IsTenantManager):
    """Tenant managers and above can manage accounting records."""
