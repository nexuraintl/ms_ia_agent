import requests

url = "https://qa-ia-agent-log-errors-58937908768.us-central1.run.app/analyze-incident"
payload = {
    "ticket_id": "3447",
    "ticket_number": "0194383",
    "title": "prueba error fatal",
    "type_id": 10,
    "type_name": "Incidente",
    "diagnostico": "El sistema falla con un error",
    "ticket_text": "Error 500 al entrar",
    "entity": "Alcaldia Prueba",
    "cliente_znuny": { "customer_id": "usr1", "customer_user": "usr1" },
    "cliente_real": { "entidad": "Alcaldia Prueba", "contacto": "", "email": "", "problema_resumido": "", "confianza": 0.9 },
    "queue": "Soporte N2",
    "state": "Nuevo",
    "priority": "3 Normal",
    "created": "2026-03-04 10:00:00",
    "processed_at": "2026-03-04T10:00:00Z"
}

r = requests.post(url, json=payload)
print(f"Status: {r.status_code}")
print(f"Response: {r.text}")
