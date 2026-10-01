# Deploy Nightline to Railway

1. Create a Railway project from this folder.
2. Open **Service → Variables** and add these service variables:
   - `DATABASE_URL`: Neon PostgreSQL connection string
   - `SECRET_KEY`: long random secret
   - `ADMIN_TOKEN`: owner token for `/settings`
   - `RUN_CLEANUP_WORKER=1`
   - Optional `GROQ_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`

Recommended API-key setup:

```text
GROQ_API_KEY=your Groq key
MISTRAL_API_KEY=your Mistral key
OPENROUTER_API_KEY=your OpenRouter key
```

Add these in Railway Variables, not in GitHub and not in `app.py`. Alternatively, after deployment visit `/settings` and enter the `ADMIN_TOKEN`; the provider-key form encrypts keys before saving them to PostgreSQL.
3. Railway uses `railway.json` and starts:

```text
gunicorn --bind 0.0.0.0:$PORT app:app
```

4. In Railway, generate a public domain for the service.

The resulting routes are:

```text
https://your-project.up.railway.app/
https://your-project.up.railway.app/cm
https://your-project.up.railway.app/settings
```

The `/cm` route is already implemented in `app.py`; no separate frontend domain or rewrite is required.
