# Marchfeld CRNS Dashboard

Compares neutron counts from two Cosmic Ray Neutron Sensors at Marchfeld:
**Hydroinnova** (open feed, no login) and **Finapp** (login required).

## How it works

- `scripts/fetch_data.py` pulls both sources and writes `docs/data.json`.
- `docs/index.html` is a static dashboard that reads `data.json` and charts it (Chart.js).
- `.github/workflows/refresh-data.yml` runs the fetch script **on demand** — click
  the "Run workflow" button under the repo's **Actions** tab whenever you want
  fresh data. It commits the updated `data.json`, and the live site picks it up
  automatically.
- Your Finapp password is never stored in the code — it lives only as a GitHub
  **secret**, injected as an environment variable when the workflow runs.

## One-time setup

1. **Create the repo.** Push this folder to a new GitHub repo (private or
   public — see note on Pages below).

2. **Enable GitHub Pages.**
   Settings → Pages → Source: "Deploy from a branch" → Branch: `main`,
   folder: `/docs`. Save. Your dashboard will be live at
   `https://<your-username>.github.io/<repo-name>/` within a minute or two.
   > Note: GitHub Pages for a *private* repo requires GitHub Pro/Team/Enterprise.
   > On the free plan, Pages requires the repo to be public. If you're on a
   > paid plan or an org, keep it private — otherwise the dashboard itself
   > will be visible to anyone with the link (your data isn't sensitive, but
   > worth knowing).

3. **Add Finapp credentials as secrets.**
   Settings → Secrets and variables → Actions → New repository secret.
   Add:
   - `FINAPP_USERNAME` — your Finapp login email
   - `FINAPP_PASSWORD` — your Finapp password

   (Finapp is a Laravel app at data.finapptech.com — the fetch script handles
   its CSRF-token + session login automatically. The data URL for installation
   10617 / IAEA SWMCNL is already baked in; override with a `FINAPP_DATA_URL`
   secret if you ever need a different installation.)

4. **Run it once.** Actions tab → "Refresh neutron data" → Run workflow.
   Check that `docs/data.json` updates with real numbers from both sources.

## Refreshing data going forward

Whenever you want current numbers: **Actions tab → "Refresh neutron data" →
Run workflow button.** Takes under a minute, then reload the dashboard page.

## Status of the Finapp integration

`fetch_finapp()` now implements a real Laravel session login (CSRF token +
email/password POST + authenticated GET), targeting installation 10617
(IAEA SWMCNL). Two things aren't confirmed yet because they require actually
logging in, which I can't do:

1. **Login field names.** Laravel's default is `email` / `password`, which is
   what the script uses — but if Finapp customized their form, login will
   fail with a clear error message telling you so.
2. **Data endpoint response shape.** The script assumes `/user/installation/info`
   returns JSON (a list of rows, or `{"data": [...]}`). If it returns
   something else (an HTML table, a different JSON structure), the script
   will raise an error and print the first 1500 characters of the real
   response — paste that back and the parsing gets fixed in one pass.

**First run:** add the two secrets, run the workflow once, and check the
Actions log. Either it works, or the error message tells us exactly what to
adjust.

## Local testing (optional)

```bash
pip install requests
python scripts/fetch_data.py
# then open docs/index.html in a browser, or run:
python -m http.server --directory docs 8000
```
