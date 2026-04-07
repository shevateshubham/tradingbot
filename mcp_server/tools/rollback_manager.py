"""
tools/rollback_manager.py — Handles /rollback command.
Fetches Railway deployment history, triggers rollback,
creates git revert commit, and confirms via Telegram.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.config import get_settings
from mcp_server.models.database import DeploymentLog

logger = logging.getLogger(__name__)

RAILWAY_GRAPHQL = "https://backboard.railway.app/graphql/v2"
GITHUB_API = "https://api.github.com"


# ── Railway API helpers ───────────────────────────────────────────

async def fetch_railway_deployments(limit: int = 5) -> list[dict]:
    """
    Fetch the last N deployments from Railway API via GraphQL.
    Returns list of {id, createdAt, status, meta} dicts.
    """
    settings = get_settings()
    if not settings.railway_api_token or not settings.railway_service_id:
        logger.warning("Railway API credentials not configured — using deployment_log table")
        return []

    query = """
    query GetDeployments($serviceId: String!, $limit: Int!) {
      deployments(
        input: { serviceId: $serviceId }
        first: $limit
      ) {
        edges {
          node {
            id
            status
            createdAt
            meta {
              commitMessage
              commitHash
              branch
            }
          }
        }
      }
    }
    """

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                RAILWAY_GRAPHQL,
                headers={
                    "Authorization": f"Bearer {settings.railway_api_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "variables": {
                        "serviceId": settings.railway_service_id,
                        "limit": limit,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()

        edges = data.get("data", {}).get("deployments", {}).get("edges", [])
        deployments = []
        for edge in edges:
            node = edge.get("node", {})
            meta = node.get("meta", {}) or {}
            deployments.append({
                "id": node.get("id", ""),
                "status": node.get("status", "UNKNOWN"),
                "created_at": node.get("createdAt", ""),
                "commit_message": meta.get("commitMessage", "No commit message"),
                "commit_hash": meta.get("commitHash", "")[:8] if meta.get("commitHash") else "",
                "branch": meta.get("branch", "main"),
            })
        return deployments

    except Exception as e:
        logger.error(f"Failed to fetch Railway deployments: {e}")
        return []


async def trigger_railway_rollback(deployment_id: str) -> bool:
    """
    Trigger Railway to redeploy a specific previous deployment.
    Returns True on success.
    """
    settings = get_settings()
    if not settings.railway_api_token:
        logger.error("RAILWAY_API_TOKEN not set — cannot trigger rollback")
        return False

    mutation = """
    mutation RollbackDeployment($id: String!) {
      deploymentRedeploy(id: $id) {
        id
        status
      }
    }
    """

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                RAILWAY_GRAPHQL,
                headers={
                    "Authorization": f"Bearer {settings.railway_api_token}",
                    "Content-Type": "application/json",
                },
                json={"query": mutation, "variables": {"id": deployment_id}},
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("errors"):
            logger.error(f"Railway rollback GraphQL error: {data['errors']}")
            return False

        new_id = data.get("data", {}).get("deploymentRedeploy", {}).get("id")
        logger.info(f"Railway rollback triggered — new deployment: {new_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to trigger Railway rollback: {e}")
        return False


# ── GitHub API helpers ────────────────────────────────────────────

async def create_github_revert_commit(commit_hash: str, reason: str) -> Optional[str]:
    """
    Creates a revert commit on main branch via GitHub API.
    Returns the URL of the new commit, or None on failure.
    """
    settings = get_settings()
    if not settings.github_token or not settings.github_username or not settings.github_repo:
        logger.warning("GitHub credentials not configured — skipping git revert commit")
        return None

    repo = f"{settings.github_username}/{settings.github_repo}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get current main branch HEAD
            ref_resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/git/ref/heads/main",
                headers=headers,
            )
            ref_resp.raise_for_status()
            current_sha = ref_resp.json()["object"]["sha"]

            # Get the tree of the target commit
            commit_resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{commit_hash}",
                headers=headers,
            )
            commit_resp.raise_for_status()
            target_tree_sha = commit_resp.json()["commit"]["tree"]["sha"]

            # Create new commit pointing to old tree (revert)
            commit_message = (
                f"revert: rollback to {commit_hash[:8]}\n\n"
                f"Reason: {reason}\n"
                f"Triggered via Telegram /rollback command\n"
                f"Previous HEAD: {current_sha[:8]}"
            )
            new_commit_resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/git/commits",
                headers=headers,
                json={
                    "message": commit_message,
                    "tree": target_tree_sha,
                    "parents": [current_sha],
                },
            )
            new_commit_resp.raise_for_status()
            new_commit_sha = new_commit_resp.json()["sha"]

            # Update main branch to point to new commit
            update_resp = await client.patch(
                f"{GITHUB_API}/repos/{repo}/git/refs/heads/main",
                headers=headers,
                json={"sha": new_commit_sha, "force": False},
            )
            update_resp.raise_for_status()

            commit_url = f"https://github.com/{repo}/commit/{new_commit_sha}"
            logger.info(f"Revert commit created: {commit_url}")
            return commit_url

    except Exception as e:
        logger.error(f"Failed to create GitHub revert commit: {e}")
        return None


# ── Telegram command handlers ─────────────────────────────────────

def _format_deployment_age(created_at_str: str) -> str:
    """Convert ISO timestamp to human-readable age."""
    if not created_at_str:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        if hours >= 24:
            days = hours // 24
            return f"{days} day{'s' if days > 1 else ''} ago"
        elif hours >= 1:
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        else:
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    except Exception:
        return created_at_str[:16]


async def handle_rollback_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
) -> None:
    """
    /rollback — show last 5 deployments with inline keyboard.
    User taps a version to restore it.
    """
    await update.message.reply_text("⏳ Fetching deployment history from Railway...")

    deployments = await fetch_railway_deployments(limit=5)

    if not deployments:
        # Fall back to database deployment log
        from sqlalchemy import select
        result = await session.execute(
            select(DeploymentLog)
            .order_by(DeploymentLog.deployed_at.desc())
            .limit(5)
        )
        db_deployments = list(result.scalars().all())

        if not db_deployments:
            await update.message.reply_text(
                "❌ No deployment history found.\n\n"
                "Manual rollback: Go to Railway dashboard → Deployments → "
                "select a previous deployment → Rollback."
            )
            return

        deployments = [
            {
                "id": str(d.railway_deployment_id or d.id),
                "commit_message": d.change_summary or "Deployment",
                "commit_hash": (d.git_commit or "")[:8],
                "created_at": d.deployed_at.isoformat() if d.deployed_at else "",
                "branch": d.git_branch or "main",
            }
            for d in db_deployments
        ]

    # Build inline keyboard with deployment options
    buttons = []
    for i, dep in enumerate(deployments, 1):
        age = _format_deployment_age(dep.get("created_at", ""))
        msg = dep.get("commit_message", "deployment")[:40]
        commit = dep.get("commit_hash", "")
        label = f"[{i}] {age} — {msg}"
        if commit:
            label += f" ({commit})"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"rollback_{dep['id']}")
        ])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rollback_cancel")])
    keyboard = InlineKeyboardMarkup(buttons)

    text = (
        "↩️ *SELECT VERSION TO RESTORE*\n\n"
        "Choose a previous deployment to roll back to\\.\n"
        "Tap a version to restore it immediately\\.\n\n"
        "⚠️ The bot will restart\\. Takes 30–60 seconds\\."
    )

    await update.message.reply_text(
        text=text,
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )


async def handle_rollback_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    deployment_id: str,
) -> None:
    """Handle tap on a specific deployment version to restore."""
    query = update.callback_query
    await query.answer("Initiating rollback...")

    if deployment_id == "cancel":
        await query.edit_message_text("↩️ Rollback cancelled. Current version unchanged.")
        return

    await query.edit_message_text(
        f"⏳ *Rolling back to deployment* `{deployment_id[:12]}...`\n\n"
        "This takes 30–60 seconds\\. Bot will restart\\.",
        parse_mode="MarkdownV2",
    )

    # Trigger Railway rollback
    success = await trigger_railway_rollback(deployment_id)

    if not success:
        await query.edit_message_text(
            "❌ *Railway rollback failed*\n\n"
            "Please rollback manually:\n"
            "1\\. Go to railway\\.app\n"
            "2\\. Open your project → Deployments\n"
            "3\\. Find the working version\n"
            "4\\. Click ⋯ → Redeploy",
            parse_mode="MarkdownV2",
        )
        return

    # Create git revert commit
    commit_url = await create_github_revert_commit(
        commit_hash=deployment_id,
        reason=f"Rollback via Telegram /rollback command to deployment {deployment_id[:12]}"
    )

    # Log to database
    log_entry = DeploymentLog(
        railway_deployment_id=deployment_id,
        change_summary=f"Rollback to {deployment_id[:12]}",
        deployed_by="telegram_rollback",
        rolled_back=False,  # This IS a rollback entry itself
    )
    session.add(log_entry)
    await session.flush()

    # Build confirmation message
    commit_line = f"\nGitHub revert: {commit_url}" if commit_url else ""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"

    await query.edit_message_text(
        f"✅ *Rollback triggered successfully\\!*\n\n"
        f"Restoring deployment: `{deployment_id[:16]}`\n"
        f"Bot is restarting with previous code\\.\n"
        f"{commit_line}\n\n"
        "Check /status in 60 seconds to confirm bot is running\\.\n"
        "If bot goes silent, check Railway dashboard logs\\.",
        parse_mode="MarkdownV2",
    )

    logger.info(f"Rollback triggered: deployment={deployment_id}, commit_url={commit_url}")
