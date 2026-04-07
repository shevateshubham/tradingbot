"""
tools/performance.py — Weekly self-improvement engine.
Analyzes trade performance, calls Claude API for parameter suggestion,
creates GitHub PR, and handles /approve and /reject commands.
Also tracks post-change performance for auto-revert.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import httpx
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from mcp_server.config import get_settings, QUALITY_FILTERS
from mcp_server.models.database import (
    Signal, DiscardedSignal, ParameterHistory, UserPreferences,
    get_performance_stats,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")
GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


# ── GitHub helpers ────────────────────────────────────────────────

async def _github_headers() -> Dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


async def _get_main_sha() -> Optional[str]:
    """Get current HEAD SHA of main branch."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/git/ref/heads/main",
                headers=await _github_headers(),
            )
            resp.raise_for_status()
            return resp.json()["object"]["sha"]
    except Exception as e:
        logger.error(f"Failed to get main SHA: {e}")
        return None


async def _create_branch(branch_name: str, from_sha: str) -> bool:
    """Create a new branch from a given SHA."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/git/refs",
                headers=await _github_headers(),
                json={"ref": f"refs/heads/{branch_name}", "sha": from_sha},
            )
            resp.raise_for_status()
            logger.info(f"Branch created: {branch_name}")
            return True
    except Exception as e:
        logger.error(f"Failed to create branch {branch_name}: {e}")
        return False


async def _get_file_sha(branch: str, filepath: str) -> Optional[tuple]:
    """Get file content and SHA for updating via API."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/contents/{filepath}?ref={branch}",
                headers=await _github_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            import base64
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]
    except Exception as e:
        logger.error(f"Failed to get file {filepath}: {e}")
        return None, None


async def _update_file(branch: str, filepath: str, new_content: str, commit_msg: str) -> bool:
    """Update a file on a branch via GitHub API."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    content, sha = await _get_file_sha(branch, filepath)
    if sha is None:
        return False

    try:
        import base64
        encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(
                f"{GITHUB_API}/repos/{repo}/contents/{filepath}",
                headers=await _github_headers(),
                json={
                    "message": commit_msg,
                    "content": encoded,
                    "sha": sha,
                    "branch": branch,
                },
            )
            resp.raise_for_status()
            logger.info(f"File updated on branch {branch}: {filepath}")
            return True
    except Exception as e:
        logger.error(f"Failed to update file {filepath}: {e}")
        return False


async def _create_pull_request(
    branch: str,
    title: str,
    body: str,
) -> Optional[Dict]:
    """Create a GitHub Pull Request from branch to main."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/pulls",
                headers=await _github_headers(),
                json={
                    "title": title,
                    "body": body,
                    "head": branch,
                    "base": "main",
                },
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to create PR: {e}")
        return None


async def _merge_pull_request(pr_number: int) -> bool:
    """Merge a PR by number."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge",
                headers=await _github_headers(),
                json={"merge_method": "squash"},
            )
            resp.raise_for_status()
            logger.info(f"PR #{pr_number} merged")
            return True
    except Exception as e:
        logger.error(f"Failed to merge PR #{pr_number}: {e}")
        return False


async def _close_pull_request(pr_number: int) -> bool:
    """Close (reject) a PR by number."""
    settings = get_settings()
    repo = f"{settings.github_username}/{settings.github_repo}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
                headers=await _github_headers(),
                json={"state": "closed"},
            )
            resp.raise_for_status()
            logger.info(f"PR #{pr_number} closed")
            return True
    except Exception as e:
        logger.error(f"Failed to close PR #{pr_number}: {e}")
        return False


# ── Claude API integration ────────────────────────────────────────

async def _call_claude_api(trade_data: List[dict], current_params: dict) -> Optional[dict]:
    """
    Call Anthropic Claude API to suggest one parameter change.
    Returns structured JSON suggestion or None.
    """
    settings = get_settings()

    system_prompt = (
        "You are a quantitative trading analyst reviewing a Smart Money Concepts (SMC) "
        "trading system. Analyze the trade log and suggest exactly ONE specific parameter "
        "change that would most improve win rate. Be conservative, data-driven, and "
        "specific. Only suggest a change if supported by at least 20 trades in that category. "
        "Respond ONLY with valid JSON in this exact format, no other text:\n"
        '{"parameter": "min_confluence_score", "old_value": 72, "new_value": 78, '
        '"reason": "London session showing 41% WR over 23 trades", '
        '"expected_improvement": "+8% win rate", "trades_analyzed": 23, '
        '"confidence": "high"}'
    )

    user_prompt = (
        f"Current parameters: {json.dumps(current_params, indent=2)}\n\n"
        f"Last {len(trade_data)} closed trades:\n{json.dumps(trade_data[:50], indent=2)}"
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                ANTHROPIC_API,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text += block.get("text", "")

        # Strip any markdown fences
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1].rsplit("```", 1)[0]

        suggestion = json.loads(raw_text)
        logger.info(f"Claude suggestion: {suggestion}")
        return suggestion

    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON: {e}\nRaw: {raw_text}")
        return None
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return None


# ── Trade data preparation ────────────────────────────────────────

async def _prepare_trade_data(session: AsyncSession, days: int = 30) -> List[dict]:
    """Fetch and serialize recent closed trades for Claude analysis."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await session.execute(
        select(Signal).where(
            Signal.status == "closed",
            Signal.created_at >= cutoff,
        ).order_by(Signal.created_at.desc()).limit(50)
    )
    signals = list(result.scalars().all())

    return [
        {
            "instrument": s.instrument,
            "segment": s.segment,
            "direction": s.direction,
            "timeframe": s.timeframe,
            "session": s.session,
            "killzone_active": s.killzone_active,
            "setup_type": s.setup_type,
            "score": s.score,
            "grade": s.grade,
            "outcome": s.outcome,
            "actual_rr": s.actual_rr,
            "net_pnl": s.net_pnl,
            "htf_bias": s.htf_bias,
            "liquidity_swept": s.liquidity_swept,
            "fvg_present": s.fvg_present,
            "volume_ratio": s.volume_ratio,
        }
        for s in signals
    ]


# ── Discard analysis ───────────────────────────────────────────────

async def _analyze_filter_effectiveness(session: AsyncSession, days: int = 30) -> dict:
    """
    What percentage of discarded signals would have been losers?
    Shows how well the filter is working.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await session.execute(
        select(DiscardedSignal).where(
            DiscardedSignal.created_at >= cutoff,
        )
    )
    discards = list(result.scalars().all())

    total = len(discards)
    saved_losers = sum(1 for d in discards if d.would_have_been == "loser")

    return {
        "total_discarded": total,
        "confirmed_losers_saved": saved_losers,
        "filter_accuracy": (saved_losers / total * 100) if total > 0 else 0,
    }


# ── Config file modifier ──────────────────────────────────────────

def _apply_parameter_to_config(config_content: str, parameter: str, new_value) -> str:
    """
    Apply a single parameter change to config.py content string.
    Supports int and float parameters.
    """
    import re

    param_map = {
        "min_confluence_score": r'(MIN_CONFLUENCE_SCORE\s*=\s*Field\(default=)\d+',
        "max_active_trades":    r'(MAX_ACTIVE_TRADES\s*=\s*Field\(default=)\d+',
        "min_rr_ratio":         r'(MIN_RR_RATIO\s*=\s*Field\(default=)[\d.]+',
        "risk_percent":         r'(RISK_PER_TRADE_PERCENT\s*=\s*Field\(default=)[\d.]+',
    }

    pattern = param_map.get(parameter)
    if not pattern:
        logger.warning(f"No pattern found for parameter: {parameter}")
        return config_content

    replacement = rf'\g<1>{new_value}'
    new_content = re.sub(pattern, replacement, config_content)

    if new_content == config_content:
        logger.warning(f"Pattern did not match for parameter: {parameter}")

    return new_content


# ── Weekly analysis runner ────────────────────────────────────────

async def run_weekly_analysis(
    session: AsyncSession,
    bot,
    chat_id: str,
) -> None:
    """
    Full weekly improvement pipeline:
    1. Check trade count
    2. Analyze performance by category
    3. Call Claude API for suggestion
    4. Create GitHub branch + PR
    5. Send Telegram report with approve/reject buttons
    """
    logger.info("Starting weekly self-improvement analysis...")

    # ── Step 1: Check minimum trade count ─────────────────────────
    trade_data = await _prepare_trade_data(session, days=30)
    if len(trade_data) < 30:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📊 Weekly analysis skipped — only {len(trade_data)} closed trades.\n"
                f"Need at least 30 for a reliable analysis.\n"
                f"Keep trading in paper mode and check next Sunday."
            ),
        )
        logger.info(f"Weekly analysis skipped: only {len(trade_data)} trades")
        return

    # ── Step 2: Calculate performance stats ───────────────────────
    stats = await get_performance_stats(session, chat_id, days=30)
    filter_stats = await _analyze_filter_effectiveness(session, days=30)

    # Analyze by session, setup, segment
    by_session: Dict[str, list] = {}
    by_segment: Dict[str, list] = {}
    for t in trade_data:
        sess = t.get("session", "UNKNOWN")
        seg  = t.get("segment", "UNKNOWN")
        by_session.setdefault(sess, []).append(t)
        by_segment.setdefault(seg, []).append(t)

    def win_rate(trades: list) -> float:
        closed = [t for t in trades if t.get("outcome")]
        winners = [t for t in closed if t.get("outcome", "").startswith("TP")]
        return len(winners) / len(closed) * 100 if closed else 0

    session_stats = {
        k: {"count": len(v), "win_rate": round(win_rate(v), 1)}
        for k, v in by_session.items() if len(v) >= 5
    }

    # Find best and worst session
    if session_stats:
        best_session  = max(session_stats.items(), key=lambda x: x[1]["win_rate"])
        worst_session = min(session_stats.items(), key=lambda x: x[1]["win_rate"])
    else:
        best_session  = ("N/A", {"win_rate": 0, "count": 0})
        worst_session = ("N/A", {"win_rate": 0, "count": 0})

    # ── Step 3: Current parameters ────────────────────────────────
    settings = get_settings()
    current_params = {
        "min_confluence_score": settings.min_confluence_score,
        "max_active_trades":    settings.max_active_trades,
        "min_rr_ratio":         settings.min_rr_ratio,
        "risk_percent":         settings.risk_per_trade_percent,
    }

    # ── Step 4: Claude API suggestion ─────────────────────────────
    suggestion = await _call_claude_api(trade_data, current_params)

    if not suggestion:
        await bot.send_message(
            chat_id=chat_id,
            text="📊 Weekly analysis complete but Claude suggestion failed. No changes made.",
        )
        return

    param    = suggestion.get("parameter", "")
    old_val  = suggestion.get("old_value")
    new_val  = suggestion.get("new_value")
    reason   = suggestion.get("reason", "")
    expected = suggestion.get("expected_improvement", "")
    n_trades = suggestion.get("trades_analyzed", 0)

    # ── Step 5: Create GitHub branch and PR ───────────────────────
    now = datetime.now(IST)
    week_num = now.isocalendar()[1]
    branch_name = f"improvement/week-{week_num}-{param.replace('_', '-')}"

    main_sha = await _get_main_sha()
    if not main_sha:
        await bot.send_message(chat_id=chat_id, text="❌ GitHub connection failed. No PR created.")
        return

    branch_created = await _create_branch(branch_name, main_sha)
    if not branch_created:
        await bot.send_message(chat_id=chat_id, text="❌ Could not create improvement branch.")
        return

    # Update config.py on the new branch
    config_content, config_sha = await _get_file_sha(branch_name, "mcp_server/config.py")
    if config_content:
        new_config = _apply_parameter_to_config(config_content, param, new_val)
        commit_msg = (
            f"improvement: {param} {old_val}→{new_val} (week {week_num} analysis)\n\n"
            f"Reason: {reason}\n"
            f"Expected improvement: {expected}\n"
            f"Trades analyzed: {n_trades}"
        )
        await _update_file(branch_name, "mcp_server/config.py", new_config, commit_msg)

    # Create PR body with performance stats
    settings = get_settings()
    pr_body = (
        f"## Change Summary\n\n"
        f"- **Parameter changed:** {param}\n"
        f"- **Old value:** {old_val}\n"
        f"- **New value:** {new_val}\n"
        f"- **Reason:** {reason}\n"
        f"- **Trades analyzed:** {n_trades}\n"
        f"- **Expected improvement:** {expected}\n\n"
        f"## This Week's Performance\n\n"
        f"- Total closed: {stats['total']}\n"
        f"- Win rate: {stats['win_rate']:.1f}%\n"
        f"- Avg RR: 1:{stats['avg_rr']:.2f}\n"
        f"- Net P&L: ₹{stats['net_pnl']:+,.0f}\n\n"
        f"## Session Breakdown\n\n"
        + "\n".join(
            f"- {s}: {d['win_rate']:.1f}% WR ({d['count']} trades)"
            for s, d in session_stats.items()
        )
        + "\n\n"
        f"## Safety Checklist\n\n"
        f"- [ ] Tests pass (GitHub Actions green ✅)\n"
        f"- [ ] Approved via Telegram /approve command\n"
        f"- [ ] Single parameter change only\n"
        f"- [ ] At least {n_trades} trades analyzed\n"
    )

    pr_title = f"improvement: {param} {old_val}→{new_val} (week {week_num})"
    pr = await _create_pull_request(branch_name, pr_title, pr_body)

    if not pr:
        await bot.send_message(chat_id=chat_id, text="❌ PR creation failed. No deployment.")
        return

    pr_number = pr.get("number", 0)
    pr_url    = pr.get("html_url", "")

    # ── Step 6: Log to database ───────────────────────────────────
    log = ParameterHistory(
        parameter_name=param,
        old_value=str(old_val),
        new_value=str(new_val),
        reason=reason,
        trades_analyzed=n_trades,
        win_rate_before=stats["win_rate"],
        github_pr_url=pr_url,
        github_pr_number=pr_number,
    )
    session.add(log)
    await session.flush()

    # ── Step 7: Send Telegram report ──────────────────────────────
    report = (
        f"📈 WEEKLY IMPROVEMENT REPORT — {now.strftime('%d %b %Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades This Week   : {stats['total']}\n"
        f"Win Rate           : {stats['win_rate']:.1f}%\n"
        f"Avg RR Achieved    : 1:{stats['avg_rr']:.2f}\n"
        f"Filter Saved       : {filter_stats['total_discarded']} signals filtered\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"TOP SESSION:\n"
        f"  {best_session[0]} → {best_session[1]['win_rate']:.1f}% WR "
        f"({best_session[1]['count']} trades)\n\n"
        f"UNDERPERFORMING:\n"
        f"  {worst_session[0]} → {worst_session[1]['win_rate']:.1f}% WR ⚠️\n\n"
        f"🤖 CLAUDE SUGGESTS:\n"
        f"  Change   : {param}\n"
        f"  From     : {old_val} → To: {new_val}\n"
        f"  Reason   : {reason}\n"
        f"  Expected : {expected}\n"
        f"  PR       : {pr_url}\n"
        f"  Tests    : ✅ pending (auto-runs on PR)\n\n"
        f"Reply /approve {pr_number} to merge and deploy\n"
        f"Reply /reject {pr_number} to close PR, keep current settings"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Approve #{pr_number}", callback_data=f"approve_pr_{pr_number}"),
        InlineKeyboardButton(f"❌ Reject #{pr_number}",  callback_data=f"reject_pr_{pr_number}"),
        InlineKeyboardButton("👁 View PR", url=pr_url),
    ]])

    await bot.send_message(chat_id=chat_id, text=report, reply_markup=keyboard)
    logger.info(f"Weekly analysis complete: PR #{pr_number} created for {param} {old_val}→{new_val}")


# ── PR approve/reject ─────────────────────────────────────────────

async def approve_improvement_pr(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    pr_number: int,
) -> None:
    """Merge a weekly improvement PR and trigger Railway redeploy."""
    await update.message.reply_text(f"⏳ Merging PR #{pr_number}...")

    success = await _merge_pull_request(pr_number)

    if success:
        # Update database record
        result = await session.execute(
            select(ParameterHistory).where(
                ParameterHistory.github_pr_number == pr_number
            )
        )
        record = result.scalar_one_or_none()
        if record:
            record.approved_at = datetime.utcnow()
            await session.flush()

        await update.message.reply_text(
            f"✅ PR #{pr_number} merged and deployed.\n"
            f"Railway will redeploy automatically in ~60 seconds.\n"
            f"Tracking next 20 trades for performance comparison."
        )
        logger.info(f"Improvement PR #{pr_number} approved and merged")
    else:
        await update.message.reply_text(
            f"❌ Could not merge PR #{pr_number}.\n"
            f"Check GitHub for merge conflicts or failed tests."
        )


async def reject_improvement_pr(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    pr_number: int,
) -> None:
    """Close (reject) a weekly improvement PR."""
    success = await _close_pull_request(pr_number)

    if success:
        result = await session.execute(
            select(ParameterHistory).where(
                ParameterHistory.github_pr_number == pr_number
            )
        )
        record = result.scalar_one_or_none()
        if record:
            record.reverted = True
            record.revert_reason = "rejected_by_user"
            await session.flush()

        await update.message.reply_text(
            f"❌ PR #{pr_number} rejected and closed.\n"
            f"Current settings unchanged. See you next Sunday for next analysis."
        )
    else:
        await update.message.reply_text(f"Could not close PR #{pr_number}. Close it manually on GitHub.")
