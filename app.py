import os
import asyncio
import threading
import logging
from flask import Flask, render_template, jsonify, request
from bot_core import (
    start_scheduler,
    stop_scheduler,
    check_and_send_bollettino,
    initial_check,
    get_bot_status,
    load_state_from_file # Aggiunto per caricare lo stato persistente
)

# --- Configurazione del Logging per app.py ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Inizializzazione Flask App ---
app = Flask(__name__)

# --- Funzioni di gestione per il thread dello scheduler ---
def run_async_in_thread(coro):
    """Esegue una coroutine asyncio in un nuovo loop eventi in un thread separato."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()

# --- Rotte Flask ---

@app.route('/')
def home():
    """Pagina Home per mostrare lo stato del bot."""
    # Ottiene lo stato dal bot_core, che ora include anche informazioni sulla persistenza
    status = get_bot_status()
    return render_template('index.html', bot_status=status)

@app.route('/api/trigger_manual_update', methods=['POST'])
def trigger_manual_update():
    """Endpoint API per avviare un aggiornamento manuale del bollettino."""
    logger.info("Comando di aggiornamento manuale ricevuto e avviato nel thread.")
    # Esegue check_and_send_bollettino che ora salverà lo stato
    thread = threading.Thread(target=run_async_in_thread, args=(check_and_send_bollettino(),))
    thread.start()
    return jsonify({"status": "Comando di aggiornamento manuale avviato. Controlla i log per lo stato più dettagliato o ricarica la pagina per lo stato riepilogativo."}), 200

@app.route('/api/get_bot_status', methods=['GET'])
def api_get_bot_status():
    """Endpoint API per ottenere lo stato corrente del bot in formato JSON."""
    status = get_bot_status()
    return jsonify(status), 200

# --- Punto di ingresso principale ---
if __name__ == '__main__':
    logger.info("Eseguo setup iniziale del bot...")

    # Carica lo stato precedente dal file all'avvio
    initial_load_thread = threading.Thread(target=run_async_in_thread, args=(load_state_from_file(),))
    initial_load_thread.start()
    initial_load_thread.join() # Aspetta che il caricamento sia completo prima di proseguire

    # Avvia lo scheduler in un thread separato
    scheduler_thread = threading.Thread(target=run_async_in_thread, args=(start_scheduler(),))
    scheduler_thread.start()
    
    # Esegui il check iniziale subito per popolare lo stato
    # Questo check ora userà lo stato caricato dal file
    initial_check_thread = threading.Thread(target=run_async_in_thread, args=(initial_check(),))
    initial_check_thread.start()
    
    logger.info("Setup bot completato.")

    # Render imposterà la porta tramite la variabile d'ambiente PORT
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False) # Debug False per produzione
