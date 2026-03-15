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

REGELN:
- Wenn mehrere Fotos Kochen UND fertiges Essen zeigen: Zaehle NUR das fertige Essen, nicht die Zutaten doppelt.
- Schaetze Kalorien und Makros grob (nicht exakt, das ist fotobasiert).
- Der Benutzer macht eine Low-Carb Diaet. Kohlenhydrate sind der Hauptfeind.
- Health Score 1-100:
  - 70-100 (gruen): Viel Protein, Gemuese, wenig Carbs
  - 40-69 (gelb): Akzeptabel aber verbesserungswuerdig
  - 1-39 (rot): Viel Carbs, Zucker, verarbeitetes Essen
- Antworte auf DEUTSCH.
- Fuer jedes Foto: Bestimme ob es Kochen/Zubereitung oder fertiges Essen zeigt.

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
  "comment": "Kurzer Kommentar mit Verbesserungsvorschlag",
  "photo_types": ["finished", "cooking"]
}"""


def _load_image_as_base64(filepath: Path) -> tuple[str, str]:
    """Load image and return (base64_data, media_type)."""
    data = filepath.read_bytes()
    b64 = base64.standard_b64encode(data).decode("utf-8")
    suffix = filepath.suffix.lower()
    media_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    return b64, media_type


async def analyze_meal_photos(photo_paths: list[str]) -> dict:
    """Analyze one or more food photos using Claude Vision.

    Args:
        photo_paths: List of file paths relative to FOOD_DIR

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

    content.append({"type": "text", "text": ANALYSIS_PROMPT})

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
