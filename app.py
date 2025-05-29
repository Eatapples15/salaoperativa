import os
import threading
import logging
from flask import Flask, render_template, jsonify, request
import bot_core # Importa l'intero modulo

# --- Configurazione del Logging per app.py ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Inizializzazione Flask App ---
app = Flask(__name__)

# --- Rotte Flask ---

@app.route('/')
def home():
    """Pagina Home per mostrare lo stato del bot."""
    status = bot_core.get_bot_status() # Ottiene lo stato dal modulo bot_core
    return render_template('index.html', bot_status=status)

@app.route('/api/trigger_manual_update', methods=['POST'])
def trigger_manual_update():
    """Endpoint API per avviare un aggiornamento manuale del bollettino."""
    logger.info("Comando di aggiornamento manuale ricevuto.")
    # Innesca il controllo manuale attraverso la funzione esposta da bot_core
    # Questa funzione ora programma il task nel loop del bot.
    success = bot_core.trigger_manual_check_from_flask()
    if success:
        return jsonify({"status": "Comando di aggiornamento manuale avviato. Controlla i log per lo stato più dettagliato o ricarica la pagina per lo stato riepilogativo."}), 200
    else:
        return jsonify({"status": "Errore: il bot non è attivo o non è riuscito ad avviare il controllo manuale."}), 500

@app.route('/api/get_bot_status', methods=['GET'])
def api_get_bot_status():
    """Endpoint API per ottenere lo stato corrente del bot in formato JSON."""
    status = bot_core.get_bot_status()
    return jsonify(status), 200

# --- Punto di ingresso principale ---
if __name__ == '__main__':
    logger.info("Avvio setup iniziale del bot nel suo thread separato...")

    # Avvia il thread che gestirà l'event loop di asyncio e il bot
    bot_thread = threading.Thread(target=bot_core.start_bot_in_thread, daemon=True) # daemon=True per chiudere il thread con l'applicazione principale
    bot_thread.start()
    
    logger.info("Setup bot completato. Avvio app Flask.")

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
