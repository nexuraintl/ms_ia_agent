import os
import time
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class KnowledgeBaseServiceError(Exception):
    """Error irrecuperable operando sobre un File Search Store."""
    pass


class KnowledgeBaseService:
    """
    Servicio para gestionar la Base de Conocimiento (File Search Store) en Gemini.
    Permite crear stores, subir archivos y preparar los recursos para RAG.
    """

    def __init__(self):
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("La variable de entorno GOOGLE_API_KEY no está configurada.")
        self.client = genai.Client(api_key=api_key)

    @staticmethod
    def _created_at(store) -> str:
        """create_time como string ordenable; '' si el SDK no lo expone."""
        return str(getattr(store, "create_time", "") or "")

    def get_store_by_display_name(self, display_name: str) -> str | None:
        """
        Busca un File Search Store existente por nombre. No crea nada.

        Devolver el primer match de un listado sin orden garantizado es
        peligroso cuando hay homónimos: se elegiría un store arbitrario, quizá
        vacío, y el RAG quedaría mudo sin lanzar ningún error. Ante duplicados
        se avisa y se resuelve por el más reciente.
        """
        matches = [s for s in self.client.file_search_stores.list() if s.display_name == display_name]
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Hay %d stores con display_name '%s'; se usa el más reciente. "
                "Revisar duplicados con GET /admin/kb-stores.",
                len(matches), display_name,
            )
            matches.sort(key=self._created_at, reverse=True)
        return matches[0].name

    def inventory_stores(self, include_documents: bool = False) -> list:
        """
        Inventario de todos los File Search Stores de la cuenta.
        Sirve para detectar stores duplicados que compiten por el mismo nombre
        lógico. Contar documentos cuesta una llamada por store: es opcional.
        """
        inventario = []
        for store in self.client.file_search_stores.list():
            info = {
                "name": store.name,
                "display_name": store.display_name,
                "create_time": self._created_at(store),
            }
            if include_documents:
                info["documents"] = self.count_documents(store.name)
            inventario.append(info)
        return inventario

    def prune_empty_stores(self, display_name: str, apply: bool = False) -> dict:
        """
        Borra los stores SIN documentos que tengan exactamente ese display_name.

        Acotado a propósito: exige el nombre exacto y solo elimina stores con
        cero documentos confirmados. Si no se pudo contar (None) el store se
        deja en paz — nunca se borra algo cuyo contenido no pudimos verificar.
        Dry-run por defecto: sin apply=True solo informa qué borraría.
        """
        candidatos = []
        for store in self.client.file_search_stores.list():
            if store.display_name != display_name:
                continue
            if self.count_documents(store.name) == 0:
                candidatos.append(store.name)

        borrados = []
        if apply:
            for name in candidatos:
                self.delete_store(name)
                borrados.append(name)
            logger.info("Prune de '%s': %d stores vacíos eliminados", display_name, len(borrados))

        return {
            "display_name": display_name,
            "aplicado": apply,
            "vacios_encontrados": len(candidatos),
            "borrados": len(borrados),
            "candidatos": candidatos,
        }

    def count_documents(self, store_name: str) -> int | None:
        """Cantidad de documentos indexados en un store; None si no se pudo consultar."""
        try:
            return sum(1 for _ in self.client.file_search_stores.documents.list(parent=store_name))
        except Exception:
            logger.exception("No se pudo contar documentos de %s", store_name)
            return None

    def get_or_create_store(self, display_name: str = "Znuny_Knowledge_Base") -> str:
        """
        Busca un File Search Store existente por nombre o crea uno nuevo.
        Retorna el `name` (resource ID) del store.
        Lanza KnowledgeBaseServiceError si falla: un store roto no debe propagarse
        como string vacío hasta un types.Tool.
        """
        logger.info("Buscando File Search Store: '%s'...", display_name)
        try:
            existing = self.get_store_by_display_name(display_name)
            if existing:
                logger.info("Store encontrado: %s", existing)
                return existing

            logger.info("No encontrado. Creando nuevo store '%s'...", display_name)
            store = self.client.file_search_stores.create(config={"display_name": display_name})
            logger.info("Store creado exitosamente: %s", store.name)
            return store.name
        except Exception as e:
            logger.exception("Error gestionando store '%s'", display_name)
            raise KnowledgeBaseServiceError(f"No se pudo obtener/crear el store '{display_name}': {e}") from e

    def find_versioned_stores(self, prefix: str) -> list:
        """
        Devuelve los stores cuyo display_name empieza por `prefix`, ordenados
        por display_name descendente (el timestamp en el nombre define el orden).
        """
        matches = [s for s in self.client.file_search_stores.list() if s.display_name and s.display_name.startswith(prefix)]
        matches.sort(key=lambda s: s.display_name, reverse=True)
        return matches

    def resolve_active_store(self, prefix: str) -> str | None:
        """Devuelve el `name` del store versionado más reciente con ese prefijo, o None."""
        matches = self.find_versioned_stores(prefix)
        return matches[0].name if matches else None

    def delete_store(self, store_name: str) -> None:
        """Borra un store por completo (documentos incluidos)."""
        try:
            self.client.file_search_stores.delete(name=store_name, config={"force": True})
            logger.info("Store eliminado: %s", store_name)
        except Exception:
            logger.exception("Error eliminando store %s", store_name)
            raise KnowledgeBaseServiceError(f"No se pudo eliminar el store '{store_name}'")

    def prune_versioned_stores(self, prefix: str, keep: int = 2) -> list:
        """Conserva los `keep` stores más recientes con ese prefijo y borra el resto."""
        matches = self.find_versioned_stores(prefix)
        to_delete = matches[keep:]
        deleted = []
        for store in to_delete:
            self.delete_store(store.name)
            deleted.append(store.name)
        return deleted

    def upload_and_index_file(
        self,
        store_name: str,
        file_path: str,
        display_name: str | None = None,
        custom_metadata: dict | None = None,
        chunk_size_tokens: int | None = None,
        chunk_overlap_tokens: int | None = None,
        timeout_s: int = 300,
        poll_interval_s: int = 5,
    ) -> bool:
        """
        Sube un archivo al store y espera a que termine de indexarse.
        `upload_to_file_search_store` devuelve una operación de larga duración:
        si no se espera, el archivo puede no estar disponible aún para consultas.

        `custom_metadata` es un dict simple {clave: valor}; el SDK espera una
        lista de `types.CustomMetadata`, así que se convierte aquí.
        """
        try:
            logger.info("Subiendo e indexando %s en %s...", file_path, store_name)
            config_kwargs = {}
            if display_name:
                config_kwargs["display_name"] = display_name
            if custom_metadata:
                config_kwargs["custom_metadata"] = [
                    types.CustomMetadata(key=k, string_value=str(v))
                    for k, v in custom_metadata.items()
                ]
            if chunk_size_tokens or chunk_overlap_tokens:
                config_kwargs["chunking_config"] = types.ChunkingConfig(
                    white_space_config=types.WhiteSpaceConfig(
                        max_tokens_per_chunk=chunk_size_tokens,
                        max_overlap_tokens=chunk_overlap_tokens,
                    )
                )

            op = self.client.file_search_stores.upload_to_file_search_store(
                file_search_store_name=store_name,
                file=file_path,
                config=types.UploadToFileSearchStoreConfig(**config_kwargs) if config_kwargs else None,
            )

            deadline = time.monotonic() + timeout_s
            while not op.done:
                if time.monotonic() > deadline:
                    raise KnowledgeBaseServiceError(
                        f"Timeout esperando indexado de {file_path} en {store_name} (> {timeout_s}s)"
                    )
                time.sleep(poll_interval_s)
                op = self.client.operations.get(op)

            if getattr(op, "error", None):
                raise KnowledgeBaseServiceError(f"Fallo indexando {file_path}: {op.error}")

            logger.info("Archivo indexado correctamente: %s", file_path)
            return True
        except KnowledgeBaseServiceError:
            raise
        except Exception as e:
            logger.exception("Error en upload_to_file_search_store para %s", file_path)
            raise KnowledgeBaseServiceError(f"No se pudo indexar {file_path} en {store_name}: {e}") from e

    def get_tool_config(self, store_names: list) -> types.Tool:
        """
        Retorna la configuración de la herramienta File Search para generate_content.
        Recibe una lista de nombres de store (resource IDs) — el campo real del SDK
        es `file_search_store_names`, no `file_search_stores`.
        """
        if isinstance(store_names, str):
            store_names = [store_names]
        return types.Tool(
            file_search=types.FileSearch(file_search_store_names=store_names)
        )
