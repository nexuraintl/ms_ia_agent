import sys
import os
import time

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types
from services.knowledge_base_service import KnowledgeBaseService
from utils.adk_client import ADKClient

def verify_rag():
    print("🚀 Iniciando verificación de RAG con Gemini File Search Store...")
    
    kb_service = KnowledgeBaseService()
    
    # 1. Crear un archivo de prueba local
    test_file_path = "test_rag_doc.txt"
    with open(test_file_path, "w") as f:
        f.write("""
        ERROR CONOCIDO: Error 999 en Módulo de Ventas
        Causa: El servidor de base de datos 'DB-SALES-01' tiene un bloqueo en la tabla 'invoices'.
        Solución: Ejecutar el script 'unlock_sales.sh' en el servidor y reiniciar el servicio 'sales-api'.
        Tipo: Incidente (10).
        """)
    print(f"📄 Archivo de prueba creado: {test_file_path}")

    try:
        # 2. Crear/Obtener Store
        store_name = kb_service.get_or_create_store(display_name="Test_RAG_Store")
        if not store_name:
            print("❌ Falló la creación del Store.")
            return

        # 3. Subir e indexar archivo
        success = kb_service.upload_and_index_file(store_name, test_file_path)
        if not success:
            print("❌ Falló la subida e indexación del archivo.")
            return

        # 5. Probar ADKClient con la herramienta
        print("\n🤖 Probando ADKClient con RAG...")
        client = ADKClient()

        # Ticket que requiere la info del archivo
        ticket_text = "Ayuda, me sale el Error 999 cuando intento facturar en Ventas. No sé qué hacer."

        tool_config = kb_service.get_tool_config([store_name])

        # Llamada real a Gemini. classify_with_rag ya no fuerza JSON mode
        # cuando hay tool_config (JSON mode + file_search produce
        # candidates=None de forma silenciosa — confirmado empíricamente).
        response = client.classify_with_rag(ticket_text, tool_config=tool_config)

        print("\n📝 Respuesta de Gemini:")
        print("-" * 40)
        print(response)
        print("-" * 40)

        # La prueba objetiva es grounding_metadata, no el match de texto: el
        # texto puede parafrasear el contenido del archivo sin citarlo literal.
        raw_response = client.client.models.generate_content(
            model=client.model_id,
            contents=ticket_text,
            config=types.GenerateContentConfig(temperature=0.1, tools=[tool_config]),
        )
        grounding = None
        try:
            grounding = raw_response.candidates[0].grounding_metadata
        except Exception:
            pass

        if grounding and grounding.grounding_chunks:
            print(f"✅ ÉXITO: grounding_metadata presente con {len(grounding.grounding_chunks)} chunk(s).")
        else:
            print("⚠️ ADVERTENCIA: la respuesta no trae grounding_metadata; el RAG no está recuperando nada.")

    except Exception as e:
        print(f"❌ Error durante la verificación: {e}")
    finally:
        # Limpieza (opcional)
        if os.path.exists(test_file_path):
            os.remove(test_file_path)

if __name__ == "__main__":
    verify_rag()
