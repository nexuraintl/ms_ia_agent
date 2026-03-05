from flask import Blueprint, request, jsonify
import datetime
import json
import os
import logging
from services.update_service import ZnunyService

# Configure logging for the controller
logger = logging.getLogger(__name__)

agent_bp = Blueprint("agent", __name__)

# Instantiate the service (Singleton pattern for this module)
znuny_service = ZnunyService()

# --------------------------------------------------------------------------
## Endpoint: Webhook de Znuny (/znuny-webhook)
# --------------------------------------------------------------------------
@agent_bp.route("/znuny-webhook", methods=["POST", "GET", "PUT"])
def znuny_webhook():
    """Recibe webhooks desde Znuny y encadena actualización automática (delegando)."""
    payload = {
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "method": request.method,
        "headers": dict(request.headers),
        "args": request.args.to_dict(),
        "json": request.get_json(silent=True),
        "form": request.form.to_dict(),
        "raw_body": request.get_data(as_text=True),
    }
    logger.debug(f"Raw body: {payload.get('raw_body')}")

    # Guardar log
    logs_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, "znuny_requests.log")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n\n")
    except Exception as e:
        logger.error(f"Failed to write to log file: {e}")

    logger.info(f"Payload received: {json.dumps(payload, ensure_ascii=False, indent=2)}")

    # Lógica para obtener TicketID 
    ticket_id = None
    payload_json = payload.get("json") or {}
    if isinstance(payload_json, dict):
        # Forma concisa y robusta de buscar TicketID en las ubicaciones más probables
        ticket_id = (
            (payload_json.get("Event") or {}).get("TicketID")
            or (payload_json.get("Ticket") or {}).get("TicketID")
            or payload_json.get("TicketID")
        )

    # Lógica de fallback para TicketID (Se mantiene el chequeo de logs si no se encuentra en el payload)
    if not ticket_id:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                entries = [e.strip() for e in f.read().split("\n\n") if e.strip()]
            for raw in reversed(entries):
                try:
                    obj = json.loads(raw)
                    pj = obj.get("json") or {}
                    ticket_id = (
                        (pj.get("Event") or {}).get("TicketID")
                        or (pj.get("Ticket") or {}).get("TicketID")
                        or pj.get("TicketID")
                    )
                    if ticket_id: break
                except Exception: continue
        except Exception: pass

    if not ticket_id:
        logger.error("No TicketID found in payload")
        return jsonify({"error": "No se encontró TicketID en el payload"}), 400

    # Session handling via ZnunyService
    try:
        # We don't strictly need to pass session_id if the service handles it internally,
        # but the original logic tried to get it from env or payload first.
        # ZnunyService.get_or_create_session_id() handles env vars and caching.
        # If payload has a session ID, we might want to use it, but usually we want our own admin session.
        # Let's rely on the service to get a valid session for the agent.
        session_id = znuny_service.get_or_create_session_id()
        logger.info(f"[Webhook] ✅ SessionID obtained: {session_id}")
    except Exception as e:
        logger.error(f"Failed to obtain SessionID: {e}")
        return jsonify({"error": f"No se pudo obtener SessionID: {e}"}), 500


    # Encadenar actualización: LLAMADA AL SERVICIO
    try:
        logger.info(f"[Webhook] Processing ticket {ticket_id}...")
   
        result = znuny_service.diagnose_and_update_ticket(
            ticket_id=ticket_id,
            session_id=session_id,
            data=payload_json
        )
        
        # Check if ticket was skipped due to state filter
        if isinstance(result, dict) and result.get("skipped"):
            logger.info(f"[Webhook] Ticket {ticket_id} skipped: {result.get('reason')}")
            return jsonify({
                "status": "skipped",
                "ticket_id": ticket_id,
                "reason": result.get("reason")
            }), 200
        
        logger.info(f"[Webhook] Update for ticket {ticket_id} completed.")
        
    except Exception as e:
        # Maneja cualquier fallo en la lógica central y lo registra.
        logger.error(f"[Webhook] Error processing webhook: {e}")
        return jsonify({"status": "error", "message": f"Fallo en la actualización: {e}"}), 500 

    return jsonify({"status": "ok", "ticket_id": ticket_id}), 200