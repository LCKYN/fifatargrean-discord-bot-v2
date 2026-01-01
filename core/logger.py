"""
JSON File Logger
Logs to JSON files, split per cog, 1 file per day, keeps 30 days
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


LOGS_DIR = Path("logs")
MAX_DAYS = 30


def get_log_file(cog_name: str) -> Path:
    """Get the log file path for a cog (creates directory if needed)"""
    LOGS_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return LOGS_DIR / f"{cog_name}_{today}.json"


def log(cog_name: str, event: str, data: dict = None):
    """
    Log an event to JSON file
    
    Args:
        cog_name: Name of the cog (e.g., "spam_detector", "points")
        event: Event type (e.g., "link_detected", "user_banned")
        data: Additional data to log
    """
    log_file = get_log_file(cog_name)
    
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event,
        "data": data or {}
    }
    
    # Read existing logs or start fresh
    logs = []
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except (json.JSONDecodeError, IOError):
            logs = []
    
    logs.append(entry)
    
    # Write back
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


def cleanup_old_logs():
    """Remove log files older than MAX_DAYS"""
    if not LOGS_DIR.exists():
        return
    
    cutoff = datetime.now() - timedelta(days=MAX_DAYS)
    
    for log_file in LOGS_DIR.glob("*.json"):
        try:
            # Extract date from filename (cog_name_YYYY-MM-DD.json)
            date_str = log_file.stem.split("_")[-1]
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            
            if file_date < cutoff:
                log_file.unlink()
        except (ValueError, IndexError):
            # Skip files with unexpected naming
            pass
