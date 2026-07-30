import pytest
from unittest.mock import patch
from fastapi import HTTPException

from utils.admin_auth import require_admin


class TestTokenEstatico:
    def test_token_correcto_pasa(self, monkeypatch):
        monkeypatch.setenv("ADMIN_SYNC_TOKEN", "secreto-123")
        result = require_admin(authorization=None, x_admin_token="secreto-123")
        assert result["method"] == "static_token"

    def test_token_incorrecto_rechazado(self, monkeypatch):
        monkeypatch.setenv("ADMIN_SYNC_TOKEN", "secreto-123")
        with pytest.raises(HTTPException) as exc:
            require_admin(authorization=None, x_admin_token="otro")
        assert exc.value.status_code == 401

    def test_sin_token_configurado_no_hay_bypass(self, monkeypatch):
        monkeypatch.delenv("ADMIN_SYNC_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc:
            require_admin(authorization=None, x_admin_token="cualquier-cosa")
        assert exc.value.status_code == 401


class TestSinCredenciales:
    def test_ausente_rechazado(self, monkeypatch):
        monkeypatch.delenv("ADMIN_SYNC_TOKEN", raising=False)
        with pytest.raises(HTTPException) as exc:
            require_admin(authorization=None, x_admin_token=None)
        assert exc.value.status_code == 401


class TestOidc:
    def test_sin_audience_configurado_falla_cerrado(self, monkeypatch):
        monkeypatch.delenv("ADMIN_OIDC_AUDIENCE", raising=False)
        with pytest.raises(HTTPException) as exc:
            require_admin(authorization="Bearer algo.invalido.aqui", x_admin_token=None)
        assert exc.value.status_code == 401

    def test_token_invalido_rechazado(self, monkeypatch):
        monkeypatch.setenv("ADMIN_OIDC_AUDIENCE", "https://servicio.example.com")
        with patch("utils.admin_auth.google_id_token.verify_oauth2_token", side_effect=ValueError("token expirado")):
            with pytest.raises(HTTPException) as exc:
                require_admin(authorization="Bearer expirado.aqui", x_admin_token=None)
        assert exc.value.status_code == 401

    def test_audience_correcta_pero_sa_no_autorizada(self, monkeypatch):
        monkeypatch.setenv("ADMIN_OIDC_AUDIENCE", "https://servicio.example.com")
        monkeypatch.setenv("ADMIN_OIDC_SERVICE_ACCOUNT", "scheduler@proyecto.iam.gserviceaccount.com")
        with patch("utils.admin_auth.google_id_token.verify_oauth2_token", return_value={"email": "otra-cuenta@x.com"}):
            with pytest.raises(HTTPException) as exc:
                require_admin(authorization="Bearer valido.aqui", x_admin_token=None)
        assert exc.value.status_code == 401

    def test_oidc_valido_y_sa_correcta_pasa(self, monkeypatch):
        monkeypatch.setenv("ADMIN_OIDC_AUDIENCE", "https://servicio.example.com")
        monkeypatch.setenv("ADMIN_OIDC_SERVICE_ACCOUNT", "scheduler@proyecto.iam.gserviceaccount.com")
        with patch(
            "utils.admin_auth.google_id_token.verify_oauth2_token",
            return_value={"email": "scheduler@proyecto.iam.gserviceaccount.com"},
        ):
            result = require_admin(authorization="Bearer valido.aqui", x_admin_token=None)
        assert result["method"] == "oidc"
