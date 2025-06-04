from flask import Flask, render_template, jsonify, request # Importa request per la health check se vuoi
import asyncio
import threading
import time
import os
import bot_core # Importa il modulo bot_core

app = Flask(__name__)

# Flag per assicurarsi che l'inizializzazione e il loop periodico avvengano una sola volta
_bot_initialized = False
_periodic_check_thread = None

async def _initial_setup_and_start_periodic_check():
    """
    Funzione asincrona per inizializzare il bot e avviare il check periodico.
    Viene eseguita una volta all'avvio del thread.
    """
    global _bot_initialized
    if not _bot_initialized:
        print("Avvio inizializzazione bot e database...")
        await bot_core.initialize_bot()
        _bot_initialized = True
        print("Inizializzazione bot completata.")
        
        print("Avvio loop di controllo periodico...")
        await periodic_check() # Avvia il loop di controllo periodico

def run_async_loop_in_thread():
    """Funzione target per il thread, esegue un loop asincrono."""
    # Crea un nuovo loop di eventi per questo thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Esegue l'inizializzazione e il loop periodico in questo thread
    loop.run_until_complete(_initial_setup_and_start_periodic_check())
    loop.close()

# Inizia il thread di background all'avvio dell'applicazione
# Questo codice verrà eseguito quando l'app.py viene eseguito,
# che è il comportamento desiderato su Render con il Procfile.
if __name__ == '__main__':
    # Se esegui localmente, assicurati che le variabili d'ambiente siano impostate
    if os.getenv('TELEGRAM_BOT_TOKEN') is None:
        print("Variabili d'ambiente TELEGRAM_BOT_TOKEN, CANALE_PROTEZIONE_CIVILE_ID o DATABASE_URL non impostate. Impostale per testare localmente.")
        # Esempio per test locale (NON usare in produzione)
        # os.environ['TELEGRAM_BOT_TOKEN'] = 'YOUR_BOT_TOKEN'
        # os.environ['CANALE_PROTEZIONE_CIVILE_ID'] = '-1001234567890' # Sostituisci con l'ID del tuo canale
        # os.environ['DATABASE_URL'] = 'postgres://user:password@host:port/dbname' # La tua stringa Neon.tech
        # os.environ['CHECK_INTERVAL_SECONDS'] = '900'

    # Avvia il thread di background per il controllo periodico e l'inizializzazione del bot
    _periodic_check_thread = threading.Thread(target=run_async_loop_in_thread, daemon=True)
    _periodic_check_thread.start()
    
    # Avvia l'app Flask
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 10000)))

# --- Funzioni del Bot (restano uguali, le importi da bot_core) ---
# ... le tue rotte Flask che chiamano bot_core.get_bot_status() etc.
# Le rotte asincrone sono gestite direttamente da Flask 3.x con ASGI
# quindi non serve più @app.before_first_request per questo specifico scopo.

async def periodic_check():
    """Loop di controllo periodico."""
    while True:
        await bot_core.check_for_new_bulletin()
        await asyncio.sleep(int(os.getenv('CHECK_INTERVAL_SECONDS', 900))) # Controlla ogni 15 minuti di default

# --- Rotte Flask ---
@app.route('/')
async def dashboard():
    status = await bot_core.get_bot_status()
    return render_template('index.html', status=status)

@app.route('/trigger_check')
async def trigger_check():
    success = await bot_core.trigger_manual_check_from_flask()
    if success:
        return jsonify({"status": "success", "message": "Controllo manuale avviato e bollettino inviato se nuovo."})
    else:
        # Questo messaggio copre sia "nessun nuovo bollettino" che "errore durante l'invio"
        return jsonify({"status": "info", "message": "Controllo manuale avviato. Nessun nuovo bollettino o errore nell'invio."})

@app.route('/health')
def health_check():
    # Una semplice health check che non dipende dallo stato interno del bot asincrono
    # ma assicura che il server web sia in ascolto.
    return "OK", 200
