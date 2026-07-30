import pytest
from unittest.mock import MagicMock, patch

from services.faq_repository import FaqRecord
from services.faq_sync_service import FaqSyncService, _sync_lock


def make_record(id_, solucion="Solución válida"):
    return FaqRecord(
        id=id_, f_number=f"F{id_}", subject="s", keywords=None,
        categoria="c", visibilidad="external", idioma="es", changed="2026-01-01",
        campos={"sintoma": "x", "problema": "y", "solucion": solucion, "comentario": ""},
    )


def make_service(monkeypatch):
    monkeypatch.setenv("MARIADB_HOST", "x")
    monkeypatch.setenv("MARIADB_USER", "x")
    monkeypatch.setenv("MARIADB_PASSWORD", "x")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-not-used")
    with patch("services.faq_sync_service.KnowledgeBaseService"):
        return FaqSyncService()


def with_successful_smoke_test(svc):
    grounding = MagicMock(grounding_chunks=["chunk1"])
    candidate = MagicMock(grounding_metadata=grounding)
    svc.kb_service.client.models.generate_content.return_value = MagicMock(candidates=[candidate])


class TestZeroRowsGuard:
    """Es la línea más importante del archivo: sin filas, no se crea store ni se hace prune."""

    def test_cero_filas_no_crea_store(self, monkeypatch):
        svc = make_service(monkeypatch)
        with patch("services.faq_sync_service.FaqRepository") as MockRepo:
            MockRepo.return_value.fetch_faqs.return_value = iter([])
            result = svc.run()

        assert result.status == "failed"
        svc.kb_service.get_or_create_store.assert_not_called()

    def test_todas_sin_solucion_no_crea_store(self, monkeypatch):
        svc = make_service(monkeypatch)
        with patch("services.faq_sync_service.FaqRepository") as MockRepo:
            MockRepo.return_value.fetch_faqs.return_value = iter(
                [make_record(1, solucion=""), make_record(2, solucion="   ")]
            )
            result = svc.run()

        assert result.status == "failed"
        assert result.rows_skipped == 2
        svc.kb_service.get_or_create_store.assert_not_called()


class TestSyncExitoso:
    def test_shards_y_prune(self, monkeypatch):
        svc = make_service(monkeypatch)
        svc.settings.faq_shard_size = 2
        svc.kb_service.get_or_create_store.return_value = "fileSearchStores/new_v1"
        svc.kb_service.prune_versioned_stores.return_value = ["old_v0"]
        with_successful_smoke_test(svc)

        with patch("services.faq_sync_service.FaqRepository") as MockRepo:
            MockRepo.return_value.fetch_faqs.return_value = iter([make_record(i) for i in range(1, 6)])
            result = svc.run()

        assert result.status == "success"
        assert result.rows_fetched == 5
        assert result.rows_skipped == 0
        assert result.shards_uploaded == 3  # ceil(5/2)
        assert svc.kb_service.upload_and_index_file.call_count == 3
        svc.kb_service.prune_versioned_stores.assert_called_once()

    def test_omite_y_cuenta_faqs_sin_solucion(self, monkeypatch):
        svc = make_service(monkeypatch)
        svc.kb_service.get_or_create_store.return_value = "fileSearchStores/new_v1"
        with_successful_smoke_test(svc)

        with patch("services.faq_sync_service.FaqRepository") as MockRepo:
            MockRepo.return_value.fetch_faqs.return_value = iter(
                [make_record(1), make_record(2, solucion=""), make_record(3)]
            )
            result = svc.run()

        assert result.status == "success"
        assert result.rows_fetched == 3
        assert result.rows_skipped == 1


class TestFalloYLimpieza:
    def test_fallo_en_smoke_test_borra_store_nuevo_y_omite_prune(self, monkeypatch):
        svc = make_service(monkeypatch)
        svc.kb_service.get_or_create_store.return_value = "fileSearchStores/new_v2"
        candidate = MagicMock(grounding_metadata=None)
        svc.kb_service.client.models.generate_content.return_value = MagicMock(candidates=[candidate])

        with patch("services.faq_sync_service.FaqRepository") as MockRepo:
            MockRepo.return_value.fetch_faqs.return_value = iter([make_record(1)])
            result = svc.run()

        assert result.status == "failed"
        svc.kb_service.delete_store.assert_called_once_with("fileSearchStores/new_v2")
        svc.kb_service.prune_versioned_stores.assert_not_called()

    def test_fallo_durante_upload_borra_store_nuevo(self, monkeypatch):
        svc = make_service(monkeypatch)
        svc.kb_service.get_or_create_store.return_value = "fileSearchStores/new_v3"
        svc.kb_service.upload_and_index_file.side_effect = RuntimeError("fallo de red")

        with patch("services.faq_sync_service.FaqRepository") as MockRepo:
            MockRepo.return_value.fetch_faqs.return_value = iter([make_record(1)])
            result = svc.run()

        assert result.status == "failed"
        svc.kb_service.delete_store.assert_called_once_with("fileSearchStores/new_v3")
        svc.kb_service.prune_versioned_stores.assert_not_called()


class TestInvocacionConcurrente:
    def test_devuelve_skipped_si_ya_hay_una_corrida(self, monkeypatch):
        svc = make_service(monkeypatch)
        _sync_lock.acquire()
        try:
            result = svc.run()
        finally:
            _sync_lock.release()

        assert result.status == "skipped"
