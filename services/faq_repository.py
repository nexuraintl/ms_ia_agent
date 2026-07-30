import re
import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterator, Optional

import pymysql
import pymysql.cursors

from config import obtener_configuracion

logger = logging.getLogger(__name__)

ALLOWED_FAQ_FIELDS = {f"f_field{i}" for i in range(1, 7)}
# Por convención, FAQ_FIELD_MAP debe incluir la clave "solucion" — es la que
# decide si una FAQ se omite del corpus por no tener solución documentada.
SOLUTION_KEY = "solucion"


class FaqRepositoryError(Exception):
    """Error irrecuperable consultando el módulo FAQ de Znuny."""
    pass


class _HtmlTextExtractor(HTMLParser):
    """Extrae solo el texto visible, descartando tags, <style> y <script>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def clean_html(raw: str) -> str:
    """Quita tags/entidades y colapsa whitespace. Vacío o None -> ''."""
    if not raw:
        return ""
    parser = _HtmlTextExtractor()
    parser.feed(raw)
    text = parser.get_text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


@dataclass
class FaqRecord:
    id: int
    f_number: str
    subject: str
    keywords: Optional[str]
    categoria: str
    visibilidad: str
    idioma: str
    changed: str
    campos: dict  # nombre semántico (de FAQ_FIELD_MAP) -> texto ya limpio

    def is_empty_solution(self) -> bool:
        return not (self.campos.get(SOLUTION_KEY) or "").strip()

    def to_text(self) -> str:
        lines = [f"=== FAQ {self.id} (#{self.f_number}) ==="]
        lines.append(f"Categoría: {self.categoria}")
        lines.append(f"Título: {self.subject}")
        if self.keywords:
            lines.append(f"Palabras clave: {self.keywords}")
        for label, value in self.campos.items():
            if value:
                lines.append(f"{label.capitalize()}: {value}")
        lines.append(f"=== FIN FAQ {self.id} ===")
        return "\n".join(lines)


class FaqRepository:
    """
    Lee el módulo FAQ nativo de Znuny (tabla faq_item) desde el MariaDB de
    producción del ticketing. Solo lectura, solo tablas faq_*.
    """

    def __init__(self):
        self.settings = obtener_configuracion()

    def _connect(self):
        if not all([self.settings.mariadb_host, self.settings.mariadb_user, self.settings.mariadb_password]):
            raise FaqRepositoryError("Faltan credenciales de MariaDB (MARIADB_HOST/USER/PASSWORD)")

        connect_kwargs = dict(
            host=self.settings.mariadb_host,
            port=self.settings.mariadb_port,
            user=self.settings.mariadb_user,
            password=self.settings.mariadb_password,
            database=self.settings.mariadb_database,
            charset="utf8mb4",
            connect_timeout=self.settings.mariadb_connect_timeout,
            cursorclass=pymysql.cursors.SSDictCursor,
        )
        if self.settings.mariadb_ssl_ca:
            connect_kwargs["ssl"] = {"ca": self.settings.mariadb_ssl_ca}

        try:
            return pymysql.connect(**connect_kwargs)
        except Exception as e:
            logger.exception("Error conectando a MariaDB")
            raise FaqRepositoryError(f"No se pudo conectar a MariaDB: {e}") from e

    def ping(self) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        finally:
            conn.close()

    def describe(self) -> dict:
        """Conteo de FAQs válidas/aprobadas por visibilidad. Smoke test de conectividad."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT st.name AS visibilidad, COUNT(*) AS total
                      FROM faq_item i
                      JOIN faq_state      s  ON s.id  = i.state_id
                      JOIN faq_state_type st ON st.id = s.type_id
                     WHERE i.valid_id = 1 AND i.approved = 1
                     GROUP BY st.name
                    """
                )
                rows = cur.fetchall()
            return {row["visibilidad"]: row["total"] for row in rows}
        finally:
            conn.close()

    def _field_columns(self) -> dict:
        """nombre_semantico -> f_fieldN, validado contra las columnas reales de faq_item."""
        mapping = self.settings.faq_field_map
        invalid = {v for v in mapping.values() if v not in ALLOWED_FAQ_FIELDS}
        if invalid:
            raise FaqRepositoryError(f"FAQ_FIELD_MAP referencia columnas no soportadas: {invalid}")
        return mapping

    def fetch_faqs(self) -> Iterator[FaqRecord]:
        """
        Streaming (SSDictCursor) para no materializar el corpus dos veces en memoria.
        La visibilidad va por placeholder %s: no hay interpolación de identificadores
        salvo los nombres de columna f_fieldN, que vienen de un whitelist fijo, no de
        entrada externa.
        """
        field_map = self._field_columns()
        field_columns = sorted(set(field_map.values()))
        select_fields = ", ".join(f"i.{col}" for col in field_columns)
        visibility_placeholders = ", ".join(["%s"] * len(self.settings.faq_visibility))

        query = f"""
            SELECT i.id, i.f_number, i.f_subject, i.f_keywords, i.content_type, i.changed,
                   {select_fields},
                   c.name AS categoria, st.name AS visibilidad, l.name AS idioma
              FROM faq_item i
              JOIN faq_category   c  ON c.id  = i.category_id
              JOIN faq_state      s  ON s.id  = i.state_id
              JOIN faq_state_type st ON st.id = s.type_id
              JOIN faq_language   l  ON l.id  = i.f_language_id
             WHERE i.valid_id = 1 AND i.approved = 1
               AND st.name IN ({visibility_placeholders})
             ORDER BY i.changed DESC
        """
        params = list(self.settings.faq_visibility)
        if self.settings.faq_max_rows:
            query += " LIMIT %s"
            params.append(self.settings.faq_max_rows)

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                for row in cur:
                    is_html = (row.get("content_type") or "").startswith("text/html")
                    campos = {}
                    for semantic_name, column in field_map.items():
                        raw_value = row.get(column) or ""
                        campos[semantic_name] = clean_html(raw_value) if is_html else raw_value.strip()

                    yield FaqRecord(
                        id=row["id"],
                        f_number=row["f_number"],
                        subject=row.get("f_subject") or "",
                        keywords=row.get("f_keywords"),
                        categoria=row.get("categoria") or "",
                        visibilidad=row.get("visibilidad") or "",
                        idioma=row.get("idioma") or "",
                        changed=str(row.get("changed")),
                        campos=campos,
                    )
        finally:
            conn.close()
