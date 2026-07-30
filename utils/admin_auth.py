import hmac
import logging
from typing import Optional

from fastapi import Header, HTTPException
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_auth_requests

from config import obtener_configuracion

logger = logging.getLogger(__name__)

_google_request = google_auth_requests.Request()


def require_admin(
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None),
):
    """
    Protege /admin/*. El servicio sigue público en /znuny-webhook (Znuny no
    autentica); esto solo cierra las rutas administrativas.

    Vía principal: OIDC del propio Cloud Scheduler, verificado contra
    ADMIN_OIDC_AUDIENCE y (si está configurado) el email de la service account.
    Vía de respaldo: X-Admin-Token estático, comparado con hmac.compare_digest,
    solo para break-glass — no debe quedar definido en prod tras el rollout.
    """
    settings = obtener_configuracion()

    if settings.admin_sync_token and x_admin_token and hmac.compare_digest(x_admin_token, settings.admin_sync_token):
        return {"method": "static_token"}

    if authorization and authorization.lower().startswith("bearer "):
        if not settings.admin_oidc_audience:
            logger.error("Admin OIDC: ADMIN_OIDC_AUDIENCE no configurado; se rechaza el token")
            raise HTTPException(status_code=401, detail="OIDC no configurado")

        token = authorization.split(" ", 1)[1]
        try:
            claims = google_id_token.verify_oauth2_token(
                token, _google_request, audience=settings.admin_oidc_audience
            )
        except Exception as e:
            logger.warning("Admin OIDC: token inválido: %s", e)
            raise HTTPException(status_code=401, detail="Token inválido")

        expected_sa = settings.admin_oidc_service_account
        if expected_sa and claims.get("email") != expected_sa:
            logger.warning("Admin OIDC: service account inesperada: %s", claims.get("email"))
            raise HTTPException(status_code=401, detail="Service account no autorizada")

        return {"method": "oidc", "email": claims.get("email")}

    raise HTTPException(status_code=401, detail="No autenticado")
