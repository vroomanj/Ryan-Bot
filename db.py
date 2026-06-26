import sqlite3
import time

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
    
    # Keep only the last 10 messages for this user
    c.execute('''
        DELETE FROM user_activity 
        WHERE user_id = ? AND rowid NOT IN (
            SELECT rowid FROM user_activity 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 10
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

def get_user_recent_messages(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT channel_name, content FROM user_activity WHERE user_id = ? ORDER BY timestamp ASC', (str(user_id),))
    rows = c.fetchall()
    conn.close()
    
    messages = []
    for r in rows:
        channel = r[0] if r[0] else "unknown"
        content = r[1]
        messages.append(f"(In #{channel}): {content}")
        
    return messages

def garbage_collect_user_activity(hours=24):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    c.execute('DELETE FROM user_activity WHERE timestamp < ?', (cutoff,))
    conn.commit()
    conn.close()
