import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from utils.admin_auth import require_admin
from services.knowledge_base_service import KnowledgeBaseService, KnowledgeBaseServiceError
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
    """Qué store está resuelto ahora mismo para el tool_config del RAG."""
    settings = obtener_configuracion()
    kb = KnowledgeBaseService()
    return {
        "rag_enabled": settings.rag_enabled,
        "faq_store": kb.resolve_active_store(settings.faq_store_prefix),
    }


@router.get("/kb-stores")
def kb_stores(con_documentos: bool = False):
    """
    Inventario de File Search Stores agrupado por display_name.

    Varios stores con el mismo nombre lógico hacen que la resolución del RAG
    dependa del orden del listado, con el riesgo de terminar consultando un
    store vacío sin ningún error visible. `con_documentos=true` cuenta los
    documentos de cada store: es una llamada por store, así que es opt-in.
    """
    kb = KnowledgeBaseService()
    stores = kb.inventory_stores(include_documents=con_documentos)

    grupos = {}
    for store in stores:
        grupos.setdefault(store["display_name"] or "(sin display_name)", []).append(store)

    return {
        "total_stores": len(stores),
        "nombres_distintos": len(grupos),
        "duplicados": {n: len(v) for n, v in sorted(grupos.items()) if len(v) > 1},
        "grupos": grupos,
    }


@router.post("/kb-stores/prune-empty")
def prune_empty_kb_stores(display_name: str, aplicar: bool = False):
    """
    Elimina los File Search Stores vacíos que compartan un display_name.

    Dry-run por defecto: hay que pasar `aplicar=true` para borrar de verdad.
    Solo toca stores con cero documentos confirmados, así que no puede
    llevarse por delante un corpus con contenido.
    """
    kb = KnowledgeBaseService()
    try:
        return kb.prune_empty_stores(display_name, apply=aplicar)
    except KnowledgeBaseServiceError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/kb-stores/delete")
def delete_kb_store(name: str, confirmar: bool = False):
    """
    Elimina un File Search Store concreto, con su contenido.

    Exige el nombre de recurso exacto (no el display_name), así que no puede
    borrar en lote por error. Sin `confirmar=true` solo informa qué se borraría
    y cuántos documentos se perderían. Es irreversible.

    Para limpiar stores vacíos en lote está /kb-stores/prune-empty, que es más
    seguro porque nunca toca uno con contenido.
    """
    kb = KnowledgeBaseService()

    if not confirmar:
        return {
            "borrado": False,
            "name": name,
            "documentos": kb.count_documents(name),
            "aviso": "Irreversible. Repetir con confirmar=true para ejecutar.",
        }

    try:
        kb.delete_store(name)
    except KnowledgeBaseServiceError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"borrado": True, "name": name}


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
