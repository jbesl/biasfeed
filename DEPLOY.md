# Deploying BiasFeed to GitHub Pages (free, auto-refreshing)

Final result: your site lives at **https://jbesl.github.io/biasfeed/** and rebuilds
itself every 3 hours. Everything below is done in the GitHub website — no Git, no
command line, no installs. Takes about 10 minutes.

You'll upload these files to a repo:
- `groundclone.py`
- `sources.csv`
- `political_topics.txt`
- `refresh.yml`  (goes in a special folder — see Step 3)

---

## Step 1 — Create the repository

1. Go to https://github.com/new
2. **Repository name:** `biasfeed`
3. Set it to **Public**.
4. Tick **"Add a README file"** (so the repo isn't empty).
5. Click **Create repository**.

## Step 2 — Upload the main files

1. On the repo page, click **Add file ▾ → Upload files**.
2. Drag in `groundclone.py`, `sources.csv`, and `political_topics.txt`.
3. Click **Commit changes**.

## Step 3 — Add the auto-refresh workflow (the one fiddly part)

The workflow must sit at the exact path `.github/workflows/refresh.yml`. The web
editor builds the folders for you if you type the path:

1. Click **Add file ▾ → Create new file**.
2. In the filename box, type exactly:  `.github/workflows/refresh.yml`
   (typing the slashes auto-creates the folders)
3. Open the provided `refresh.yml`, copy everything, and paste it into the editor.
4. Click **Commit changes**.

## Step 4 — Let the workflow write back to the repo

1. Go to **Settings → Actions → General**.
2. Scroll to **Workflow permissions**.
3. Choose **Read and write permissions** → **Save**.
   (Without this, the refresh can fetch news but can't save the page.)

## Step 5 — Run it once by hand to generate the page

1. Go to the **Actions** tab.
2. If prompted, click the green button to enable workflows.
3. Click **Refresh BiasFeed** in the left list → **Run workflow ▾ → Run workflow**.
4. Wait ~1–2 minutes. A green check means it built and committed `index.html`.
   (Open the run and check the log if it goes red — usually a typo in Step 3 or a
   skipped Step 4.)

## Step 6 — Turn on GitHub Pages

1. Go to **Settings → Pages**.
2. Under **Source**, choose **Deploy from a branch**.
3. **Branch:** `main`, **Folder:** `/ (root)` → **Save**.
4. Wait ~1 minute. The page will show your live URL: **https://jbesl.github.io/biasfeed/**

Done. From now on the workflow rebuilds and republishes every 3 hours
automatically. You can also hit **Run workflow** anytime for an instant refresh.

---

## Editing it later

- **Change a bias rating or add an outlet:** edit `sources.csv` in the repo
  (click the file → pencil icon → edit → Commit). Next refresh picks it up.
- **Change what counts as political:** edit `political_topics.txt` the same way.
- **Refresh faster/slower:** edit the `cron` line in `refresh.yml`
  (`0 */3 * * *` = every 3 hours; `0 */6 * * *` = every 6).

## If the live site looks thin on articles

GitHub's servers sometimes get rate-limited by feeds (especially the Google News
ones feeding Reuters/AP) in ways your home computer isn't. If a hosted build pulls
noticeably fewer stories than running it locally does, that's the cause — tell me
and we'll add fallback handling for the server environment.

## Cost

Public repos get effectively unlimited Actions minutes and free Pages hosting with
no expiry. A build takes a minute or two; 8 builds a day is trivial. Nothing here
starts a billing clock — unlike the AWS/Azure 12-month free tiers.
