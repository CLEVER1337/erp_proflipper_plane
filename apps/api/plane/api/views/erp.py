"""Internal ERP endpoints.

Not part of the public Plane API: called by ERP services over the cluster-internal
network with the shared `INTEGRATION_API_KEY`, not with a Plane API token. Kept in
its own module so upstream merges never touch it.
"""

import hmac
import logging
import os

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from plane.utils.erp_provisioning import erp_workspace_and_project, provision_employee

logger = logging.getLogger("plane.api")


class ErpProvisionUserAPIEndpoint(APIView):
    """Provision one ERP employee in Plane and push the ERP<->Plane pair back.

    UserService calls this when somebody is hired or becomes an employee. Until it
    existed, employees only reached Plane through `erp_bootstrap` on container start,
    so a new hire could neither be given a task nor create one — the gateway answered
    `assignee_{id}_not_mapped_to_plane` until the next restart.

    Same body as one entry of UserService `/users/for-plane`, so both paths agree.
    """

    # The caller is a service holding the ERP integration key, not a Plane user:
    # the API-token authentication of the public API does not apply here.
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        expected = os.environ.get("INTEGRATION_API_KEY")
        provided = request.headers.get("X-Api-Key")
        if not expected or not provided or not hmac.compare_digest(provided, expected):
            return Response({"error": "unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

        email = (request.data.get("email") or "").strip().lower()
        erp_id = request.data.get("erpUserId")
        if not email or not erp_id:
            return Response(
                {"error": "email_and_erp_user_id_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ws, project = erp_workspace_and_project()
        if ws is None or project is None:
            # Bootstrap has not run yet — reporting it is better than creating a
            # second workspace that the gateway does not look at.
            return Response({"error": "erp_workspace_not_bootstrapped"}, status=status.HTTP_409_CONFLICT)

        try:
            user = provision_employee(
                request.data,
                email,
                erp_id,
                ws,
                project,
                os.environ.get("USER_SERVICE_URL"),
                expected,
            )
        except Exception as e:
            logger.warning("[erp] provisioning failed for %s (erp id %s): %s", email, erp_id, e)
            return Response({"error": "provisioning_failed"}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(
            {"erpUserId": erp_id, "planeUserId": str(user.id), "planeEmail": email},
            status=status.HTTP_200_OK,
        )
