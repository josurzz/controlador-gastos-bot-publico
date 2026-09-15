# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal expense tracker: send a Telegram voice note ("gasté 3000 en el super con débito"), it gets transcribed locally (Whisper) and interpreted by an AI into structured fields, then written as a row in a Google Sheet.

**This is the "backend"** — it only writes expenses to the Sheet, it has no UI to view/edit them. Optionally paired with a separate, sibling repo — [`controlador-gastos-webapp-publico`](https://github.com/josurzz/controlador-gastos-webapp-publico), the "frontend": a static PWA (own repo, not required, no shared code) that reads/writes the same Sheet directly from the browser for dashboards, editing, and income tracking. Neither depends on the other to work.

**One shared `main.py` serves any number of people** — each has their own folder under `people/<name>/` with their own config (categories, payment methods), credentials (`.env`, `service_account.json`), and Google Sheet. Never hardcode something specific to one person into `main.py` — it belongs in that person's `config.json`/`.env` instead. See `README.md` for the human-facing "add a new person" steps; this file covers what a coding session needs to know to change the shared logic safely.

**No real person's folder is ever tracked in git** — `people/_example/` with placeholder values is the only person-shaped thing that should ever be committed. `.gitignore` has `/people/*` + `!/people/_example/`: everything under `people/` is ignored except that example folder. Don't add narrower per-person exceptions.

## `people/<name>/` — per-person data, not code

```
people/
├── _example/           # tracked in git: template for onboarding a new person
│   ├── config.json
│   └── .env.example
├── alice/               # one folder per real person, NEVER tracked
│   ├── config.json     # categorias, medios_pago, aclaraciones_categorias
│   ├── .env             # tokens/keys, real Sheet ID
│   └── service_account.json
└── bob/
    └── (same shape)
```

`config.json` schema (all of `categorias`/`medios_pago` required, `aclaraciones_categorias` optional — empty string if unused):

```json
{
  "categorias": ["Supermercado", "Transporte", "..."],
  "medios_pago": ["Efectivo", "Debito", "Credito"],
  "aclaraciones_categorias": "free text inserted verbatim into the AI prompt, explaining what this person's non-obvious category names mean (nicknames, personal shorthand, etc.) — omit or leave empty if none apply"
}
```

`main.py` loads `.env` and `config.json` from the **current working directory** — this only works because each systemd instance sets `WorkingDirectory` to that person's `people/<name>/` folder (see `systemd/gastos-bot@.service`, a template unit — `sudo systemctl enable --now gastos-bot@<name>.service`). `NOMBRE_PERSONA` (used only for the log filename) is derived from that same cwd's basename — don't reintroduce a separate "name" field in `config.json` for this, it's redundant with the folder name.

**`setup.py`** is an interactive wizard that creates a `people/<name>/` folder (first install or adding another person) — validates the name doesn't already collide, and if other people already exist, offers to reuse whatever's shareable between them (AI provider + its key, `service_account.json`, server-wide settings) instead of re-asking. It only touches disk at the very end (after all prompts succeed) specifically so Ctrl-C mid-wizard never leaves a half-created `people/<name>/` folder blocking a retry with the same name — keep that ordering if you touch this file. It has zero dependencies beyond the stdlib, deliberately, so it can run before `pip install -r requirements.txt`.

## `main.py`

Single shared file, `python-telegram-bot`. Flow: audio → `transcribir_audio()` (faster-whisper, local, free) → `interpretar_gastos()` (builds a system prompt listing that person's `CATEGORIAS`/`MEDIOS_PAGO`, sends it via `_llamar_ia()` to whichever AI is configured, gets back a JSON array — one message can describe several purchases) → `_escribir_gasto()` writes to the Sheet.

**AI provider is not hardcoded** — `AI_PROVIDER` env var (`anthropic` default, `openai`, `gemini`, `ollama`) picks one of `_PROVEEDORES_IA`'s functions (`_llamar_anthropic`, `_llamar_openai`, `_llamar_gemini`, `_llamar_ollama`). Each lazily imports its own SDK and reads its own env vars *inside* the function — never move a provider's `import` or required env var read to module level, that would force every installation to have that provider's package/key present even when unused. All four return the same thing: the raw response text (JSON as a string, not yet parsed) — `interpretar_gastos()` does the parsing/validation identically regardless of provider. When adding a new provider, follow that exact shape and register it in `_PROVEEDORES_IA`.

Key invariant: **cuota (installment) cascading is done with Sheet formulas, not app code.** Cuota 1's `Monto` cell is a formula (`=MontoTotal/cuotas`), cuota 1's date is literal (month of purchase, no offset), and cuotas 2+ are formulas referencing cuota 1's row (`=EDATE(...)` for date, direct cell refs for everything else). Editing cuota 1 anywhere automatically recalculates the rest — never write code that recalculates cuotas 2+ manually. The one exception is the **"Mes de pago"** column (see below): unlike every other column, each cuota writes its own independent literal value, not a formula referencing cuota 1 — deliberate, so a single installment's billing month can be corrected without affecting the others.

**"Mes de pago"**: a credit card's real statement-close date isn't a fixed day of the month (e.g. "the last Thursday", which can land anywhere from a few days before month-end to a few days into the next month) — so instead of assuming "purchase month + 1" always, the bot detects when a credit purchase falls in an ambiguous window (last `DIAS_ANTES_FIN_DE_MES_PARA_PREGUNTAR` days of a month, or first `DIAS_DESPUES_INICIO_DE_MES_PARA_PREGUNTAR` days of the next) and asks via inline buttons: "Ya cerró" / "Todavía no cerró" / "No sé" (treated the same as "Ya cerró", with a popup clarifying it's an assumption). The answer is cached for the rest of that calendar day (`_cache_cierre`) so it isn't asked again for every subsequent credit purchase logged that day. Requires `concurrent_updates=True` on the `Application` — without it, the bot can't process the button-click update while the message handler that's awaiting it is still running (a real deadlock this project hit once).

Other things the AI is explicitly told never to do: multiply/divide the spoken amount by quantity, cuotas, etc. — the extracted `monto` must be the literal number said, copied as-is (`monto_es_por_cuota` flags the one exception where the total needs computing from a stated per-installment value).

Logging: writes to console and `logs/<name>.log` (rotating, gitignored, one file per person) — resolved relative to where `main.py` itself lives, not the per-person `WorkingDirectory`, so all people's logs land in one shared `logs/` folder instead of scattering into each `people/<name>/`. `httpx`/`httpcore` and `telegram` are forced to `WARNING` (they otherwise log every Telegram long-poll request — including the bot token in the URL — and library lifecycle noise like "Application started" on every restart). Only generic operational lines should be logged (e.g. "Llamando a la API de Anthropic", "Gasto registrado en la planilla") — never the actual monto/categoria/descripcion, if you care about that kind of privacy for your own use. Never delete or truncate a log file while its service is running (`rm`, `> logs/<name>.log`) — the process keeps writing to the deleted file's inode, invisible at that path, until it's restarted; use `sudo systemctl restart gastos-bot@<name>.service` instead.

**Categories/payment methods can live in a "Config" tab of the Sheet, not just `config.json`** — if you pair this with a webapp/dashboard that lets you edit them live, `config.json` becomes only the one-time seed and the "Config" tab (created once, never overwritten if it already exists) is read fresh on every message. If you don't have such a companion app, `config.json` is all you need.

## Running / deploying

No test suite, no build step.

```bash
# Restart one person's bot after editing their config.json/.env, or main.py (shared - restart ALL instances then)
sudo systemctl restart gastos-bot@<name>.service
sudo systemctl status gastos-bot@<name>.service --no-pager
tail -f logs/<name>.log

# Restarting multiple instances: one at a time with a pause between —
# each loads its own Whisper model into memory; doing it simultaneously
# can push the machine into swap, making the last one take minutes.
```
