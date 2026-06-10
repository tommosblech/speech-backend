const express = require("express");
const cors = require("cors");
require("dotenv").config();

const app = express();

// Render sitzt hinter einem Proxy – nötig, damit req.ip die echte Client-IP ist
app.set("trust proxy", 1);

// ============================================================
// 1. ORIGIN-SCHUTZ (Hauptschutz)
// Nur diese Domains dürfen das Backend aufrufen.
// ============================================================
const ALLOWED_ORIGINS = [
  "https://mitarbeitergespraeche.lovable.app", // Haupt-App
  "https://marcsgespraeche.lovable.app",       // Hasselmeyer-Clone
];

app.use(
  cors({
    origin: (origin, callback) => {
      if (!origin || ALLOWED_ORIGINS.includes(origin)) {
        return callback(null, true);
      }
      return callback(new Error("Origin nicht erlaubt"));
    },
  })
);

app.use(express.json({ limit: "200kb" }));

// ============================================================
// 2. RATE-LIMITS (ohne zusätzliche npm-Pakete)
// ============================================================

// Pro IP: großzügig, weil im Seminarraum viele Nutzer EINE IP teilen.
const IP_LIMIT_MAX = 60;               // max. Session-Starts pro IP ...
const IP_LIMIT_WINDOW_MS = 60_000;     // ... pro Minute
const ipHits = new Map();

// Global: harte Kostenbremse für das gesamte Backend.
const GLOBAL_LIMIT_MAX = 300;          // Sessions pro Stunde, gesamt
const GLOBAL_LIMIT_WINDOW_MS = 60 * 60_000;
let globalHits = { count: 0, reset: Date.now() + GLOBAL_LIMIT_WINDOW_MS };

function ipRateLimited(ip) {
  const now = Date.now();
  const entry = ipHits.get(ip);
  if (!entry || entry.reset < now) {
    ipHits.set(ip, { count: 1, reset: now + IP_LIMIT_WINDOW_MS });
    return false;
  }
  entry.count += 1;
  return entry.count > IP_LIMIT_MAX;
}

function globalRateLimited() {
  const now = Date.now();
  if (globalHits.reset < now) {
    globalHits = { count: 1, reset: now + GLOBAL_LIMIT_WINDOW_MS };
    return false;
  }
  globalHits.count += 1;
  return globalHits.count > GLOBAL_LIMIT_MAX;
}

// Aufräumen, damit die IP-Map bei langer Laufzeit nicht wächst
setInterval(() => {
  const now = Date.now();
  for (const [ip, entry] of ipHits) {
    if (entry.reset < now) ipHits.delete(ip);
  }
}, 5 * 60_000);

// ============================================================
// 3. SPRACH- & ROLLENTREUE
// Wird an JEDE Session angehängt – sorgt für akzentfreies
// Hochdeutsch und stabiles Rollenverhalten.
// ============================================================
const LANGUAGE_AND_ROLE_RULES = `

SPRACHE (HÖCHSTE PRIORITÄT):
- Sprich AUSSCHLIESSLICH Deutsch – klares, natürliches Hochdeutsch.
- KEIN englischer Akzent, keine englische Aussprache deutscher Wörter, keine englischen Füllwörter.
- Sprich Namen, Zahlen und Abkürzungen deutsch aus.

ROLLENTREUE:
- Bleibe durchgehend in deiner zugewiesenen Rolle, auch bei langen Gesprächen.
- Wechsle NIEMALS in eine Coach-, Trainer- oder Assistentenrolle.
- Erfinde keine neuen Identitäten und ändere deinen Namen nicht.`;

// Maximale Länge der vom Frontend gelieferten Instructions (Schutz vor Missbrauch)
const MAX_INSTRUCTIONS_CHARS = 50_000;

// ============================================================
// Routen
// ============================================================

app.get("/", (req, res) => {
  res.send("Backend läuft");
});

app.post("/api/realtime/session", async (req, res) => {
  // Session-Endpoint NUR für Browser-Anfragen mit erlaubtem Origin.
  // curl/Skripte ohne Origin-Header werden hier abgewiesen.
  const origin = req.headers.origin;
  if (!origin || !ALLOWED_ORIGINS.includes(origin)) {
    console.warn("Abgewiesen – ungültiger Origin:", origin ?? "(keiner)");
    return res.status(403).json({ error: "Zugriff nicht erlaubt" });
  }

  if (globalRateLimited()) {
    console.warn("Globales Rate-Limit erreicht!");
    return res
      .status(429)
      .json({ error: "Zu viele Anfragen. Bitte in einigen Minuten erneut versuchen." });
  }

  if (ipRateLimited(req.ip)) {
    console.warn("IP-Rate-Limit erreicht:", req.ip);
    return res
      .status(429)
      .json({ error: "Zu viele Anfragen. Bitte kurz warten." });
  }

  try {
    const { persona, instructions, voice } = req.body || {};
    const personaName = persona?.name || "Thomas";
    const personaRole = persona?.role || "Mitarbeitender";

    // Stimme: explizite Frontend-Angabe hat Vorrang, dann Persona-Präferenz
    const sessionVoice = voice || persona?.voicePreference || "marin";

    // Instructions: Die vollständigen Roleplay-Instructions des Frontends
    // direkt in der Session verankern – das hält die Persona deutlich
    // zuverlässiger in ihrer Rolle als ein Nachschieben per Data Channel.
    const frontendInstructions =
      typeof instructions === "string" && instructions.trim()
        ? instructions.slice(0, MAX_INSTRUCTIONS_CHARS)
        : `Du bist ${personaName}, ${personaRole}. Warte auf die Führungskraft.`;

    const sessionInstructions = frontendInstructions + LANGUAGE_AND_ROLE_RULES;

    // Timeout: hängende OpenAI-Anfragen nach 15s abbrechen
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);

    let response;
    try {
      response = await fetch("https://api.openai.com/v1/realtime/client_secrets", {
        method: "POST",
        signal: controller.signal,
        headers: {
          Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          session: {
            type: "realtime",
            model: "gpt-realtime-2",
            instructions: sessionInstructions,
            audio: {
              input: {
                transcription: {
                  model: "whisper-1",
                  language: "de",
                },
              },
              output: {
                voice: sessionVoice,
              },
            },
          },
        }),
      });
    } finally {
      clearTimeout(timeout);
    }

    const data = await response.json();

    // Nur Status loggen — niemals das vollständige Response-Objekt (enthält ephemeral key)
    console.log(
      "OpenAI Session-Status:",
      response.status,
      data?.error?.message ?? "OK",
      "| Voice:", sessionVoice,
      "| Instructions:", sessionInstructions.length, "Zeichen"
    );

    if (!response.ok) {
      // Keine OpenAI-Rohdaten an den Client weiterreichen
      return res.status(response.status).json({
        error: "Session konnte nicht erstellt werden",
      });
    }

    res.json(data);
  } catch (error) {
    const isTimeout = error?.name === "AbortError";
    console.error(
      isTimeout ? "OpenAI-Anfrage Timeout (15s)" : "Fehler beim Erstellen der Realtime-Session:",
      isTimeout ? "" : error
    );
    res.status(isTimeout ? 504 : 500).json({ error: "Session konnte nicht erstellt werden" });
  }
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
  console.log(`Backend läuft auf Port ${PORT}`);
});
