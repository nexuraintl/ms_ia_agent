import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from utils.admin_auth import require_admin
from services.knowledge_base_service import KnowledgeBaseService
from services.faq_repository import FaqRepository, FaqRepositoryError
from services.faq_sync_service import FaqSyncService
from config import obtener_configuracion

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.post("/sync-faqs")
def sync_faqs():
    """
    Dispara la sync blue/green de FAQs. Síncrona a propósito: con
    BackgroundTasks, Cloud Run podría estrangular la CPU tras responder y
    Cloud Scheduler registraría 200 de un job que nunca terminó de correr.
    El timeout real lo impone Cloud Run (--timeout=900s en el despliegue).
    """
    try:
        result = FaqSyncService().run()
    except Exception as e:
        logger.exception("Error inesperado en /admin/sync-faqs")
        raise HTTPException(status_code=500, detail=str(e))

    if result.status == "failed":
        raise HTTPException(status_code=500, detail=result.error or "La sincronización falló")

    return asdict(result)


@router.get("/rag-status")
def rag_status():
    """Qué stores están resueltos ahora mismo para el tool_config del RAG."""
    settings = obtener_configuracion()
    kb = KnowledgeBaseService()
    return {
        "rag_enabled": settings.rag_enabled,
        "drive_store": kb.get_store_by_display_name(settings.drive_store_name),
        "faq_store": kb.resolve_active_store(settings.faq_store_prefix),
    }


@router.get("/faq-schema")
def faq_schema():
    """
    Smoke test de conectividad a MariaDB (conteo de FAQs por visibilidad).
    Borrar esta ruta una vez validada la conectividad: es superficie de
    divulgación sobre una base de datos de producción.
    """
    try:
        counts = FaqRepository().describe()
    except FaqRepositoryError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"faq_counts_by_visibility": counts}
