import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import datetime
import os
import asyncio

# --- Configurazione del Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variabili d'Ambiente ---
# LEGGI LE VARIABILI D'AMBIENTE USANDO IL LORO NOME (KEY), NON IL LORO VALORE!
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANALE_PROTEZIONE_CIVILE_ID = os.getenv("CANALE_PROTEZIONE_CIVILE_ID")
URL_BOLLETTINO = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"

# --- Stato Globale (In Memoria) ---
last_processed_bollettino_date = None # Dovrebbe essere un oggetto datetime.date

# --- Istanza dell'Applicazione Telegram ---
application = None

def init_telegram_application():
    """Inizializza l'istanza dell'applicazione Telegram."""
    global application
    
    # LOG DI DEBUG TEMPORANEO: Rimuovilo quando il bot funziona!
    logger.info(f"Tentativo di inizializzare l'applicazione. Token letto (prime 5 car): {TELEGRAM_BOT_TOKEN[:5] if TELEGRAM_BOT_TOKEN else 'None'}")
    logger.info(f"ID Canale letto: {CANALE_PROTEZIONE_CIVILE_ID if CANALE_PROTEZIONE_CIVILE_ID else 'None'}")
    # FINE LOG DI DEBUG

    if not TELEGRAM_BOT_TOKEN:
        logger.error("ERRORE: TELEGRAM_BOT_TOKEN non impostato. Il bot non può avviarsi.")
        return None
    if application is None:
        try:
            application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            logger.info("Applicazione Telegram inizializzata con successo.")
        except Exception as e:
            logger.exception(f"ERRORE: Impossibile inizializzare l'applicazione Telegram: {e}")
            application = None # Assicurati che sia None in caso di fallimento
    return application

async def get_bollettino_info():
    """
    Funzione per scaricare e parsare le informazioni del bollettino dalla pagina
    della Protezione Civile Basilicata.
    """
    try:
        logger.info(f"Tentativo di scaricare il bollettino da: {URL_BOLLETTINO}")
        response = requests.get(URL_BOLLETTINO, timeout=15) # Aumentato il timeout
        response.raise_for_status() # Lancia un'eccezione per risposte HTTP con errori (4xx, 5xx)

        # Utilizza 'html.parser' per una maggiore compatibilità, anche se 'lxml' è più veloce se disponibile.
        soup = BeautifulSoup(response.text, 'html.parser')

        bollettino_link = None
        bollettino_date = None

        # --- LOGICA DI WEB SCRAPING SPECIFICA PER IL SITO ---
        # Devi ispezionare il codice HTML del sito (con F12 nel browser) per trovare gli elementi corretti.
        # Basato su un'ispezione comune di siti PA, cerchiamo un link che contenga "bollettino"
        # e una data associata.
        
        # Cerca il div che contiene i bollettini. Spesso hanno una classe specifica.
        # Ad esempio, sul sito che hai indicato, ci sono blocchi <div class="list-item">
        list_items = soup.find_all('div', class_='list-item')

        for item in list_items:
            # Cerca un link (<a>) all'interno di questo 'list-item' che ha come target='_blank'
            # e il cui testo (o href) contenga la parola 'bollettino'.
            link_element = item.find('a', href=True)
            if link_element and 'bollettino' in link_element.get_text(strip=True).lower():
                bollettino_link = link_element['href']
                
                # Cerca la data, che potrebbe essere in un elemento <small> o <span> vicino al link.
                # Questa parte è molto sensibile alle modifiche del sito.
                date_element = item.find('small') # Assumiamo che la data sia in un tag <small>
                if date_element:
                    date_str = date_element.get_text(strip=True)
                    # Converti la stringa data (es. '26/05/2025') in un oggetto datetime.date
                    try:
                        bollettino_date = datetime.datetime.strptime(date_str, "%d/%m/%Y").date()
                    except ValueError:
                        logger.warning(f"Formato data non riconosciuto '{date_str}'. Impossibile parsare la data.")
                        bollettino_date = None
                
                # Se abbiamo trovato un link e una data validi, possiamo fermarci qui (prendiamo il primo)
                if bollettino_link and bollettino_date:
                    break
        
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
    Questa funzione viene chiamata sia dallo scheduler che dal comando manuale.
    """
    global last_processed_bollettino_date
    
    logger.info("Avvio controllo aggiornamento bollettino per invio Telegram...")
    link, current_bollettino_date = await get_bollettino_info()

    if link and current_bollettino_date:
        logger.info(f"Bollettino trovato: Data={current_bollettino_date}, Link={link}")
        
        # Confronta la data del bollettino trovato con l'ultima data processata
        if last_processed_bollettino_date is None or current_bollettino_date > last_processed_bollettino_date:
            message = (
                f"**🚨 AGGIORNAMENTO BOLLETTINO CRITICITÀ 🚨**\n\n"
                f"**Protezione Civile Regione Basilicata**\n\n"
                f"Data del bollettino: `{current_bollettino_date.strftime('%d/%m/%Y')}`\n\n"
                f"Clicca qui per i dettagli: [Bollettino Odierno]({link})"
            )
            try:
                if CANALE_PROTEZIONE_CIVILE_ID:
                    # Assicurati che CANALE_PROTEZIONE_CIVILE_ID sia un intero per chat_id
                    try:
                        chat_id_int = int(CANALE_PROTEZIONE_CIVILE_ID)
                    except ValueError:
                        logger.error(f"CANALE_PROTEZIONE_CIVILE_ID '{CANALE_PROTEZIONE_CIVILE_ID}' non è un numero intero valido.")
                        return {"success": False, "message": "ID canale Telegram non valido."}

                    await app_instance.bot.send_message(
                        chat_id=chat_id_int, # Usa l'ID convertito a intero
                        text=message,
                        parse_mode='Markdown',
                        disable_web_page_preview=True # Per non mostrare l'anteprima del link
                    )
                    logger.info(f"Bollettino {current_bollettino_date.strftime('%d/%m/%Y')} inviato al canale {CANALE_PROTEZIONE_CIVILE_ID}")
                    last_processed_bollettino_date = current_bollettino_date # Aggiorna l'ultima data processata
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

# --- Comandi del Bot Telegram ---

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
        result = await send_bollettino_update_to_telegram(context.application)
        await update.message.reply_text(f"Operazione completata: {result['message']}")

def run_bot_polling():
    """Avvia il polling del bot Telegram per ricevere i comandi degli utenti."""
    global application
    if application:
        logger.info("Avvio del polling del bot Telegram...")
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("aggiorna", aggiorna_manuale_command))
        # Avvia il bot. Questo è un ciclo bloccante, quindi deve essere in un thread separato.
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        logger.error("Applicazione Telegram non inizializzata. Impossibile avviare il polling del bot.")

def setup_scheduler(app_instance: Application):
    """Configura e avvia lo scheduler per gli aggiornamenti automatici periodici."""
    scheduler = BackgroundScheduler()
    # Esegui ogni giorno alle 08:00 (ora di Roma).
    # Assicurati che il fuso orario sia corretto per il tuo ambiente di hosting (Render.com è basato su UTC,
    # ma specificando il fuso orario, APScheduler lo gestisce correttamente).
    scheduler.add_job(
        lambda: asyncio.run(send_bollettino_update_to_telegram(app_instance)),
        'cron',
        hour=8,
        minute=0,
        timezone='Europe/Rome'
    )
    # --- Per testing, puoi usare un intervallo più breve (disabilita quello sopra) ---
    # scheduler.add_job(
    #     lambda: asyncio.run(send_bollettino_update_to_telegram(app_instance)),
    #     'interval',
    #     minutes=5, # Esegui ogni 5 minuti per test
    #     args=[app_instance]
    # )
    # --- Fine blocco testing ---

    scheduler.start()
    logger.info("Scheduler avviato per l'aggiornamento automatico (ogni giorno alle 08:00 ora di Roma).")
