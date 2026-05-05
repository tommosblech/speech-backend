const express = require("express");
const cors = require("cors");
require("dotenv").config();

const app = express();
app.use(cors());
app.use(express.json());

app.get("/", (req, res) => {
  res.send("Backend läuft");
});

app.post("/api/realtime/session", async (req, res) => {
  try {
    const { persona, scenario, phase } = req.body || {};
    const personaName = persona?.name || "Thomas";
    const personaRole = persona?.role || "Mitarbeitender";
    const personaStyle = persona?.style || "sachlich, leicht kritisch, zurückhaltend";
    const personaBehavior = persona?.behavior || "antwortet knapp, hinterfragt Aussagen, fordert Klarheit";
    const voicePreference = persona?.voicePreference || "echo";

    const instruction = `
Du simulierst ausschließlich einen Mitarbeitenden in einem Mitarbeitergespräch.

WICHTIGE ROLLE:
- Du bist NICHT Coach.
- Du bist NICHT Trainer.
- Du bist NICHT Führungskraft.
- Du bist ausschließlich der Mitarbeitende in der Simulation.

IDENTITÄT:
- Dein Name ist genau: ${personaName}
- Deine Rolle ist genau: ${personaRole}
- Verwende keinen anderen Namen.
- Stelle dich nicht mit einem anderen Namen vor.
- Erfinde keine neue Identität.

PERSÖNLICHKEIT:
- Stil: ${personaStyle}
- Verhalten: ${personaBehavior}

SZENARIO:
${scenario || "Standard-Mitarbeitergespräch"}

AKTUELLE PHASE:
${phase || "Einstieg"}

VERHALTENSREGELN:
- Antworte kurz, maximal 2 bis 3 Sätze.
- Sprich natürlich und dialogisch.
- Bleibe in der Rolle des Mitarbeitenden.
- Gib keine Coaching-Hinweise.
- Übernimm niemals die Rolle der Führungskraft.
- Bleibe in der aktuellen Gesprächsphase.
- Greife keine späteren Phasen vor.
- Beginne nicht mit einer freien Selbstvorstellung.
- Warte auf die Führungskraft.
- Reagiere erst auf die Ansprache.

AUSDRÜCKLICH VERBOTEN:
- Coaching
- Moderation
- Meta-Kommentare
- Rollenwechsel
- neuer Name
`;

    const response = await fetch(
      "https://api.openai.com/v1/realtime/client_secrets",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          session: {
            type: "realtime",
            model: "gpt-realtime",
            instructions: instruction,
            audio: {
              output: {
                voice: voicePreference,
              },
            },
          },
        }),
      }
    );

    const data = await response.json();
    console.log("OpenAI Antwort:", JSON.stringify(data, null, 2));

    if (!response.ok) {
      return res.status(response.status).json({
        error: "OpenAI Fehler",
        details: data,
      });
    }

    res.json(data);
  } catch (error) {
    console.error("Fehler beim Erstellen der Realtime-Session:", error);
    res.status(500).json({ error: "Session konnte nicht erstellt werden" });
  }
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
  console.log(`Backend läuft auf Port ${PORT}`);
});
