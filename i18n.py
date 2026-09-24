"""Language and theme helpers. Source strings are English; Malagasy and French come from translations.py."""
from translations import TR

LANGS = {"mg": {"name": "Malagasy"}, "fr": {"name": "Français"}, "en": {"name": "English"}}
THEMES = {  # the flag's three fields per theme: the same colours as the CSS custom properties
    "saina": {"label": "Default", "f1": "#FFFFFF", "f2": "#FC3D32", "f3": "#007E3A"},  # default: the flag itself, all three colours
    "fotsy": {"label": "White", "f1": "#FFFFFF", "f2": "#FFE3E0", "f3": "#DDF0E5", "sw": ("#FFFFFF", "#FC3D32", "#111111")},  # swatch: the White logo (white, red arc, black rings)
    "maitso": {"label": "Green", "f1": "#003D1D", "f2": "#007E3A", "f3": "#005A2B"},
    "mena": {"label": "Red", "f1": "#7A0F0A", "f2": "#FC3D32", "f3": "#B8241B"},
}
MISSING = set()  # filled while the app runs so tests and tools/i18n_keys.py can list gaps
_IDX = {"fr": 0, "mg": 1}


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def negotiate_language(header):
    """Best supported language from an Accept-Language header, English when nothing matches."""
    best, best_q = "en", -1.0
    for part in (header or "").split(","):
        code, _, q = part.strip().partition(";q=")
        code = code.strip().lower()[:2]
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        if code in LANGS and weight > best_q:
            best, best_q = code, weight
    return best


def tt(key, lang, **params):
    """Translate an English source string and fill its {placeholders}. Unknown strings fall back to English."""
    text = key
    if lang in _IDX:
        entry = TR.get(key)
        if entry:
            text = entry[_IDX[lang]]
        elif key and not key.startswith("{"):
            MISSING.add(key)
    if params:
        try:
            text = text.format_map(_Safe({k: v for k, v in params.items()}))
        except (ValueError, IndexError):
            pass
    return text
