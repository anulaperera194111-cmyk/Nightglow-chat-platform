# Nightline

A dark, WhatsApp-inspired private chat platform built with **Python Flask**, server-rendered HTML/CSS/JS, and **Neon PostgreSQL**.

## Run it

```bash
cd chat-platform
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Put your Neon DATABASE_URL, SECRET_KEY and ADMIN_TOKEN in .env
export $(grep -v '^#' .env | xargs)
python app.py
```

Open `http://localhost:5000`. The room manager is a direct web route: `http://localhost:5000/cm` locally, or `https://example.com/cm` after deployment. For production, use a process manager and HTTPS.

## Where to add API keys

### Local development

Copy `.env.example` to `.env`, then fill in these values:

```env
DATABASE_URL=your_neon_connection_string
GROQ_API_KEY=your_groq_key
MISTRAL_API_KEY=your_mistral_key
OPENROUTER_API_KEY=your_openrouter_key
SECRET_KEY=your_long_random_secret
ADMIN_TOKEN=your_owner_token
```

Never commit `.env` to GitHub.

### Railway deployment

In Railway open **your service → Variables → New Variable** and add the same variable names. Railway injects them into the Flask process; do not paste them into source files or GitHub.

The AI keys can also be added after deployment from `https://your-project.up.railway.app/settings` by entering the `ADMIN_TOKEN`. The app encrypts keys before storing them in PostgreSQL.

## Product behavior

- Username-only sign-in with exact date-of-birth gate; minimum age is 13.
- `/cm` is the room manager: create password-protected rooms, join by room ID + password, and delete rooms as their owner.
- Rooms expire automatically after 48 hours. A background cleanup worker purges messages, members, chat rows, and stored attachments when expired.
- Files accepted: PNG, JPG/JPEG, PDF, ZIP, common video formats, audio, and GIF up to `MAX_UPLOAD_MB`.
- AI commands: start a message with `@groq`, `@mistral`, or `@openr`. The answer is saved into the chat as a labeled message.
- `/settings` lets the owner enter provider keys after supplying `ADMIN_TOKEN`. Keys are encrypted with the app secret before database storage; environment variables are supported too.

## Security notes

Username-only identity is intentionally lightweight and is not proof of identity. Room deletion is bound to the creator's active session to reduce username impersonation risk. For a public deployment, add rate limits, CSRF protection, malware scanning for uploads, object storage, and real authentication before sharing widely. Never commit `.env` or API keys.

The local SQLite fallback is only for smoke tests when `DATABASE_URL` is absent; set the Neon URL to use the requested PostgreSQL database.
