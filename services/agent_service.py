import json
import logging
from typing import Union, Dict, Optional, Any
from pydantic import BaseModel, Field
from utils.adk_client import ADKClient

# Configuración de logging
logger = logging.getLogger(__name__)

# --- Modelos de Datos para asegurar consistencia ---

class TicketDiagnosisResponse(BaseModel):
    """Esquema estricto para la respuesta del diagnóstico."""
    type_id: Optional[int] = None
    requires_visual: bool = False
    criticality_score: int = Field(default=5, ge=1, le=10)
    is_security_alert: bool = False
    diagnostico: str
    raw_ai_response: Optional[str] = None  # Para debugging

# --- Clase de Servicio ---

class AgentService:
    def __init__(self):
        # Mantenemos el ADKClient original (Singleton interno)
        self.adk_client = ADKClient()

    def diagnose_ticket(self, ticket_text: str, tool_config=None) -> TicketDiagnosisResponse:
        """
        Llama al modelo de IA, procesa la respuesta (JSON o Texto) 
        y devuelve un objeto TicketDiagnosisResponse garantizado.
        """
        response_text = self.adk_client.diagnose_ticket(ticket_text, tool_config)
        
        if not response_text:
            logger.warning("IA no respondió. Generando respuesta de fallback.")
            return TicketDiagnosisResponse(
                diagnostico="Diagnóstico automático no disponible (El modelo no respondió)."
            )

        # Intentar parsear el JSON retornado por la IA
        try:
            
            data = json.loads(response_text)
            
            # Normalización de campos (La IA a veces varía entre diagnostico/diagnosis)
            diagnostico_final = data.get("diagnostico") or data.get("diagnosis")
            
            if not diagnostico_final:
                logger.warning("JSON recibido sin campo de diagnóstico.")
                return TicketDiagnosisResponse(
                    diagnostico="Diagnóstico incompleto (IA no entregó descripción).",
                    raw_ai_response=response_text
                )

            # Retornamos el objeto validado por Pydantic
            return TicketDiagnosisResponse(
                type_id=data.get("type_id", 14),
                requires_visual=bool(data.get("requires_visual", False)),
                criticality_score=int(data.get("criticality_score", 5)),
                is_security_alert=bool(data.get("is_security_alert", False)),
                diagnostico=diagnostico_final,
                raw_ai_response=response_text
            )

        except Exception as e:
            logger.error(f"Fallo al validar JSON de IA: {e}")
            # Si falla el JSON, devolvemos el texto crudo en el campo diagnostico
            return TicketDiagnosisResponse(
                diagnostico=response_text,
                type_id=14,
                raw_ai_response=response_text
            )

    def extract_client_info(self, metadata: dict, article_text: str) -> dict:
        """
        Extrae información del cliente real delegando al ADKClient.
        """
        try:
            client_info = self.adk_client.extract_client(metadata, article_text)
            return client_info if isinstance(client_info, dict) else {}
        except Exception as e:
            logger.error(f"Error extrayendo información del cliente: {e}")
            return {"error": "No se pudo extraer información del cliente"}