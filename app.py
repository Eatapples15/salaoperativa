from flask import Flask, render_template, jsonify, request
from threading import Thread
import datetime
import os
import asyncio
import logging

# Importa le funzioni e l'istanza dell'applicazione dal bot_core
# Assicurati che 'bot_core.py' esista e sia nella stessa directory
from bot_core import (
    init_telegram_application,
    send_bollettino_update_to_telegram,
    run_bot_polling,
    setup_scheduler,
    application # L'istanza dell'applicazione Telegram, inizializzata in bot_core.py
)

# Configurazione del logging per app.py
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Stato del bot per la dashboard (persistenza in memoria, non tra riavvii) ---
# Per una persistenza reale tra riavvii (es. su Render Free Tier),
# dovresti usare un file JSON o un piccolo database (come SQLite, con le sue limitazioni sul Free Tier)
bot_status = {
    "lastManualRun": None,
    "lastAutomaticCheck": None,
    "lastOperationFeedback": {"success": None, "message": "Nessuna operazione recente."},
    "botRunning": False
}

# Funzione per eseguire le coroutine asincrone del bot in un thread separato.
# Flask è sincrono per le route, quindi le operazioni del bot (che sono async)
# devono essere eseguite in questo modo per non bloccare il server web.
def _run_async_in_thread(coro):
    asyncio.run(coro)

# Route per servire la pagina HTML della dashboard
@app.route('/')
def dashboard():
    """Rende la pagina HTML della dashboard dell'operatore."""
    return render_template('dashboard_operatore.html')

# API per attivare manualmente l'aggiornamento del bollettino
@app.route('/api/trigger_manual_update', methods=['POST'])
def trigger_manual_update():
    """Endpoint API per avviare un controllo manuale del bollettino."""
    global bot_status
    
    if not application:
        logger.error("Tentativo di attivare il bot ma l'applicazione Telegram non è inizializzata.")
        return jsonify({"success": False, "message": "Il bot non è ancora stato inizializzato. Riprova tra un momento."}), 500

    # Aggiorna lo stato temporaneamente
    bot_status["lastOperationFeedback"] = {"success": None, "message": "Operazione in corso..."}
    bot_status["lastManualRun"] = datetime.datetime.now().isoformat()

    # Avvia la funzione di aggiornamento del bot in un thread separato.
    # Questo permette alla richiesta HTTP di ritornare immediatamente,
    # mentre il bot lavora in background.
    update_thread = Thread(target=_run_async_in_thread, args=(send_bollettino_update_to_telegram(application),))
    update_thread.start()

    logger.info("Comando di aggiornamento manuale ricevuto e avviato nel thread.")
    return jsonify({
        "success": True,
        "message": "Comando di aggiornamento inviato al bot. La dashboard si aggiornerà a breve con il feedback.",
        "timestamp": bot_status["lastManualRun"]
    })

# API per ottenere lo stato corrente del bot
@app.route('/api/get_bot_status', methods=['GET'])
def get_bot_status():
    """Endpoint API per recuperare lo stato corrente del bot (timestamp, feedback, etc.)."""
    # Restituisce lo stato corrente mantenuto in memoria.
    # Per una soluzione più robusta, leggere da un file persistente o DB.
    return jsonify(bot_status)

# Funzione per avviare il bot Telegram e lo scheduler in un thread dedicato.
# Questa funzione viene chiamata una volta all'avvio dell'applicazione Flask.
def run_telegram_bot_and_scheduler():
    """Inizializza e avvia il bot Telegram e lo scheduler in un thread separato."""
    global bot_status
    try:
        # Inizializza l'applicazione Telegram (crea l'istanza del bot)
        app_instance = init_telegram_application()
        if app_instance:
            # Configura e avvia lo scheduler per gli aggiornamenti automatici
            setup_scheduler(app_instance) # Passa l'istanza dell'applicazione allo scheduler
            
            bot_status["botRunning"] = True
            bot_status["lastAutomaticCheck"] = datetime.datetime.now().isoformat() # Primo aggiornamento data check automatico
            logger.info("Bot Telegram e Scheduler avviati nel thread di background.")
            
            # Avvia il polling del bot per ricevere comandi Telegram come /start o /aggiorna
            run_bot_polling()
        else:
            logger.error("Impossibile avviare bot e scheduler: applicazione Telegram non inizializzata.")
            bot_status["botRunning"] = False
    except Exception as e:
        logger.exception(f"Errore critico nell'avvio del thread del bot/scheduler: {e}")
        bot_status["botRunning"] = False

# Punto di ingresso dell'applicazione Flask
if __name__ == '__main__':
    # Avvia il bot Telegram e lo scheduler in un thread separato.
    # Il thread.daemon = True fa sì che il thread del bot si chiuda
    # automaticamente quando l'applicazione Flask principale si chiude.
    bot_thread = Thread(target=run_telegram_bot_and_scheduler)
    bot_thread.daemon = True
    bot_thread.start()

    # Avvia il server Flask.
    # host='0.0.0.0' per renderlo accessibile dall'esterno su Render.com.
    # port=os.getenv('PORT', 5000) usa la porta fornita da Render (o 5000 di default).
    # debug=os.getenv('FLASK_DEBUG', 'False') == 'True' per abilitare il debug solo se specificato.
    logger.info("Avvio del server Flask...")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=os.getenv('FLASK_DEBUG', 'False') == 'True')
