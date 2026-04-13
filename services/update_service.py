import os
import json
import time
import logging
import requests
import datetime
from typing import Optional, Dict, Any, Union
# Importamos el modelo de datos para el tipado
from .agent_service import AgentService, TicketDiagnosisResponse
from .knowledge_base_service import KnowledgeBaseService

logger = logging.getLogger(__name__)

class ZnunyService:
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

        ticket_text = self._extract_relevant_text(articles)

        # B. Diagnóstico con IA (Aquí usamos el nuevo objeto Pydantic)
        tool_config = self._get_rag_tool_config()
        ai: TicketDiagnosisResponse = self.agent_service.diagnose_ticket(ticket_text, tool_config)
        
        diagnosis_body = ai.diagnostico
        type_id = ai.type_id

        # C. Lógica de Servicios Externos (Multimodal / Logs)
        if ai.requires_visual:
            visual = self._call_multimodal_service(ticket_id, ticket_text)
            if visual:
                diagnosis_body = visual["diagnosis"]
                type_id = visual["type_id"] or type_id

        if type_id == 10: # Incidente
            client_info = self.agent_service.extract_client_info(metadata, ticket_text)
            incident_payload = self._build_incident_data(ticket_id, metadata, diagnosis_body, type_id, client_info, ticket_text)
            log_summary = self._notify_log_monitor(incident_payload)
            if log_summary:
                diagnosis_body += f"\n\n─ ANALISIS TÉCNICO DE LOGS ─\n{log_summary}"

        # D. Lógica de Emergencia
        if ai.criticality_score >= 9 or ai.is_security_alert:
            diagnosis_body = "🚨 [ALERTA CRÍTICA] PROTOCOLO DE EMERGENCIA ACTIVADO\n" + diagnosis_body

        # E. Update Final
        return self.update_ticket(
            ticket_id=ticket_id,
            session_id=session_id,
            title=metadata.get("Title"),
            user=metadata.get("CustomerUserID"),
            queue_id=metadata.get("QueueID", 9),
            priority_id=metadata.get("PriorityID", 3),
            state_id=metadata.get("StateID", 1),
            subject="Diagnóstico Automático Nexura IA",
            body=f"[Procesado por: mod_agentes]\n\n{diagnosis_body}"
        )

    # --- 3. MÉTODOS DE APOYO ---

    def _fetch_all_articles(self, ticket_id, session_id):
        url = f"{self.base_url}/Ticket/{ticket_id}?SessionID={session_id}&AllArticles=1"
        try:
            r = requests.get(url, timeout=10)
            data = r.json().get("Ticket")
            return data[0].get("Article", []) if data else []
        except: return []

    def _extract_relevant_text(self, articles):
        valid = [a for a in articles if a.get("SenderType") != "system"]
        last = valid[-1] if valid else articles[0]
        return f"Subject: {last.get('Subject')}\nBody: {last.get('Body')}"

    def update_ticket(self, **kwargs):
        url = f"{self.base_url}/Ticket/{kwargs['ticket_id']}"
        
        # Construimos el payload siguiendo estrictamente el Manual Técnico
        payload = {
            "SessionID": kwargs['session_id'],
            "Ticket": {
                "Title": kwargs['title'],
                "PriorityID": kwargs['priority_id'],
                "StateID": kwargs['state_id']
            },
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
            # El .yml confirma que es PATCH
            r = requests.patch(url, json=payload, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Error al actualizar ticket en Znuny: {e}")
            return {"status": "error", "message": str(e)}

    def _call_multimodal_service(self, tid, txt):
        url = os.environ.get("MULTIMODAL_URL")
        try:
            r = requests.post(f"{url}/diagnose", json={"ticket_id": str(tid), "ticket_text": txt}, timeout=120)
            return r.json()
        except: return None

    def _notify_log_monitor(self, data):
        url = os.environ.get("LOG_MONITOR_URL")
        try:
            r = requests.post(f"{url}/analyze-incident", json=data, timeout=15)
            r.raise_for_status()
            return r.json().get("mensaje_resumen")
        except requests.exceptions.Timeout:
            logger.warning("⏳ El monitor de logs superó los 15s (anormal según métricas).")
            return "Análisis de logs omitido por latencia."
        except Exception as e:
            logger.error(f"❌ Error en Monitor de Logs: {e}")
            return None

    def _get_rag_tool_config(self):
        try:
            store = self.kb_service.get_or_create_store(display_name="Znuny_Tickets_KB")
            return self.kb_service.get_tool_config(store)
        except: return None

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