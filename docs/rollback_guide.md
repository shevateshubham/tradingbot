# Rollback Guide — Instant Version Restore

## When to Rollback

Rollback when:
- The bot stops working after a code change
- A weekly improvement parameter change hurts performance
- Signals stop arriving after an update
- Something unexpected changed in bot behavior

## How to Rollback via Telegram

This is the fastest and safest method.

### Step 1 — Send the Command

Open Telegram, send your bot:
```
/rollback
```

### Step 2 — Select Version

The bot replies with the last 5 deployments:
```
↩️ SELECT VERSION TO RESTORE

Choose a previous deployment to roll back to:
Tap a version to restore it immediately.

[1] 2 hours ago — "improvement: increase min_score 72→78"
[2] Yesterday 8:14 PM — "weekly review parameter update"
[3] 3 days ago — "fix: segment detector MCX symbols"
[4] 1 week ago — "initial stable version"
[5] 2 weeks ago — "first deployment"

Current version is [1].
Rolling back will restart the bot with the selected code.
```

### Step 3 — Tap to Confirm

Tap any button (e.g., [2] Yesterday 8:14 PM).

The bot confirms:
```
⏳ Rolling back to: Yesterday 8:14 PM
This takes 30–60 seconds...
```

Then:
```
✅ Rollback complete

Restored to: Yesterday 8:14 PM
Commit: abc1234
Bot is now running the previous working code.
Railway deployment: dep_xxxxx

A revert commit has been created on main branch.
GitHub: github.com/yourrepo/commit/def5678
```

## How Rollback Works Internally

1. Bot calls Railway API to fetch deployment history
2. Triggers Railway to redeploy the selected previous deployment
3. Creates a `git revert` commit on the `main` branch (for full git history)
4. Logs the rollback event to the database
5. Sends confirmation with both Railway deployment ID and git commit

## Manual Rollback via Railway Dashboard

If the bot itself is broken and can't respond:

1. Go to [railway.app](https://railway.app)
2. Open your project → your service
3. Click **Deployments** tab
4. Find the last working deployment (green checkmark)
5. Click the three dots (⋯) → **Rollback**
6. Confirm — Railway redeploys that exact version
7. Wait 1–2 minutes for bot to restart

## Manual Rollback via GitHub

If you want to revert the code permanently:

```bash
# Find the commit to revert to
git log --oneline

# Revert to a specific commit (creates a new revert commit)
git revert HEAD~1  # revert last commit
# or
git revert abc1234  # revert a specific commit

git push origin main
# Railway auto-deploys the revert
```

## Rollback After Weekly Improvement

If you approved an improvement and it hurt performance:

1. Send `/rollback` immediately
2. Select the deployment **before** the improvement was merged
3. Bot reverts Railway and creates a git revert commit

The system also auto-monitors after every approved improvement:
- Tracks next 20 trades in the affected category
- If win rate drops below pre-change level:
  - Bot sends: `⚠️ Improvement may be hurting performance. Current WR: 38% vs previous 52%. Tap to auto-revert.`
  - Buttons: `[↩️ Auto-Revert] [📊 View More Data] [✅ Keep Change]`

## Database is Not Rolled Back

**Important**: Rolling back code does NOT rollback the database.

- All signals remain in the database
- User preferences remain
- Performance history is preserved

This is intentional — you never want to lose trade data.

If you need to undo a database change specifically, contact a developer.

## Preventing Rollback Needs

Best practices to avoid needing rollbacks:
- Always run paper mode for 1–2 weeks before going live
- Review weekly improvement PRs carefully before approving
- Check GitHub diff before merging any PR
- Never force-push to main branch
- Keep the `changes` branch separate from `main`
