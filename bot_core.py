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
    logger.info(f"Tentativo di inizializzare l'applicazione. Token letto (
