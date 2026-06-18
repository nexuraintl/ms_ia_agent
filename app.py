from fastapi import FastAPI

app = FastAPI(
    title="ms_ia_agent",
    version="1.0.0"
)

@app.get("/health")
def health():
    return {
        "status": "UP"
    }

@app.get("/version")
def version():
    return {
        "service": "ms_ia_agent",
        "version": "1.0.0"
    }
