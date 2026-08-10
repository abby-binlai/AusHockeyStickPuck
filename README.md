# Austin Hockey Ice Finder — Railway fixed

This version explicitly forces Railway to use the Dockerfile rather than
Railpack.

## GitHub repository root

These files must appear at the top level of the repository:

- Dockerfile
- railway.json
- hockey_sessions.py
- requirements.txt

## Railway settings

1. Deploy the GitHub repository.
2. Service -> Settings:
   - Root Directory: leave blank or `/` when the files above are at repo root.
   - Start Command: leave blank.
3. Redeploy.

The `railway.json` file sets the builder to `DOCKERFILE`. The Dockerfile
already contains the Streamlit start command and uses Railway's `$PORT`.

If you put these files inside a subfolder in GitHub, set Railway's Root
Directory to that subfolder instead.

After deployment succeeds, use Settings -> Networking -> Generate Domain.
