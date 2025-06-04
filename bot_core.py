import os
import requests
from bs4 import BeautifulSoup
import telegram
from datetime import datetime
import asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# --- Configurazione del Bot ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CANALE_PROTEZIONE_CIVILE_ID = os.getenv('CANALE_PROTEZIONE_CIVILE_ID')
DATABASE_URL = os.getenv('DATABASE_URL') # La stringa di connessione a Neon.tech

bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

# --- Variabili di Stato del Bot (verranno caricate/salvate dal DB) ---
# Contengono lo stato del bot che deve persistere tra i riavvii.
# Inizializzate come None, verranno caricate dal DB all'avvio.
bot_state = {
    'last_bulletin_url': None,
    'last_sent_timestamp': None,
    'last_check_timestamp': None # Questo può non essere persistito nel DB se non strettamente necessario
}

# --- Funzioni di gestione del Database ---

# Usa create_async_engine per connessioni asincrone, compatibile con asyncio
async_engine = None

async def _init_db_engine():
    """Inizializza l'engine del database."""
    global async_engine
    if async_engine is None:
        if not DATABASE_URL:
            print("Errore: DATABASE_URL non impostata. Impossibile connettersi al database.")
            return False
        # Aggiungi '?sslmode=require' se non è già presente nella stringa di connessione di Neon.tech
        # Neon.tech richiede SSL
        if 'sslmode=require' not in DATABASE_URL:
            db_url_with_ssl = f"{DATABASE_URL}?sslmode=require"
        else:
            db_url_with_ssl = DATABASE_URL
        
        # Sostituisci postgresql:// con postgresql+asyncpg:// per asyncpg
        async_db_url = db_url_with_ssl.replace("postgresql://", "postgresql+asyncpg://")

        print(f"Tentativo di connessione al database: {async_db_url.split('@')[0]}@...") # Non stampare la password
        try:
            async_engine = create_async_engine(async_db_url, echo=False) # echo=True per vedere le query SQL nei log
            async with async_engine.connect() as conn:
                await conn.execute(text("SELECT 1")) # Prova una query semplice per verificare la connessione
            print("Connessione al database riuscita.")
            return True
        except Exception as e:
            print(f"Errore nella connessione al database: {e}")
            async_engine = None # Reset engine in caso di errore
            return False
    return True

async def _create_state_table_async():
    """Crea la tabella 'bot_state' se non esiste."""
    global async_engine
    if async_engine is None:
        print("Errore: Engine del database non inizializzato. Impossibile creare la tabella.")
        return

    create_table_sql = text("""
        CREATE TABLE IF NOT EXISTS bot_state (
            id SERIAL PRIMARY KEY,
            last_bulletin_url TEXT,
            last_sent_timestamp TIMESTAMP
        );
    """)
    async with async_engine.begin() as conn: # begin() crea una transazione e la committa
        await conn.execute(create_table_sql)
    print("Tabella 'bot_state' verificata/creata con successo.")

async def _load_state_from_db_async():
    """Carica lo stato del bot dal database."""
    global bot_state, async_engine
    if async_engine is None:
        print("Errore: Engine del database non inizializzato. Impossibile caricare lo stato.")
        return

    async with async_engine.connect() as conn:
        result = await conn.execute(text("SELECT last_bulletin_url, last_sent_timestamp FROM bot_state WHERE id = 1"))
        row = result.fetchone()
        if row:
            bot_state['last_bulletin_url'] = row[0]
            bot_state['last_sent_timestamp'] = row[1]
            print(f"Stato del bot caricato dal database: {bot_state}")
        else:
            print("Nessuno stato trovato nel database. Avvio con stato vuoto.")
            # Inserisci una riga iniziale se la tabella è vuota
            await _save_state_to_db_async(None, None) # Inserisce la riga 1 vuota


async def _save_state_to_db_async(bulletin_url, sent_timestamp):
    """Salva lo stato del bot nel database."""
    global bot_state, async_engine
    if async_engine is None:
        print("Errore: Engine del database non inizializzato. Impossibile salvare lo stato.")
        return

    bot_state['last_bulletin_url'] = bulletin_url
    bot_state['last_sent_timestamp'] = sent_timestamp

    # Aggiorna o inserisci la riga con id=1 (che sarà l'unica riga per lo stato globale del bot)
    # UPSERT: se la riga esiste, aggiornala; altrimenti, inseriscila.
    # PostgreSQL usa ON CONFLICT per l'UPSERT
    upsert_sql = text("""
        INSERT INTO bot_state (id, last_bulletin_url, last_sent_timestamp)
        VALUES (1, :bulletin_url, :sent_timestamp)
        ON CONFLICT (id) DO UPDATE SET
            last_bulletin_url = EXCLUDED.last_bulletin_url,
            last_sent_timestamp = EXCLUDED.last_sent_timestamp;
    """)

    async with async_engine.begin() as conn:
        await conn.execute(upsert_sql, {
            'bulletin_url': bot_state['last_bulletin_url'],
            'sent_timestamp': bot_state['last_sent_timestamp']
        })
    print(f"Stato del bot salvato nel database: {bot_state}")

# --- Funzioni del Bot ---

async def get_latest_bulletin_data():
    """
    Funzione per ottenere il link dell'ultimo bollettino dalla pagina.
    Restituisce un tuple (link, data_bollettino_str) o (None, None) in caso di errore/non trovato.
    """
    url = "https://centrofunzionale.regione.basilicata.it/bollettini-di-criticita/"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Cerca il div con id "content"
        content_div = soup.find('div', id='content')
        if not content_div:
            print("Div 'content' non trovato.")
            return None, None

        # Cerca il link più recente che contiene "Bollettino_Criticita_Regione_Basilicata" e ".pdf"
        # Ordina per trovare il più recente in base alla data nel nome del file
        links = content_div.find_all('a', href=True)
        bulletin_links = []
        for link in links:
            href = link['href']
            if "Bollettino_Criticita_Regione_Basilicata" in href and ".pdf" in href:
                # Estrai la data dal nome del file (es. 03_06_2025)
                match = re.search(r'_(\d{2}_\d{2}_\d{4})\.pdf', href)
                if match:
                    date_str = match.group(1)
                    try:
                        # Converti la data in un oggetto datetime per il confronto
                        bulletin_date = datetime.strptime(date_str, '%d_%m_%Y')
                        bulletin_links.append((bulletin_date, href))
                    except ValueError:
                        continue # Salta link con data non valida

        if not bulletin_links:
            print("Nessun link del bollettino trovato.")
            return None, None

        # Trova il bollettino più recente
        latest_bulletin = max(bulletin_links, key=lambda item: item[0])
        latest_link = latest_bulletin[1]
        latest_date_str = latest_bulletin[0].strftime('%d/%m/%Y')
        
        print(f"Ultimo bollettino trovato: {latest_link} del {latest_date_str}")
        return latest_link, latest_date_str

    except requests.exceptions.RequestException as e:
        print(f"Errore durante la richiesta HTTP: {e}")
        return None, None
    except Exception as e:
        print(f"Errore generico durante il parsing del bollettino: {e}")
        return None, None

async def send_bulletin_to_telegram(bulletin_url, bulletin_date_str):
    """Invia il link del bollettino al canale Telegram."""
    if not CANALE_PROTEZIONE_CIVILE_ID:
        print("ID del canale Telegram non impostato. Impossibile inviare il messaggio.")
        return

    message_text = (
        "📢 **Nuovo Bollettino di Criticità - Protezione Civile Basilicata**\n\n"
        f"🗓️ **Data del Bollettino:** {bulletin_date_str}\n"
        f"🔗 [Scarica il Bollettino]({bulletin_url})\n\n"
        "Si prega di consultare il bollettino per tutti i dettagli."
    )
    try:
        await bot.send_message(chat_id=CANALE_PROTEZIONE_CIVILE_ID, text=message_text, parse_mode=telegram.ParseMode.MARKDOWN)
        print(f"Bollettino {bulletin_url} inviato con successo a Telegram.")
        return True
    except telegram.error.TelegramError as e:
        print(f"Errore nell'invio del messaggio a Telegram: {e}")
        return False
    except Exception as e:
        print(f"Errore generico nell'invio del messaggio a Telegram: {e}")
        return False

async def check_for_new_bulletin():
    """
    Controlla la presenza di un nuovo bollettino e lo invia se diverso dall'ultimo salvato.
    """
    global bot_state
    print(f"Eseguo controllo per nuovo bollettino alle {datetime.now().strftime('%H:%M:%S')}")

    # Aggiorna il timestamp dell'ultima verifica
    bot_state['last_check_timestamp'] = datetime.now()

    latest_bulletin_url, latest_bulletin_date_str = await get_latest_bulletin_data()

    if latest_bulletin_url:
        print(f"Ultimo bollettino online: {latest_bulletin_url}")
        print(f"Ultimo bollettino inviato (dal DB): {bot_state.get('last_bulletin_url')}")

        if latest_bulletin_url != bot_state.get('last_bulletin_url'):
            print("Trovato un nuovo bollettino! Invio a Telegram...")
            success = await send_bulletin_to_telegram(latest_bulletin_url, latest_bulletin_date_str)
            if success:
                await _save_state_to_db_async(latest_bulletin_url, datetime.now())
                print(f"Nuovo bollettino ({latest_bulletin_url}) inviato e stato aggiornato nel DB.")
                return True # Nuovo bollettino trovato e inviato
            else:
                print("Invio del bollettino fallito. Lo stato non è stato aggiornato.")
                return False # Invio fallito
        else:
            print("Nessun nuovo bollettino. Ultimo bollettino online è lo stesso dell'ultimo inviato.")
            return False # Nessun nuovo bollettino
    else:
        print("Non è stato possibile recuperare il link dell'ultimo bollettino online.")
        return False # Nessun bollettino valido recuperato

async def get_bot_status():
    """Restituisce lo stato attuale del bot per la dashboard."""
    return {
        "current_time": datetime.now().strftime("%d %B %Y alle %H:%M:%S"),
        "last_bulletin_found": bot_state.get('last_bulletin_url'),
        "last_check_timestamp": bot_state.get('last_check_timestamp', 'N/A').strftime("%d %B %Y alle %H:%M:%S") if bot_state.get('last_check_timestamp') else 'N/A',
        "last_bulletin_sent_timestamp": bot_state.get('last_sent_timestamp', 'N/A').strftime("%d %B %Y alle %H:%M:%S") if bot_state.get('last_sent_timestamp') else 'N/A',
        "persistent_load_status": f"Stato caricato dal DB: {bot_state.get('last_bulletin_url')}" if bot_state.get('last_bulletin_url') else "Stato iniziale (DB vuoto o errore)",
        "general_state_summary": f"Ultimo bollettino inviato: {os.path.basename(bot_state.get('last_bulletin_url', 'N/A'))}. Controllato: {bot_state.get('last_check_timestamp', 'N/A').strftime('%H:%M:%S') if bot_state.get('last_check_timestamp') else 'N/A'}"
    }

async def trigger_manual_check_from_flask():
    """Funzione per innescare un controllo manuale da Flask."""
    print("Richiesta di controllo manuale ricevuta.")
    return await check_for_new_bulletin()

# --- Funzione di inizializzazione per l'avvio del bot ---
async def initialize_bot():
    """Inizializza il database e carica lo stato all'avvio del bot."""
    if await _init_db_engine():
        await _create_state_table_async()
        await _load_state_from_db_async()
    else:
        print("Avviso: Il bot continuerà senza persistenza del database a causa di un errore di connessione.")

# Questo è cruciale: chiama la funzione di inizializzazione all'avvio dell'applicazione.
# Verrà richiamato da app.py
if __name__ == '__main__':
    # Questo blocco viene eseguito solo se esegui bot_core.py direttamente
    # Non verrà eseguito quando importato da app.py
    async def main_test():
        await initialize_bot()
        # Puoi aggiungere qui dei test manuali
        # await check_for_new_bulletin()
        status = await get_bot_status()
        print("\n--- Stato del Bot ---")
        for key, value in status.items():
            print(f"{key}: {value}")
    
    asyncio.run(main_test())
