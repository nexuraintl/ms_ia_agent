import os
from dotenv import load_dotenv
from fastapi import FastAPI
from controllers.agent_controller import router as agent_router

# 1. Cargar variables de entorno
load_dotenv("env_vars/.env")

# 2. Inicializar App
app = FastAPI(
    title="Nexura Agent Orchestrator",
    description="Orquestador principal para la automatización de tickets Znuny",
    version="2.0.0"
)

# 3. Registrar Rutas (Sin prefijo para mantener compatibilidad)
# Al quitar el prefix, la ruta será directamente /znuny-webhook
app.include_router(agent_router, tags=["Agent"])

# 4. Root Health Check (Requerido por Cloud Run)
@app.get("/health")
async def health_check():
    return {"status": "healthy"}

# 5. Version del microservicio
@app.get("/version")
async def version():
    return {
        "service": "ms_ia_agent",
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
