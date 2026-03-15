"""Food photo analysis using Claude Vision."""
import base64
import json
import logging
from pathlib import Path

import anthropic

from .config import settings

logger = logging.getLogger(__name__)

FOOD_DIR = Path("/app/data/food")

ANALYSIS_PROMPT = """Analysiere diese Essensfotos und gib eine strukturierte Bewertung.

KONTEXT:
Nachhaltige Ernaehrungsumstellung (inspiriert von Slow Carb, KEINE strikte Low-Carb Diaet).
Ziel: Langfristig gesund essen, 6 Tage pro Woche. Vorher: Cola, Pasta, Pizza, Burger.
Bereits ~7kg abgenommen. Es geht um Fortschritt, nicht Perfektion.

BEWERTUNGSREGELN (nach Wichtigkeit):
SEHR GUT: Protein (Fleisch, Fisch, Eier), Gemuese aller Art, Huelsenfruechte (Bohnen, Linsen, Kichererbsen), gesunde Fette (Olivenoel, Nuesse, Avocado)
GUT: Salat (auch mit Light-Dressing — besser als keinen Salat), Suppen, fermentierte Lebensmittel
NEUTRAL/AKZEPTABEL: Reis (normale Portion), Kartoffeln (nicht frittiert), Mais, Milchprodukte
SCHLECHT: Weissbrot, Pasta, Pizza-Teig, Suessigkeiten, Frittiertes
SEHR SCHLECHT: Zucker, Softdrinks, Fast Food, stark verarbeitete Lebensmittel
ALKOHOL: Bier = schlecht (fluessiges Brot), Wein/Spirituosen = maessig

WICHTIG:
- Gemuese NIEMALS bestrafen, auch kohlenhydrathaltiges (Karotten, Tomaten, Mais)
- Huelsenfruechte sind erwuenscht, NICHT als "zu viel Carbs" bewerten
- Lieber gesund und satt als hungrig — grosse Portionen sind OK wenn der Inhalt stimmt
- Verbesserungsvorschlaege realistisch und machbar, nicht idealistisch
- Tonfall: Ermutigend wie ein Freund. Keine Predigt. Kurz und knackig.

FOTOS:
- Wenn mehrere Fotos Kochen UND fertiges Essen zeigen: Zaehle NUR das fertige Essen, nicht doppelt.
- Schaetze Kalorien und Makros grob (fotobasiert, nicht exakt).
- Fuer jedes Foto: Bestimme ob es Kochen/Zubereitung oder fertiges Essen zeigt.

HEALTH SCORE 1-100:
- 85-100 (green): Vorbildlich — Protein + Gemuese + Huelsenfruechte
- 70-84 (green): Gut — gesunde Mahlzeit
- 50-69 (yellow): Okay — akzeptabel, Verbesserungspotential
- 30-49 (yellow): Maessig — zu viel verarbeitete Carbs oder ungesund
- 1-29 (red): Ungesund — Fast Food, viel Zucker/Weissmehl

Antworte NUR mit diesem JSON (kein anderer Text):
{
  "items": [
    {
      "name": "Beschreibung des Essens",
      "portion": "geschaetzte Menge",
      "calories": 500,
      "protein_g": 30,
      "carbs_g": 20,
      "fat_g": 25
    }
  ],
  "total_calories": 500,
  "total_protein_g": 30,
  "total_carbs_g": 20,
  "total_fat_g": 25,
  "health_score": 65,
  "health_color": "yellow",
  "comment": "Kurzer ermutigender Kommentar",
  "photo_types": ["finished", "cooking"]
}"""


def _load_image_as_base64(filepath: Path, max_size_bytes: int = 4_500_000) -> tuple[str, str]:
    """Load image, resize if needed, and return (base64_data, media_type).

    Claude API has a 5MB limit per image. We target 4.5MB to leave margin.
    """
    from io import BytesIO
    from PIL import Image

    data = filepath.read_bytes()
    media_type = "image/jpeg"

    if len(data) > max_size_bytes:
        img = Image.open(filepath)
        # Resize proportionally until under limit
        quality = 85
        while True:
            buf = BytesIO()
            img.save(buf, "JPEG", quality=quality)
            if buf.tell() <= max_size_bytes or quality <= 30:
                data = buf.getvalue()
                break
            # Reduce dimensions by 25%
            w, h = img.size
            img = img.resize((int(w * 0.75), int(h * 0.75)), Image.LANCZOS)
            quality = max(quality - 10, 30)
        logger.info(f"Resized {filepath.name}: {len(data)} bytes")

    b64 = base64.standard_b64encode(data).decode("utf-8")
    return b64, media_type


CHEAT_DAY_ADDENDUM = """

CHEAT DAY! Heute ist Cheat Day — der Benutzer DARF und SOLL heute alles essen was er will.
Das ist bewusst Teil der Ernaehrungsstrategie (Slow Carb Prinzip).
- Health Score: Bewerte trotzdem ehrlich wie gesund/ungesund das Essen ist.
- Kommentar: Feiere das Essen! Motivierend, lustig, ermutigend reinzuhauen.
  Keine Schuldgefuehle, keine Verbesserungsvorschlaege. Heute ist Genuss-Tag.
  Beispiele: "Cheat Day = verdient! Geniess es!", "So muss das am Samstag!"
"""


async def analyze_meal_photos(photo_paths: list[str], is_cheat_day: bool = False) -> dict:
    """Analyze one or more food photos using Claude Vision.

    Args:
        photo_paths: List of file paths relative to FOOD_DIR
        is_cheat_day: If True, adds cheat day context to the prompt

    Returns:
        Analysis result dict with items, scores, comment, photo_types
    """
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Build content with images
    content = []
    for path in photo_paths:
        filepath = FOOD_DIR / path
        if not filepath.exists():
            logger.warning(f"Photo not found: {filepath}")
            continue

        b64, media_type = _load_image_as_base64(filepath)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        })

    if not content:
        raise RuntimeError("No valid photos to analyze")

    prompt = ANALYSIS_PROMPT
    if is_cheat_day:
        prompt += CHEAT_DAY_ADDENDUM
    content.append({"type": "text", "text": prompt})

    logger.info(f"Analyzing {len(photo_paths)} photo(s) with Claude Sonnet...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    # Parse JSON from response
    response_text = response.content[0].text.strip()

    # Handle markdown code blocks
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response: {e}\nResponse: {response_text}")
        raise RuntimeError(f"AI returned invalid JSON: {e}")

    logger.info(f"Analysis complete: score={result.get('health_score')}, "
                f"calories={result.get('total_calories')}")

    return result
