import json
import logging
from typing import Union, Dict, Optional, Any
from pydantic import BaseModel, Field
from utils.adk_client import ADKClient

# Configuración de logging
logger = logging.getLogger(__name__)

# --- Modelos de Datos para asegurar la sintonía con el flujo ---

class TicketClassification(BaseModel):
    """Resultado del Triaje inicial del Orquestador."""
    category: str = Field(description="diseño | incidente | consulta_general")
    type_id: int  # 10: Incidente, 14: Requerimiento, 19: Petición [cite: 14]
    is_critical: bool = False
    requires_visual: bool = False
    reasoning: str # Breve explicación de la ruta elegida

class TicketDiagnosisResponse(BaseModel):
    """Esquema para la respuesta final que irá a Znuny."""
    type_id: Optional[int] = None
    diagnostico: str
    raw_ai_response: Optional[str] = None 

# --- Clase de Servicio ---

class AgentService:
    def __init__(self):
        self.adk_client = ADKClient()

    def classify_and_route(self, ticket_text: str, tool_config=None) -> TicketClassification:
        """
        PASO 1: Clasifica el ticket usando RAG para determinar la ruta[cite: 35, 43].
        Determina si el ticket va a Multimodal, Log_Errors o Agente (Otros)[cite: 6].
        """
        logger.info("🤖 Clasificando ticket con RAG...")
        
        # El ADKClient debe implementar este método con un prompt de clasificación
        response_text = self.adk_client.classify_with_rag(ticket_text, tool_config)
        
        try:
            data = json.loads(response_text)
            
            # Determinamos si requiere visual basándonos en la categoría 
            is_design = data.get("category") == "diseño"
            
            return TicketClassification(
                category=data.get("category", "consulta_general"),
                type_id=data.get("type_id", 19),
                is_critical=bool(data.get("criticality_score", 0) >= 9 or data.get("is_security_alert")), # [cite: 51, 67]
                requires_visual=is_design,
                reasoning=data.get("reasoning", "Clasificación estándar")
            )
        except Exception as e:
            logger.error(f"Error parseando clasificación: {e}")
            # Fallback seguro: Ruta general
            return TicketClassification(
                category="consulta_general",
                type_id=19,
                reasoning="Error en clasificación, se redirige a consulta general"
            )

    def generate_final_report(self, original_text: str, insumos_especialistas: str, tool_config=None) -> TicketDiagnosisResponse:
        """
        PASO 2: Redacta el diagnóstico final para Znuny combinando el ticket original
        con los insumos recibidos de los especialistas (Multimodal o Logs)[cite: 17, 32].
        """
        logger.info("✍️ Redactando diagnóstico final estructurado...")
        
        # Combinamos la información para que Gemini genere la nota final [cite: 58, 59]
        contexto_final = f"""
        Ticket Original: {original_text}
        Insumos de especialistas: {insumos_especialistas}
        """
        
        response_text = self.adk_client.generate_final_diagnosis(contexto_final, tool_config)
        
        try:
            data = json.loads(response_text)
            return TicketDiagnosisResponse(
                type_id=data.get("type_id"),
                diagnostico=data.get("diagnostico") or data.get("diagnosis")
            )
        except:
            return TicketDiagnosisResponse(diagnostico=response_text)

    def extract_client_info(self, metadata: dict, article_text: str) -> dict:
        """Extrae información del cliente real[cite: 102, 106]."""
        try:
            client_info = self.adk_client.extract_client(metadata, article_text)
            return client_info if isinstance(client_info, dict) else {}
        except Exception as e:
            logger.error(f"Error extrayendo información del cliente: {e}")
            return {"error": "No se pudo extraer información"}