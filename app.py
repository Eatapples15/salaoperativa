from flask import Flask, render_template, jsonify, request
from threading import Thread
import datetime
import os
import asyncio # Assicurati che asyncio sia importato
import logging

# Importa le funzioni e l'istanza dell'applicazione dal bot_core
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
bot_status = {
    "lastManualRun": None,
    "lastAutomaticCheck": None,
    "lastOperationFeedback": {"success": None, "message": "Nessuna operazione recente."},
    "botRunning": False
}

# Funzione per eseguire le coroutine asincrone del bot in un thread separato.
def _run_async_in_thread(coro):
    # NON NECESSARIO MODIFICARE QUI
    asyncio.run(coro)

# Route per servire la pagina HTML della dashboard
@app.route('/')
def dashboard():
    return render_template('dashboard_operatore.html')

# API per attivare manualmente l'aggiornamento del bollettino
@app.route('/api/trigger_manual_update', methods=['POST'])
def trigger_manual_update():
    global bot_status
    
    # Questo controllo è cruciale
    if not application or not bot_status["botRunning"]: # Aggiunto controllo bot_status["botRunning"]
        logger.error("Tentativo di attivare il bot ma l'applicazione Telegram non è inizializzata o non risulta in esecuzione.")
        bot_status["lastOperationFeedback"] = {"success": False, "message": "Il bot non è attivo. Controlla i log di Render per gli errori di avvio."}
        return jsonify({"success": False, "message": "Il bot non è ancora stato inizializzato. Riprova tra un momento o controlla i log."}), 500

    bot_status["lastOperationFeedback"] = {"success": None, "message": "Operazione in corso..."}
    bot_status["lastManualRun"] = datetime.datetime.now().isoformat()

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
    return jsonify(bot_status)

# Funzione per avviare il bot Telegram e lo scheduler in un thread dedicato.
def run_telegram_bot_and_scheduler():
    """
    Inizializza e avvia il bot Telegram e lo scheduler in un thread separato.
    Crea un nuovo event loop per questo thread.
    """
    global bot_status
    # *** MODIFICA CRUCIALE QUI ***
    try:
        # Crea un nuovo event loop per questo thread e lo imposta come corrente
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("Nuovo asyncio event loop creato e impostato per il thread del bot.")

        # Inizializza l'applicazione Telegram (crea l'istanza del bot)
        app_instance = init_telegram_application()
        if app_instance:
            setup_scheduler(app_instance)
            
            # Questo logger è importante per vedere se il bot effettivamente "parte"
            logger.info("Bot Telegram e Scheduler avviati nel thread di background.")
            bot_status["botRunning"] = True
            bot_status["lastAutomaticCheck"] = datetime.datetime.now().isoformat()
            
            # Avvia il polling del bot. Questo blocco il thread in questo punto.
            run_bot_polling()
        else:
            logger.error("Impossibile avviare bot e scheduler: applicazione Telegram non inizializzata.")
            bot_status["botRunning"] = False
    except Exception as e:
        logger.exception(f"Errore critico nell'avvio del thread del bot/scheduler: {e}")
        bot_status["botRunning"] = False
    finally:
        # Assicurati che l'event loop venga chiuso quando il thread termina
        if 'loop' in locals() and not loop.is_closed():
            loop.close()
            logger.info("Asyncio event loop del bot chiuso.")


# Punto di ingresso dell'applicazione Flask
if __name__ == '__main__':
    # Avvia il bot Telegram e lo scheduler in un thread separato.
    bot_thread = Thread(target=run_telegram_bot_and_scheduler)
    bot_thread.daemon = True # Permette al thread di chiudersi con l'applicazione principale
    bot_thread.start()

    logger.info("Avvio del server Flask...")
    # Render imposterà la variabile d'ambiente PORT.
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=os.getenv('FLASK_DEBUG', 'False') == 'True')
