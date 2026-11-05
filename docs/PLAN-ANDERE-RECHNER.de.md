# Plan: die GPU eines anderen Rechners nutzen (Aragon)

Stand 05.11.2026, Entwurf aus einem Gespräch mit Peuqui in Agent-Orc. Noch nichts davon ist gebaut.

## Anlass

Auf dem Mini belegt ein sehr großes GLM-Modell den Grafikspeicher fast ganz. Ob daneben noch das
GPU-Modell der STT-Suite Platz hat, ist offen. Die TTS-Suite kann ihre Engines schon auf Aragon
laufen lassen; die STT-Suite soll das genauso können: Steht Aragons GPU in der Automatik-Liste vorn,
transkribiert Aragon, und fällt Aragon aus, übernimmt der Mini.

## Wie es die TTS-Suite macht (zum Vergleich)

- Ein Dienst auf dem Mini kennt die Rechner (`~/.config/tts-suite/config.json`, `hosts`: Aragon,
  `10.0.0.2`, SSH-Port 2222, GPU-Orc unter `http://10.0.0.2:5070`).
- Auf Aragon liegen eine Kopie des Repositorys und Docker; jede Engine ist ein eigener Container.
- Gestartet und gestoppt wird per SSH mit einem eigenen Schlüssel der Suite, der auf Aragon nur
  `scripts/tts-host-ctl.sh` ausführen darf (`start`, `stop`, `status`, `receive-voices`).
- Die Lesung schickt der Dienst auf dem Mini übers Netz direkt an den Container auf Aragon, nachdem
  er dessen GPU-Orc gefragt hat.
- Ein Aufrufer (Agent-Orc, AIfred) nennt nur eine Liste von Engine und Rechner; der erste Eintrag,
  der sprechen kann, spricht.

llama-swap hat damit nichts zu tun; er wechselt nur die Sprachmodelle.

## Wie die STT-Suite heute ist

- Ein Container je Rechner (`stt-suite`). Das CPU-Modell bleibt geladen; das GPU-Modell lädt ein
  Kindprozess (GPU-Worker) auf einer Karte **dieses** Rechners und endet nach `STT_GPU_TTL_MINUTES`
  Leerlauf.
- `device=auto` geht die Automatik-Liste (`auto_chain`) durch, deren Einträge nur diesen Rechner
  kennen.
- Seit 05.11.2026 (5fc5bb9) kann der Dienst auf dem LAN lauschen (`STT_HOST`), abgesichert mit
  einem Token (`STT_TOKEN_DIR`, `STT_TOKEN_FILE`, Kopf `X-STT-Suite-Token`). Das ist der erste
  Baustein.

## Vorschlag: nach dem Muster der TTS-Suite

Die Aufrufer fragen weiter **nur** die STT-Suite auf dem Mini. Die schickt eine Aufnahme bei Bedarf
an eine **zweite STT-Suite auf Aragon** (`POST /transcribe` mit `device=cuda`). Die Automatik-Liste
bekommt Einträge mit Rechner, zum Beispiel:

1. GPU auf Aragon (Whisper large-v3)
2. GPU auf dem Mini
3. CPU auf dem Mini

Ist Aragon aus, antwortet er nicht rechtzeitig oder ist seine Karte belegt (`503`), geht es mit dem
nächsten Eintrag weiter, wie heute schon zwischen GPU und CPU.

Anders als bei der TTS-Suite braucht es **keinen Start per SSH**: Der STT-Container ist leicht (nur
das CPU-Modell im Arbeitsspeicher), läuft auf Aragon dauerhaft und lädt das GPU-Modell erst bei
einer Anfrage; nach dem Leerlauf gibt er die Karte von selbst frei.

## Voraussetzungen

### Auf Aragon

1. Docker mit NVIDIA Container Toolkit in WSL (für die TTS-Container vermutlich schon da; prüfen).
2. Eine Kopie der STT-Suite und ihr Container mit `restart: unless-stopped`; die Modelle lädt er
   dort einmal herunter (Volume).
3. Lauschen fürs LAN: `STT_HOST` (die Adresse in WSL oder `0.0.0.0`) und ein Token;
   `STT_GPU_ORC_URL` auf den GPU-Orc von Aragon.
4. Eine Windows-Portweiterleitung für 5080 nach WSL, wie für 2222 („WSL2 Port Manager“): WSL läuft
   mit NAT, sonst erreicht niemand den Port.
5. WSL startet ohne Windows-Anmeldung (geplante Aufgabe „WSL beim Systemstart“, eingerichtet am
   05.11.2026; der Test nach einem Neustart ohne Anmeldung steht noch aus).

### In der STT-Suite auf dem Mini (der eigentliche Bau)

6. Eine Liste der Rechner in der Konfiguration: Name, Adressen (Kabel `10.0.0.2`, Heimnetz
   `192.168.0.1`; die erste, die antwortet), Port, Token-Datei.
7. Einträge der Automatik-Liste mit Rechner; das Weiterreichen der Aufnahme an die Suite dort, mit
   Zeitgrenze und Rückfall auf den nächsten Eintrag.
8. In der Oberfläche: Aragon im Engines-Reiter (Zustand, Modell, Burn-in dort) und in der
   Automatik-Liste.
9. `/gpu/holders` und `/gpu/release` bleiben je Rechner: Den GPU-Worker auf Aragon gibt der GPU-Orc
   von Aragon frei, nicht der des Mini.

### In den Aufrufern

10. Agent-Orc und AIfred: nichts. Sie fragen weiter den Mini mit `device=auto`.

## Offen

- Wer baut es (eine Sitzung im Ordner STT-Suite oder Agent-Orc)?
- Soll die Antwort sagen, wo transkribiert wurde (heute `device`: `cuda`/`cpu`), etwa mit dem
  Rechner dazu, damit Agent-Orc am Knopf „GPU Aragon“ zeigen kann?
- Wie lange darf Aragon brauchen, bis der Mini übernimmt (Zeitgrenze für einen kalten GPU-Worker,
  der das Modell erst laden muss)?
