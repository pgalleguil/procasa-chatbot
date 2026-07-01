import re
import unicodedata


def normalize_text(text: str, preserve_utf8: bool = True) -> str:
    """
    Normaliza texto para comparación.
    Si preserve_utf8=True, mantiene caracteres UTF-8 (ñ, tildes, etc.).
    Si preserve_utf8=False, elimina acentos (uso legacy).
    """
    if not text:
        return ""
    text = str(text).lower().strip()
    text = re.sub(r"\s+", " ", text)
    if not preserve_utf8:
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ASCII", "ignore").decode("ASCII")
    return text


def clean_emoji(text: str) -> str:
    """Elimina emojis y caracteres no-ASCII no imprimibles."""
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticonos
        "\U0001F300-\U0001F5FF"  # símbolos varios
        "\U0001F680-\U0001F6FF"  # transporte
        "\U0001F1E0-\U0001F1FF"  # banderas
        "\U00002702-\U000027B0"  # dingbats
        "\U000024C2-\U0001F251"  # varios
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text)


def extract_phone_numbers(text: str) -> list[str]:
    """Extrae posibles números de teléfono chilenos del texto."""
    if not text:
        return []
    phones = re.findall(r"\+?56?\s*9\s*\d{4}\s*\d{4}", str(text))
    phones += re.findall(r"\+?56?\s*2\s*\d{4}\s*\d{4}", str(text))
    return list(set(p.replace(" ", "") for p in phones))


def extract_whatsapp_links(html: str) -> list[str]:
    """Extrae números de WhatsApp de enlaces en HTML."""
    if not html:
        return []
    return re.findall(r"wa\.me/(\d+)", html)
