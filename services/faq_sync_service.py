import os
import shutil
import logging
import tempfile
import threading
import datetime
from dataclasses import dataclass, field
from typing import Optional, List

from google.genai import types

from config import obtener_configuracion
from services.faq_repository import FaqRepository, FaqRepositoryError
from services.knowledge_base_service import KnowledgeBaseService, KnowledgeBaseServiceError

logger = logging.getLogger(__name__)

# Lock a nivel de módulo (no de instancia): protege contra invocaciones
# solapadas del Scheduler dentro de la misma instancia de Cloud Run. No es
# un lock distribuido entre instancias; con --max-retry-attempts=1 y un
# job diario esto cubre el caso real de reintentos superpuestos.
_sync_lock = threading.Lock()


@dataclass
class FaqSyncResult:
    status: str  # "success" | "failed" | "skipped"
    rows_fetched: int = 0
    rows_skipped: int = 0
    shards_uploaded: int = 0
    store_name: Optional[str] = None
    pruned_stores: List[str] = field(default_factory=list)
    error: Optional[str] = None


class FaqSyncService:
    """
    Sincroniza las FAQs de Znuny (MariaDB) hacia un File Search Store nuevo,
    versionado por timestamp (blue/green). No borra el store anterior hasta
    confirmar que el nuevo quedó bien armado.
    """

    def __init__(self):
        self.settings = obtener_configuracion()
        self.kb_service = KnowledgeBaseService()

    def run(self) -> FaqSyncResult:
        if not _sync_lock.acquire(blocking=False):
            logger.warning("FAQ sync: ya hay una corrida en curso; se omite esta invocación")
            return FaqSyncResult(status="skipped")

        new_store_name = None
        try:
            repo = FaqRepository()
            records = list(repo.fetch_faqs())

            # Es la línea más importante del archivo: un rebuild vacío seguido
            # de prune borraría el corpus completo. Sin filas -> sin store nuevo.
            if not records:
                logger.error("FAQ sync: fetch_faqs() devolvió 0 filas; se aborta sin crear store ni hacer prune")
                return FaqSyncResult(status="failed", error="fetch_faqs devolvió 0 filas")

            usable_records = [r for r in records if not r.is_empty_solution()]
            rows_skipped = len(records) - len(usable_records)

            if not usable_records:
                logger.error("FAQ sync: todas las filas quedaron sin solución tras limpiar HTML; se aborta")
                return FaqSyncResult(
                    status="failed",
                    rows_fetched=len(records),
                    rows_skipped=rows_skipped,
                    error="ninguna FAQ tiene solución utilizable",
                )

            timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            display_name = f"{self.settings.faq_store_prefix}{timestamp}"
            new_store_name = self.kb_service.get_or_create_store(display_name=display_name)

            shards_uploaded = self._upload_shards(new_store_name, usable_records, timestamp)

            # Smoke test: si el store nuevo no responde con grounding, no se promueve ni se hace prune.
            self._smoke_test(new_store_name)

            pruned = self.kb_service.prune_versioned_stores(
                self.settings.faq_store_prefix, keep=self.settings.faq_sync_keep_versions
            )

            return FaqSyncResult(
                status="success",
                rows_fetched=len(records),
                rows_skipped=rows_skipped,
                shards_uploaded=shards_uploaded,
                store_name=new_store_name,
                pruned_stores=pruned,
            )

        except (FaqRepositoryError, KnowledgeBaseServiceError) as e:
            logger.exception("FAQ sync: fallo controlado")
            self._cleanup_failed_store(new_store_name)
            return FaqSyncResult(status="failed", error=str(e))
        except Exception as e:
            logger.exception("FAQ sync: fallo inesperado")
            self._cleanup_failed_store(new_store_name)
            return FaqSyncResult(status="failed", error=str(e))
        finally:
            _sync_lock.release()

    def _upload_shards(self, store_name: str, records: list, timestamp: str) -> int:
        shard_size = self.settings.faq_shard_size
        shards_uploaded = 0
        tmp_dir = tempfile.mkdtemp(prefix="faq_sync_")
        try:
            for i in range(0, len(records), shard_size):
                shard = records[i:i + shard_size]
                shard_index = i // shard_size + 1
                shard_text = "\n\n".join(r.to_text() for r in shard)
                shard_path = os.path.join(tmp_dir, f"shard_{shard_index}.txt")
                with open(shard_path, "w", encoding="utf-8") as f:
                    f.write(shard_text)

                self.kb_service.upload_and_index_file(
                    store_name,
                    shard_path,
                    display_name=f"faq_shard_{shard_index}",
                    custom_metadata={"source": "faq_sync", "shard": str(shard_index), "synced_at": timestamp},
                    chunk_size_tokens=512,
                    chunk_overlap_tokens=64,
                )
                shards_uploaded += 1
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return shards_uploaded

    def _cleanup_failed_store(self, store_name: Optional[str]) -> None:
        if not store_name:
            return
        try:
            self.kb_service.delete_store(store_name)
            logger.info("FAQ sync: store fallido %s eliminado, no se hizo prune", store_name)
        except Exception:
            logger.exception("FAQ sync: no se pudo limpiar el store fallido %s", store_name)

    def _smoke_test(self, store_name: str) -> None:
        """Exige grounding_metadata no vacío contra el store recién creado."""
        tool = self.kb_service.get_tool_config([store_name])
        response = self.kb_service.client.models.generate_content(
            model="gemini-3.6-flash",
            contents="Menciona brevemente el tema de alguna FAQ de este corpus.",
            config=types.GenerateContentConfig(temperature=0.1, tools=[tool]),
        )
        grounding = None
        try:
            grounding = response.candidates[0].grounding_metadata
        except Exception:
            pass
        if not grounding or not grounding.grounding_chunks:
            raise KnowledgeBaseServiceError(f"Smoke test del store {store_name} no devolvió grounding_metadata")
