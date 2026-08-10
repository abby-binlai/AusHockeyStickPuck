# Austin Hockey Schedule

Public Streamlit page that combines selected sessions from:

- Crossover — Private Hockey Coaches Ice, Stick & Puck
- Chaparral — Hockey Stick and Puck
- The Pond — Barn Time, Pond Time

## Deploy to Railway

1. Create a new GitHub repository.
2. Upload every file in this folder, including `Dockerfile` and `.streamlit/config.toml`.
3. In Railway, create a new project and choose **Deploy from GitHub repo**.
4. Select the repository. Railway will detect the `Dockerfile` and build the app.
5. In the Railway service, open **Settings → Networking** and generate a public domain.
6. Open the generated URL and share it.

No API keys are required by this version.

## Run locally

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
streamlit run hockey_sessions.py
```

## Notes

This project reads public rink calendar pages using a headless Chromium browser. If a rink changes its calendar HTML, its scraper selector may need an update. Results are cached for 15 minutes to reduce repeated requests.
