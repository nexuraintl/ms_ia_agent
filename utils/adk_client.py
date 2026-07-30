import os
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

class ADKClient:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
        # gemini-2.0-flash fue retirado (shutdown); se migra a gemini-3.6-flash (estable, sin fecha de baja anunciada)
        self.model_id = "gemini-3.6-flash"

    def _call_gemini(self, prompt: str, tool_config=None, force_json_mime: bool = True):
        """
        Método base para llamadas a Gemini con soporte RAG.

        `force_json_mime` se ignora cuando hay tool_config: Gemini devuelve
        candidates=None (sin excepción) cuando response_mime_type="application/json"
        se combina con la herramienta file_search — confirmado empíricamente,
        no es un límite de tokens. El prompt ya pide JSON en texto plano, así
        que el parseo defensivo (utils/json_parse.extract_json) cubre la
        ausencia del modo forzado.
        """
        has_tools = bool(tool_config)
        use_json_mime = force_json_mime and not has_tools
        try:
            generate_config = types.GenerateContentConfig(temperature=0.1)
            if use_json_mime:
                generate_config.response_mime_type = "application/json"

            # PROTECCIÓN CRÍTICA: Solo asignar tools si realmente hay contenido en tool_config
            if has_tools:
                if isinstance(tool_config, list):
                    generate_config.tools = tool_config
                else:
                    generate_config.tools = [tool_config]

            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=generate_config
            )

            if has_tools:
                grounding = None
                try:
                    grounding = response.candidates[0].grounding_metadata
                except Exception:
                    pass
                if grounding and grounding.grounding_chunks:
                    logger.info("RAG: grounding_metadata con %d chunk(s)", len(grounding.grounding_chunks))
                else:
                    logger.warning("RAG: se pasaron tools pero la respuesta no trae grounding_metadata")

            return response.text or ""

        except Exception as e:
            logger.exception("Error en la llamada a Gemini: %s", e)
            return "{}"

    def classify_with_rag(self, ticket_text: str, tool_config=None) -> str:
        """
        PASO 1: Clasificación inicial (Triaje) usando RAG.
        Determina si el ticket es de diseño, incidente o consulta general.
        """
        prompt = f"""
        Actúa como el Clasificador de Entradas de la Mesa de Servicio Nexura.
        Tu objetivo es determinar la ruta técnica del ticket basándote en su contenido y en el
        corpus de recuperación (File Search) adjunto, que contiene tickets de Znuny ya resueltos
        (FAQs pregunta/respuesta del módulo FAQ) y documentación técnica del área.

        Categorías posibles:
        1. "diseño": Ajustes visuales, UI/UX, logos, colores, HTML/CSS.
        2. "incidente": Fallas técnicas, errores 500, lentitud, caídas de sistema.
        3. "consulta_general": Dudas de proceso, normativa o uso que no requieren visión ni logs.

        Reglas de Negocio:
        - Si el score de criticidad es >= 9 o hay amenazas de seguridad, marca is_security_alert: true.
        - Incidentes: type_id 10. Requerimientos: type_id 14. Peticiones: type_id 19.

        Ticket: {ticket_text}

        Responde estrictamente en JSON, sin texto adicional antes o después, con este formato:
        {{
            "category": "diseño | incidente | consulta_general",
            "type_id": int,
            "criticality_score": int (1-10),
            "is_security_alert": bool,
            "reasoning": "Breve explicación de la ruta elegida"
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
        - Prioriza la solución documentada en el corpus de recuperación (File Search) si aplica
          al caso; cita el número de FAQ (f_number) en "faq_referencia" cuando la uses.
        - Si el corpus no tiene nada aplicable a este ticket, dilo explícitamente en el
          diagnóstico en vez de inventar pasos de solución.
        - No menciones nombres de microservicios internos, habla como soporte técnico.
        - Si hay insumos de especialistas, intégralos de forma natural.
        - Sé profesional y técnico.

        Responde en JSON, sin texto adicional antes o después:
        {{
            "type_id": int,
            "diagnostico": "Resumen profesional para el agente de primer nivel",
            "faq_referencia": "f_number de la FAQ citada, o null si no se usó ninguna"
        }}
        """
        return self._call_gemini(prompt, tool_config)

    def extract_client(self, metadata: dict, article_text: str) -> dict:
        """Extrae la entidad y NIT del cliente real."""
        prompt = f"Analiza esta metadata: {metadata} y texto: {article_text}. Extrae 'entidad' y 'nit' en JSON."
        response = self._call_gemini(prompt)
        try: return json.loads(response)
        except: return {}
