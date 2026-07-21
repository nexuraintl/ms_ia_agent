import os
import json
import logging
import datetime
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from services.agent_service import AgentService
from services.update_service import ZnunyService

logger = logging.getLogger(__name__)
router = APIRouter()

# Instanciamos los servicios
znuny_service = ZnunyService()
agent_service = AgentService()

@router.api_route("/znuny-webhook", methods=["POST", "GET", "PUT"])
async def znuny_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Punto de entrada rápido para Znuny. 
    Responde 200 OK de inmediato y procesa la IA en segundo plano.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # 1. Identificar TicketID con tu lógica robusta
    ticket_id = (
        (payload.get("Event") or {}).get("TicketID")
        or (payload.get("Ticket") or {}).get("TicketID")
        or payload.get("TicketID")
    )

    if not ticket_id:
        logger.error("No TicketID found in payload")
        return {"status": "error", "message": "No TicketID"}

    # 2. Guardar Log (Opcional, pero sin bloquear el flujo)
    background_tasks.add_task(save_request_log, request.method, payload)

    # 3. LANZAR PROCESAMIENTO PESADO EN BACKGROUND
    # Esto libera a Znuny en milisegundos
    background_tasks.add_task(process_ticket_full_cycle, ticket_id, payload)

    return {
        "status": "received", 
        "ticket_id": ticket_id,
        "message": "Procesamiento iniciado en segundo plano"
    }

async def process_ticket_full_cycle(ticket_id: int, payload: dict):
    """
    Lógica que ejecuta el RAG, el diagnóstico y la actualización en Znuny.
    """
    try:
        logger.info(f"🚀 Iniciando ciclo de IA para Ticket #{ticket_id}")
        
        session_id = znuny_service.get_or_create_session_id()
        
        # Usamos el servicio centralizado que ya ajustamos antes
        result = znuny_service.diagnose_and_update_ticket(
            ticket_id=ticket_id,
            session_id=session_id,
            data=payload
        )
        
        if isinstance(result, dict) and result.get("skipped"):
            logger.info(f"⏭️ Ticket {ticket_id} omitido: {result.get('reason')}")
        else:
            logger.info(f"✅ Ciclo completado para Ticket {ticket_id}")

    except Exception as e:
        logger.error(f"❌ Error crítico en process_ticket_full_cycle para #{ticket_id}: {e}")

def save_request_log(method: str, payload: dict):
    """Guarda el log en disco sin afectar la respuesta al cliente."""
    try:
        logs_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.path.join(logs_dir, "znuny_requests.log")
        
        log_entry = {
            "time": datetime.datetime.utcnow().isoformat() + "Z",
            "method": method,
            "json": payload
        }
        
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Error escribiendo log: {e}")