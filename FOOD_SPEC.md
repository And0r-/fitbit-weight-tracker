# Food Tracking Feature Spec

## Konzept
Foto-basiertes Essenstagebuch mit AI-Analyse. Admins laden Essensfotos hoch,
Claude Sonnet analysiert sie, gruppiert sie automatisch zu Mahlzeiten und
bewertet die Gesundheit. Integriert in fit.fdh.li.

## Kernregeln

### Tagesgrenze
- Ein "Tag" endet um **06:00 Uhr morgens**, nicht um Mitternacht
- Foto um 03:00 Sonntag = gehört zu Samstag
- Foto um 07:00 Sonntag = gehört zu Sonntag

### Meal Grouping
- Fotos innerhalb von **2 Stunden** nach dem ersten Foto = selbes Essen
- Keine festen Kategorien (Frühstück/Mittag/Abend) — nur "Essen 1", "Essen 2" etc.
- Kochen + fertiges Essen auf mehreren Fotos = AI erkennt das und zählt nicht doppelt

### Multi-Upload Debounce
- Nach Upload: **60 Sekunden** warten bevor AI-Analyse startet
- Neues Foto während der 60s = Timer reset
- Neues Foto zu bestehendem Meal = alte Analyse verwerfen, komplett neu analysieren

### Cheat Day
- Konfigurierbar via `CHEAT_DAY=saturday` (oder mehrere: `CHEAT_DAY=saturday,sunday`)
- Cheat Day Meals per Default **ausgeblendet** in der Gallery
- Toggle "Cheat Day anzeigen" blendet sie ein (spezielle Markierung)
- Nicht in Durchschnitte/Kalorienzählung eingerechnet wenn ausgeblendet
- In API/Summary **immer** enthalten (mit `is_cheat_day` Flag)

## Berechtigungen
- `can_view_food` Boolean auf ShareToken (wie `can_view_oura`)
- **Nur Admins** können Fotos hochladen und AI-Text editieren
- Freunde mit `can_view_food` sehen die Gallery read-only
- Alle Fotos gehören dem Account-Owner (single-user diary)

## AI-Analyse (Claude Sonnet)

### Schritt 1: Einzelfoto-Analyse
- Was ist auf dem Foto zu sehen?
- Kochen/Zubereitung ODER fertiges Essen?

### Schritt 2: Meal-Gesamtanalyse (alle Fotos der Mahlzeit)
- Smarte Deduplizierung (Kochen + Fertig = nicht doppelt zählen)
- Item-Breakdown mit geschätzten Portionen
- Kalorien-Schätzung (Bereich, nicht exakt)
- Makros: Protein, Kohlenhydrate, Fett
- **Health Score 1-100**:
  - 🟢 70-100: Gesund (viel Protein, Gemüse, wenig Carbs)
  - 🟡 40-69: Okay (verbesserungswürdig)
  - 🔴 1-39: Ungesund (viel Carbs, Zucker, verarbeitetes Essen)
- AI-Kommentar auf Deutsch mit Verbesserungsvorschlag
- Scoring-Fokus: Low-Carb Diät, Kohlenhydrate sind der Hauptfeind

## Datenmodell (PostgreSQL)

### Tabelle: meals
- id, created_at, updated_at
- day (date) — berechneter Tag (06:00-Regel)
- first_photo_at (datetime) — Zeitstempel des ersten Fotos
- is_cheat_day (bool)
- analysis_status: pending | analyzing | complete | failed
- total_calories, total_protein_g, total_carbs_g, total_fat_g
- health_score (1-100)
- health_color (green/yellow/red)
- ai_comment (text)
- items_json (jsonb) — Array der einzelnen Essens-Items

### Tabelle: meal_photos
- id, meal_id (FK), created_at
- filename (stored path)
- original_filename
- photo_taken_at (datetime) — aus EXIF oder Upload-Zeit
- thumbnail_path
- photo_type: cooking | finished | unknown (von AI gesetzt)

## API Endpoints

### Upload
```
POST /api/food/upload
Content-Type: multipart/form-data
- files: 1+ Bilder
- Nur Admin-Tokens
- Returns: meal_id, photo_ids
```

### Gallery
```
GET /api/food?days=7&show_cheat=false
- Requires: can_view_food
- Returns: Meals mit Fotos, Analyse, Scores
```

### Summary Integration
```json
{
  "food": {
    "today": [...meals],
    "last_7_days": {
      "avg_score": 62,
      "avg_calories": 1800,
      "meals_logged": 12
    }
  }
}
```

## Analysis Queue (PostgreSQL-basiert)

Keine externe Queue (Redis/RabbitMQ) nötig — PostgreSQL reicht fuer unser
Volumen und ist transaktional mit den Meal-Daten.

### Tabelle: analysis_queue
- id, meal_id (FK), created_at
- status: pending | processing | complete | failed
- run_after (datetime) — fruehester Ausfuehrungszeitpunkt (fuer Debounce)
- retry_count (int, default 0, max 3)
- error_message (text, nullable)
- completed_at (datetime, nullable)

### Flow
1. **Upload** → Foto gespeichert → Job in Queue mit `run_after = now + 60s`
2. **Weiteres Foto zum selben Meal** → bestehenden pending Job canceln,
   neuen mit `run_after = now + 60s` (Debounce-Reset)
3. **Worker** (APScheduler, alle 10s): `SELECT ... WHERE status = 'pending'
   AND run_after <= now() FOR UPDATE SKIP LOCKED LIMIT 1`
4. **Analyse fehlgeschlagen** → `retry_count++`, `run_after = now + 5min`
5. **Max Retries (3) erreicht** → `status = failed`, roter Banner im Admin
6. **API wieder da** → Admin kann failed Jobs manuell retriggen

### Fehlerbehandlung
- Claude API down / Guthaben leer → Job bleibt in Queue, wird spaeter retried
- Roter Banner in Admin-UI: "X Analysen fehlgeschlagen — Retry"
- Fotos gehen nie verloren, nur die Analyse wartet

## Tech Stack
- Bilder: PostgreSQL (Pfade) + Filesystem (/app/data/food/)
- Thumbnails: Pillow (server-seitig generiert)
- AI: Claude Sonnet via Anthropic API (`ANTHROPIC_API_KEY` in .env)
- Queue: PostgreSQL-basiert (kein extra Service)
- Worker: APScheduler Job (alle 10s Queue pruefen)
- Max Dateigrösse: 10MB pro Foto

## Phasen

### Phase 1 (MVP)
- DB-Modell (meals, meal_photos, analysis_queue)
- Upload Endpoint + Bildspeicherung + EXIF-Parsing
- Meal Grouping (2h Fenster) + 06:00 Tagesgrenze
- Analysis Queue + Worker
- Claude Sonnet Analyse (Einzelfoto + Meal-Gesamtanalyse)
- Einfache Gallery-Ansicht
- can_view_food Permission
- Cheat Day Logic (ENV config + is_cheat_day Flag)
- Fehler-Banner in Admin

### Phase 2
- Cheat Day Toggle in Gallery
- Thumbnail-Generierung
- AI-Text editieren (Admin)
- Summary-Integration (/api/summary)

### Phase 3
- Gallery-Verbesserungen (Statistiken, Wochen-Trends)
- Re-Analyse Button (wenn AI-Modell besser wird)
