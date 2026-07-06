# Outlook-Rechnungsassistent

Ein Python-Assistent, der dein Outlook-Postfach über die Microsoft-Graph-API
(die offizielle Outlook-Schnittstelle) nach Rechnungen durchsucht — PDF- und
Text-Anhänge sowie reine Text-Rechnungen im Mailkörper —, sie analysiert,
klassifiziert und archiviert. Er greift direkt auf dein Outlook-Konto in der
Microsoft-Cloud zu; es spielt keine Rolle, ob du Outlook am Desktop, im
Browser oder am Handy nutzt.

**Bedienung:** über eine lokale Web-Oberfläche im Browser — Doppelklick auf
`Rechnungsassistent.bat` (Windows) bzw. `./rechnungsassistent.sh`
(macOS/Linux) genügt. Für Automatisierung gibt es zusätzlich eine
Kommandozeile.

**Grundprinzipien:**

- **Erklärt jeden Schritt** und tut nichts ohne dein Zutun: Anmeldung, Scan
  und jede Zuordnung werden per Klick ausgelöst.
- **Fragt bei unbekannten Absendern**, ob die Rechnung privat oder
  geschäftlich ist und zu welcher Kostenkategorie sie gehört
  (z. B. „Elektroladen", „KI-Tools").
- **Lernt aus deinen Antworten**: Die Zuordnung wird als Regel gespeichert
  (pro Absenderadresse oder ganzer Domain) und künftig automatisch angewendet.
- **Archiviert geschäftliche Rechnungen** unter `data/invoices/JAHR/MONAT/`
  und erfasst alle Rechnungen in einer lokalen SQLite-Datenbank.
- **Monatsbericht**: erzeugt eine druckbare HTML-Zusammenfassung pro Monat,
  gruppiert nach Kostenkategorie, mit Summen je Kategorie und Gesamtsumme.
- **Nur Leserechte** am Postfach (`Mail.Read`) — der Assistent verändert
  oder löscht keine E-Mails. Alle Daten bleiben lokal.

## 1. Einmalige Einrichtung

### a) Azure-App registrieren (kostenlos, ~5 Minuten)

Die Graph-API verlangt eine registrierte App als „Zugangstür":

1. <https://portal.azure.com> → **Microsoft Entra ID** → **App-Registrierungen** → **Neue Registrierung**
2. Name: z. B. `Rechnungsassistent`
3. Unterstützte Kontotypen: **„Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten"**
4. Redirect-URI: leer lassen
5. Nach dem Anlegen: **Anwendungs-ID (Client-ID)** kopieren
6. Unter **Authentifizierung** → „Öffentliche Clientflows zulassen" auf **Ja** stellen
7. Unter **API-Berechtigungen**: `Microsoft Graph → Delegiert → Mail.Read` hinzufügen

### b) Installation

Voraussetzung: Python 3.10+ (Windows: von <https://python.org>, „Add to PATH" ankreuzen).

1. `config.example.json` als `config.json` kopieren und die Client-ID eintragen.
2. `Rechnungsassistent.bat` doppelklicken (Windows) bzw. `./rechnungsassistent.sh`
   ausführen (macOS/Linux) — beim ersten Start werden die Abhängigkeiten
   automatisch installiert, danach öffnet sich der Browser.

Bei einem Firmen-/Schulkonto (Microsoft 365) in `config.json` statt
`"tenant": "consumers"` die Tenant-ID oder `"organizations"` eintragen.

## 2. Benutzung (Web-Oberfläche)

Nach dem Start öffnet sich <http://127.0.0.1:8321> — die Oberfläche läuft nur
auf deinem Rechner, nichts ist von außen erreichbar.

1. **Anmelden**: Knopf klicken, den angezeigten Code auf der
   Microsoft-Seite eingeben. Einmalig; der Zugang wird lokal gespeichert.
2. **Scan starten**: Zeitraum wählen. Der Scan läuft im Hintergrund,
   der Fortschritt wird angezeigt.
3. **Offene Fragen** beantworten: Für jede Rechnung eines unbekannten
   Absenders wählst du per Klick privat/geschäftlich und die Kategorie
   (oder tippst eine neue). Mit dem Häkchen „Zuordnung merken" wird daraus
   eine Regel — für die einzelne Adresse oder die ganze Domain — die künftig
   automatisch angewendet wird.
4. **Rechnungen**: Liste aller erfassten Rechnungen, filterbar nach Monat und Art.
5. **Gelernte Regeln**: alle Zuordnungen einsehen und bei Bedarf löschen
   (dann fragt der Assistent beim nächsten Treffer neu).
6. **Monatsberichte**: druckbare Zusammenfassung erzeugen und im Browser
   öffnen (Strg+P zum Drucken).

## 3. Benutzung (Kommandozeile, optional)

### Postfach durchsuchen

```bash
python -m invoice_assistant scan                       # letzte 31 Tage
python -m invoice_assistant scan --since 2026-06-01 --until 2026-07-01
python -m invoice_assistant scan --query Rechnung      # zusätzlich Volltextsuche
```

Ablauf (jeder Schritt wird angekündigt und bestätigt):

1. **Anmeldung** per Device-Code: Es erscheint ein Code, den du unter
   <https://microsoft.com/devicelogin> eingibst. Der Token wird lokal
   zwischengespeichert, du musst dich nicht bei jedem Lauf neu anmelden.
2. **Suche** der Mails im Zeitraum.
3. **Erkennung**: PDF-/Text-Anhänge und ggf. der Mailtext werden auf
   Rechnungsmerkmale geprüft (Schlüsselwörter, Rechnungsnummer, Betrag).
   Betrag, Datum und Rechnungsnummer werden automatisch extrahiert.
4. **Klassifizierung**:
   - Bekannter Absender → gelernte Regel wird automatisch angewendet.
   - Unbekannter Absender → Rückfrage: privat oder geschäftlich? Welche
     Kategorie? Regel für Adresse oder ganze Domain speichern?
5. **Ablage**: Geschäftliche Rechnungen werden nach Bestätigung unter
   `data/invoices/JAHR/MONAT/` gespeichert; private werden nur in der
   Datenbank vermerkt.

Bereits erfasste Rechnungen (gleiche Mail + Datei) werden übersprungen —
der Scan ist also beliebig oft wiederholbar.

### Monatsbericht erzeugen

```bash
python -m invoice_assistant report                 # Vormonat
python -m invoice_assistant report --month 2026-06
```

Erzeugt `data/reports/rechnungen_2026-06.html` — im Browser öffnen und mit
Strg+P drucken. Enthält alle geschäftlichen Rechnungen des Monats, nach
Kategorie gruppiert, mit Zwischensummen und Gesamtsumme.

### Gelernte Regeln ansehen

```bash
python -m invoice_assistant rules
```

Falsche Zuordnung? Einfach beim nächsten Treffer neu beantworten — oder die
Regel direkt in der SQLite-Datenbank (`data/invoices.sqlite3`, Tabelle
`rules`) anpassen. Eine neue Antwort auf dasselbe Muster überschreibt die
alte Regel.

## 4. Datenablage

```
data/
├── invoices.sqlite3        # Datenbank: Rechnungen + gelernte Regeln
├── token_cache.json        # Microsoft-Login-Token (nur lokal)
├── invoices/2026/06/       # abgelegte geschäftliche Rechnungen
├── pending/                # noch nicht zugeordnete Rechnungen (Offene Fragen)
└── reports/                # druckbare Monatsberichte
```

Der gesamte `data/`-Ordner sowie `config.json` sind per `.gitignore`
ausgeschlossen und landen nie im Repository.

## 5. Automatisierung (optional)

Für einen regelmäßigen Lauf ohne Rückfragen — unbekannte Absender werden
dann übersprungen und beim nächsten interaktiven Lauf nachgefragt:

```bash
python -m invoice_assistant scan --yes
```
