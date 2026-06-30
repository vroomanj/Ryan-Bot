import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

DB_FILE = 'ryanbot.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Conversation History Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversation_history (
            channel_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        )
    ''')
    # User Activity Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_activity (
            user_id TEXT,
            channel_name TEXT,
            content TEXT,
            timestamp REAL
        )
    ''')
    
    # User Profiles Table (Long-term memory)
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            last_updated REAL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_notes (
            user_id TEXT,
            note TEXT,
            added_at REAL
        )
    ''')
    
    # Try to alter table to add channel_name if it was created before this update
    try:
        c.execute('ALTER TABLE user_activity ADD COLUMN channel_name TEXT')
    except sqlite3.OperationalError:
        pass # Column already exists
    conn.commit()
    conn.close()

def add_conversation_message(channel_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO conversation_history (channel_id, role, content, timestamp) VALUES (?, ?, ?, ?)',
              (str(channel_id), role, content, time.time()))
    conn.commit()
    conn.close()

def get_conversation_history(channel_id, limit=20):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT role, content FROM conversation_history 
        WHERE channel_id = ? 
        ORDER BY timestamp DESC LIMIT ?
    ''', (str(channel_id), limit))
    rows = c.fetchall()
    conn.close()
    # Return in chronological order
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def prune_conversation_history(channel_id, max_age_seconds=14400):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - max_age_seconds
    c.execute('DELETE FROM conversation_history WHERE channel_id = ? AND timestamp < ?', (str(channel_id), cutoff))
    conn.commit()
    conn.close()

def log_user_activity(user_id, channel_name, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO user_activity (user_id, channel_name, content, timestamp) VALUES (?, ?, ?, ?)',
              (str(user_id), str(channel_name), content, time.time()))
    
    # Keep up to the last 100 messages for this user (for better profiling)
    c.execute('''
        DELETE FROM user_activity 
        WHERE user_id = ? AND rowid NOT IN (
            SELECT rowid FROM user_activity 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 100
        )
    ''', (str(user_id), str(user_id)))
    conn.commit()
    conn.close()

def get_active_users(minutes=30):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - (minutes * 60)
    c.execute('SELECT DISTINCT user_id FROM user_activity WHERE timestamp >= ?', (cutoff,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_user_recent_messages(user_id, limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Fetch up to 'limit' messages, ordered by timestamp descending, then reverse so they are chronological
    c.execute('''
        SELECT channel_name, content, timestamp 
        FROM user_activity 
        WHERE user_id = ? 
        ORDER BY timestamp DESC 
        LIMIT ?
    ''', (str(user_id), limit))
    rows = c.fetchall()
    conn.close()
    
    messages = []
    for r in reversed(rows):
        channel = r[0] if r[0] else "unknown"
        content = r[1]
        dt = datetime.fromtimestamp(r[2], tz=ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
        messages.append(f"[{dt}] (In #{channel}): {content}")
        
    return messages

def garbage_collect_user_activity(hours=24):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    c.execute('DELETE FROM user_activity WHERE timestamp < ?', (cutoff,))
    conn.commit()
    conn.close()

def get_user_profile(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT summary FROM user_profiles WHERE user_id = ?', (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_user_notes(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT note FROM user_notes WHERE user_id = ? ORDER BY added_at ASC', (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_full_user_profile(user_id):
    profile = get_user_profile(user_id)
    notes = get_user_notes(user_id)
    
    if not profile and not notes:
        return None
        
    full_text = profile if profile else "No AI profile exists yet."
    if notes:
        notes_str = "\n".join(f"• {note}" for note in notes)
        full_text += f"\n\n🚨 **MODERATOR NOTES:**\n{notes_str}"
        
    return full_text

def add_user_note(user_id, note):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO user_notes (user_id, note, added_at) VALUES (?, ?, ?)',
              (str(user_id), note, time.time()))
    conn.commit()
    conn.close()

def delete_user_note(user_id, index):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT added_at FROM user_notes WHERE user_id = ? ORDER BY added_at ASC', (str(user_id),))
    rows = c.fetchall()
    
    success = False
    if 0 <= index < len(rows):
        target_time = rows[index][0]
        c.execute('DELETE FROM user_notes WHERE user_id = ? AND added_at = ?', (str(user_id), target_time))
        conn.commit()
        success = True
        
    conn.close()
    return success

def update_user_profile(user_id, summary):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO user_profiles (user_id, summary, last_updated)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET summary = excluded.summary, last_updated = excluded.last_updated
    ''', (str(user_id), summary, time.time()))
    conn.commit()
    conn.close()
    
def get_users_needing_profile_update(limit=5):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - (24 * 3600) # 24 hours
    # Find users with recent activity (last 24 hours) who either have no profile, or an outdated profile
    c.execute('''
        SELECT DISTINCT a.user_id 
        FROM user_activity a
        LEFT JOIN user_profiles p ON a.user_id = p.user_id
        WHERE a.timestamp >= ? AND (p.last_updated IS NULL OR p.last_updated < ?)
        LIMIT ?
    ''', (cutoff, cutoff, limit))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]
