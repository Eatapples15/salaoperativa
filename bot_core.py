import logging
# Rimuovi: import requests (non più necessario)
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
# Rimuovi: from webdriver_manager.chrome import ChromeDriverManager (solo per locale)
from bs4 import BeautifulSoup
import datetime
import os
import re

logger = logging.getLogger(__name__)

URL_BOLLETTINO = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"
BASE_URL_SITO = "https://centrofunzionale.regione.basilicata.it"

async def get_bollettino_info():
    driver = None
    try:
        # Configurazione delle opzioni per Chrome/Chromium
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')          # Esegui il browser in background (senza interfaccia grafica)
        options.add_argument('--no-sandbox')        # Necessario per ambienti Linux containerizzati (come Render)
        options.add_argument('--disable-dev-shm-usage') # Previene problemi di memoria condivisa
        options.add_argument('--disable-gpu')       # Disabilita l'accelerazione GPU (utile in headless)
        
        # Imposta il percorso del binario di Chromium/Google Chrome se Render non lo trova automaticamente
        # Su Render, spesso si trova in '/usr/bin/google-chrome' o '/usr/bin/chromium-browser'
        # Potrebbe non essere necessario se Render lo configura già per Selenium
        # options.binary_location = '/usr/bin/google-chrome' 
        
        # Per Render, non usiamo ChromeDriverManager, ma instanziamo il driver direttamente.
        # Render dovrebbe avere un ChromeDriver preinstallato e nel PATH.
        # Se ricevi errori del tipo 'chromedriver not found', dovrai investigare il percorso esatto su Render.
        # Per ora, proviamo con l'instanza diretta senza specificare il service.
        driver = webdriver.Chrome(options=options)
        
        logger.info(f"Tentativo di scaricare il bollettino con Selenium da: {URL_BOLLETTINO}")
        driver.get(URL_BOLLETTINO)
        
        # Aspetta implicitamente che gli elementi siano presenti (fino a 10 secondi)
        # Questo è fondamentale per i contenuti caricati via JavaScript.
        driver.implicitly_wait(10) 

        # Ottieni la sorgente della pagina dopo che JavaScript è stato eseguito
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        bollettino_link = None
        bollettino_date = None

        bollettino_entries = soup.find_all('div', class_='div-one-pdf')
        logger.info(f"Selenium ha trovato {len(bollettino_entries)} elementi 'div-one-pdf'.")

        # Se troviamo elementi, procediamo con l'estrazione
        if bollettino_entries:
            # Prendiamo il primo elemento, che dovrebbe essere il più recente
            entry = bollettino_entries[0] 
            
            link_element = entry.find('a', href=True)
            text_date_element = entry.find('div', class_='div-one-pdf-text')

            if link_element and text_date_element:
                current_link = link_element['href']
                
                # Assicurati che il link sia assoluto
                if not current_link.startswith('http'):
                    current_link = BASE_URL_SITO + current_link
                
                date_text = text_date_element.get_text(strip=True)
                
                logger.info(f"Link trovato con Selenium: {current_link}")
                logger.info(f"Testo data trovato con Selenium: '{date_text}'")

                # Regex per estrarre la data (giorno, mese, anno)
                match = re.search(r'del\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', date_text, re.IGNORECASE)
                
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
                            bollettino_date = datetime.datetime.strptime(date_str_formatted, "%d/%m/%Y").date()
                            logger.info(f"Data parsata con successo: {bollettino_date}")
                            bollettino_link = current_link
                        except ValueError as ve:
                            logger.warning(f"Formato data '{date_str_formatted}' non valido. Errore: {ve}. Impossibile parsare la data.")
                    else:
                        logger.warning(f"Nome mese '{month_name}' non riconosciuto. Impossibile parsare la data.")
                else:
                    logger.warning(f"Pattern data non trovato nella stringa '{date_text}'.")
            else:
                logger.warning("Manca link o testo data all'interno del primo 'div-one-pdf' trovato.")
        else:
            logger.warning("Nessun elemento 'div-one-pdf' trovato con Selenium.")
                
        if not bollettino_link:
            logger.warning("Nessun link al bollettino trovato con la logica attuale.")
        if not bollettino_date:
            logger.warning("Nessuna data del bollettino valida trovata con la logica attuale.")

        return bollettino_link, bollettino_date

    except Exception as e:
        logger.exception(f"Errore inatteso durante il parsing del bollettino con Selenium: {e}")
        return None, None
    finally:
        if driver:
            driver.quit() # Fondamentale: chiudi sempre il browser
