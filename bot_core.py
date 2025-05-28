import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import datetime
import os
import asyncio
import re # Importa il modulo re per le espressioni regolari

# --- Configurazione del Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variabili d'Ambiente ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANALE_PROTEZIONE_CIVILE_ID = os.getenv("CANALE_PROTEZIONE_CIVILE_ID")
URL_BOLLETTINO = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"
BASE_URL_SITO = "https://centrofunzionale.regione.basilicata.it" # Aggiunto per gestire URL relativi

# --- Stato Globale (In Memoria) ---
last_processed_bollettino_date = None

def init_telegram_application():
    """Inizializza l'istanza dell'applicazione Telegram e aggiunge gli handler."""
    logger.info(f"Tentativo di inizializzare l'applicazione. Token letto (prime 5 car): {TELEGRAM_BOT_TOKEN[:5] if TELEGRAM_BOT_TOKEN else 'None'}")
    logger.info(f"ID Canale letto: {CANALE_PROTEZIONE_CIVILE_ID if CANALE_PROTEZIONE_CIVILE_ID else 'None'}")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("ERRORE: TELEGRAM_BOT_TOKEN non impostato. Il bot non può avviarsi.")
        return None
    
    try:
        app_instance = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        # Aggiungi gli handler
        app_instance.add_handler(CommandHandler("start", start_command))
        app_instance.add_handler(CommandHandler("aggiorna", aggiorna_manuale_command))
        logger.info("Applicazione Telegram inizializzata con successo e handler aggiunti.")
        return app_instance
    except Exception as e:
        logger.exception(f"ERRORE: Impossibile inizializzare l'applicazione Telegram: {e}")
        return None

async def get_bollettino_info():
    """
    Funzione per scaricare e parsare le informazioni del bollettino dalla pagina
    della Protezione Civile Basilicata.
    """
    try:
        logger.info(f"Tentativo di scaricare il bollettino da: {URL_BOLLETTINO}")
        response = requests.get(URL_BOLLETTINO, timeout=15)
        response.raise_for_status() # Lancia un'eccezione per status code HTTP di errore

        soup = BeautifulSoup(response.text, 'html.parser')

        bollettino_link = None
        bollettino_date = None

        # Cerca tutti i div che contengono i link e le date dei bollettini
        # Dallo screenshot, la classe è 'div-one-pdf'
        bollettino_entries = soup.find_all('div', class_='div-one-pdf')

        # Itera sulle entry. Il primo elemento trovato dovrebbe essere il più recente.
        for entry in bollettino_entries:
            link_element = entry.find('a', href=True)
            text_date_element = entry.find('div', class_='div-one-pdf-text') # Elemento che contiene la data testuale

            if link_element and text_date_element:
                current_link = link_element['href']
                
                # Assicurati che il link sia assoluto
                if not current_link.startswith('http'):
                    current_link = BASE_URL_SITO + current_link
                
                date_text = text_date_element.get_text(strip=True) # E.g., "Bollettino del 27 maggio 2025"
                
                # Regex per estrarre la data (giorno, mese, anno)
                # Formato atteso: "Bollettino del GG MESE_NOME AAAA"
                match = re.search(r'del\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', date_text, re.IGNORECASE)
                
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
                            bollettino_date = datetime.datetime.strptime(date_str_formatted, "%d/%m/%Y").date()
                            # Se abbiamo trovato link e data, possiamo uscire dal loop,
                            # presumendo che il primo sia il più recente.
                            bollettino_link = current_link
                            break # Esci dal ciclo for una volta trovato il primo bollettino valido
                        except ValueError:
                            logger.warning(f"Formato data '{date_str_formatted}' non valido. Impossibile parsare la data.")
                            bollettino_date = None
                    else:
                        logger.warning(f"Nome mese '{month_name}' non riconosciuto. Impossibile parsare la data.")
                else:
                    logger.warning(f"Pattern data non trovato nella stringa '{date_text}'.")

        if not bollettino_link:
            logger.warning("Nessun link al bollettino trovato con la logica attuale.")
        if not bollettino_date:
            logger.warning("Nessuna data del bollettino valida trovata con la logica attuale.")

        return bollettino_link, bollettino_date

    except requests.exceptions.Timeout:
        logger.error(f"Timeout durante la richiesta HTTP a {URL_BOLLETTINO}")
        return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Errore durante la richiesta HTTP per il bollettino: {e}")
        return None, None
    except Exception as e:
        logger.exception(f"Errore inatteso durante il parsing del bollettino: {e}")
        return None, None

async def send_bollettino_update_to_telegram(app_instance: Application):
    """
    Controlla se c'è un nuovo bollettino e, se sì, lo invia al canale Telegram.
    """
    global last_processed_bollettino_date
    
    logger.info("Avvio controllo aggiornamento bollettino per invio Telegram...")
    link, current_bollettino_date = await get_bollettino_info()

    if link and current_bollettino_date:
        logger.info(f"Bollettino trovato: Data={current_bollettino_date}, Link={link}")
        
        # Confronta con la data dell'ultimo bollettino processato
        if last_processed_bollettino_date is None or current_bollettino_date > last_processed_bollettino_date:
            message = (
                f"**🚨 AGGIORNAMENTO BOLLETTINO CRITICITÀ 🚨**\n\n"
                f"**Protezione Civile Regione Basilicata**\n\n"
                f"Data del bollettino: `{current_bollettino_date.strftime('%d/%m/%Y')}`\n\n"
                f"Clicca qui per i dettagli: [Bollettino Odierno]({link})"
            )
            try:
                if CANALE_PROTEZIONE_CIVILE_ID:
                    try:
                        chat_id_int = int(CANALE_PROTEZIONE_CIVILE_ID)
                    except ValueError:
                        logger.error(f"CANALE_PROTEZIONE_CIVILE_ID '{CANALE_PROTEZIONE_CIVILE_ID}' non è un numero intero valido.")
                        return {"success": False, "message": "ID canale Telegram non valido."}

                    await app_instance.bot.send_message(
                        chat_id=chat_id_int,
                        text=message,
                        parse_mode='Markdown',
                        disable_web_page_preview=True
                    )
                    logger.info(f"Bollettino {current_bollettino_date.strftime('%d/%m/%Y')} inviato al canale {CANALE_PROTEZIONE_CIVILE_ID}")
                    last_processed_bollettino_date = current_bollettino_date
                    return {"success": True, "message": f"Bollettino {current_bollettino_date.strftime('%d/%m/%Y')} controllato e inviato (nuovo)."}
                else:
                    logger.error("CANALE_PROTEZIONE_CIVILE_ID non impostato. Impossibile inviare il messaggio.")
                    return {"success": False, "message": "ID canale Telegram non configurato. Impossibile inviare."}
            except Exception as e:
                logger.exception(f"Errore nell'invio del messaggio al canale Telegram: {e}")
                return {"success": False, "message": f"Errore invio Telegram: {e}"}
        else:
            logger.info(f"Bollettino odierno ({current_bollettino_date.strftime('%d/%m/%Y')}) già processato.")
            return {"success": True, "message": "Bollettino già aggiornato e processato. Nessun nuovo invio necessario."}
    else:
        logger.warning("Impossibile recuperare il link o la data del bollettino dal sito.")
        return {"success": False, "message": "Impossibile recuperare il bollettino dal sito. Controllare i log."}

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce il comando /start inviato al bot su Telegram."""
    if update.message:
        await update.message.reply_text(
            'Ciao! Sono il bot della Protezione Civile Basilicata. '
            'Verifico il bollettino in automatico e posso farlo anche manualmente con /aggiorna.'
        )

async def aggiorna_manuale_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce il comando /aggiorna per attivare un controllo manuale dal Telegram."""
    if update.message:
        await update.message.reply_text("Richiesta di aggiornamento manuale avviata. Controllo il bollettino...")
        # Quando chiamato da Telegram, il risultato viene gestito qui, non dalla dashboard
        result = await send_bollettino_update_to_telegram(context.application)
        await update.message.reply_text(f"Operazione completata: {result['message']}")

# Modifica setup_scheduler per accettare la funzione _run_async_in_thread e il callback della dashboard
def setup_scheduler(app_instance: Application, run_async_func, dashboard_callback):
    """Configura e avvia lo scheduler per gli aggiornamenti automatici periodici."""
    scheduler = BackgroundScheduler()
    # Lo scheduler ora userà la funzione run_async_func (che è _run_async_in_thread da app.py)
    # per le sue chiamate asincrone, e passerà il dashboard_callback.
    scheduler.add_job(
        lambda: run_async_func(send_bollettino_update_to_telegram(app_instance), dashboard_callback),
        'cron',
        hour=8, # Ogni giorno alle 8 del mattino
        minute=0,
        timezone='Europe/Rome'
    )
    scheduler.start()
    logger.info("Scheduler avviato per l'aggiornamento automatico (ogni giorno alle 08:00 ora di Roma).")
