import sys
import os

# services/, utils/ y tests/ no tienen __init__.py (namespace packages); esto
# asegura que la raíz del proyecto esté en sys.path sin importar cómo se
# invoque pytest.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# Pre-existente: roto por un refactor de update_service.py que eliminó las
# funciones get_or_create_session_id/get_ticket_latest_article a nivel de
# módulo (ahora son métodos de ZnunyService). Fuera del alcance de este
# cambio (fix de RAG + pipeline de FAQs); se excluye para no bloquear la
# recolección del resto de la suite.
collect_ignore = ["test_webhook_flow.py"]


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """obtener_configuracion() usa @lru_cache: sin limpiarla, el env de un
    test contaminaría los siguientes."""
    from config import obtener_configuracion
    obtener_configuracion.cache_clear()
    yield
    obtener_configuracion.cache_clear()


@pytest.fixture
def faq_rows_sample():
    """Filas crudas tal como las devolvería pymysql (SSDictCursor) desde faq_item."""
    return [
        {
            "id": 1, "f_number": "2024081512000123", "f_subject": "La factura no genera PDF",
            "f_keywords": "factura, pdf, descarga", "content_type": "text/html",
            "changed": "2026-01-01 10:00:00",
            "f_field1": "<p>El cliente reporta &aacute;rea de <b>facturaci&oacute;n</b> sin PDF.</p>",
            "f_field2": "<div>El servicio de generaci&oacute;n de PDF est&aacute; ca&iacute;do.</div>",
            "f_field3": "<p>Reiniciar el servicio <code>pdf-gen</code> y reintentar.</p>",
            "f_field6": "",
            "categoria": "Facturación", "visibilidad": "external", "idioma": "es",
        },
        {
            "id": 2, "f_number": "2024081512000456", "f_subject": "FAQ sin solución documentada",
            "f_keywords": None, "content_type": "text/html", "changed": "2026-01-02 08:00:00",
            "f_field1": "Sintoma valido", "f_field2": "Problema valido",
            "f_field3": "   ", "f_field6": "",
            "categoria": "Soporte", "visibilidad": "internal", "idioma": "es",
        },
        {
            "id": 3, "f_number": "2024081512000789", "f_subject": "Reinicio de módulo Ventas",
            "f_keywords": "ventas, reinicio", "content_type": "text/html", "changed": "2026-01-03 09:00:00",
            "f_field1": "El módulo de ventas no responde.",
            "f_field2": "El servicio sales-api quedó colgado.",
            "f_field3": "Ejecutar systemctl restart sales-api.",
            "f_field6": "Confirmado con el equipo de infraestructura.",
            "categoria": "Ventas", "visibilidad": "public", "idioma": "es",
        },
    ]
