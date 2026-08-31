"""
Configuración del pipeline de RAG (FAQs de Znuny).
Se introduce de forma aditiva: el resto del servicio sigue leyendo
variables de entorno directamente con os.environ.get.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Configuracion(BaseSettings):
    """Configuración del pipeline de RAG cargada desde variables de entorno."""

    model_config = SettingsConfigDict(env_file="env_vars/.env", env_file_encoding="utf-8", extra="ignore")

    # RAG general
    rag_enabled: bool = Field(True, alias="RAG_ENABLED")
    rag_store_cache_ttl_seconds: int = Field(300, alias="RAG_STORE_CACHE_TTL_SECONDS")

    # Sync de FAQs (MariaDB -> File Search Store)
    faq_store_prefix: str = Field("Znuny_FAQ_KB_", alias="FAQ_STORE_PREFIX")
    faq_sync_keep_versions: int = Field(2, alias="FAQ_SYNC_KEEP_VERSIONS")
    faq_shard_size: int = Field(200, alias="FAQ_SHARD_SIZE")
    faq_max_rows: int | None = Field(None, alias="FAQ_MAX_ROWS")
    faq_field_map: dict[str, str] = Field(
        default={"sintoma": "f_field1", "problema": "f_field2", "solucion": "f_field3", "comentario": "f_field6"},
        alias="FAQ_FIELD_MAP",
    )
    faq_visibility: list[str] = Field(default=["internal", "external"], alias="FAQ_VISIBILITY")

    # MariaDB (BD de producción de Znuny, solo lectura sobre faq_*)
    mariadb_host: str | None = Field(None, alias="MARIADB_HOST")
    mariadb_port: int = Field(3306, alias="MARIADB_PORT")
    mariadb_user: str | None = Field(None, alias="MARIADB_USER")
    mariadb_password: str | None = Field(None, alias="MARIADB_PASSWORD")
    mariadb_database: str = Field("znuny", alias="MARIADB_DATABASE")
    mariadb_ssl_ca: str | None = Field(None, alias="MARIADB_SSL_CA")
    mariadb_connect_timeout: int = Field(10, alias="MARIADB_CONNECT_TIMEOUT")

    # Endpoint admin
    admin_oidc_service_account: str | None = Field(None, alias="ADMIN_OIDC_SERVICE_ACCOUNT")
    admin_oidc_audience: str | None = Field(None, alias="ADMIN_OIDC_AUDIENCE")
    admin_sync_token: str | None = Field(None, alias="ADMIN_SYNC_TOKEN")


@lru_cache()
def obtener_configuracion() -> Configuracion:
    """Obtiene la configuración del pipeline de RAG. Cacheada para no releer el .env en cada llamada."""
    return Configuracion()
