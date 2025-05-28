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

# --- Configurazione del Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Variabili d'Ambiente ---
# Assicurati che queste variabili siano impostate nel tuo ambiente Render
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANALE_PROTEZIONE_CIVILE_ID = os.getenv("CANALE_PROTEZIONE_CIVILE_ID")

# --- Variabili Globali per il Bot ---
# Questi valori vengono aggiornati dopo ogni check del bollettino
last_bollettino_link = None
last_bollettino_date = None
last_successful_check_time = None
last_check_status = "In attesa del primo controllo."

# --- Costanti per lo Scraping ---
URL_BOLLETTINO = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"
BASE_URL_SITO = "https://centrofunzionale.regione.basilicata.it"

# --- Inizializzazione Bot Telegram ---
bot = None
if TELEGRAM_BOT_TOKEN:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
else:
    logger.error("TELEGRAM_BOT_TOKEN non è configurato. Il bot non potrà inviare messaggi.")

# --- Funzione di Scraping Corretta ---
async def get_bollettino_info():
    """
    Funzione per scaricare e parsare le informazioni del bollettino dalla pagina
    della Protezione Civile Basilicata.
    """
    global last_bollettino_date # Per poter modificare la variabile globale
    
    current_bollettino_link = None
    current_bollettino_date = None

    try:
        logger.info(f"Tentativo di scaricare il bollettino da: {URL_BOLLETTINO}")
        response = requests.get(URL_BOLLETTINO, timeout=15)
        response.raise_for_status() # Lancia un'eccezione per status code HTTP di errore

        soup = BeautifulSoup(response.text, 'html.parser')

        # Cerca tutti i div che hanno la classe ESATTA 'one-pdf'
        # Questa è la correzione chiave basata sull'HTML fornito.
        bollettino_entries = soup.find_all('div', class_='one-pdf')
        logger.info(f"Trovati {len(bollettino_entries)} elementi 'one-pdf'.")

        # Itera sulle entry. Il primo elemento trovato dovrebbe essere il più recente.
        for entry in bollettino_entries:
            link_element = entry.find('a', href=True)
            
            if link_element: # Basta che ci sia il link per iniziare
                current_link_from_entry = link_element['href']
                
                # Assicurati che il link sia assoluto
                if not current_link_from_entry.startswith('http'):
                    current_link_from_entry = BASE_URL_SITO + current_link_from_entry
                
                # La data è nel testo del link <a>
                # Es: "Bollettino del 27 maggio 2025"
                date_text_from_entry = link_element.get_text(strip=True)
                
                logger.info(f"Link trovato nell'entry: {current_link_from_entry}")
                logger.info(f"Testo del link (con data) trovato: '{date_text_from_entry}'")

                # Regex per estrarre la data (giorno, mese, anno) dal testo del link
                # Formato atteso nel testo del link: "Bollettino del GG MESE_NOME AAAA"
                match = re.search(r'del\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', date_text_from_entry, re.IGNORECASE)
                
                if match:
                    day = match.group(1)
                    month_name = match.group(2).lower()
                    year = match.group(3)

                    # Mappa i nomi dei mesi in italiano ai numeri
                    mesi_numeri = {
                        'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
                        'maggio': '05', 'giugno': '06', 'luglio': '07', 'agosto': '08',
                        'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12'
                    }
                    
                    month_num = mesi_numeri.get(month_name)

                    if month_num:
                        date_str_formatted = f"{day}/{month_num}/{year}"
                        try:
                            # Converte la stringa della data in un oggetto date
                            current_bollettino_date = datetime.strptime(date_str_formatted, "%d/%m/%Y").date()
                            logger.info(f"Data parsata con successo: {current_bollettino_date}")
                            current_bollettino_link = current_link_from_entry # Assegna il link trovato
                            # Il primo che troviamo è il più recente dato l'ordine HTML.
                            break 
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

# --- Funzione di Check e Invio Telegram ---
async def check_and_send_bollettino():
    """
    Controlla se c'è un nuovo bollettino e lo invia al canale Telegram.
    """
    global last_bollettino_link, last_bollettino_date, last_successful_check_time, last_check_status

    logger.info("Avvio controllo aggiornamento bollettino per invio Telegram...")

    link, new_date = await get_bollettino_info()

    if link and new_date:
        # Se è la prima volta che controlliamo o il bollettino è più recente
        if last_bollettino_date is None or new_date > last_bollettino_date:
            try:
                # Controlla che il bot sia stato inizializzato
                if not bot:
                    raise ValueError("Bot Telegram non inizializzato (TELEGRAM_BOT_TOKEN mancante).")
                if not CANALE_PROTEZIONE_CIVILE_ID:
                    raise ValueError("ID canale Telegram non configurato (CANALE_PROTEZIONE_CIVILE_ID mancante).")

                # Formatta la data per il messaggio
                data_formattata = new_date.strftime("%d/%m/%Y")
                
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
                    disable_web_page_preview=True # Per evitare preview automatiche
                )
                logger.info(f"Bollettino del {data_formattata} inviato con successo a Telegram.")
                
                last_bollettino_link = link
                last_bollettino_date = new_date
                last_successful_check_time = datetime.now()
                last_check_status = f"Ultimo bollettino: {data_formattata} ({link}). Stato: Inviato."

            except ValueError as ve:
                logger.error(f"Errore di configurazione Telegram: {ve}")
                last_check_status = f"Errore configurazione Telegram: {ve}"
            except Exception as e:
                logger.exception(f"Errore durante l'invio del messaggio Telegram: {e}")
                last_check_status = f"Errore invio Telegram: {e}"
        elif new_date == last_bollettino_date:
            logger.info(f"Bollettino del {new_date.strftime('%d/%m/%Y')} già presente e aggiornato. Nessun nuovo invio.")
            last_successful_check_time = datetime.now()
            last_check_status = f"Ultimo bollettino: {new_date.strftime('%d/%m/%Y')} ({link}). Stato: Già presente."
        else: # new_date < last_bollettino_date
            logger.warning(f"Bollettino trovato ({new_date.strftime('%d/%m/%Y')}) è più vecchio dell'ultimo registrato ({last_bollettino_date.strftime('%d/%m/%Y')}). Nessun invio.")
            last_successful_check_time = datetime.now()
            last_check_status = f"Bollettino trovato ({new_date.strftime('%d/%m/%Y')}) più vecchio. Nessun invio."
    else:
        logger.warning("Impossibile recuperare il link o la data del bollettino dal sito.")
        last_check_status = "Impossibile recuperare il bollettino dal sito. Controllare i log."

# --- Funzione per ottenere lo stato del bot (per l'API Flask) ---
def get_bot_status():
    """Restituisce lo stato corrente del bot."""
    return {
        "last_bollettino_link": last_bollettino_link,
        "last_bollettino_date": str(last_bollettino_date) if last_bollettino_date else "N/A",
        "last_successful_check_time": str(last_successful_check_time) if last_successful_check_time else "N/A",
        "last_check_status": last_check_status
    }

# --- Scheduler per i controlli automatici ---
scheduler = AsyncIOScheduler()

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
