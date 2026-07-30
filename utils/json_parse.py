import re


def extract_json(text: str) -> str:
    """
    Extrae el objeto JSON de una respuesta de modelo que puede venir sin
    response_mime_type forzado (p.ej. con fences ```json ... ``` o texto
    alrededor). Si no encuentra nada parseable, devuelve "{}".
    """
    if not text:
        return "{}"

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]

    return "{}"
