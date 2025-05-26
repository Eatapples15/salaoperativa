from flask import Flask, render_template, jsonify, request
from threading import Thread
import datetime
import os
import asyncio
import logging

# Importa le funzioni e l'istanza dell'applicazione dal bot_core
from bot_core import (
    init_telegram_application,
    send_bollettino_update_to_telegram,
    setup_scheduler,
    application # L'istanza dell'applicazione Telegram
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
    "botRunning": False # Verrà impostato a True dopo l'inizializzazione del webhook
}

# Variabile globale per l'URL del webhook, che Render ci fornisce
WEBHOOK_URL = None

# Funzione per eseguire le coroutine asincrone del bot in un thread separato.
def _run_async_in_thread(coro):
    asyncio.run(coro)

# Route per servire la pagina HTML della dashboard
@app.route('/')
def dashboard():
    return render_template('dashboard_operatore.html')

# API per attivare manualmente l'aggiornamento del bollettino
@app.route('/api/trigger_manual_update', methods=['POST'])
def trigger_manual_update():
    global bot_status
    
    if not application or not bot_status["botRunning"]:
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

# NUOVO ENDPOINT PER IL WEBHOOK DI TELEGRAM
@app.route('/telegram', methods=['POST'])
async def telegram_webhook():
    """
    Gestisce gli aggiornamenti in arrivo da Telegram tramite webhook.
    """
    global application
    if not application:
        logger.error("Webhook ricevuto ma applicazione Telegram non inizializzata.")
        return jsonify({"status": "error", "message": "Bot non attivo."}), 500

    try:
        # Passa l'aggiornamento a PTB per l'elaborazione
        # PTB si aspetta la richiesta HTTP grezza, non solo il JSON
        await application.update_queue.put(
            # Utilizza una classe Update.webhook_x per creare l'oggetto Update
            # da una richiesta HTTP. `post_body` è il JSON grezzo.
            # `data` contiene gli header
            # `url` è l'URL a cui telegram sta inviando il webhook.
            # Python-Telegram-Bot 20.x semplifica la gestione del webhook.
            # L'argomento della richiesta è il corpo JSON, non il request object intero di Flask.
            Update.de_json(request.get_json(force=True), application.bot)
        )
        logger.info("Aggiornamento Telegram ricevuto e messo in coda per l'elaborazione.")
        return jsonify({"status": "success"})
    except Exception as e:
        logger.exception(f"Errore nella gestione del webhook Telegram: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# Funzione per avviare il bot Telegram (Ora solo webhooks e scheduler)
def run_telegram_bot_and_scheduler_webhooks():
    """
    Inizializza e avvia il bot Telegram (modalità webhook) e lo scheduler.
    """
    global bot_status, WEBHOOK_URL
    try:
        app_instance = init_telegram_application()
        if app_instance:
            # Configura lo scheduler
            setup_scheduler(app_instance)
            
            # --- Configurazione Webhook ---
            # Render imposta la variabile d'ambiente RENDER_EXTERNAL_HOSTNAME.
            # Usala per costruire l'URL del webhook pubblico.
            render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
            if not render_hostname:
                logger.error("RENDER_EXTERNAL_HOSTNAME non impostato. Impossibile configurare il webhook.")
                bot_status["botRunning"] = False
                return

            WEBHOOK_URL = f"https://{render_hostname}/telegram"
            
            # Imposta il webhook su Telegram
            # Usiamo application.bot.set_webhook, che è una coroutine, quindi dobbiamo awaited.
            # Questo deve essere eseguito in un event loop.
            asyncio.run(app_instance.bot.set_webhook(url=WEBHOOK_URL))
            logger.info(f"Webhook Telegram impostato su: {WEBHOOK_URL}")

            # PTB per i webhooks necessita di essere eseguito in un loop di background per processare gli aggiornamenti.
            # Il metodo `run_webhook` è il successore di `run_polling` per i webhooks.
            # Nota: `run_webhook` NON blocca, bensì avvia un loop di background per la gestione degli aggiornamenti.
            # Quindi, il thread non si bloccherà qui.
            asyncio.run(app_instance.run_webhook())
            
            bot_status["botRunning"] = True
            bot_status["lastAutomaticCheck"] = datetime.datetime.now().isoformat()
            logger.info("Bot Telegram avviato in modalità webhook e Scheduler attivo.")

        else:
            logger.error("Impossibile avviare bot e scheduler: applicazione Telegram non inizializzata.")
            bot_status["botRunning"] = False
    except Exception as e:
        logger.exception(f"Errore critico nell'avvio del bot/scheduler (webhook): {e}")
        bot_status["botRunning"] = False

# Punto di ingresso dell'applicazione Flask
if __name__ == '__main__':
    # Avvia il bot Telegram e lo scheduler in un thread separato.
    # Il thread.daemon = True fa sì che il thread del bot si chiuda
    # automaticamente quando l'applicazione Flask principale si chiude.
    bot_thread = Thread(target=run_telegram_bot_and_scheduler_webhooks) # Modificato il target
    bot_thread.daemon = True
    bot_thread.start()

    # Avvia il server Flask.
    logger.info("Avvio del server Flask...")
    # host='0.0.0.0' per renderlo accessibile dall'esterno su Render.com.
    # port=os.getenv('PORT', 5000) usa la porta fornita da Render (o 5000 di default).
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=os.getenv('FLASK_DEBUG', 'False') == 'True')
