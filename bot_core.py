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
import pytz # Aggiunto per la gestione dei fusi orari
import json # Aggiunto per la persistenza JSON

# --- Configurazione del Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Variabili d'Ambiente ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANALE_PROTEZIONE_CIVILE_ID = os.getenv("CANALE_PROTEZIONE_CIVILE_ID")
STATE_FILE = "bot_state.json" # File per salvare lo stato del bot

# --- Variabili Globali per il Bot ---
last_bollettino_link = None
last_bollettino_date = None # Questo sarà un oggetto date
last_successful_check_time = None # Questo sarà un oggetto datetime con timezone
last_check_status = "In attesa del primo controllo."
state_load_time = None # Tempo dell'ultimo caricamento dello stato dal file

# --- Costanti per lo Scraping ---
URL_BOLLETTINO = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"
BASE_URL_SITO = "https://centrofunzionale.regione.basilicata.it"

# --- Inizializzazione Bot Telegram ---
bot = None
if TELEGRAM_BOT_TOKEN:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
else:
    logger.error("TELEGRAM_BOT_TOKEN non è configurato. Il bot non potrà inviare messaggi.")

# --- Configurazione Fuso Orario ---
# Utilizza il fuso orario di Roma per coerenza con l'ora italiana
ROME_TZ = pytz.timezone('Europe/Rome')


# --- Funzioni di Persistenza dello Stato ---
async def load_state_from_file():
    global last_bollettino_link, last_bollettino_date, last_successful_check_time, last_check_status, state_load_time
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            
            # Carica i dati e converti stringhe in oggetti datetime/date
            last_bollettino_link = state.get('last_bollettino_link')
            
            # Converte la stringa ISO della data in oggetto date
            date_str = state.get('last_bollettino_date')
            last_bollettino_date = datetime.fromisoformat(date_str).date() if date_str else None

            # Converte la stringa ISO del datetime in oggetto datetime (timezone-aware)
            time_str = state.get('last_successful_check_time')
            if time_str:
                last_successful_check_time = datetime.fromisoformat(time_str)
                # Assicurati che sia timezone-aware se non lo è già (dal salvataggio)
                if last_successful_check_time.tzinfo is None:
                    last_successful_check_time = ROME_TZ.localize(last_successful_check_time)
            else:
                last_successful_check_time = None
            
            last_check_status = state.get('last_check_status', "Stato caricato dal file.")
            state_load_time = datetime.now(ROME_TZ) # Tempo di caricamento del file

            logger.info(f"Stato del bot caricato da {STATE_FILE}.")
            logger.info(f"Ultimo bollettino caricato: {last_bollettino_date} ({last_bollettino_link})")
            logger.info(f"Ultimo check riuscito caricato: {last_successful_check_time}")
        else:
            logger.info(f"File di stato '{STATE_FILE}' non trovato. Avvio con stato vuoto.")
            state_load_time = datetime.now(ROME_TZ)
    except Exception as e:
        logger.error(f"Errore durante il caricamento dello stato dal file {STATE_FILE}: {e}")
        last_check_status = "Errore durante il caricamento dello stato dal file."
        state_load_time = datetime.now(ROME_TZ) # Registra comunque il tentativo di caricamento

async def save_state_to_file():
    global last_bollettino_link, last_bollettino_date, last_successful_check_time, last_check_status
    try:
        state = {
            'last_bollettino_link': last_bollettino_link,
            'last_bollettino_date': last_bollettino_date.isoformat() if last_bollettino_date else None,
            'last_successful_check_time': last_successful_check_time.isoformat() if last_successful_check_time else None,
            'last_check_status': last_check_status
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
        logger.info(f"Stato del bot salvato in {STATE_FILE}.")
    except Exception as e:
        logger.error(f"Errore durante il salvataggio dello stato nel file {STATE_FILE}: {e}")


# --- Funzione di Scraping Corretta ---
async def get_bollettino_info():
    """
    Funzione per scaricare e parsare le informazioni del bollettino dalla pagina
    della Protezione Civile Basilicata.
    """
    current_bollettino_link = None
    current_bollettino_date = None

    try:
        logger.info(f"Tentativo di scaricare il bollettino da: {URL_BOLLETTINO}")
        response = requests.get(URL_BOLLETTINO, timeout=15)
        response.raise_for_status() # Lancia un'eccezione per status code HTTP di errore

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
                
                logger.debug(f"Link trovato nell'entry: {current_link_from_entry}")
                logger.debug(f"Testo del link (con data) trovato: '{date_text_from_entry}'")

                match = re.search(r'del\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', date_text_from_entry, re.IGNORECASE)
                
                if match:
                    day = match.group(1)
                    month_name = match.group(2).lower()
                    year = match.group(3)

                    mesi_numeri = {
                        'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
                        'maggio': '05', 'giugno': '06', 'luglio': '07', 'agosto': '08',
                        'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12'
                    }
                    
                    month_num = mesi_numeri.get(month_name)

                    if month_num:
                        date_str_formatted = f"{day}/{month_num}/{year}"
                        try:
                            current_bollettino_date = datetime.strptime(date_str_formatted, "%d/%m/%Y").date()
                            logger.info(f"Data parsata con successo: {current_bollettino_date}")
                            current_bollettino_link = current_link_from_entry # Assegna il link trovato
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
    """
    global last_bollettino_link, last_bollettino_date, last_successful_check_time, last_check_status

    logger.info("Avvio controllo aggiornamento bollettino per invio Telegram...")

    link, new_date = await get_bollettino_info()

    # Tempo attuale per aggiornare lo stato del check
    current_check_time = datetime.now(ROME_TZ)

    if link and new_date:
        # Condizione per l'invio:
        # 1. Se è il primo controllo in assoluto (last_bollettino_date è None)
        # 2. Se la data del nuovo bollettino è *più recente* della data dell'ultimo bollettino registrato
        # 3. Se la data è la stessa, ma il link è diverso (potrebbe essere una revisione del bollettino dello stesso giorno)
        if last_bollettino_date is None or \
           new_date > last_bollettino_date or \
           (new_date == last_bollettino_date and link != last_bollettino_link):
            try:
                # Controlla che il bot sia stato inizializzato
                if not bot:
                    raise ValueError("Bot Telegram non inizializzato (TELEGRAM_BOT_TOKEN mancante).")
                if not CANALE_PROTEZIONE_CIVILE_ID:
                    raise ValueError("ID canale Telegram non configurato (CANALE_PROTEZIONE_CIVILE_ID mancante).")

                data_formattata = new_date.strftime("%d/%m/%Y")
                
                # Messaggio standard per il bollettino più recente
                message = (
                    f"🔔 *Nuovo Bollettino di Criticità Regionale disponibile!* 🔔\n\n"
                    f"🗓 Data: `{data_formattata}`\n"
                    f"🔗 [Scarica il bollettino]({link})\n\n"
                    f"Rimani aggiornato sulla situazione."
                )
                
                await bot.send_message(
                    chat_id=CANALE_PROTEZIONE_CIVILE_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                logger.info(f"Bollettino del {data_formattata} (link: {link}) inviato con successo a Telegram.")
                
                last_bollettino_link = link
                last_bollettino_date = new_date
                last_successful_check_time = current_check_time
                last_check_status = f"Ultimo bollettino: {data_formattata} ({link}). Stato: Inviato (più recente)."
                
                await save_state_to_file() # Salva lo stato dopo l'invio
                
            except ValueError as ve:
                logger.error(f"Errore di configurazione Telegram: {ve}")
                last_check_status = f"Errore configurazione Telegram: {ve}"
                last_successful_check_time = current_check_time
            except Exception as e:
                logger.exception(f"Errore durante l'invio del messaggio Telegram: {e}")
                last_check_status = f"Errore invio Telegram: {e}"
                last_successful_check_time = current_check_time

        else: # Nessun nuovo bollettino (stesso o più vecchio)
            logger.info(f"Bollettino del {new_date.strftime('%d/%m/%Y')} (link: {link}) già presente o più vecchio. Nessun nuovo invio.")
            last_successful_check_time = current_check_time
            last_check_status = f"Ultimo bollettino: {new_date.strftime('%d/%m/%Y')} ({link}). Stato: Già presente."

    else:
        logger.warning("Impossibile recuperare il link o la data del bollettino dal sito.")
        last_check_status = "Impossibile recuperare il bollettino dal sito. Controllare i log."
        last_successful_check_time = current_check_time # Aggiorna l'orario anche se fallisce

# --- Funzione per ottenere lo stato del bot (per l'API Flask) ---
def get_bot_status():
    """Restituisce lo stato corrente del bot."""
    return {
        "last_bollettino_link": last_bollettino_link,
        "last_bollettino_date": str(last_bollettino_date) if last_bollettino_date else "N/A",
        # Converti il datetime timezone-aware in stringa ISO per una corretta trasmissione e parsing JS
        "last_successful_check_time": last_successful_check_time.isoformat() if last_successful_check_time else "N/A",
        "last_check_status": last_check_status,
        "state_load_time": state_load_time.isoformat() if state_load_time else "N/A"
    }

# --- Scheduler per i controlli automatici ---
scheduler = AsyncIOScheduler(timezone=ROME_TZ) # Imposta il fuso orario per lo scheduler

async def start_scheduler():
    """Avvia lo scheduler e aggiunge il job."""
    # Esegui il check ogni 15 minuti.
    scheduler.add_job(check_and_send_bollettino, 'interval', minutes=15)
    scheduler.start()
    logger.info("Scheduler avviato. Controllo bollettino ogni 15 minuti.")

async def stop_scheduler():
    """Arresta lo scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler arrestato.")

# --- Esecuzione iniziale del check all'avvio ---
async def initial_check():
    logger.info("Eseguo il check iniziale del bollettino all'avvio.")
    await check_and_send_bollettino()
