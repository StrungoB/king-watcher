#!/usr/bin/env python3
"""
Multisala King (Lonato) - watcher
Ti avvisa quando:
  1) compare una parola chiave (es. "doomsday") in programmazione / prossimamente / eventi
  2) compare un NUOVO film in programmazione
  3) compaiono NUOVI orari/date (nuove proiezioni)

Dipendenze:  pip install requests beautifulsoup4
Uso:         python king_watcher.py
Cron (ogni 10 min):  */10 * * * * /usr/bin/python3 /percorso/king_watcher.py
"""

import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

# ----------------------------- CONFIGURAZIONE -----------------------------

# Parole chiave (minuscolo). "doomsday" evita falsi positivi con "Avengers: Endgame".
KEYWORDS = ["doomsday", "avengers: doomsday", "avengers doomsday"]

# Notifiche: compila UNO (o entrambi) dei due canali.
# Telegram: crea un bot con @BotFather, poi scrivigli e ottieni il chat id (vedi README sotto)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Se lasci vuoto il chat id, lo script lo trova da solo dopo che hai scritto un messaggio al bot
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# ntfy.sh: scegli un nome di topic lungo e segreto, installa l'app ntfy e iscriviti al topic
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Avvisami anche per nuovi orari di film gia' in programmazione?
NOTIFY_NEW_SHOWTIMES = True

BASE = "https://www.multisalaking.it"
PAGES = {
    "programmazione": BASE + "/",
    "prossimamente": BASE + "/prossimamente/",
    "eventi": BASE + "/eventi/",
}
STATE_FILE = Path(__file__).with_name("king_state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; king-watcher/1.0)"}

# --------------------------------------------------------------------------


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=(10, 30))
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_programmazione(soup):
    """Ritorna {titolo: {sp_id: 'Martedì 06/10 19:00'}} leggendo i link di acquisto."""
    films = {}
    for a in soup.find_all("a", href=re.compile(r"loadPerformance")):
        qs = parse_qs(urlparse(a["href"]).query)
        sp = (qs.get("sp") or [""])[0]
        if not sp:
            continue
        h3 = a.find_previous("h3")
        title = re.sub(r"^NEW!\s*", "", h3.get_text(" ", strip=True)) if h3 else "?"
        li = a.find_parent("li")
        label = li.get_text(" ", strip=True) if li else a.get_text(strip=True)
        films.setdefault(title, {})[sp] = label
    # film in programmazione senza orari (se mai capita)
    for h3 in soup.find_all("h3"):
        t = re.sub(r"^NEW!\s*", "", h3.get_text(" ", strip=True))
        if t:
            films.setdefault(t, {})
    return films


def page_text(soup):
    return soup.get_text(" ", strip=True).lower()


CHAT_ID_FILE = Path(__file__).with_name("telegram_chat_id.txt")


def get_chat_id():
    """Usa il chat id configurato, oppure lo ricava dall'ultimo messaggio inviato al bot."""
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    if CHAT_ID_FILE.exists():
        return CHAT_ID_FILE.read_text().strip()
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=20
        ).json()
        for upd in reversed(r.get("result", [])):
            msg = upd.get("message") or upd.get("channel_post")
            if msg:
                cid = str(msg["chat"]["id"])
                CHAT_ID_FILE.write_text(cid)
                return cid
    except Exception as e:
        print("Errore lettura chat id:", e, file=sys.stderr)
    print("Chat id non trovato: scrivi un messaggio al bot su Telegram e rilancia.",
          file=sys.stderr)
    return ""


def notify(title, message):
    print(f"[NOTIFICA] {title}\n{message}\n")
    chat_id = get_chat_id() if TELEGRAM_TOKEN else ""
    if TELEGRAM_TOKEN and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": f"{title}\n\n{message}"},
                timeout=20,
            )
        except Exception as e:
            print("Errore Telegram:", e, file=sys.stderr)
    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title.encode("utf-8"), "Priority": "high",
                         "Click": BASE + "/"},
                timeout=20,
            )
        except Exception as e:
            print("Errore ntfy:", e, file=sys.stderr)


LOG_FILE = Path(__file__).with_name("king_log.txt")


def now_str():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def log(line):
    """Aggiunge una riga con data e ora a king_log.txt (storico delle novita')."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {line}\n")
    except Exception as e:
        print("Errore scrittura log:", e, file=sys.stderr)


def migrate_state(state):
    """Converte il vecchio formato {titolo: {sp: label}} in quello con le date."""
    for title, val in list(state.get("films", {}).items()):
        if "shows" in val and isinstance(val["shows"], dict):
            continue
        state["films"][title] = {
            "first_seen": None,  # visto prima che aggiungessi le date
            "shows": {sp: {"label": lab, "first_seen": None} for sp, lab in val.items()},
        }
    return state


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    soups = {}
    for name, url in PAGES.items():
        try:
            soups[name] = fetch(url)
        except Exception as e:
            print(f"Impossibile leggere {url}: {e}", file=sys.stderr)
            log(f"ERRORE lettura {url}: {e}")

    if "programmazione" not in soups:
        sys.exit(1)

    ts = now_str()
    films = parse_programmazione(soups["programmazione"])
    texts = {n: page_text(s) for n, s in soups.items()}
    kw_hits = {n: [k for k in KEYWORDS if k in t] for n, t in texts.items()}

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"films": {}, "keyword_alerted": False}
    state = migrate_state(state)

    old_films = state["films"]
    alerts = []

    # Protezione: se la pagina non contiene nessun film ma in passato ne conteneva,
    # e' un'anomalia temporanea del sito. Non tocco lo stato (altrimenti al ritorno
    # dei film arriverebbero decine di falsi "Nuovo film").
    if not films and old_films and not first_run:
        log("ATTENZIONE: pagina senza film (anomalia del sito?), controllo ignorato.")
        print("Pagina senza film: ignorata.")
        return

    # 1) parola chiave
    hit_pages = [n for n, h in kw_hits.items() if h]
    if hit_pages and not state.get("keyword_alerted"):
        state["keyword_alerted"] = True
        state["keyword_first_seen"] = ts
        alerts.append((
            "AVENGERS: DOOMSDAY trovato!",
            "La parola chiave e' comparsa su: " + ", ".join(hit_pages)
            + f"\nRilevato il: {ts}\nControlla: {BASE}/",
        ))

    # 2) nuovi film  3) nuovi orari  (con data/ora di rilevamento)
    new_state_films = {}
    for title, shows in films.items():
        old = old_films.get(title)
        entry = {
            "first_seen": old["first_seen"] if old else (None if first_run else ts),
            "shows": {},
        }
        added = []
        for sp, label in shows.items():
            if old and sp in old["shows"]:
                entry["shows"][sp] = old["shows"][sp]  # conserva la data originale
            else:
                entry["shows"][sp] = {"label": label, "first_seen": None if first_run else ts}
                added.append(label)
        new_state_films[title] = entry

        if first_run:
            continue
        if old is None:
            lines = "\n".join(f"- {v}" for v in added) or "(orari non ancora pubblicati)"
            alerts.append((f"Nuovo film: {title}", f"{lines}\nRilevato il: {ts}"))
        elif added and NOTIFY_NEW_SHOWTIMES:
            alerts.append((
                f"Nuovi orari: {title}",
                "\n".join(f"- {v}" for v in added) + f"\nRilevato il: {ts}",
            ))
        elif added:
            log(f"NUOVI ORARI (non notificati): {title} -> " + "; ".join(added))

    # Memoria: i film che non compaiono piu' restano nello stato per 60 giorni.
    # Se ricompaiono non sono "nuovi". Poi vengono dimenticati.
    for title, entry in new_state_films.items():
        entry["last_seen"] = ts
    for title, entry in old_films.items():
        if title in new_state_films:
            continue
        last = entry.get("last_seen")
        try:
            age = (datetime.now() - datetime.strptime(last, "%d/%m/%Y %H:%M:%S")).days if last else 0
        except Exception:
            age = 0
        if not last:
            entry["last_seen"] = ts  # stato vecchio senza last_seen: parte da ora
        if age <= 60:
            new_state_films[title] = entry
    state["films"] = new_state_films

    if first_run:
        print(f"Prima esecuzione: salvato lo stato di base ({len(films)} film). "
              "Da ora in poi ricevi solo le novita'.")
        log(f"Avvio monitoraggio: {len(films)} film in programmazione (stato di base).")
        notify("King Watcher attivo",
               f"Monitoraggio avviato il {ts}: {len(films)} film in programmazione. "
               "Ti avviso per Doomsday, nuovi film e nuovi orari.")
        if hit_pages:
            for t, m in alerts:
                if "DOOMSDAY" in t:
                    log(t + " | " + m.replace("\n", " | "))
                    notify(t, m)
    else:
        for t, m in alerts:
            log(t + " | " + m.replace("\n", " | "))
            notify(t, m)
        if not alerts:
            print("Nessuna novita'.")

    save_state(state)


if __name__ == "__main__":
    import threading
    import traceback

    socket.setdefaulttimeout(30)

    # Watchdog: se qualcosa si blocca (rete assente, DNS...), termina dopo 2 minuti
    # cosi' l'attivita' pianificata non resta "In esecuzione" per sempre.
    def _watchdog():
        log("TIMEOUT: esecuzione bloccata per 2 minuti, terminata dal watchdog")
        os._exit(2)

    _wd = threading.Timer(120, _watchdog)
    _wd.daemon = True
    _wd.start()

    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log("ERRORE: " + traceback.format_exc().replace("\n", " | "))
        raise
