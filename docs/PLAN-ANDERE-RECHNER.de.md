# Plan: die GPU eines anderen Rechners nutzen (Aragon)

Entwurf vom 05.11.2026 aus einem Gespräch mit Peuqui in Agent-Orc; **gebaut am 22.11.2026**. Wie es jetzt
funktioniert, steht im README (Abschnitt „Andere Rechner“). Was sich gegenüber dem Entwurf geändert hat:

- Ein Eintrag mit Rechner kann **CPU oder GPU** des Rechners sein (nicht nur GPU): Aragons CPU ist frei, wenn der Mini
  ausgelastet ist. Auf dem Mini eingestellte Reihenfolge: GPU Aragon, GPU Mini (Parakeet, dann Whisper medium), CPU Aragon,
  CPU Mini.
- Aragon nimmt nur, was frei ist (kein `/free` am GPU-Orc von Aragon): Ist die Karte voll, antwortet er `503`, und der
  nächste Eintrag übernimmt.
- Die Sprechertrennung geht mit an den Rechner, der transkribiert; lokal startet sie erst, wenn ein lokaler Eintrag drankommt.
- Zwei Zeitgrenzen: `host_probe_timeout_ms` (300 ms) für „ist er da“, `host_timeout_s` (wie die eigene GPU) für die Arbeit.
  Antwortet ein Rechner gar nicht, fallen in dieser Anfrage alle seine Einträge weg.
- Adresse über das Heimnetz (`http://aragon:5080`), nicht über das Direktkabel; Windows leitet 5080 per
  `wsl-port-manager.bat` (Regel „WSL2 STT-Suite“, nur Heimnetz) an WSL weiter.
- Gemessen am 22.11.2026 mit 71 s Sprache, Parakeet fp32: CPU Mini unter Last 5,1–7,9 s, CPU Aragon 1,9 s, GPU Aragon
  warm 0,72 s, über den Mini mit `device=auto` an Aragons GPU 0,93 s gesamt.

Der Text darunter ist der ursprüngliche Entwurf.

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

## Offen (Stand 05.11.2026) und wie es entschieden wurde

- Wer baut es? Die Sitzung im Ordner STT-Suite, am 22.11.2026.
- Sagt die Antwort, wo transkribiert wurde? Ja: `host` (`null` = dieser Rechner) neben `device`.
- Wie lange darf Aragon brauchen? Ob er da ist, entscheidet die kurze Grenze (300 ms); für die Transkription gilt dieselbe
  Grenze wie für die eigene GPU. Ein kalter GPU-Worker auf Aragon lädt Parakeet in etwa 1,5 s.
