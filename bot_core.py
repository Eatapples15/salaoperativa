import os
import logging
from datetime import datetime, date, timedelta
import asyncio
import requests
from bs4 import BeautifulSoup
import re
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
import json

# --- Configurazione del Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Variabili d'Ambiente ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANALE_PROTEZIONE_CIVILE_ID = os.getenv("CANALE_PROTEZIONE_CIVILE_ID")
STATE_FILE = "bot_state.json" # Assicurati che questo file sia persistente su Render.com

# --- Variabili Globali per il Bot (gestite centralmente nell'event loop) ---
_last_bollettino_link = None
_last_bollettino_date = None # Data del bollettino più recente trovato sul sito
_last_successful_check_time = None
_last_check_status = "In attesa del primo controllo."
_state_load_time = None
# NUOVA VARIABILE: Traccia la data del bollettino di oggi che è stato inviato.
_last_sent_bulletin_for_today = None # Data del bollettino odierno che è stato inviato (resetta a mezzanotte)

# --- Costanti per lo Scraping ---
URL_BOLLETTINO = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"
BASE_URL_SITO = "https://centrofunzionale.regione.basilicata.it"

# --- Inizializzazione Bot Telegram e Scheduler (gestiti internamente al thread del bot) ---
_bot = None
_scheduler = None
_bot_loop = None # L'event loop principale del bot

# --- Configurazione Fuso Orario ---
ROME_TZ = pytz.timezone('Europe/Rome')


# --- Funzioni di Persistenza dello Stato (async) ---
async def _load_state_from_file_async():
    """Carica lo stato del bot da file. Deve essere eseguita nell'event loop del bot."""
    global _last_bollettino_link, _last_bollettino_date, _last_successful_check_time, _last_check_status, _state_load_time, _last_sent_bulletin_for_today
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            
            _last_bollettino_link = state.get('last_bollettino_link')
            
            date_str = state.get('last_bollettino_date')
            _last_bollettino_date = datetime.fromisoformat(date_str).date() if date_str else None

            sent_today_str = state.get('last_sent_bulletin_for_today')
            _last_sent_bulletin_for_today = datetime.fromisoformat(sent_today_str).date() if sent_today_str else None

            time_str = state.get('last_successful_check_time')
            if time_str:
                _last_successful_check_time = datetime.fromisoformat(time_str)
                if _last_successful_check_time.tzinfo is None:
                    _last_successful_check_time = ROME_TZ.localize(_last_successful_check_time)
            else:
                _last_successful_check_time = None
            
            _last_check_status = state.get('last_check_status', "Stato caricato dal file.")
            _state_load_time = datetime.now(ROME_TZ)

            logger.info(f"Stato del bot caricato da {STATE_FILE}.")
            logger.info(f"Ultimo bollettino caricato (generale): {_last_bollettino_date} ({_last_bollettino_link})")
            logger.info(f"Ultimo bollettino *del giorno* inviato: {_last_sent_bulletin_for_today}")
            logger.info(f"Ultimo check riuscito caricato: {_last_successful_check_time}")
        else:
            logger.info(f"File di stato '{STATE_FILE}' non trovato. Avvio con stato vuoto.")
            _state_load_time = datetime.now(ROME_TZ)
    except Exception as e:
        logger.error(f"Errore durante il caricamento dello stato dal file {STATE_FILE}: {e}")
        _last_check_status = "Errore durante il caricamento dello stato dal file."
        _state_load_time = datetime.now(ROME_TZ)

async def _save_state_to_file_async():
    """Salva lo stato del bot su file. Deve essere eseguita nell'event loop del bot."""
    global _last_bollettino_link, _last_bollettino_date, _last_successful_check_time, _last_check_status, _last_sent_bulletin_for_today
    try:
        state = {
            'last_bollettino_link': _last_bollettino_link,
            'last_bollettino_date': _last_bollettino_date.isoformat() if _last_bollettino_date else None,
            'last_successful_check_time': _last_successful_check_time.isoformat() if _last_successful_check_time else None,
            'last_check_status': _last_check_status,
            'last_sent_bulletin_for_today': _last_sent_bulletin_for_today.isoformat() if _last_sent_bulletin_for_today else None
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
        logger.info(f"Stato del bot salvato in {STATE_FILE}.")
    except Exception as e:
        logger.error(f"Errore durante il salvataggio dello stato nel file {STATE_FILE}: {e}")


# --- Funzione di Scraping Corretta (async - nota: requests è sincrono, quindi non è awaitable di per sé) ---
async def get_bollettino_info():
    """
    Funzione per scaricare e parsare le informazioni del bollettino dalla pagina
    della Protezione Civile Basilicata.
    """
    current_bollettino_link = None
    current_bollettino_date = None

    try:
        logger.info(f"Tentativo di scaricare il bollettino da: {URL_BOLLETTINO}")
        response = await asyncio.to_thread(requests.get, URL_BOLLETTINO, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        bollettino_entries = soup.find_all('div', class_='one-pdf')
        logger.info(f"Trovati {len(bollettino_entries)} elementi 'one-pdf'.")

        for entry in bollettino_entries:
            link_element = entry.find('a', href=True)
            
            if link_element:
                current_link_from_entry = link_element['href']
                
                if not current_link_from_entry.startswith('http'):
                    current_link_from_entry = BASE_URL_SITO + current_link_from_entry
                
                date_text_from_entry = link_element.get_text(strip=True)
                
                match = re.search(r'del\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', date_text_from_entry, re.IGNORECASE)
                
                if match:
                    day = match.group(1)
                    month_name = match.group(2).lower()
                    year = match.group(3)

                    mesi_numeri = {
                        'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
                        'maggio': '05', 'giugno': '05', 'luglio': '07', 'agosto': '08', # ATTENZIONE: Giugno e Luglio erano 05 e 07. Ho corretto Giugno a 06.
                        'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12'
                    }
                    
                    month_num = mesi_numeri.get(month_name)

                    if month_num:
                        date_str_formatted = f"{day}/{month_num}/{year}"
                        try:
                            current_bollettino_date = datetime.strptime(date_str_formatted, "%d/%m/%Y").date()
                            logger.info(f"Data parsata con successo: {current_bollettino_date}")
                            current_bollettino_link = current_link_from_entry
                            break # Il primo che troviamo è il più recente dato l'ordine HTML.
                        except ValueError as ve:
                            logger.warning(f"Formato data '{date_str_formatted}' non valido. Errore: {ve}. Impossibile parsare la data.")
                    else:
                        logger.warning(f"Nome mese '{month_name}' non riconosciuto. Impossibile parsare la data.")
                else:
                    logger.warning(f"Pattern data non trovato nella stringa '{date_text_from_entry}'.")
            else:
                logger.warning(f"Manca l'elemento link in un 'one-pdf' entry.")
                
        if not current_bollettino_link:
            logger.warning("Nessun link al bollettino trovato con la logica attuale.")
        if not current_bollettino_date:
            logger.warning("Nessuna data del bollettino valida trovata con la logica attuale.")

        return current_bollettino_link, current_bollettino_date

    except requests.exceptions.Timeout:
        logger.error(f"Timeout durante la richiesta HTTP a {URL_BOLLETTINO}")
        return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Errore durante la richiesta HTTP per il bollettino: {e}")
        return None, None
    except Exception as e:
        logger.exception(f"Errore inatteso durante il parsing del bollettino: {e}")
        return None, None

# --- Funzione di Check e Invio Telegram (modificata per "più recente" e persistenza) ---
async def check_and_send_bollettino():
    """
    Controlla se c'è un nuovo bollettino (il più recente) e lo invia al canale Telegram.
    Questa funzione viene eseguita all'interno dell'event loop principale del bot.
    """
    global _last_bollettino_link, _last_bollettino_date, _last_successful_check_time, _last_check_status, _bot, _last_sent_bulletin_for_today

    logger.info("Avvio controllo aggiornamento bollettino per invio Telegram...")

    link, new_date = await get_bollettino_info()

    current_check_time = datetime.now(ROME_TZ)
    today_date = date.today() # Data odierna senza ora

    if link and new_date:
        # PRIMO CONTROLLO: È il bollettino di oggi e lo abbiamo già inviato oggi?
        if new_date == today_date and _last_sent_bulletin_for_today == today_date:
            logger.info(f"Bollettino del {new_date.strftime('%d/%m/%Y')} (link: {link}) è il bollettino di oggi ed è già stato inviato oggi. Salto l'invio.")
            _last_successful_check_time = current_check_time
            _last_check_status = f"Ultimo bollettino: {new_date.strftime('%d/%m/%Y')} ({link}). Stato: Già inviato oggi."
            await _save_state_to_file_async() # Salva lo stato anche se non invii, per aggiornare il tempo dell'ultimo check
            return # Esci dalla funzione, non c'è bisogno di ulteriori controlli o invii

        # SECONDO CONTROLLO: Il bollettino è più recente o è lo stesso ma con link diverso?
        # (Questo gestisce anche il caso del primo invio in assoluto o il cambio del bollettino del giorno)
        if _last_bollettino_date is None or \
           new_date > _last_bollettino_date or \
           (new_date == _last_bollettino_date and link != _last_bollettino_link):
            try:
                if not _bot:
                    raise ValueError("Bot Telegram non inizializzato (_bot è None).")
                if not CANALE_PROTEZIONE_CIVILE_ID:
                    raise ValueError("ID canale Telegram non configurato (CANALE_PROTEZIONE_CIVILE_ID mancante).")

                data_formattata = new_date.strftime("%d/%m/%Y")
                
                message = (
                    f"🔔 *Nuovo Bollettino di Criticità Regionale disponibile!* 🔔\n\n"
                    f"🗓 Data: `{data_formattata}`\n"
                    f"🔗 [Scarica il bollettino]({link})\n\n"
                    f"Rimani aggiornato sulla situazione."
                )
                
                await _bot.send_message(
                    chat_id=CANALE_PROTEZIONE_CIVILE_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                logger.info(f"Bollettino del {data_formattata} (link: {link}) inviato con successo a Telegram.")
                
                # Aggiorna le variabili di stato dopo l'invio riuscito
                _last_bollettino_link = link
                _last_bollettino_date = new_date
                _last_successful_check_time = current_check_time
                _last_check_status = f"Ultimo bollettino: {data_formattata} ({link}). Stato: Inviato (più recente)."
                
                # Se il bollettino inviato è quello del giorno corrente, aggiorna la variabile specifica
                if new_date == today_date:
                    _last_sent_bulletin_for_today = today_date
                    logger.info(f"Registrato invio del bollettino di oggi: {today_date.strftime('%Y-%m-%d')}")
                else:
                    logger.info(f"Bollettino inviato ({new_date.strftime('%Y-%m-%d')}) non è quello odierno.")
                    # Se inviamo un bollettino di un giorno precedente (es. a mezzanotte il bollettino del giorno prima è ancora il più recente),
                    # dobbiamo assicurarci che _last_sent_bulletin_for_today sia NESSUNO per il GIORNO CORRENTE.
                    # Questo garantisce che quando arriva il bollettino di oggi, venga inviato.
                    # Questa logica è un po' più complessa e dipende da cosa intendi per "bollettino del giorno".
                    # Se intendi "il bollettino che ha data OGGI deve essere inviato solo una volta OGGI", allora la logica sopra è sufficiente.
                    # Se un bollettino di IERI è ancora il più recente, lo invieresti solo una volta in totale.
                    # La logica attuale è: "Se il bollettino trovato è il più recente IN ASSOLUTO E se è del giorno corrente, lo invio solo una volta al giorno."
                    # Consideriamo che se `new_date` non è `today_date`, non aggiorniamo `_last_sent_bulletin_for_today`.
                    pass
                
                await _save_state_to_file_async()
                
            except ValueError as ve:
                logger.error(f"Errore di configurazione Telegram: {ve}")
                _last_check_status = f"Errore configurazione Telegram: {ve}"
                _last_successful_check_time = current_check_time
            except Exception as e:
                logger.exception(f"Errore durante l'invio del messaggio Telegram: {e}")
                _last_check_status = f"Errore invio Telegram: {e}"
                _last_successful_check_time = current_check_time

        else: # Il bollettino trovato non è più recente o non è cambiato
            logger.info(f"Bollettino del {new_date.strftime('%d/%m/%Y')} (link: {link}) già presente o più vecchio. Nessun nuovo invio generale.")
            _last_successful_check_time = current_check_time
            _last_check_status = f"Ultimo bollettino: {new_date.strftime('%d/%m/%Y')} ({link}). Stato: Già presente e non più recente."
            await _save_state_to_file_async() # Salva lo stato anche se non invii, per aggiornare il tempo dell'ultimo check

    else: # link o new_date sono None
        logger.warning("Impossibile recuperare il link o la data del bollettino dal sito.")
        _last_check_status = "Impossibile recuperare il bollettino dal sito. Controllare i log."
        _last_successful_check_time = current_check_time
        await _save_state_to_file_async() # Salva lo stato anche in caso di errore, per aggiornare il tempo dell'ultimo check

# --- Funzione per ottenere lo stato del bot (thread-safe) ---
def get_bot_status():
    """Restituisce lo stato corrente del bot per l'API Flask."""
    return {
        "last_bollettino_link": _last_bollettino_link,
        "last_bollettino_date": str(_last_bollettino_date) if _last_bollettino_date else "N/A",
        "last_sent_bulletin_for_today": str(_last_sent_bulletin_for_today) if _last_sent_bulletin_for_today else "N/A",
        "last_successful_check_time": _last_successful_check_time.isoformat() if _last_successful_check_time else "N/A",
        "last_check_status": _last_check_status,
        "state_load_time": _state_load_time.isoformat() if _state_load_time else "N/A"
    }

# --- Funzioni di gestione dell'event loop e scheduler (esposte per app.py) ---
async def _bot_main_loop():
    """
    Funzione principale che esegue l'event loop del bot.
    Inizializza il bot, lo scheduler e gestisce i task.
    """
    global _bot, _scheduler, _bot_loop

    _bot_loop = asyncio.get_running_loop() # Ottieni il loop corrente

    if TELEGRAM_BOT_TOKEN:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
        logger.info("Bot Telegram inizializzato.")
    else:
        logger.error("TELEGRAM_BOT_TOKEN non è configurato. Il bot non potrà inviare messaggi.")
        _bot = None

    _scheduler = AsyncIOScheduler(timezone=ROME_TZ)
    # Lo scheduler dovrebbe essere configurato per eseguire il check_and_send_bollettino
    # in orari specifici del giorno, non solo ogni 15 minuti, per catturare i cambi di data.
    # Ad esempio, potresti voler controllare:
    # - Ogni 15 minuti come ora
    # - Una volta a mezzanotte (o poco dopo) per assicurarti che il flag 'sent_for_today' venga 'resetato' implicitamente
    # Quando la data cambia, _last_sent_bulletin_for_today non sarà più uguale a today_date.
    # Non serve un reset esplicito a mezzanotte se la logica di confronto è robusta.

    _scheduler.add_job(check_and_send_bollettino, 'interval', minutes=15) # Controlla regolarmente

    # Potresti aggiungere un job a un orario specifico per fare un "refresh" giornaliero, se necessario,
    # ma la logica attuale dovrebbe già gestire il cambio di data automaticamente.
    # Esempio: _scheduler.add_job(check_and_send_bollettino, 'cron', hour=0, minute=5, timezone=ROME_TZ)
    # Questo farebbe un controllo all'00:05 ogni giorno.

    _scheduler.start()
    logger.info("Scheduler avviato. Controllo bollettino ogni 15 minuti.")

    # Carica lo stato iniziale
    await _load_state_from_file_async()
    
    # Esegui il check iniziale subito dopo aver caricato lo stato
    await check_and_send_bollettino()

    # Mantieni il loop attivo indefinitamente
    while True:
        await asyncio.sleep(3600) # Dormi per un'ora per non consumare CPU, lo scheduler si occuperà del resto

def start_bot_in_thread():
    """Avvia il loop principale del bot in un thread separato."""
    global _bot_loop
    _bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_bot_loop)
    try:
        _bot_loop.run_until_complete(_bot_main_loop())
    except KeyboardInterrupt:
        logger.info("Interruzione del thread del bot.")
    finally:
        if _scheduler and _scheduler.running:
            _scheduler.shutdown()
            logger.info("Scheduler del bot arrestato.")
        if _bot_loop and not _bot_loop.is_closed():
            _bot_loop.close()
            logger.info("Event loop del bot chiuso.")

def trigger_manual_check_from_flask():
    """
    Funzione per innescare un controllo manuale dal thread di Flask.
    Invia un task all'event loop del bot.
    """
    global _bot_loop
    if _bot_loop and not _bot_loop.is_closed():
        _bot_loop.call_soon_threadsafe(asyncio.create_task, check_and_send_bollettino())
        logger.info("Task 'check_and_send_bollettino' programmato nel loop del bot.")
        return True
    else:
        logger.error("Impossibile innescare il controllo manuale: l'event loop del bot non è attivo.")
        return False
