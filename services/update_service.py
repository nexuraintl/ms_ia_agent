import os
import json
import time
import logging
import requests
import datetime
from typing import Optional, Dict, Any, Union
# Importamos el modelo de datos para el tipado
from .agent_service import AgentService, TicketDiagnosisResponse, TicketClassification
from .knowledge_base_service import KnowledgeBaseService, KnowledgeBaseServiceError
from config import obtener_configuracion
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_auth_requests

logger = logging.getLogger(__name__)

# Los servicios complementarios (multimodal, monitor de logs) son Cloud Run
# privados: solo aceptan tráfico interno y exigen un ID token OIDC cuya audience
# es la URL base del servicio. Se reutiliza un único Request() para el metadata
# server, igual que en utils/admin_auth.py.
_oidc_request = google_auth_requests.Request()

class ZnunyService:
    # 10: Incidente, 14: Requerimiento, 19: Petición. Se validan antes de
    # mandarlos a Znuny: el type_id del diagnóstico lo redacta el modelo.
    TIPOS_VALIDOS = {10, 14, 19}
    TIPOS_NOMBRES = {10: "Incidente", 14: "Requerimiento", 19: "Petición"}

    SYSTEM_PATTERNS = [
        "La solicitud ha sido registrada",
        "Cordial saludo",
        "información adicional ingresando a la Plataforma de seguimiento",
        "Este correo electrónico y su contenido son para el uso exclusivo",
        "ha sido registrado en la mesa de servicios"
    ]

    def __init__(self):
        self.base_url = os.environ.get("ZNUNY_BASE_API", "").rstrip("/")
        self.username = os.environ.get("ZNUNY_USERNAME")
        self.password = os.environ.get("ZNUNY_PASSWORD")
        self.session_ttl = int(os.environ.get("ZNUNY_SESSION_TTL", "3300"))
        self._cached_session_id: Optional[str] = None
        self._cached_session_ts: float = 0.0
        self._agent_service: Optional[AgentService] = None
        self._kb_service: Optional[KnowledgeBaseService] = None
        self._cached_tool_config = None
        self._cached_tool_config_ts: float = 0.0

    @property
    def agent_service(self) -> AgentService:
        if self._agent_service is None:
            self._agent_service = AgentService()
        return self._agent_service

    @property
    def kb_service(self) -> KnowledgeBaseService:
        if self._kb_service is None:
            self._kb_service = KnowledgeBaseService()
        return self._kb_service

    # --- 1. MÉTODOS DE SESIÓN Y METADATA ---

    def _login_create_session(self) -> str:
        if not all([self.username, self.password, self.base_url]):
            raise ValueError("Missing environment variables for Znuny Auth")
        url = f"{self.base_url}/Session"
        payload = {"UserLogin": self.username, "Password": self.password}
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "mod_agentes/1.0",
        }
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            sid = resp.json().get("SessionID")
            if not sid: raise RuntimeError("No SessionID in response")
            return sid
        except Exception as e:
            logger.error(f"Error login Znuny: {e}")
            raise

    def get_or_create_session_id(self) -> str:
        env_sid = os.environ.get("ZNUNY_SESSION_ID") or os.environ.get("SESSION_ID")
        if env_sid: return env_sid
        now = time.time()
        if self._cached_session_id and (now - self._cached_session_ts) < self.session_ttl:
            return self._cached_session_id
        self._cached_session_id = self._login_create_session()
        self._cached_session_ts = now
        return self._cached_session_id

    def get_ticket_metadata(self, ticket_id: int, session_id: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/Ticket/{ticket_id}?SessionID={session_id}"
        try:
            r = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
            r.raise_for_status()
            ticket = r.json().get("Ticket")
            if isinstance(ticket, list): ticket = ticket[0]
            return ticket # Retornamos el objeto crudo de Znuny para mayor flexibilidad
        except Exception as e:
            logger.error(f"Error metadata ticket {ticket_id}: {e}")
            return None

    # --- 2. LÓGICA DE ORQUESTACIÓN  ---

    def diagnose_and_update_ticket(self, ticket_id: int, session_id: str = None, data: dict = None) -> Dict[str, Any]:
        data = data or {}
        if not session_id: session_id = self.get_or_create_session_id()

        # A. Obtener Info y Validar Estado
        metadata = self.get_ticket_metadata(ticket_id, session_id)
        if not metadata or metadata.get("State") != "Nuevo":
            return {"skipped": True, "reason": "Ticket no es 'Nuevo'"}

        articles = self._fetch_all_articles(ticket_id, session_id)
        if len(articles) > 2:
            return {"skipped": True, "reason": "Ticket con más de 2 artículos"}
        if not articles:
            return {"skipped": True, "reason": "No se pudieron obtener artículos del ticket"}

        ticket_text = self._extract_relevant_text(articles)

        # B. Diagnóstico con IA (Aquí usamos el nuevo objeto Pydantic)
        tool_config = self._get_rag_tool_config()

        # El RAG ayuda a decidir si es Diseño, Incidente o Consulta General
        classification: TicketClassification = self.agent_service.classify_and_route(ticket_text, tool_config)
        
        insumos_especialistas = ""
        final_type_id = classification.type_id

        # C. Lógica de Servicios Externos (Multimodal / Logs)

        # Ruta 1: Diseño (Multimodal)
        if classification.category == "diseño" or classification.requires_visual:
            logger.info(f"🎨 Delegando a Multimodal por categoría: {classification.category}")
            visual_data = self._call_multimodal_service(ticket_id, ticket_text)
            if visual_data:
                # Extraemos el diagnóstico técnico del multimodal como insumo
                insumos_especialistas += f"\n[INSUMO VISUAL]: {visual_data.get('diagnosis', visual_data.get('diagnostico'))}"
                final_type_id = visual_data.get("type_id") or final_type_id

        # Ruta 2: Incidente / Crítico (Log Errors)
        if classification.category == "incidente" or classification.is_critical:
            logger.info(f"🛠️ Delegando a Log Errors por criticidad/categoría")
            client_info = self.agent_service.extract_client_info(metadata, ticket_text)
            # Preparamos un payload preliminar para el monitor de logs
            incident_payload = self._build_incident_data(
                ticket_id, metadata, "Análisis en curso", final_type_id, client_info, ticket_text
            )
            log_summary = self._notify_log_monitor(incident_payload)
            if log_summary:
                insumos_especialistas += f"\n[INSUMO TÉCNICO LOGS]: {log_summary}"

        # D. GENERACIÓN DE REPORTE FINAL (UNIFICACIÓN CON RAG)
        reporte: TicketDiagnosisResponse = self.agent_service.generate_final_report(
            ticket_text, insumos_especialistas, tool_config
        )

        # El diagnóstico final es la última palabra sobre el tipo porque ya vio
        # los insumos de los especialistas; si no devuelve uno válido se conserva
        # el de la clasificación inicial.
        if reporte.type_id in self.TIPOS_VALIDOS:
            final_type_id = reporte.type_id

        # Kill switch: la gestión de tipos en Znuny estuvo deshabilitada meses
        # por problemas previos con la asignación automática. Con la bandera en
        # false se sigue calculando el tipo (para el texto del diagnóstico),
        # pero no se manda TypeID a Znuny.
        settings = obtener_configuracion()
        type_id_a_enviar = final_type_id if settings.ticket_type_enabled else None

        # Aplicar prefijo de emergencia si es necesario
        diagnosis_body = reporte.diagnostico
        if classification.is_critical:
            diagnosis_body = "🚨 [ALERTA CRÍTICA] PROTOCOLO DE EMERGENCIA ACTIVADO\n" + diagnosis_body

        # Siempre se agrega el tipo en texto plano, esté o no habilitado el
        # envío del TypeID: si la asignación automática está apagada, es la
        # única señal que le queda a mesa de servicios para asignarlo a mano.
        tipo_nombre = self.TIPOS_NOMBRES.get(final_type_id, str(final_type_id))
        diagnosis_body = f"{diagnosis_body}\n\nTipo de ticket: {tipo_nombre}"

        logger.info(
            "Ticket %s: categoría=%s type_id=%s crítico=%s asignación_automática=%s",
            ticket_id, classification.category, final_type_id, classification.is_critical,
            settings.ticket_type_enabled
        )

        # E. Update Final
        return self.update_ticket(
            ticket_id=ticket_id,
            session_id=session_id,
            title=metadata.get("Title"),
            user=metadata.get("CustomerUserID"),
            queue_id=metadata.get("QueueID", 9),
            priority_id=metadata.get("PriorityID", 3),
            state_id=metadata.get("StateID", 1),
            type_id=type_id_a_enviar,
            subject="Diagnóstico Automático Nexura IA",
            body=f"[Procesado por: mod_agentes]\n\n{diagnosis_body}"
        )

    # --- 3. MÉTODOS DE APOYO ---

    def _fetch_all_articles(self, ticket_id, session_id):
        url = f"{self.base_url}/Ticket/{ticket_id}?SessionID={session_id}&AllArticles=1"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json().get("Ticket")
            return data[0].get("Article", []) if data else []
        except Exception:
            # Antes se tragaba silenciosamente cualquier error (timeout, HTTP
            # de error, JSON malformado) y devolvía [] indistinguible de un
            # ticket que de verdad no tiene artículos. Ahora queda logueado
            # con traceback: sin esto, una falla de Znuny terminaba
            # apareciendo como un IndexError críptico más abajo.
            logger.exception("Error obteniendo artículos del ticket %s", ticket_id)
            return []

    def _extract_relevant_text(self, articles):
        if not articles:
            return ""
        valid = [a for a in articles if a.get("SenderType") != "system"]
        last = valid[-1] if valid else articles[0]
        return f"Subject: {last.get('Subject')}\nBody: {last.get('Body')}"

    def update_ticket(self, **kwargs):
        url = f"{self.base_url}/Ticket/{kwargs['ticket_id']}"
        
        # Construimos el payload siguiendo estrictamente el Manual Técnico
        ticket_fields = {
            "Title": kwargs['title'],
            "PriorityID": kwargs['priority_id'],
            "StateID": kwargs['state_id']
        }

        # Sin TypeID el triaje no se aplica: el ticket queda con el tipo que
        # traía y toda la clasificación se pierde.
        if kwargs.get('type_id'):
            ticket_fields["TypeID"] = kwargs['type_id']

        payload = {
            "SessionID": kwargs['session_id'],
            "Ticket": ticket_fields,
            "Article": {
                "Subject": kwargs['subject'],
                "Body": kwargs['body'],
                "ContentType": "text/plain; charset=utf8",
                "MimeType": "text/plain",
                "Charset": "utf8",
                "SenderType": "system",
                "HistoryType": "OwnerUpdate",
                "HistoryComment": "Diagnóstico generado por IA"
            }
        }
        
        try:

            r = requests.patch(url, json=payload, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Error al actualizar ticket en Znuny: {e}")
            return {"status": "error", "message": str(e)}

    def _oidc_headers(self, audience: str) -> Dict[str, str]:
        """Cabecera Authorization con un ID token OIDC para invocar un Cloud Run
        privado. `audience` es la URL base del servicio destino (sin path). Si la
        emisión falla se devuelve vacío y la llamada seguirá su curso: fallará
        con 401/403 en destino y el motivo queda en el log."""
        audience = (audience or "").rstrip("/")
        if not audience:
            return {}
        try:
            token = google_id_token.fetch_id_token(_oidc_request, audience)
            return {"Authorization": f"Bearer {token}"}
        except Exception as e:
            logger.warning("No se pudo obtener ID token para %s: %s", audience, e)
            return {}

    def _call_multimodal_service(self, tid, txt):
        url = os.environ.get("MULTIMODAL_URL")
        base = (url or "").rstrip("/")
        try:
            r = requests.post(f"{base}/diagnose", json={"ticket_id": str(tid), "ticket_text": txt}, headers=self._oidc_headers(base), timeout=120)
            return r.json()
        except: return None

    def _notify_log_monitor(self, data):
        url = os.environ.get("LOG_MONITOR_URL")
        base = (url or "").rstrip("/")
        try:
            # El endpoint hace SSH + grep remoto + uno o varios diagnósticos
            # Gemini; 15s se quedaba corto y el resultado se descartaba.
            r = requests.post(f"{base}/analyze-incident", json=data, headers=self._oidc_headers(base), timeout=45)
            r.raise_for_status()
            return r.json().get("mensaje_resumen")
        except requests.exceptions.Timeout:
            logger.warning("⏳ El monitor de logs superó los 45s; se omite el insumo de logs.")
            return "Análisis de logs omitido por latencia."
        except Exception as e:
            logger.error(f"❌ Error en Monitor de Logs: {e}")
            return None

    def _get_rag_tool_config(self):
        """
        Resuelve el store de FAQs de Znuny y construye el tool_config.
        No crea nada en el camino caliente del ticket: si el store no existe,
        simplemente se continúa sin recuperación. Cacheado con TTL porque este
        método corre en cada ticket y resolver el store implica listar por red.
        """
        settings = obtener_configuracion()
        if not settings.rag_enabled:
            return None

        now = time.time()
        if self._cached_tool_config is not None and (now - self._cached_tool_config_ts) < settings.rag_store_cache_ttl_seconds:
            return self._cached_tool_config

        try:
            faq_store = self.kb_service.resolve_active_store(settings.faq_store_prefix)
            if not faq_store:
                logger.warning("RAG: no hay store de FAQs; se continúa sin recuperación")
                self._cached_tool_config = None
                self._cached_tool_config_ts = now
                return None

            logger.info("RAG activo con store de FAQs: %s", faq_store)
            tool_config = self.kb_service.get_tool_config([faq_store])
            self._cached_tool_config = tool_config
            self._cached_tool_config_ts = now
            return tool_config
        except KnowledgeBaseServiceError:
            logger.exception("RAG: fallo construyendo tool config")
            return None
        except Exception:
            logger.exception("RAG: fallo inesperado resolviendo el store de FAQs")
            return None

    def _build_incident_data(self, tid, meta, diag, type_id, client, txt):
        return {
            "ticket_id": str(tid),
            "title": meta.get("Title"),
            "type_id": type_id,
            "diagnostico": diag,
            "ticket_text": txt,
            "entity": client.get("entidad", "No identificado"),
            "processed_at": datetime.datetime.utcnow().isoformat() + "Z"
        }