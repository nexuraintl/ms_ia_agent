FROM python:3.12-slim

WORKDIR /app

# Optimizamos caché de dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Eliminamos la exposición fija y usamos la variable PORT de Cloud Run
ENV PORT=8080
EXPOSE 8080

# IMPORTANTE: Cambiamos a uvicorn para FastAPI
# Forma shell (no exec form) para que $PORT se expanda; Cloud Run inyecta
# un puerto distinto de 8080 en algunos entornos.
CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT}