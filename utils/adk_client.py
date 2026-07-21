import os
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

class ADKClient:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
        self.model_id = "gemini-2.0-flash"

    def _call_gemini(self, prompt: str, tool_config=None, response_mime="application/json"):
        """Método base para llamadas a Gemini con soporte RAG."""
        try:
            generate_config = types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type=response_mime
            )
            # PROTECCIÓN CRÍTICA: Solo asignar tools si realmente hay contenido en tool_config
            if tool_config:
                if isinstance(tool_config, list) and len(tool_config) > 0:
                    generate_config.tools = tool_config
                elif not isinstance(tool_config, list):
                    generate_config.tools = [tool_config]

            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=generate_config
            )
            return response.text
        
        except Exception as e:
            logger.error(f"Error en la llamada a Gemini: {e}")
            return "{}" if response_mime == "application/json" else ""

    def classify_with_rag(self, ticket_text: str, tool_config=None) -> str:
        """
        PASO 1: Clasificación inicial (Triaje) usando RAG.
        Determina si el ticket es de diseño, incidente o consulta general.
        """
        prompt = f"""
        Actúa como el Clasificador de Entradas de la Mesa de Servicio Nexura.
        Tu objetivo es determinar la ruta técnica del ticket basándote en su contenido y el historial (RAG).

        Categorías posibles:
        1. "diseño": Ajustes visuales, UI/UX, logos, colores, HTML/CSS.
        2. "incidente": Fallas técnicas, errores 500, lentitud, caídas de sistema.
        3. "consulta_general": Dudas de proceso, normativa o uso que no requieren visión ni logs.

        Reglas de Negocio:
        - Si el score de criticidad es >= 9 o hay amenazas de seguridad, marca is_security_alert: true.
        - Incidentes: type_id 10. Requerimientos: type_id 14. Peticiones: type_id 19.

        Ticket: {ticket_text}

        Responde estrictamente en JSON con este formato:
        {{
            "category": "diseño | incidente | consulta_general",
            "type_id": int,
            "criticality_score": int (1-10),
            "is_security_alert": bool,
            "reasoning": "Breve explicación basada en el historial RAG"
        }}
        """
        return self._call_gemini(prompt, tool_config)

    def generate_final_diagnosis(self, context: str, tool_config=None) -> str:
        """
        PASO 2: Redacción final unificando insumos de especialistas.
        """
        prompt = f"""
        Eres el Agente de Diagnóstico Final de Nexura.
        Debes redactar un diagnóstico legible y estructurado para Znuny.

        Contexto e Insumos:
        {context}

        Instrucciones:
        - Si hubo un Protocolo de Emergencia, inicia con "[ALERTA CRÍTICA]".
        - Resume la solución basándote en la base de conocimiento (RAG).
        - No menciones nombres de microservicios internos, habla como soporte técnico.
        - Si hay insumos de especialistas, intégralos de forma natural.
        - Sé profesional y técnico.

        Responde en JSON:
        {{
            "type_id": int,
            "diagnostico": "Resumen profesional para el agente de primer nivel"
        }}
        """
        return self._call_gemini(prompt, tool_config)

    def extract_client(self, metadata: dict, article_text: str) -> dict:
        """Extrae la entidad y NIT del cliente real."""
        prompt = f"Analiza esta metadata: {metadata} y texto: {article_text}. Extrae 'entidad' y 'nit' en JSON."
        response = self._call_gemini(prompt)
        try: return json.loads(response)
        except: return {}