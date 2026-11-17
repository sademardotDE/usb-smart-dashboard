# Image bridge — Bilder von anderen Plugins auf dem Display

Seit **0.5.0** kann das USB-Smart-Dashboard Bilder anzeigen, die andere
Hermes-Plugins veröffentlichen. Der aktive Renderer (Gateway-Renderthread oder
`standalone.py`) zeigt das Bild skaliert auf 480×320 und fällt danach auf die
Live-Statistik zurück.

## Inbox-Format

Verzeichnis: `<Hermes-Root>/usb-smart-dashboard/` (typisch `~/.hermes/...`).

| Datei | Inhalt |
| --- | --- |
| `show-image.jpg` | anzuzeigendes JPEG |
| `show-image.json` | `created` (Epoch-Sekunden), `duration` (Sekunden), `source` (freier Text, z. B. `motion:196758286858`) |

Regeln:

- Dateien **atomar** schreiben (`.tmp` + `os.replace`), erst Bild, dann Meta.
- `duration` ≥ 1; nach `created + duration` wird das Bild ignoriert.
- Ein neues Bild ersetzt das alte; der Renderer erkennt es über
  `mtime_ns:size` und zeichnet nur bei Änderung neu.
- Kein Löschen nötig — abgelaufene Bilder werden einfach nicht mehr angezeigt.

## Beispiel (Python)

```python
import json, os, time
from pathlib import Path

home = Path.home() / ".hermes" / "usb-smart-dashboard"
home.mkdir(parents=True, exist_ok=True)

jpeg = Path("frame.jpg").read_bytes()
tmp = home / "show-image.jpg.tmp"
tmp.write_bytes(jpeg)
os.replace(tmp, home / "show-image.jpg")

(home / "show-image.json").write_text(json.dumps(
    {"created": time.time(), "duration": 10, "source": "my-plugin"}))
```

Alternativ das Modul des Plugins nutzen (bei installiertem Plugin):

```python
import imagebridge
imagebridge.publish(jpeg_bytes, duration=10, source="my-plugin")
imagebridge.active()   # {"path": ..., "key": ..., "remaining": 9.4, "source": ...}
```

## Wie es angezeigt wird

- **Gateway-Renderthread** (`__init__.py`): prüft die Inbox alle 2 s; bei aktivem
  Bild `driver.write_image(...)`.
- **Standalone-Daemon** (`standalone.py`, `usb-smart-dashboard.service`): prüft
  die Inbox pro Tick und zeigt das Bild, solange es gültig ist.
- Der Turing-Treiber skaliert das JPEG seitenverhältnistreu und zentriert es auf
  schwarzem Hintergrund (480×320 landscape).
- Nur ein Prozess darf das Panel gleichzeitig ansteuern (Advisory-Flock); die
  Inbox ist der gemeinsame Kanal für beide.

## Integrationen

| Plugin | Nutzung |
| --- | --- |
| `iwfcam4hermes` | sendet den Bewegungs-Snapshot (`relay_motion_display=true`, Dauer 8 s) und bietet Status/Testbild (`camera_display`, `GET/POST /display`). Doku dort: `docs/USB_DISPLAY.md` |
| eigene Plugins/Skripte | JPEG + Meta in die Inbox schreiben (siehe oben) |

## Testen

```bash
# Display-Status/Test aus iwfcam4hermes
hermes cameras display status
curl -s -X POST http://127.0.0.1:8099/display/test | jq

# Journal des Renderers
journalctl --user -u usb-smart-dashboard.service -n 30 --no-pager | grep -i image
```

Erwartete Logzeile: `showing image from motion (8s)` bzw.
`usb-display: image shown (source=…)`.

## Grenzen

- Nur JPEG (der Treiber decodiert via Pillow; RGB565-Ausgabe).
- Sehr große Bilder werden vor dem Senden herunterskaliert (480×320).
- Die Anzeige ist „best effort": tote Panels/Serial-Fehler werden geloggt, nie
  in den Agenten propagiert.
