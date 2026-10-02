# Deploying VORA

Frontend on Cloudflare Pages, sign-in on Supabase, backend on a free Hugging Face **Docker Space**. One container runs VORA, Chromium and a private SearXNG. The Space's disk is wiped on every restart, so the database is restored from, and snapshotted to, a private Hugging Face dataset.

## One-time setup (in the browser)

1. **Token**: huggingface.co → Settings → Access Tokens → New token, type **Write**, name `vora-space`.

2. **Backup dataset**: New → Dataset → `vora-db` → **Private**.

3. **Space**: New → Space → `vora-api` → SDK **Docker** → Blank → **CPU basic (free)** → **Public**. Address: `https://<user>-vora-api.hf.space`.

4. **Space settings → Variables and secrets**

   | Name | Kind | Value |
   | --- | --- | --- |
   | `GROQ_API_KEY`, `GEMINI_API_KEY`, `NVIDIA_API_KEY` | Secret | your model keys |
   | `HF_TOKEN` | Secret | the write token |
   | `SEARXNG_SECRET` | Secret | any long random hex |
   | `SUPABASE_URL` | Secret | your Supabase project URL |
   | `VORA_AUTH` | Variable | `supabase` |
   | `VORA_CORS_ORIGINS` | Variable | `https://awdax.pages.dev` (comma-separate more sites) |
   | `VORA_BACKUP_REPO` | Variable | `<user>/vora-db` |
   | `VORA_BACKUP_MINUTES` | Variable | optional, default `5` |

5. **Frontend**: GitHub repository variable `VITE_API_BASE_URL=https://<user>-vora-api.hf.space`.

6. **Keep-alive** (optional): GitHub repository variable `SPACE_URL=https://<user>-vora-api.hf.space`; the `keepalive.yml` workflow pings `/health` every 6 hours. Check Hugging Face's terms before relying on it.

## Deploy

```powershell
cd Vora
git remote add space https://huggingface.co/spaces/<user>/vora-api
git push space main          # username + the write token as the password
```

The Space builds the `Dockerfile` (first build takes several minutes) and starts `deploy/start.sh`.

## Test the image on your PC first

```powershell
docker compose --profile app up -d --build
curl http://127.0.0.1:7860/ready
```

It reads your `.env`; snapshots run only if `HF_TOKEN` and `VORA_BACKUP_REPO` are set there.

## Day to day

- **Logs**: Space page → Logs. Lines starting `backup:` show restores and uploads.
- **Restore by hand**: delete the Space's runtime (Settings → Factory reboot); on start it downloads `vora.db.gz`.
- **Rotate a key**: change the secret in Space settings; the Space restarts and restores the database.
- **Data at risk**: rows collected in the last `VORA_BACKUP_MINUTES` before a crash. Clean stops upload a final snapshot.