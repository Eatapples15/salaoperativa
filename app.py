from flask import Flask, render_template, jsonify
import asyncio
import threading
import time
import os
import bot_core # Importa il modulo bot_core

app = Flask(__name__)

# --- Inizializzazione del Bot e del Database ---
# Questo viene eseguito una volta all'avvio dell'applicazione Flask.
# Utilizziamo un thread separato per l'inizializzazione asincrona
# o la chiamiamo direttamente in un contesto asincrono se il server lo supporta.
# Dato che Render esegue `python app.py`, possiamo chiamarla direttamente qui
# e far partire il loop asincrono per i controlli periodici.

# Flag per assicurarsi che l'inizializzazione avvenga una sola volta
_bot_initialized = False

async def _initial_setup():
    global _bot_initialized
    if not _bot_initialized:
        print("Avvio inizializzazione bot e database...")
        await bot_core.initialize_bot()
        _bot_initialized = True
        print("Inizializzazione bot completata.")

# Esegui l'inizializzazione all'avvio dell'app Flask
# Questo è un modo semplice per eseguire async code all'avvio di Flask
# in un ambiente WSGI come Gunicorn/Render.
# Una soluzione più robusta potrebbe usare un background task runner
# ma per un semplice check all'avvio, questo va bene.
@app.before_first_request
def setup_bot_on_startup():
    asyncio.run(_initial_setup())
    # Avvia il loop di controllo periodico in un thread separato
    thread = threading.Thread(target=run_async_in_thread, daemon=True)
    thread.start()

def run_async_in_thread():
    """Funzione per eseguire il loop asincrono in un thread separato."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(periodic_check())
    loop.close()

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
        return jsonify({"status": "info", "message": "Controllo manuale avviato. Nessun nuovo bollettino o errore."})

@app.route('/health')
def health_check():
    return "OK", 200

if __name__ == '__main__':
    # Se esegui localmente, assicurati che le variabili d'ambiente siano impostate
    if os.getenv('TELEGRAM_BOT_TOKEN') is None:
        print("Variabili d'ambiente TELEGRAM_BOT_TOKEN, CANALE_PROTEZIONE_CIVILE_ID o DATABASE_URL non impostate. Impostale per testare localmente.")
        # Esempio per test locale (NON usare in produzione)
        # os.environ['TELEGRAM_BOT_TOKEN'] = 'YOUR_BOT_TOKEN'
        # os.environ['CANALE_PROTEZIONE_CIVILE_ID'] = '-1001234567890' # Sostituisci con l'ID del tuo canale
        # os.environ['DATABASE_URL'] = 'postgres://user:password@host:port/dbname' # La tua stringa Neon.tech
    
    # Avvia l'app Flask
    app.run(host='0.0.0.0', port=os.getenv('PORT', 10000))
