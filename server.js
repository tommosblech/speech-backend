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

    // Minimale Basis-Instruction — die vollständigen Roleplay-Instructions
    // werden vom Frontend als System-Message über den Data Channel gesendet.
    const instruction = `Du bist ${personaName}, ${personaRole}. Antworte auf Deutsch. Warte auf die Führungskraft.`;

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
            model: "gpt-realtime-2",
            instructions: instruction,
            audio: {
              input: {
                transcription: {
                  model: "whisper-1",
                  language: "de",
                },
              },
              output: {
                voice: voicePreference || "marin",
              },
            },
          },
        }),
      }
    );

    const data = await response.json();
    // Nur Status loggen — niemals das vollständige Response-Objekt (enthält ephemeral key)
    console.log("OpenAI Session-Status:", response.status, data?.error?.message ?? "OK");

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
