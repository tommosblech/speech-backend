# Outlook-Rechnungsassistent

Ein Python-Assistent, der dein Outlook-Postfach nach Rechnungen durchsucht —
PDF- und Text-Anhänge sowie reine Text-Rechnungen im Mailkörper —, sie
analysiert, klassifiziert und archiviert.

Er kennt **zwei Wege zum Postfach**:

- **Lokales Outlook (Standard, empfohlen)**: liest die Mails direkt aus dem
  auf deinem Windows-Rechner installierten Outlook — egal, bei welchem
  Anbieter das Postfach liegt. Keine Anmeldung, kein Azure, keine
  Zugangsdaten. Voraussetzung: klassisches Outlook (beim „neuen Outlook"
  den Schalter oben rechts ausstellen).
- **Microsoft-Cloud (Graph-API, optional)**: greift direkt auf ein
  Outlook.com-/Microsoft-365-Postfach in der Cloud zu; braucht eine
  einmalige (kostenlose) Azure-App-Registrierung. Nützlich, wenn der
  Assistent auf einem Rechner ohne Outlook laufen soll.

Ohne Konfiguration startet der Assistent mit dem lokalen Outlook; sobald in
`config.json` eine `client_id` steht (oder `"mail_source": "graph"`), nutzt
er die Cloud.

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
- **Nur Lesezugriff** auf das Postfach — der Assistent verändert oder löscht
  keine E-Mails. Alle Daten bleiben lokal auf deinem Rechner.

## 1. Installation

Voraussetzung: Python 3.10+ (Windows: von <https://python.org>, beim
Installieren „Add to PATH" ankreuzen).

**Das war's schon:** `Rechnungsassistent.bat` doppelklicken (Windows) bzw.
`./rechnungsassistent.sh` ausführen (macOS/Linux). Beim ersten Start werden
die Abhängigkeiten automatisch installiert, danach öffnet sich der Browser
mit der Oberfläche. Eine `config.json` ist nur für die Cloud-Variante oder
eigene Kategorien nötig (Vorlage: `config.example.json`).

<details>
<summary><b>Nur für die Cloud-Variante:</b> Azure-App registrieren (kostenlos, ~5 Minuten)</summary>

Die Graph-API verlangt eine registrierte App als „Zugangstür":

1. <https://portal.azure.com> → **Microsoft Entra ID** → **App-Registrierungen** → **Neue Registrierung**
   (falls dein Konto nur Gast in fremden Verzeichnissen ist: vorher unter
   „Mandanten verwalten" → „Erstellen" ein eigenes Verzeichnis anlegen)
2. Name: z. B. `Rechnungsassistent`
3. Unterstützte Kontotypen: **„Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten"**
4. Redirect-URI: leer lassen
5. Nach dem Anlegen: **Anwendungs-ID (Client-ID)** kopieren und in `config.json` eintragen
6. Unter **Authentifizierung** → „Öffentliche Clientflows zulassen" auf **Ja** stellen
7. Unter **API-Berechtigungen**: `Microsoft Graph → Delegiert → Mail.Read` hinzufügen

Bei einem Firmen-/Schulkonto (Microsoft 365) in `config.json` statt
`"tenant": "consumers"` die Tenant-ID oder `"organizations"` eintragen.
</details>

## 2. Benutzung (Web-Oberfläche)

Nach dem Start öffnet sich <http://127.0.0.1:8321> — die Oberfläche läuft nur
auf deinem Rechner, nichts ist von außen erreichbar.

1. **Verbinden**: Ein Klick verbindet mit deinem lokalen Outlook (bzw. zeigt
   bei der Cloud-Variante den Microsoft-Anmeldecode). Meist verbindet sich
   der Assistent beim Start schon automatisch.
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

1. **Verbindung**: mit dem lokalen Outlook direkt; bei der Cloud-Variante
   per Device-Code (Code unter <https://microsoft.com/devicelogin> eingeben,
   wird lokal zwischengespeichert).
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
