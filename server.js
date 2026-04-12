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

    const instruction = `
Du simulierst einen Mitarbeitenden in einem Mitarbeitergespräch.

Persona:
${persona || "kritisch"}

Szenario:
${scenario || "Standard-Mitarbeitergespräch"}

Aktuelle Phase:
${phase || "Einstieg"}

Regeln:
- antworte kurz, maximal 2 bis 3 Sätze
- bleibe in der aktuellen Phase
- keine Monologe
- keine Meta-Kommentare
`;

  const response = await fetch("https://api.openai.com/v1/realtime/client_secrets", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.OPENAI_API_KEY}`,
    "Content-Type": "application/json"
  },
  body: JSON.stringify({
  session: {
    type: "realtime",
    model: "gpt-realtime",
    instructions: instruction,
    audio: {
      input: {
        format: "pcm16"
      },
      output: {
        format: "pcm16",
        voice: "marin"
      }
    }
  }
})

    const data = await response.json();

    if (!response.ok) {
      return res.status(response.status).json(data);
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