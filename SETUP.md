# Gold AI-Score Signal Bot -- Einrichtung

Ein kostenloser, von TradingView unabhängiger Bot, der alle 4 Stunden prüft, ob die
Gold-AI-Score-Strategie ein neues Long/Short-Signal auf XAUTUSDT (Bitget) ausgelöst hat,
und dir Entry/Stop-Loss/TP1/TP2 per Telegram schickt. Läuft auf GitHub Actions (kostenlos),
dein PC muss dafür nicht an sein.

Drei Schritte, alle einmalig:

## 1. Telegram-Bot erstellen (2 Minuten)

1. Öffne Telegram, suche nach **@BotFather** und starte einen Chat.
2. Schick ihm `/newbot`, gib deinem Bot einen Namen und einen Nutzernamen (muss auf `bot` enden, z.B. `mein_gold_signal_bot`).
3. BotFather schickt dir einen **Token** zurück (sieht aus wie `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`). **Notieren.**
4. Schreib deinem neuen Bot irgendeine Nachricht (z.B. "Hallo"), damit er weiß, wer du bist.
5. Öffne im Browser: `https://api.telegram.org/bot<DEIN_TOKEN>/getUpdates` (Token einsetzen).
   In der Antwort findest du `"chat":{"id":123456789,...}` -- diese Zahl ist deine **Chat-ID**. **Notieren.**

## 2. GitHub-Repository erstellen (5 Minuten)

1. Falls du noch keinen Account hast: kostenlos auf [github.com](https://github.com) registrieren.
2. Neues **privates** Repository erstellen (z.B. `gold-signal-bot`).
3. Diesen ganzen Ordner (`gold-signal-bot/`, inklusive dem versteckten Unterordner
   `.github/workflows/`) in das Repository hochladen -- entweder per Drag&Drop im Browser
   ("Add file" -> "Upload files") oder mit `git push`, falls du damit vertraut bist.

## 3. Secrets hinterlegen (2 Minuten)

Im Repository: **Settings -> Secrets and variables -> Actions -> New repository secret**

Zwei Secrets anlegen:
- Name `TELEGRAM_BOT_TOKEN`, Wert: dein Token aus Schritt 1
- Name `TELEGRAM_CHAT_ID`, Wert: deine Chat-ID aus Schritt 1

## Fertig

Der Bot läuft ab jetzt automatisch alle 4 Stunden (00:10, 04:10, 08:10, 12:10, 16:10, 20:10 UTC).
Zum sofortigen Testen: im Repository unter **Actions -> Gold AI-Score Signal Bot -> Run workflow**
manuell einmal auslösen -- du solltest innerhalb einer Minute entweder eine Telegram-Nachricht
bekommen (falls gerade ein Signal aktiv ist) oder der Lauf endet einfach ohne Nachricht (kein
Signal gerade), beides ist normal.

## Wichtig zu wissen

- Der Bot **handelt nicht**, er schickt nur Nachrichten. Kein API-Key mit Handelsrechten nötig,
  nur die öffentlichen (unauthentifizierten) Bitget-Kursdaten.
- Er führt intern ein "Papier-Trade"-Buch (`state.json`), um zu wissen, ob gerade eine simulierte
  Position offen ist -- das wird nach jedem Lauf automatisch ins Repository zurückgeschrieben.
- Wenn wir die Strategie im Pine-Skript später nochmal ändern (z.B. Threshold, Stop-Multiplikator),
  muss `signal_bot.py` von Hand nachgezogen werden -- die beiden Implementierungen laufen sonst auseinander.
