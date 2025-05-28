from flask import Flask, render_template, jsonify, request
from threading import Thread
import datetime
import os
import asyncio
import logging
from telegram import Update # Importa Update

# Importa le funzioni e l'istanza dell'applicazione dal bot_core
# NON importare 'application' direttamente qui se la inizializzi dopo
from bot_core import (
    init_telegram_application,
    send_bollettino_update_to_telegram,
    setup_scheduler,
    # Rimuovi 'application' da qui
)

# Configurazione del logging per app.py
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Stato del bot per la dashboard (persistenza in memoria) ---
bot_status = {
    "lastManualRun": None,
    "lastAutomaticCheck": None,
    "lastOperationFeedback": {"success": None, "message": "Nessuna operazione recente."},
    "botRunning": False
}

# Definisci 'application' come variabile globale qui,
# e le verrà assegnato un valore dopo l'inizializzazione.
application = None 

# Funzione per eseguire le coroutine asincrone del bot in un thread separato.
def _run_async_in_thread(coro, callback=None): # Aggiungi un callback opzionale
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(coro)
        if callback:
            callback(result) # Esegui il callback con il risultato
    finally:
        loop.close()

# Callback per aggiornare lo stato del bot dopo un'operazione manuale o automatica
def update_bot_operation_feedback(result):
    global bot_status
    bot_status["lastOperationFeedback"] = {"success": result["success"], "message": result["message"]}
    # Puoi aggiungere un aggiornamento per lastAutomaticCheck qui se vuoi
    if "Bollettino già aggiornato" in result["message"] or result["success"]:
        bot_status["lastAutomaticCheck"] = datetime.datetime.now().isoformat()
    logger.info(f"Feedback operazione aggiornato: {result['message']}")


# Route per servire la pagina HTML della dashboard
@app.route('/')
def dashboard():
    return render_template('dashboard_operatore.html')

# API per attivare manualmente l'aggiornamento del bollettino
@app.route('/api/trigger_manual_update', methods=['POST'])
def trigger_manual_update():
    global bot_status
    global application # Assicurati di usare la variabile globale definita qui

    if not application:
        logger.error("Tentativo di attivare l'aggiornamento manuale ma l'applicazione Telegram non è inizializzata.")
        bot_status["lastOperationFeedback"] = {"success": False, "message": "Il bot non è inizializzato. Riprova tra un momento."}
        return jsonify({"success": False, "message": "Il bot non è ancora stato inizializzato."}), 500

    bot_status["lastOperationFeedback"] = {"success": None, "message": "Operazione in corso..."}
    bot_status["lastManualRun"] = datetime.datetime.now().isoformat()

    # Esegui la coroutine send_bollettino_update_to_telegram in un thread separato
    # con un suo event loop, passando il callback.
    update_thread = Thread(target=_run_async_in_thread, 
                           args=(send_bollettino_update_to_telegram(application), update_bot_operation_feedback))
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

# ENDPOINT PER IL WEBHOOK DI TELEGRAM
@app.route('/telegram', methods=['POST'])
async def telegram_webhook():
    """
    Gestisce gli aggiornamenti in arrivo da Telegram tramite webhook.
    Questo endpoint è asincrono perché process_update è una coroutine.
    """
    global application # Assicurati di usare la variabile globale definita qui
    if not application:
        logger.error("Webhook ricevuto ma applicazione Telegram non inizializzata.")
        return jsonify({"status": "error", "message": "Bot non attivo."}), 500

    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        await application.process_update(update)
        
        logger.info("Aggiornamento Telegram ricevuto e processato via webhook.")
        return jsonify({"status": "success"})
    except Exception as e:
        logger.exception(f"Errore nella gestione del webhook Telegram: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Funzione per avviare il bot Telegram (Solo scheduler e setup webhook iniziale)
def setup_bot_on_startup():
    """
    Inizializza il bot e imposta il webhook su Telegram.
    Questa funzione viene eseguita nel thread principale o in un thread separato
    per non bloccare il server Flask.
    """
    global bot_status
    global application # QUI È IL CAMBIAMENTO CHIAVE!

    try:
        # ASSEGNA l'istanza di application alla variabile globale di app.py
        application = init_telegram_application() 
        if application: # Ora stiamo controllando la variabile globale appena assegnata
            # Configura lo scheduler. Lo scheduler userà _run_async_in_thread
            # per le sue chiamate asincrone.
            # Passa il callback allo scheduler job per aggiornare lo stato automatico
            setup_scheduler(application, update_bot_operation_feedback) # Modifica questa chiamata
            
            # --- Configurazione Webhook ---
            render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
            if not render_hostname:
                logger.error("RENDER_EXTERNAL_HOSTNAME non impostato. Impossibile configurare il webhook.")
                bot_status["botRunning"] = False
                return

            webhook_url = f"https://{render_hostname}/telegram"
            
            logger.info(f"Impostazione del webhook Telegram su: {webhook_url}")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(application.bot.set_webhook(url=webhook_url))
                logger.info("Webhook Telegram impostato con successo.")
                bot_status["botRunning"] = True
            except Exception as e:
                logger.exception(f"Errore durante l'impostazione del webhook: {e}")
                bot_status["botRunning"] = False
            finally:
                loop.close()

            logger.info("Bot Telegram configurato (modalità webhook) e Scheduler attivo.")

        else:
            logger.error("Impossibile avviare bot e scheduler: applicazione Telegram non inizializzata.")
            bot_status["botRunning"] = False
    except Exception as e:
        logger.exception(f"Errore critico nella configurazione del bot (webhook): {e}")
        bot_status["botRunning"] = False

# Punto di ingresso dell'applicazione Flask
if __name__ == '__main__':
    # Avvia la configurazione del bot (webhook e scheduler) in un thread separato.
    bot_setup_thread = Thread(target=setup_bot_on_startup)
    bot_setup_thread.daemon = True
    bot_setup_thread.start()

    logger.info("Avvio del server Flask...")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=os.getenv('FLASK_DEBUG', 'False') == 'True')
