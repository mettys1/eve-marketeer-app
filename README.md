# eve-jita-poller — GCP deployment

A scheduled Cloud Run Job that scans ESI directly (no Fuzzwork/EVE Tycoon) and writes to
BigQuery: buy/sell orders, aggregated margins, and daily turnover history. Replaces the manual
`node esi-scan.js` workflow with something that runs itself.



## What's in here

```
gcp-poller/
  deploy.sh              ← run this once, section by section, to bootstrap everything
  cloudbuild.yaml         ← CI/CD pipeline: build → push → update the Cloud Run Job on each push
  .gitignore
  bigquery/schema.sql     ← dataset + 3 tables (snapshots, raw orders, history)
  poller/
    poller.js             ← the actual scan logic (ESI → BigQuery)
    Dockerfile
    package.json
```

## How to run it

1. Open `deploy.sh` and skim the config block at the top (`PROJECT_ID`, `REGION`, cron schedule).
   `PROJECT_ID` has a random suffix by default since project IDs must be globally unique — change
   it to something you'll recognize if you want.
2. **Step 0 is manual, on purpose**: create a GCP project and set up billing in the console
   (billing needs your card details entered by you — I won't touch that). Links are in the
   script's comments.
3. Run `bash deploy.sh` — it's split into numbered `echo "== Step N: ... =="` sections so you can
   see where it is. If something fails partway (a resource name collision, an API not yet
   propagated, whatever), just fix that one step and re-run from there — most of the `gcloud`
   commands are safe to re-run (project/dataset/repo creation will just error "already exists,"
   which is fine, everything after that step is idempotent-ish).
4. Step 10 test-runs the job once and waits for it — watch the log output, that's your first real
   signal whether the ESI scan logic actually works against live GCP infra.
5. Check data landed:
   ```
   bq query --use_legacy_sql=false \
     'SELECT * FROM `PROJECT_ID.eve_jita_scanner.market_snapshots` ORDER BY scanned_at DESC LIMIT 10'
   ```

## What it costs

At this data volume (52-84 items, one scan/day, raw order rows in the low thousands per scan),
this should run close to $0/month:
- Cloud Run Jobs: pay per execution second, a few minutes/day is pennies.
- BigQuery: free tier is 10 GB storage + 1 TB queries/month — you'd need a very long time to
  outgrow that at this scale. The `market_orders_raw` table auto-expires partitions after 90 days
  (edit `bigquery/schema.sql`'s `partition_expiration_days` before running Step 5 if you want a
  different window, or none).
- Cloud Scheduler: free tier covers 3 jobs, you're using 1.
- Artifact Registry: a few MB for the container image, free tier covers it.

The one thing that costs real money if you're not careful: `WRITE_RAW_ORDERS=true` with a very
frequent schedule (e.g. every few minutes instead of daily) would accumulate storage fast. Daily
or even hourly is fine; don't set it to run every minute without thinking about that table's
growth.

## Querying it for the dashboard

Once data's flowing, the existing `jita-skener.html` Artifact can stay exactly as it is (Claude
regenerates its embedded WATCHLIST from a BigQuery query when asked to refresh) — or it can
become a live page with its own small API reading BigQuery directly. That's a separate build;
mention it if you want that next. For now, a query to get the latest snapshot per item:

```sql
SELECT * EXCEPT(rn) FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY scanned_at DESC) AS rn
  FROM `PROJECT_ID.eve_jita_scanner.market_snapshots`
)
WHERE rn = 1
ORDER BY station_margin_pct DESC;
```

## Connect to GitHub + Cloud Build CI/CD

Once `deploy.sh` has run successfully at least once (job, dataset, service accounts all exist),
this wires up "push to GitHub → automatically rebuilds and redeploys the poller image" so you
stop running Step 7/9 by hand for future code changes.

**I can't do any of this part myself** — no shell on your machine (so I can't run `git`), and
connecting Cloud Build to GitHub is an interactive OAuth consent flow in the Console with no
scriptable equivalent. Everything here is commands to run yourself.

1. **Create the GitHub repo** (empty, no README/license — this folder already has one) at
   github.com, or with `gh repo create eve-jita-poller --private --source=. --remote=origin` if
   you have the `gh` CLI.

2. **Push this folder to it**, from inside `gcp-poller/`:
   ```
   git init
   git add .
   git commit -m "Initial commit: ESI poller + BigQuery + deploy script"
   git branch -M main
   git remote add origin https://github.com/<you>/eve-jita-poller.git
   git push -u origin main
   ```

3. **Grant Cloud Build's service account permission to update the Run Job** (one-time, per
   project — the default Cloud Build SA doesn't have this by default):
   ```
   PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
     --role="roles/run.developer"
   ```

4. **Connect the repo in the Console** (this is the part that can't be scripted): Cloud Build →
   Triggers → Connect Repository → pick GitHub → authorize the Cloud Build GitHub App → select
   `eve-jita-poller`. Then Create Trigger: event = push to a branch (`^main$`), configuration =
   "Cloud Build configuration file", location = `/cloudbuild.yaml`.

5. From then on: edit `poller/poller.js` (or anything else), `git push`, and Cloud Build rebuilds
   the image and updates the Cloud Run Job automatically. Check progress under Cloud Build →
   History, or `gcloud builds list --limit=5`.

`cloudbuild.yaml`'s substitution variables (`_REGION`, `_AR_REPO`, `_JOB_NAME`) default to what
`deploy.sh` creates — only change them if you edited those in `deploy.sh` too.

## Tearing it down

If you want to stop and delete everything (e.g. it's not working out, or you're done
experimenting):

```
gcloud scheduler jobs delete eve-jita-poller-trigger --location=$REGION
gcloud run jobs delete eve-jita-poller --region=$REGION
bq rm -r -d $PROJECT_ID:eve_jita_scanner    # deletes ALL the data, careful
gcloud projects delete $PROJECT_ID           # nuclear option — deletes everything in the project
```
