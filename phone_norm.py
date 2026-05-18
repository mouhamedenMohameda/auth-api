"""Normalize WhatsApp numbers to a comparable E.164-style string (digits with leading +)."""


def normalize_whatsapp(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        raise ValueError("Numéro WhatsApp requis.")
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        raise ValueError("Numéro WhatsApp invalide.")
    # Mauritanie (222): si seulement chiffres nationaux sans indicatif
    if not s.lstrip().startswith("+") and 8 <= len(digits) <= 12 and not digits.startswith("222"):
        digits = "222" + digits.lstrip("0")
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError("Numéro WhatsApp invalide.")
    return f"+{digits}"


def normalize_nni(raw: str) -> str:
    s = "".join((raw or "").split())
    if not s.isdigit():
        raise ValueError("Le NNI doit contenir uniquement des chiffres.")
    if not (10 <= len(s) <= 20):
        raise ValueError("Le NNI doit comporter entre 10 et 20 chiffres.")
    return s
