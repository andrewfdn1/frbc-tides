# Hammersmith Tide Monitor — Flask Version

Lightweight Flask alternative to the Streamlit version. All API calls happen server-side; the browser receives plain HTML.

## File Structure

```
flask-version/
├── app.py                          # Flask app + all API logic
├── requirements.txt
├── render.yaml                     # Render.com deployment config
├── templates/
│   └── index.html                  # Jinja2 template
└── static/
    └── FRBC logo White on black.png   # Copy your logo here
```

## Local Development

```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

## Deploying to Render (free)

1. Push this folder to a `flask-version` branch on your GitHub repo
2. Go to https://render.com and sign in with GitHub
3. Click **New → Web Service**
4. Select your repo and the `flask-version` branch
5. Render will detect `render.yaml` and configure automatically
6. Click **Deploy** — your app will be live at `yourapp.onrender.com`

## Key Differences from Streamlit Version

| | Streamlit | Flask |
|---|---|---|
| Client JS bundle | ~1.5MB | ~0KB |
| WebSocket connection | Yes (persistent) | No |
| Auto-refresh method | Full re-render via `st_autorefresh` | `location.reload()` after 10 min |
| API calls | Server-side | Server-side |
| Framework overhead | High | Minimal |

## Notes

- Copy `FRBC logo White on black.png` into the `static/` folder
- The `/data` JSON endpoint is available for future use (e.g. partial page updates)
- The `render.yaml` uses the free plan — note Render free tier spins down after 15 min inactivity
