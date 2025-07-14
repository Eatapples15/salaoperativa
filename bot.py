import os
import asyncio
import requests
import pdfplumber
from bs4 import BeautifulSoup
from telegram import Bot

# --- CONFIGURAZIONE ---
# L'URL della pagina dove vengono pubblicati i bollettini
URL_PAGINA = "https://centrofunzionale.regione.basilicata.it/it/bollettini-avvisi.php?lt=A"

# Recupera le credenziali dalle variabili d'ambiente (più sicuro!)
# Le imposteremo direttamente su Render
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Percorso temporaneo dove salvare il PDF
PDF_PATH = "bollettino_temp.pdf"

def trova_ultimo_bollettino():
    """
    Analizza la pagina web per trovare l'URL del bollettino PDF più recente.
    """
    print("🔎 Cerco il bollettino più recente...")
    try:
        response = requests.get(URL_PAGINA, timeout=15)
        response.raise_for_status()  # Solleva un errore se la richiesta fallisce

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Trova il primo link che sembra un bollettino nella lista delle pubblicazioni
        # La struttura della pagina è una lista di link con date
        primo_link = soup.select_one(".pubblicazioni a[href*='criticita']")

        if primo_link and primo_link.has_attr('href'):
            url_relativo = primo_link['href']
            # Costruisce l'URL completo partendo da quello relativo
            url_completo = f"https://centrofunzionale.regione.basilicata.it{url_relativo}"
            print(f"✅ Trovato URL: {url_completo}")
            return url_completo
        else:
            print("❌ Nessun link al bollettino trovato.")
            return None
            
    except requests.RequestException as e:
        print(f"Errore durante la richiesta HTTP: {e}")
        return None

def estrai_allerte_da_pdf(percorso_pdf):
    """
    Apre un file PDF ed estrae il testo relativo ai livelli di allerta
    per le zone "Basi-".
    """
    print(f"📄 Analizzo il file PDF: {percorso_pdf}")
    testo_completo = ""
    try:
        with pdfplumber.open(percorso_pdf) as pdf:
            for page in pdf.pages:
                testo_pagina = page.extract_text()
                if testo_pagina:
                    testo_completo += testo_pagina + "\n"

        # Parole chiave da cercare nel testo per identificare le allerte
        allerte_trovate = []
        parole_chiave = [
            "ORDINARIA CRITICITÀ - ALLERTA GIALLA",
            "MODERATA CRITICITÀ - ALLERTA ARANCIONE",
            "ELEVATA CRITICITÀ - ALLERTA ROSSA"
        ]

        for riga in testo_completo.split('\n'):
            for allerta in parole_chiave:
                if allerta in riga:
                    # Se troviamo una riga con un'allerta, cerchiamo le zone "Basi-"
                    # in quella stessa riga o nelle immediate vicinanze.
                    # Questo potrebbe richiedere aggiustamenti se il formato del PDF cambia.
                    if "Basi-" in riga:
                        messaggio_allerta = f"🟡 {riga.strip()}" if "GIALLA" in allerta \
                            else f"🟠 {riga.strip()}" if "ARANCIONE" in allerta \
                            else f"🔴 {riga.strip()}"
                        allerte_trovate.append(messaggio_allerta)
        
        if allerte_trovate:
            print(f"✅ Trovate {len(allerte_trovate)} allerte.")
            return "\n".join(allerte_trovate)
        else:
            print("ℹ️ Nessuna allerta specifica per le zone 'Basi-' trovata nel testo.")
            return "Nessuna criticità specificata per le zone di allertamento."

    except Exception as e:
        print(f"Errore durante l'analisi del PDF: {e}")
        return "Impossibile analizzare il contenuto del bollettino."

async def main():
    """
    Funzione principale che orchestra il tutto.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Errore: Le variabili d'ambiente TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID non sono state impostate.")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    url_pdf = trova_ultimo_bollettino()

    if url_pdf:
        # Scarica il PDF
        response = requests.get(url_pdf)
        with open(PDF_PATH, 'wb') as f:
            f.write(response.content)
        
        # Estrai il testo delle allerte dal PDF
        testo_allerte = estrai_allerte_da_pdf(PDF_PATH)
        
        messaggio_caption = f"🚨 *Bollettino di Criticità Regione Basilicata* 🚨\n\n"
        messaggio_caption += f"*{testo_allerte}*\n\n"
        messaggio_caption += "_In allegato il bollettino ufficiale._"

        print("🤖 Invio del messaggio su Telegram...")
        
        # Invia il PDF come documento con la didascalia
        with open(PDF_PATH, 'rb') as pdf_file:
            await bot.send_document(
                chat_id=TELEGRAM_CHAT_ID,
                document=pdf_file,
                filename="Bollettino_Criticita_Basilicata.pdf",
                caption=messaggio_caption,
                parse_mode='Markdown'
            )
        
        # Pulisci il file temporaneo
        os.remove(PDF_PATH)
        print("✅ Messaggio inviato e pulizia completata.")
    else:
        print("Nessun nuovo bollettino da inviare.")

if __name__ == "__main__":
    asyncio.run(main())