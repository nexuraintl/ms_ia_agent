import pytest
from unittest.mock import patch

from services.faq_repository import FaqRepository, FaqRepositoryError, clean_html


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.executed = (query, params)

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return FakeCursor(self._rows)

    def close(self):
        self.closed = True


def make_repo(monkeypatch):
    monkeypatch.setenv("MARIADB_HOST", "test-host")
    monkeypatch.setenv("MARIADB_USER", "test-user")
    monkeypatch.setenv("MARIADB_PASSWORD", "test-pass")
    return FaqRepository()


class TestCleanHtml:
    def test_quita_tags_y_desescapa_entidades(self):
        html = "<div>Hola <b>mundo</b>&nbsp;con &aacute;cento</div>"
        cleaned = clean_html(html)
        assert "<" not in cleaned
        assert "mundo" in cleaned
        assert "á" in cleaned

    def test_quita_style_y_script_completos(self):
        html = "<style>.x{color:red}</style><p>Texto real</p><script>alert(1)</script>"
        cleaned = clean_html(html)
        assert "color:red" not in cleaned
        assert "alert" not in cleaned
        assert "Texto real" in cleaned

    def test_colapsa_whitespace(self):
        html = "<p>Linea 1</p>\n\n\n<p>Linea 2</p>"
        cleaned = clean_html(html)
        assert "\n\n\n" not in cleaned

    def test_vacio_o_none(self):
        assert clean_html("") == ""
        assert clean_html(None) == ""


class TestFetchFaqs:
    def test_mapea_campos_segun_field_map(self, monkeypatch, faq_rows_sample):
        repo = make_repo(monkeypatch)
        with patch.object(repo, "_connect", return_value=FakeConn(faq_rows_sample)):
            records = list(repo.fetch_faqs())

        assert len(records) == 3
        r1 = records[0]
        assert r1.campos["sintoma"] == "El cliente reporta área de facturación sin PDF."
        assert r1.campos["solucion"] == "Reiniciar el servicio pdf-gen y reintentar."

    def test_detecta_solucion_vacia_tras_limpiar(self, monkeypatch, faq_rows_sample):
        repo = make_repo(monkeypatch)
        with patch.object(repo, "_connect", return_value=FakeConn(faq_rows_sample)):
            records = list(repo.fetch_faqs())

        assert records[0].is_empty_solution() is False
        assert records[1].is_empty_solution() is True  # solo espacios en blanco

    def test_to_text_tiene_delimitadores_y_f_number_en_cabecera(self, monkeypatch, faq_rows_sample):
        repo = make_repo(monkeypatch)
        with patch.object(repo, "_connect", return_value=FakeConn(faq_rows_sample)):
            records = list(repo.fetch_faqs())

        text = records[0].to_text()
        assert text.startswith("=== FAQ 1 (#2024081512000123) ===")
        assert text.rstrip().endswith("=== FIN FAQ 1 ===")
        assert "Reiniciar el servicio pdf-gen" in text

    def test_field_map_invalido_lanza_error(self, monkeypatch, faq_rows_sample):
        monkeypatch.setenv("FAQ_FIELD_MAP", '{"solucion": "f_field99"}')
        repo = make_repo(monkeypatch)
        with pytest.raises(FaqRepositoryError):
            list(repo.fetch_faqs())


class TestConnect:
    def test_sin_credenciales_lanza_error_claro(self, monkeypatch):
        monkeypatch.delenv("MARIADB_HOST", raising=False)
        monkeypatch.delenv("MARIADB_USER", raising=False)
        monkeypatch.delenv("MARIADB_PASSWORD", raising=False)
        repo = FaqRepository()
        with pytest.raises(FaqRepositoryError):
            repo.ping()
