import os
import json
import time
import logging
import requests
import datetime
from typing import Optional, Dict, Any, Union
from concurrent.futures import ThreadPoolExecutor
from .agent_service import AgentService
from .knowledge_base_service import KnowledgeBaseService

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class ZnunyService:
    """
    Service to interact with Znuny API, handling authentication,
    ticket retrieval, and updates.
    """

    # Patrones de notificaciones automáticas que deben ignorar
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
        
        # Lazy initialization of AgentService to avoid import errors at module level
        self._agent_service: Optional[AgentService] = None
        self._kb_service: Optional[KnowledgeBaseService] = None
        
        # ThreadPoolExecutor for async incident processing
        self._executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="incident_")

    @property
    def kb_service(self) -> KnowledgeBaseService:
        if self._kb_service is None:
            self._kb_service = KnowledgeBaseService()
        return self._kb_service

    @property
    def agent_service(self) -> AgentService:
        if self._agent_service is None:
            try:
                self._agent_service = AgentService()
            except ImportError as e:
                logger.error(f"Failed to load AgentService: {e}")
                raise RuntimeError(f"Failed to load AgentService: {e}")
        return self._agent_service

    def _login_create_session(self) -> str:
        """Creates a new SessionID by authenticating against Znuny."""
        if not all([self.username, self.password, self.base_url]):
            raise ValueError("Missing required environment variables: ZNUNY_USERNAME, ZNUNY_PASSWORD, or ZNUNY_BASE_API")

        url = f"{self.base_url}/Session"
        payload = {"UserLogin": self.username, "Password": self.password}
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "mod_agentes/1.0",
        }

        try:
            resp = requests.patch(
                url,
                data=json.dumps(payload),
                headers=headers,
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            
            session_id = data.get("SessionID")
            if not session_id:
                raise RuntimeError(f"Znuny did not return SessionID. Response: {data}")

            return session_id
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Connection error during authentication: {e}")
            raise RuntimeError(f"Connection error during authentication: {e}")

    def get_or_create_session_id(self) -> str:
        """Retrieves or generates a valid SessionID, using memory cache."""
        # Check environment variable override
        env_sid = os.environ.get("ZNUNY_SESSION_ID") or os.environ.get("SESSION_ID")
        if env_sid:
            return env_sid

        now = time.time()
        if self._cached_session_id and (now - self._cached_session_ts) < self.session_ttl:
            return self._cached_session_id

        logger.info("Creating new Znuny session...")
        self._cached_session_id = self._login_create_session()
        self._cached_session_ts = now
        return self._cached_session_id

    def get_ticket_metadata(self, ticket_id: int, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene la metadata completa de un ticket, incluyendo cliente.
        """
        url = f"{self.base_url}/Ticket/{ticket_id}?SessionID={session_id}"
        
        try:
            r = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            
            ticket = data.get("Ticket")
            if isinstance(ticket, list):
                ticket = ticket[0]
            
            return {
                "ticket_id": ticket.get("TicketID"),
                "ticket_number": ticket.get("TicketNumber"),
                "title": ticket.get("Title"),
                "customer_id": ticket.get("CustomerID"),        # Empresa
                "customer_user": ticket.get("CustomerUserID"),  # Usuario
                "queue": ticket.get("Queue"),
                "state": ticket.get("State"),
                "priority": ticket.get("Priority"),
                "owner": ticket.get("Owner"),
                "type": ticket.get("Type"),
                "created": ticket.get("Created"),
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get metadata for ticket {ticket_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting ticket metadata: {e}")
            return None

    def get_ticket_latest_article(self, ticket_id: int, session_id: str) -> Optional[str]:
        """
        Retrieves the text of the most relevant article from a Znuny ticket.
        """
        headers = {"Accept": "application/json"}
        url_ticket = f"{self.base_url}/Ticket/{ticket_id}?SessionID={session_id}&AllArticles=1"

        try:
            r = requests.get(url_ticket, headers=headers, timeout=10)
            r.raise_for_status() 
            data = r.json()
            
            ticket_data = data.get("Ticket")
            if isinstance(ticket_data, list):
                ticket_data = ticket_data[0]
                
            articles = ticket_data.get("Article") if ticket_data else None
            return self._extract_relevant_text(articles)

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get article for ticket {ticket_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error processing Znuny articles: {e}")
            return None

    def _extract_relevant_text(self, articles: list) -> Optional[str]:
        """Helper to extract text from a list of articles."""
        if not isinstance(articles, list) or not articles:
            return None

        # Sort by CreateTime or ArticleID
        # Sort by CreateTime and use ArticleID as tie-break for stability
        sorted_articles = sorted(
            articles,
            key=lambda a: (a.get("CreateTime") or "", a.get("ArticleID") or 0)
        )
        
        def is_auto_notification(article):
            body = article.get("Body", "")
            # Si el remitente es sistema, es obvio
            if article.get("SenderType") == "system":
                return True
            # Si el asunto tiene el formato de ticket automático
            subject = article.get("Subject", "")
            if "[Ticket#" in subject and "La solicitud ha sido registrada" in body:
                return True
            # Búsqueda de patrones en el cuerpo
            for pattern in self.SYSTEM_PATTERNS:
                if pattern in body:
                    return True
            return False

        # 1. Intentar obtener el artículo del CLIENTE que NO sea notificación
        customer_articles = [
            a for a in sorted_articles 
            if a.get("SenderType") == "customer" and not is_auto_notification(a)
        ]
        
        if customer_articles:
            # Tomamos el último mensaje del cliente real
            last_relevant = customer_articles[-1]
        else:
            # 2. Fallback: Si no hay del cliente limpio, buscamos cualquier no-notificación
            valid_articles = [a for a in sorted_articles if not is_auto_notification(a)]
            if valid_articles:
                # Tomamos el primero de los válidos (usualmente la apertura)
                last_relevant = valid_articles[0]
            else:
                # Fallback extremo: el primero de la lista
                last_relevant = sorted_articles[0]
        
        subject = last_relevant.get("Subject", "")
        body = last_relevant.get("Body", "")
        
        if not subject and not body:
            return None
        
        return f"Subject: {subject}\n---\nBody:\n{body}"

    def update_ticket(self, ticket_id: int, session_id: str, title: str, user: str, 
                     queue_id: int, priority_id: int, state_id: int, 
                     subject: str, body: str, dynamic_fields: Optional[dict] = None, 
                     type_id: Optional[int] = None) -> Dict[str, Any]:
        """Updates a ticket in Znuny adding a new article and metadata."""
        
        url = f"{self.base_url}/Ticket/{ticket_id}"
        payload = {
            "SessionID": session_id,
            "TicketID": ticket_id,
            "Ticket": {
                "Title": title,
                "CustomerUser": user,
                "QueueID": queue_id,
                # "TypeID": type_id,  # Comentado: No modificar el tipo de ticket
                "PriorityID": priority_id,
                "StateID": state_id
            },
            "Article": {
                "Subject": subject,
                "Body": body,
                "ContentType": "text/plain; charset=utf8"
            }
        }

        if dynamic_fields:
            payload["Ticket"]["DynamicFields"] = dynamic_fields
            
        # Comentado: No modificar el tipo de ticket
        # if type_id is not None:
        #     payload["Ticket"]["TypeID"] = type_id
        
        logger.debug(f"Sending update payload to Znuny: {json.dumps(payload, indent=2, ensure_ascii=False)}")

        try:
            r = requests.patch(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=10
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update Znuny ticket {ticket_id}: {e}")
            return {"error": str(e)}

    def _get_rag_tool_config(self):
        """Gets the RAG tool configuration. Returns None if unavailable."""
        try:
            store_name = self.kb_service.get_or_create_store(display_name="Znuny_Tickets_KB")
            if store_name:
                tool_config = self.kb_service.get_tool_config(store_name)
                logger.info(f"✅ RAG Tool Configured with Store: {store_name}")
                return tool_config
            logger.warning("⚠️ Failed to get Store Name for RAG.")
            return None
        except Exception as e:
            logger.error(f"❌ Error configuring RAG: {e}")
            return None

    def _generate_diagnosis(self, ticket_text: str, tool_config) -> Dict[str, Any]:
        """
        Generates AI diagnosis from ticket text.
        Returns dict with 'type_id', 'requires_visual', and 'diagnostico'.
        """
        response_data = self.agent_service.diagnose_ticket(ticket_text, tool_config)
        
        if isinstance(response_data, str):
            return {"type_id": None, "requires_visual": False, "diagnostico": response_data}
        
        return {
            "type_id": response_data.get("type_id"),
            "requires_visual": response_data.get("requires_visual", False),
            "criticality_score": response_data.get("criticality_score", 5),
            "is_security_alert": response_data.get("is_security_alert", False),
            "diagnostico": response_data.get("diagnostico")
        }

    def _build_incident_data(self, ticket_id: int, metadata: dict, 
                               diagnosis_body: str, type_id: int, 
                               client_info: dict, ticket_text: str) -> dict:
        """Builds the incident data structure."""
        return {
            "ticket_id": ticket_id,
            "ticket_number": metadata.get("ticket_number"),
            "title": metadata.get("title"),
            "type_id": type_id,
            "type_name": "Incidente",
            "diagnostico": diagnosis_body,
            "ticket_text": ticket_text,
            "cliente_znuny": {
                "customer_id": metadata.get("customer_id"),
                "customer_user": metadata.get("customer_user")
            },
            "cliente_real": client_info,
            "queue": metadata.get("queue"),
            "state": metadata.get("state"),
            "priority": metadata.get("priority"),
            "created": metadata.get("created"),
            "processed_at": datetime.datetime.utcnow().isoformat() + "Z"
        }

    def _save_incident_to_file(self, ticket_id: int, incident_data: dict) -> str:
        """Saves incident data to JSON file. Returns the file path."""
        incidents_dir = os.path.join(os.path.dirname(__file__), "..", "logs", "incidents")
        os.makedirs(incidents_dir, exist_ok=True)
        
        json_path = os.path.join(incidents_dir, f"ticket_{ticket_id}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(incident_data, f, ensure_ascii=False, indent=2)
        
        return json_path

    def _process_incident(self, ticket_id: int, session_id: str, 
                          ticket_text: str, diagnosis_body: str, 
                          type_id: int) -> Optional[dict]:
        """
        Processes incident tickets (type_id=10).
        Extracts client info and saves to JSON.
        Returns technical summary if found, or None.
        """
        if type_id != 10:
            return None
            
        logger.info("🔍 Ticket is INCIDENT - Extracting real client info...")
        
        try:
            metadata = self.get_ticket_metadata(ticket_id, session_id)
            if not metadata:
                logger.warning("Could not get ticket metadata for incident processing")
                return None
            
            # Extract client using AI
            client_info = self.agent_service.extract_client_info(metadata, ticket_text)
            
            # Build and save incident data
            incident_data = self._build_incident_data(
                ticket_id, metadata, diagnosis_body, type_id, client_info, ticket_text
            )
            
            json_path = self._save_incident_to_file(ticket_id, incident_data)
            
            logger.info(f"✅ Incident data saved to: {json_path}")
            logger.info(f"📍 Cliente real detectado: {client_info.get('entidad', 'No identificado')}")
            
            # Notify external log monitor service and WAIT for result
            return self._notify_log_monitor(incident_data)
            
        except Exception as e:
            logger.error(f"❌ Error processing incident data: {e}")
            return None

    def _notify_log_monitor(self, incident_data: dict) -> Optional[str]:
        """
        Notifies the external log monitor service about the incident.
        Returns the technical summary if available.
        """
        log_monitor_url = os.environ.get("LOG_MONITOR_URL")
        if not log_monitor_url:
            logger.info("ℹ️ LOG_MONITOR_URL not set - skipping technical log analysis.")
            return None        

        endpoint = f"{log_monitor_url}/analyze-incident"
        
        try:
            logger.info(" Requesting technical log analysis from error_log...")
            response = requests.post(
                endpoint,
                json=incident_data,
                timeout=45  # Wait for SSH and AI analysis
            )
            
            if response.status_code == 200:
                data = response.json()
                summary = data.get("mensaje_resumen")
                if summary:
                    logger.info("✅ Technical analysis received from error_log.")
                    return summary
            
            logger.info(f"📤 Incident sent to log monitor: {response.status_code}")
            return None
        except requests.exceptions.Timeout:
            logger.warning("⚠️ Log monitor request timed out - continuing without logs")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning("⚠️ Could not connect to log monitor - service may be down")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Error notifying log monitor: {e}")
            return None

    def _call_multimodal_service(self, ticket_id: int, ticket_text: str) -> Optional[Dict[str, Any]]:
        """
        Calls the multimodal-images service for visual/design ticket analysis.
        Waits for response and returns the diagnosis.
        
        Returns dict with 'type_id' and 'diagnosis' or None on failure.
        """
        multimodal_url = os.environ.get("MULTIMODAL_URL")
        if not multimodal_url:
            logger.info("ℹ️ MULTIMODAL_URL not set - skipping visual analysis.")
            return None

        endpoint = f"{multimodal_url}/diagnose"
        
        payload = {
            "ticket_id": str(ticket_id),
            "ticket_text": ticket_text,
            "use_rag": True
        }
        
        try:
            logger.info(f"🎨 Calling multimodal service for ticket {ticket_id}...")
            response = requests.post(
                endpoint,
                json=payload,
                timeout=120  # Visual analysis can take time
            )
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("status") == "error":
                logger.error(f"❌ Multimodal service error: {data.get('error')}")
                return None
            
            # Handle diagnosis - can be string or array
            diagnosis = data.get("diagnosis")
            if isinstance(diagnosis, list):
                diagnosis = json.dumps(diagnosis, indent=2, ensure_ascii=False)
            
            logger.info(f"🎨 Visual diagnosis received. TypeID: {data.get('type_id')}, Time: {data.get('processing_time_ms')}ms")
            
            return {
                "type_id": data.get("type_id"),
                "diagnosis": diagnosis,
                "processing_time_ms": data.get("processing_time_ms")
            }
            
        except requests.exceptions.Timeout:
            logger.warning("⚠️ Multimodal service request timed out (120s)")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning("⚠️ Could not connect to multimodal service - may be down")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Error calling multimodal service: {e}")
            return None

    def diagnose_and_update_ticket(self, ticket_id: int, 
                                    session_id: Optional[str] = None, 
                                    data: Optional[dict] = None) -> Dict[str, Any]:
        """
        Orchestrates the ticket diagnosis and update workflow.
        Delegates specific tasks to specialized methods.
        """
        data = data or {}
        
        # 1. Session management
        if not session_id:
            session_id = self.get_or_create_session_id()
            logger.info("SessionID obtained for operation.")

        # 2. Get ticket metadata to preserve original title
        metadata = self.get_ticket_metadata(ticket_id, session_id)
        original_title = metadata.get("title") if metadata else f"Ticket Update {ticket_id}"
        
        # 3. Extract parameters
        title = data.get("titulo") or original_title  # Use original title if not specified
        user = data.get("usuario") or ""
        queue_id = data.get("queue_id") or 9  # QueueID 9 = Mesa de Servicios
        priority_id = data.get("priority_id") or 3
        state_id = data.get("state_id") or 1  # StateID 1 = Nuevo
        subject = data.get("subject") or "Automatic Diagnosis (AI)"

        # 3. Get ticket content
        logger.info(f"Fetching latest article for ticket {ticket_id}...")
        ticket_text = self.get_ticket_latest_article(ticket_id, session_id)
        if not ticket_text:
            raise ValueError("No ticket text found (latest article).")
            
        logger.info(f"DEBUG: Texto que va a la IA:\n{'='*20}\n{ticket_text}\n{'='*20}")

        # 4. Get RAG configuration
        tool_config = self._get_rag_tool_config()

        # 5. Generate AI diagnosis
        logger.info("Generating diagnosis from ticket...")
        diagnosis_result = self._generate_diagnosis(ticket_text, tool_config)
        
        type_id_from_ia = diagnosis_result["type_id"]
        requires_visual = diagnosis_result.get("requires_visual", False)
        diagnosis_body = diagnosis_result["diagnostico"]
        criticality = diagnosis_result.get("criticality_score", 5)
        is_security_alert = diagnosis_result.get("is_security_alert", False)
        
        if not diagnosis_body or not diagnosis_body.strip():
            raise RuntimeError("AI returned empty diagnosis.")
            
        # --- LÓGICA DE MODO EMERGENCIA (PREPARACIÓN) ---
        emergency_header = ""
        if is_security_alert or criticality >= 9:
            logger.warning(f"🚨 MODO EMERGENCIA ACTIVADO para ticket {ticket_id} (Criticidad: {criticality})")
            
            # Encabezado de protocolo de emergencia
            emergency_header = (
                "╔" + "═" * 53 + "╗\n"
                "║ [ALERTA DE SEGURIDAD CRÍTICA - PROTOCOLO DE EMERGENCIA] ║\n"
                "╠" + "═" * 53 + "╣\n"
                "║ ACCIÓN INMEDIATA SUGERIDA:                            ║\n"
                "║ 1. Aislar servicios afectados.                        ║\n"
                "║ 2. Verificar accesos no autorizados a base de datos.  ║\n"
                "║ 3. No realizar pagos ni ceder a extorsiones.          ║\n"
                "╚" + "═" * 53 + "╝\n\n"
            )
            
            # Modificar el asunto para llamar la atención en Znuny
            subject = f"!!! ALERTA CRÍTICA SEGURIDAD: {subject} !!!"
        
        logger.info(f"Diagnosis generated. TypeID: {type_id_from_ia}, Criticality: {criticality}")

        # 6. Route to multimodal service if visual analysis needed
        if requires_visual:
            logger.info("🎨 Ticket requires visual analysis - calling multimodal service...")
            visual_result = self._call_multimodal_service(ticket_id, ticket_text)
            
            if visual_result:
                # Use visual diagnosis instead of classic diagnosis
                diagnosis_body = visual_result["diagnosis"]
                type_id_from_ia = visual_result["type_id"] or type_id_from_ia
                logger.info(f"🎨 Using visual diagnosis. New TypeID: {type_id_from_ia}")
            else:
                logger.warning("⚠️ Visual analysis failed - using classic diagnosis as fallback")

        # --- CONSTRUCCIÓN FINAL DEL CUERPO ---
        if emergency_header:
            diagnosis_body = f"{emergency_header}─── Diagnóstico de IA ───\n{diagnosis_body}"
        
        # 7. Process incident (conditional - only if type_id == 10)
        # We wait for log analysis if it's an incident
        log_summary = self._process_incident(
            ticket_id, session_id, ticket_text, diagnosis_body, type_id_from_ia
        )

        if log_summary:
            diagnosis_body = f"{diagnosis_body}\n\n─ ANALISIS TÉCNICO DE LOGS ─\n{log_summary}"

        # 7. Update Znuny ticket
        logger.info(f"Sending update to ticket {ticket_id}...")
        
        # Add service identifier for traceability
        body_with_identifier = f"[Procesado por: mod_agentes]\n\n{diagnosis_body}"
        
        resp = self.update_ticket(
            ticket_id=ticket_id,
            session_id=session_id,
            title=title,
            user=user,
            queue_id=queue_id,
            priority_id=priority_id,
            state_id=state_id,
            subject=subject,
            body=body_with_identifier,
            type_id=None  # Comentado en el método: No modificar el tipo de ticket
        )
        
        if isinstance(resp, dict) and 'error' in resp:
            raise RuntimeError(f"Failed to update Znuny: {resp['error']}")

        # 10. Build response
        result = {
            "ok": True,
            "ticket_id": ticket_id,
            "type_id_from_ia": type_id_from_ia,
            "diagnosis_body": diagnosis_body,
            "update_response": resp
        }
        
        if log_summary:
            result["log_summary"] = log_summary
            
        return result