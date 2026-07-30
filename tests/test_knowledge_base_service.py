import pytest
from unittest.mock import MagicMock, patch

from services.knowledge_base_service import KnowledgeBaseService, KnowledgeBaseServiceError


def make_service():
    with patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-key"}):
        with patch("services.knowledge_base_service.genai.Client") as MockClient:
            svc = KnowledgeBaseService()
            svc.client = MockClient.return_value
            return svc


def make_store_mock(display_name, name):
    # OJO: MagicMock(name=...) nombra al mock para debug, no crea un atributo
    # `.name`. Hay que asignarlo después de construir el mock.
    m = MagicMock()
    m.display_name = display_name
    m.name = name
    return m


class TestGetToolConfig:
    def test_emite_file_search_store_names(self):
        svc = make_service()
        tool = svc.get_tool_config(["fileSearchStores/a", "fileSearchStores/b"])
        assert tool.file_search.file_search_store_names == ["fileSearchStores/a", "fileSearchStores/b"]

    def test_nunca_emite_file_search_stores(self):
        # Este es el bug de producción: el campo no existe en el SDK real.
        svc = make_service()
        tool = svc.get_tool_config(["fileSearchStores/a"])
        assert not hasattr(tool.file_search, "file_search_stores")

    def test_acepta_string_unico(self):
        svc = make_service()
        tool = svc.get_tool_config("fileSearchStores/solo-uno")
        assert tool.file_search.file_search_store_names == ["fileSearchStores/solo-uno"]


class TestGetOrCreateStore:
    def test_lanza_en_vez_de_devolver_string_vacio(self):
        svc = make_service()
        svc.client.file_search_stores.list.side_effect = RuntimeError("boom")
        with pytest.raises(KnowledgeBaseServiceError):
            svc.get_or_create_store(display_name="X")

    def test_reutiliza_store_existente_sin_crear(self):
        svc = make_service()
        existing = make_store_mock("Znuny_Tickets_KB", "fileSearchStores/existing")
        svc.client.file_search_stores.list.return_value = [existing]

        result = svc.get_or_create_store(display_name="Znuny_Tickets_KB")

        assert result == "fileSearchStores/existing"
        svc.client.file_search_stores.create.assert_not_called()

    def test_crea_cuando_no_existe(self):
        svc = make_service()
        svc.client.file_search_stores.list.return_value = []
        created = make_store_mock("Nuevo", "fileSearchStores/nuevo")
        svc.client.file_search_stores.create.return_value = created

        result = svc.get_or_create_store(display_name="Nuevo")

        assert result == "fileSearchStores/nuevo"


class TestUploadAndIndexFile:
    def test_polling_sale_cuando_operacion_termina(self, tmp_path):
        svc = make_service()
        file_path = tmp_path / "f.txt"
        file_path.write_text("contenido")

        op_pending = MagicMock(done=False, error=None)
        op_done = MagicMock(done=True, error=None)
        svc.client.file_search_stores.upload_to_file_search_store.return_value = op_pending
        svc.client.operations.get.return_value = op_done

        with patch("services.knowledge_base_service.time.sleep"):
            ok = svc.upload_and_index_file("fileSearchStores/s", str(file_path), poll_interval_s=0)

        assert ok is True

    def test_lanza_en_timeout(self, tmp_path):
        svc = make_service()
        file_path = tmp_path / "f.txt"
        file_path.write_text("contenido")

        op_pending = MagicMock(done=False, error=None)
        svc.client.file_search_stores.upload_to_file_search_store.return_value = op_pending
        svc.client.operations.get.return_value = op_pending  # nunca termina

        times = iter([0, 1, 1000])
        with patch("services.knowledge_base_service.time.sleep"), \
             patch("services.knowledge_base_service.time.monotonic", side_effect=lambda: next(times, 1000)):
            with pytest.raises(KnowledgeBaseServiceError):
                svc.upload_and_index_file("fileSearchStores/s", str(file_path), timeout_s=5, poll_interval_s=0)

    def test_construye_custom_metadata_y_chunking_config(self, tmp_path):
        svc = make_service()
        file_path = tmp_path / "f.txt"
        file_path.write_text("contenido")
        op_done = MagicMock(done=True, error=None)
        svc.client.file_search_stores.upload_to_file_search_store.return_value = op_done

        svc.upload_and_index_file(
            "fileSearchStores/s", str(file_path),
            custom_metadata={"source": "faq_sync"},
            chunk_size_tokens=512, chunk_overlap_tokens=64,
        )

        _, kwargs = svc.client.file_search_stores.upload_to_file_search_store.call_args
        config = kwargs["config"]
        assert config.custom_metadata[0].key == "source"
        assert config.custom_metadata[0].string_value == "faq_sync"
        assert config.chunking_config.white_space_config.max_tokens_per_chunk == 512
        assert config.chunking_config.white_space_config.max_overlap_tokens == 64


class TestPruneVersionedStores:
    def test_conserva_los_keep_mas_recientes(self):
        svc = make_service()
        stores = [
            make_store_mock("Znuny_FAQ_KB_20260101T000000Z", "a"),
            make_store_mock("Znuny_FAQ_KB_20260103T000000Z", "c"),
            make_store_mock("Znuny_FAQ_KB_20260102T000000Z", "b"),
            make_store_mock("Otro_Store", "z"),
        ]
        svc.client.file_search_stores.list.return_value = stores

        deleted = svc.prune_versioned_stores("Znuny_FAQ_KB_", keep=2)

        assert deleted == ["a"]
        svc.client.file_search_stores.delete.assert_called_once_with(name="a", config={"force": True})

    def test_resolve_active_store_devuelve_el_mas_reciente(self):
        svc = make_service()
        stores = [
            make_store_mock("Znuny_FAQ_KB_20260101T000000Z", "a"),
            make_store_mock("Znuny_FAQ_KB_20260103T000000Z", "c"),
            make_store_mock("Znuny_FAQ_KB_20260102T000000Z", "b"),
        ]
        svc.client.file_search_stores.list.return_value = stores

        assert svc.resolve_active_store("Znuny_FAQ_KB_") == "c"

    def test_resolve_active_store_none_si_no_hay_matches(self):
        svc = make_service()
        svc.client.file_search_stores.list.return_value = []
        assert svc.resolve_active_store("Znuny_FAQ_KB_") is None
