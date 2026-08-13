# ERP internal routes. Guarded by INTEGRATION_API_KEY, not by a Plane API token.

from django.urls import path

from plane.api.views import ErpProvisionUserAPIEndpoint

urlpatterns = [
    path(
        "erp/provision-user/",
        ErpProvisionUserAPIEndpoint.as_view(http_method_names=["post"]),
        name="erp-provision-user",
    )
]
