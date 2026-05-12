"""
Mise à jour quotidienne de data.json.
 
Lit les emails Gmail dont le sujet contient KEEPLINK, extrait les URLs et notes,
récupère le contenu des pages, génère description + catégorie via Claude, et
écrit le nouveau data.json (commit/push géré par le workflow GitHub Actions).
"""
 
from __future__ import annotations
 
import email
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
 
import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup
 
# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
 
DATA_FILE = Path("data.json")
KEEPLINK_TAG = "KEEPLINK"
MODEL = "claude-haiku-4-5"
PAGE_FETCH_TIMEOUT = 15
PAGE_TEXT_LIMIT = 3000  # caractères passés au LLM
USER_AGENT = "Mozilla/5.0 (compatible; KeeplinkBot/1.0)"
 
URL_RE = re.compile(r"https?://[^\s<>\"']+")
CATEGORY_OVERRIDE_RE = re.compile(r"\[([^\]]+)\]")
 
 
# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
 
 
def load_existing_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text("utf-8"))
    return {"lastUpdated": None, "resources": []}
 
 
def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        (b.decode(enc or "utf-8", errors="replace") if isinstance(b, bytes) else b)
        for b, enc in parts
    )
 
 
def get_plaintext_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""
 
 
_NOTE_STOP_PATTERNS = [
    # Délimiteurs de message transféré (Outlook, Apple Mail, Gmail, Thunderbird...)
    r"^-{3,}\s*(?:Forwarded message|Message transf[ée]r[ée]|Original [Mm]essage|Message d'origine|Reply [Mm]essage)\s*-{3,}\s*$",
    r"^Begin forwarded message:",
    r"^From:.*\bForwarded\b",
    # Headers d'email injectés dans le corps quand on forward
    r"^(?:De|From|Envoy[ée]|Sent|[ÀA]|To|Cc|Objet|Subject|Date)\s*:\s*",
    # Délimiteurs de signature
    r"^-- ?$",                              # RFC 3676
    r"^_{3,}\s*$",
    r"^={3,}\s*$",
    # Phrases de signature courantes
    r"^(?:Cordialement|Bien (?:à\s+(?:vous|toi)|cordialement)|Bonne (?:journ[ée]e|soir[ée]e|continuation)|Salutations(?:\s+distingu[ée]es)?|Amicalement|À\s+bient[ôo]t|Sinc[èe]rement|Merci(?:\s+et\s+bonne)?|Tr[èe]s\s+cordialement)\b",
    r"^(?:Best\s+(?:regards|wishes)|Regards|Sincerely|Cheers|Thanks(?:\s+and\s+regards)?)\b",
    # Mentions "envoyé depuis"
    r"^(?:Envoy[ée]e?\s+(?:depuis|de\s+mon|de\s+l'application)|Sent\s+from\s+my)\b",
]
_NOTE_STOP_RE = re.compile("|".join(_NOTE_STOP_PATTERNS), re.IGNORECASE | re.MULTILINE)
 
 
def _clean_body_for_note(body: str) -> str:
    """Coupe le corps avant la première signature/header de forward,
    retire les lignes citées et les espaces excessifs."""
    if not body:
        return ""
    lines = body.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if _NOTE_STOP_RE.match(stripped):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    # Compacte les sauts de ligne excessifs
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned
 
 
def extract_url_and_note(body: str) -> tuple[str | None, str]:
    """Retourne (première URL trouvée dans le corps, note nettoyée).
 
    L'URL est cherchée dans tout le corps (y compris partie forwardée).
    La note est extraite uniquement de la partie « propre » : tout ce qui
    précède une signature ou un header de message transféré.
    """
    match = URL_RE.search(body)
    if not match:
        return None, ""
    url = match.group(0).rstrip(".,;:)")
 
    cleaned = _clean_body_for_note(body)
    note = URL_RE.sub("", cleaned).strip()
    if len(note) > 300:
        note = note[:297].rstrip() + "…"
    return url, note
 
 
def extract_category_override(subject: str) -> str | None:
    """Extrait [Catégorie] du sujet si présent."""
    cleaned = subject.replace(KEEPLINK_TAG, "")
    match = CATEGORY_OVERRIDE_RE.search(cleaned)
    return match.group(1).strip() if match else None
 
 
# --------------------------------------------------------------------------- #
# Gmail                                                                       #
# --------------------------------------------------------------------------- #
 
 
def fetch_keeplink_messages(address: str, app_password: str) -> list[dict]:
    """Connexion IMAP, recherche des messages KEEPLINK, retourne les essentiels."""
    messages: list[dict] = []
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(address, app_password)
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SUBJECT", f'"{KEEPLINK_TAG}"')
        if typ != "OK":
            return []
        for mid in data[0].split():
            typ, msg_data = imap.fetch(mid, "(RFC822)")
            if typ != "OK":
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                date_header = msg.get("Date")
                try:
                    iso_date = parsedate_to_datetime(date_header).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError):
                    iso_date = datetime.now(timezone.utc).isoformat()
                messages.append(
                    {
                        "id": msg.get("Message-ID", "").strip(),
                        "subject": decode_mime(msg.get("Subject")),
                        "date": iso_date,
                        "body": get_plaintext_body(msg),
                    }
                )
                break
    return messages
 
 
# --------------------------------------------------------------------------- #
# Page fetch + LLM                                                            #
# --------------------------------------------------------------------------- #
 
 
def fetch_page_summary(url: str) -> tuple[str, str]:
    """Récupère la page web et renvoie (titre, texte nettoyé tronqué)."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=PAGE_FETCH_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        title = (soup.title.string or "").strip() if soup.title else ""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        return title, text[:PAGE_TEXT_LIMIT]
    except Exception as exc:  # noqa: BLE001 — on swallow les erreurs de fetch
        print(f"  ⚠ Erreur fetch {url}: {exc}", file=sys.stderr)
        return "", ""
 
 
def call_llm(
    client: Anthropic,
    url: str,
    user_note: str,
    page_title: str,
    page_text: str,
    existing_categories: list[str],
    category_override: str | None,
) -> dict:
    """Demande à Claude une description courte et une catégorie."""
    if category_override:
        category_instruction = (
            f'La catégorie est imposée : "{category_override}". Reproduis-la telle quelle.'
        )
    elif existing_categories:
        category_instruction = (
            "Catégories déjà utilisées sur le site (privilégie-les si pertinent) : "
            f"{', '.join(existing_categories)}.\n"
            "Sinon propose une catégorie courte (1 à 3 mots)."
        )
    else:
        category_instruction = "Propose une catégorie courte (1 à 3 mots)."
 
    prompt = f"""Tu reçois une ressource web à intégrer dans un index. Génère description et catégorie.
 
URL : {url}
Note de l'utilisateur : {user_note or "(aucune)"}
Titre de la page : {page_title or "(non extrait)"}
 
Extrait du contenu de la page :
{page_text}
 
{category_instruction}
 
Réponds en JSON strict (rien d'autre), exactement ce schéma :
{{"description": "1 à 2 phrases en français qui résument à quoi sert la ressource", "category": "Catégorie courte"}}
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)
 
 
# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
 
 
def main() -> int:
    address = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
 
    client = Anthropic(api_key=anthropic_key)
    data = load_existing_data()
    existing_ids = {r.get("id") for r in data["resources"] if r.get("id")}
    existing_categories = sorted(
        {r["category"] for r in data["resources"] if r.get("category")}
    )
 
    print(
        f"Chargé {len(data['resources'])} ressources, "
        f"{len(existing_categories)} catégories existantes."
    )
 
    messages = fetch_keeplink_messages(address, app_password)
    print(f"Gmail : {len(messages)} messages KEEPLINK au total.")
 
    new_resources: list[dict] = []
    for msg in messages:
        if msg["id"] and msg["id"] in existing_ids:
            continue
 
        url, note = extract_url_and_note(msg["body"])
        if not url:
            print(f"  ⚠ Pas d'URL dans le message : {msg['subject']!r}")
            continue
 
        domain = urlparse(url).netloc
        category_override = extract_category_override(msg["subject"])
        page_title, page_text = fetch_page_summary(url)
 
        try:
            llm_result = call_llm(
                client,
                url,
                note,
                page_title,
                page_text,
                existing_categories
                + [r["category"] for r in new_resources if r.get("category")],
                category_override,
            )
            description = llm_result.get("description", "")
            category = category_override or llm_result.get("category", "Non catégorisé")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Erreur LLM {url}: {exc}", file=sys.stderr)
            description = ""
            category = category_override or "Non catégorisé"
 
        new_resources.append(
            {
                "id": msg["id"],
                "url": url,
                "note": note,
                "description": description,
                "category": category,
                "categorySource": "manual" if category_override else "ai",
                "date": msg["date"],
                "domain": domain,
            }
        )
        print(f"  + {domain} → [{category}]")
 
    if not new_resources:
        print("Rien de nouveau à publier.")
        return 0
 
    data["resources"].extend(new_resources)
    data["resources"].sort(key=lambda r: r["date"], reverse=True)
    data["lastUpdated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
 
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"data.json mis à jour : +{len(new_resources)} ressource(s).")
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
