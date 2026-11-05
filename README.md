# YouTube MCP serveris

Vienas kodas veikia dviem būdais:
- **lokaliai** per Claude Desktop (stdio): `python server.py`
- **debesyje** per Prefect Horizon: entrypoint `server.py:mcp`

Horizon dokumentacijoje platforma dabar vadinama ir „FastMCP Gateway“.

| Grupė | Įrankiai | Lokaliai | Horizon |
|---|---|---|---|
| Paieška, video | `search_videos`, `get_video`, `get_comments` | ✓ | ✓ |
| Transkripcijos | `get_transcript` | ✓ | ✓ (gali reikėti proxy, žr. žemiau) |
| Kanalai, grojaraščiai | `get_channel`, `list_channel_videos`, `list_playlists`, `get_playlist_items` | ✓ | ✓ |
| Paskyros veiksmai | `post_comment`, `reply_to_comment`, `rate_video`, `create_playlist`, `add_to_playlist`, `remove_from_playlist`, `update_video` | ✓ | ✓ |
| Įkėlimas | `upload_video` | ✓ | ✗ (failas turi būti serverio diske) |

## Aplinkos kintamieji

| Kintamasis | Paskirtis |
|---|---|
| `YOUTUBE_API_KEY` | skaitymo įrankiai |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | paskyros veiksmai (OAuth). Lokaliai vietoj jų gali būti `token.json` |
| `YOUTUBE_READ_ONLY=1` | paslepia visus rašymo įrankius |
| `WEBSHARE_PROXY_USERNAME` + `WEBSHARE_PROXY_PASSWORD` arba `TRANSCRIPT_PROXY_URL` | proxy transkripcijoms |

---

## A. Google Cloud (bendra abiem variantams)

1. [console.cloud.google.com](https://console.cloud.google.com) → projektas → **APIs & Services → Library** → įjunk **YouTube Data API v3**.
2. **Credentials → Create credentials → API key**.
3. **OAuth consent screen**: External. **Audience → Test users → Add users** → tavo Gmail.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → atsisiųsk JSON kaip `client_secret.json` šalia `server.py`.

## B. Lokalus paleidimas

```bash
py -3.13 -m venv .venv                     # macOS: python3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py auth    # atsidarys naršyklė, sukurs token.json
```

`claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "youtube": {
      "command": "C:\\kelias\\youtube\\.venv\\Scripts\\python.exe",
      "args": ["C:\\kelias\\youtube\\server.py"],
      "env": { "YOUTUBE_API_KEY": "..." }
    }
  }
}
```

---

## C. Talpinimas Prefect Horizon

### 1. Gauk OAuth reikšmes debesiui

Debesyje nėra naršyklės ir nuolatinio disko, todėl `token.json` ten neveiks. Tas pačias reikšmes reikia perduoti per aplinkos kintamuosius.

**Svarbu prieš tai:** kol consent screen yra *Testing* režimu, Google refresh tokenas galioja ~7 dienas, ir hostintas serveris kas savaitę nustos veikti. Asmeniniam naudojimui:
**Google Auth Platform → Audience → Publish app** (*In production*). Patikros (verification) nereikia. Prisijungiant matysi įspėjimą „Google hasn't verified this app“ → *Advanced → Go to …*.

Tada lokaliai:
```bash
.venv\Scripts\python.exe server.py auth        # iš naujo, po publikavimo
.venv\Scripts\python.exe server.py print-env   # išspausdins 3 eilutes
```
Išsaugok šias tris reikšmes. Tai slaptažodžio lygio duomenys: niekur jų neskelbk ir nekelk į git.

### 2. Įkelk kodą į GitHub

Repo turi būti tik šie failai: `server.py`, `requirements.txt`, `.gitignore`, `README.md`. `.gitignore` jau neleidžia įkelti `token.json` ir `client_secret.json`.

```bash
git init
git add .
git status            # patikrink: token.json ir client_secret.json NETURI būti sąraše
git commit -m "YouTube MCP server"
git branch -M main
git remote add origin https://github.com/<tavo-vardas>/youtube-mcp.git   # pirma susikurk repo GitHub'e
git push -u origin main
```
Repo gali būti privatus.

### 3. Sukurk serverį Horizon

1. Prisijunk [horizon.prefect.io](https://horizon.prefect.io) → sukurk organizaciją.
2. Prijunk GitHub: autorizuok GitHub App ir suteik prieigą prie `youtube-mcp` repo.
3. Pasirink repo ir nustatyk:
   - **Name:** pvz. `youtube-mcp`. Pagal jį bus URL `https://youtube-mcp.fastmcp.app/mcp`.
   - **Entrypoint:** `server.py:mcp`
   - Priklausomybės iš `requirements.txt` aptinkamos automatiškai. Python numatytai 3.12, tinka.
4. **Settings → Environment Variables → Add variable**, kiekvienam:
   - `YOUTUBE_API_KEY`
   - `YOUTUBE_CLIENT_ID`
   - `YOUTUBE_CLIENT_SECRET`
   - `YOUTUBE_REFRESH_TOKEN`
   - (nebūtina) `YOUTUBE_READ_ONLY` = `1`, jei nori tik skaitymo
5. Palauk, kol pirmas build'as baigsis (logai matosi **Deployments** puslapyje). Pakeitus kintamuosius reikia **naujo deploy**. Kiekvienas `git push` į `main` deploy'ina automatiškai.

### 4. Palik įjungtą Horizon Authentication

Ji įjungta numatytai, ir **neišjunk jos**. Serveris komentuoja, vertina ir redaguoja video **tavo** YouTube vardu. Be autentifikacijos tą galėtų daryti bet kas, žinantis URL. Kitiems žmonėms prieigą suteiksi pakviesdamas juos į Horizon organizaciją.

### 5. Prijunk prie Claude

- **Claude Desktop:** Horizon → serveris → **Connect → Add to Claude Desktop** → atsidaryk parsisiųstą `.mcpb` failą. Jei rodo **Authorize and retry**, prisijunk ir pakartok.
- **Claude Code:**
  ```bash
  claude mcp add --scope local --transport http youtube-mcp https://youtube-mcp.fastmcp.app/mcp
  ```
  tada Claude Code lange `/mcp` ir prisijunk.
- **claude.ai (custom connector):** Horizon dokumentacijoje neaprašyta. Patikrink **Connect** dialogą.

Lokalų `youtube` įrašą `claude_desktop_config.json` faile išimk, kitaip turėsi du vienodus serverius.

### 6. Kuo hostinta versija skiriasi

- **Nėra `upload_video`.** Serveris nemato tavo kompiuterio failų, o vienos užklausos riba yra 170 s. Įkelk lokaliai arba per YouTube Studio.
- **Transkripcijos.** YouTube dažnai blokuoja debesų IP adresus. Jei `get_transcript` grąžina „blocked“, reikia gyvenamųjų (residential) IP proxy, pvz. Webshare (mokama), ir kintamųjų `WEBSHARE_PROXY_USERNAME`/`PASSWORD`. Alternatyva: transkripcijoms naudoti lokalų serverį.
- **Diskas laikinas, 1 GB RAM.** Šiam serveriui to pakanka, jis nieko nesaugo.
- **Kvota ta pati:** 10 000 vnt./d. visam Google projektui. `search_videos` = 100 vnt.

### Gedimų paieška

| Simptomas | Priežastis |
|---|---|
| Build fail | žiūrėk build logus; dažniausiai klaida `requirements.txt` |
| „No OAuth credentials“ | nenustatyti `YOUTUBE_*` kintamieji arba po pakeitimo nebuvo redeploy |
| „Google rejected the refresh token“ | tokenas pasibaigė (Testing režimas) arba atšauktas → 1 žingsnis iš naujo, atnaujink `YOUTUBE_REFRESH_TOKEN`, redeploy |
| 401 / 403 iš Horizon | neprisijungęs arba neturi prieigos prie serverio |
| Įrankių sąrašas senas | atsijunk ir vėl prijunk klientą |
