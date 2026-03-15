#!/usr/bin/env python3
"""
Fund Analysis Network - Backend API
Tracks persons, on-chain addresses, and their relationships
"""

import sqlite3
import json
import uuid
import threading
import os
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g
from flask_cors import CORS
from crawler import find_path, analyze_addresses

app = Flask(__name__)
CORS(app)

# ─── In-memory job store ──────────────────────────────────────────────────────
_jobs = {}   # job_id -> {'status': 'running'|'done'|'error', 'progress': [], 'result': ...}
_jobs_lock = threading.Lock()

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

DATABASE = 'fund_network.db'


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT,
                organization TEXT,
                email TEXT,
                phone TEXT,
                notes TEXT,
                tags TEXT,
                risk_level TEXT DEFAULT 'unknown',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                label TEXT,
                balance TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE,
                UNIQUE(chain, address)
            );

            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'other',
                strength INTEGER DEFAULT 1,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (source_id) REFERENCES persons(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES persons(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                amount TEXT,
                chain TEXT,
                tx_hash TEXT UNIQUE,
                tx_time TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        db.commit()
        print("Database initialized successfully.")


# ─── Person CRUD ─────────────────────────────────────────────────────────────

@app.route('/api/persons', methods=['GET'])
def get_persons():
    db = get_db()
    persons = db.execute(
        "SELECT * FROM persons ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for p in persons:
        person = dict(p)
        addresses = db.execute(
            "SELECT * FROM addresses WHERE person_id = ?", (p['id'],)
        ).fetchall()
        person['addresses'] = [dict(a) for a in addresses]
        result.append(person)
    return jsonify(result)


@app.route('/api/persons/<int:pid>', methods=['GET'])
def get_person(pid):
    db = get_db()
    p = db.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone()
    if not p:
        return jsonify({'error': 'Person not found'}), 404
    person = dict(p)
    addresses = db.execute(
        "SELECT * FROM addresses WHERE person_id = ?", (pid,)
    ).fetchall()
    person['addresses'] = [dict(a) for a in addresses]
    return jsonify(person)


@app.route('/api/persons', methods=['POST'])
def create_person():
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO persons (name, role, organization, email, phone, notes, tags, risk_level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (data['name'], data.get('role'), data.get('organization'),
         data.get('email'), data.get('phone'), data.get('notes'),
         data.get('tags'), data.get('risk_level', 'unknown'))
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'message': 'Person created'}), 201


@app.route('/api/persons/<int:pid>', methods=['PUT'])
def update_person(pid):
    data = request.get_json()
    db = get_db()
    db.execute(
        """UPDATE persons SET name=?, role=?, organization=?, email=?, phone=?,
           notes=?, tags=?, risk_level=?, updated_at=datetime('now')
           WHERE id=?""",
        (data.get('name'), data.get('role'), data.get('organization'),
         data.get('email'), data.get('phone'), data.get('notes'),
         data.get('tags'), data.get('risk_level', 'unknown'), pid)
    )
    db.commit()
    return jsonify({'message': 'Person updated'})


@app.route('/api/persons/<int:pid>', methods=['DELETE'])
def delete_person(pid):
    db = get_db()
    db.execute("DELETE FROM persons WHERE id = ?", (pid,))
    db.commit()
    return jsonify({'message': 'Person deleted'})


# ─── Address CRUD ─────────────────────────────────────────────────────────────

@app.route('/api/addresses', methods=['POST'])
def create_address():
    data = request.get_json()
    if not data or not data.get('person_id') or not data.get('address'):
        return jsonify({'error': 'person_id and address are required'}), 400
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO addresses (person_id, chain, address, label, balance, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (data['person_id'], data.get('chain', 'ETH'), data['address'],
             data.get('label'), data.get('balance'), data.get('notes'))
        )
        db.commit()
        return jsonify({'id': cur.lastrowid, 'message': 'Address added'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Address already exists on this chain'}), 409


@app.route('/api/addresses/<int:aid>', methods=['DELETE'])
def delete_address(aid):
    db = get_db()
    db.execute("DELETE FROM addresses WHERE id = ?", (aid,))
    db.commit()
    return jsonify({'message': 'Address deleted'})


# ─── Relationship CRUD ────────────────────────────────────────────────────────

@app.route('/api/relationships', methods=['GET'])
def get_relationships():
    db = get_db()
    rows = db.execute("""
        SELECT r.*,
               p1.name as source_name,
               p2.name as target_name
        FROM relationships r
        JOIN persons p1 ON r.source_id = p1.id
        JOIN persons p2 ON r.target_id = p2.id
        ORDER BY r.created_at DESC
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/relationships', methods=['POST'])
def create_relationship():
    data = request.get_json()
    if not data or not data.get('source_id') or not data.get('target_id'):
        return jsonify({'error': 'source_id and target_id are required'}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO relationships (source_id, target_id, type, strength, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (data['source_id'], data['target_id'], data.get('type', 'other'),
         data.get('strength', 1), data.get('notes'))
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'message': 'Relationship created'}), 201


@app.route('/api/relationships/<int:rid>', methods=['DELETE'])
def delete_relationship(rid):
    db = get_db()
    db.execute("DELETE FROM relationships WHERE id = ?", (rid,))
    db.commit()
    return jsonify({'message': 'Relationship deleted'})


# ─── Graph Data ───────────────────────────────────────────────────────────────

@app.route('/api/graph', methods=['GET'])
def get_graph():
    """Return data formatted for Cytoscape.js"""
    db = get_db()
    persons = db.execute("SELECT * FROM persons").fetchall()
    relationships = db.execute("""
        SELECT r.*, p1.name as source_name, p2.name as target_name
        FROM relationships r
        JOIN persons p1 ON r.source_id = p1.id
        JOIN persons p2 ON r.target_id = p2.id
    """).fetchall()

    risk_colors = {
        'high': '#e74c3c',
        'medium': '#e67e22',
        'low': '#27ae60',
        'unknown': '#95a5a6'
    }

    nodes = []
    for p in persons:
        addr_count = db.execute(
            "SELECT COUNT(*) as cnt FROM addresses WHERE person_id = ?", (p['id'],)
        ).fetchone()['cnt']
        nodes.append({
            'data': {
                'id': str(p['id']),
                'label': p['name'],
                'role': p['role'] or '',
                'organization': p['organization'] or '',
                'risk_level': p['risk_level'] or 'unknown',
                'addr_count': addr_count,
                'color': risk_colors.get(p['risk_level'], '#95a5a6')
            }
        })

    edges = []
    for r in relationships:
        edges.append({
            'data': {
                'id': f"e{r['id']}",
                'source': str(r['source_id']),
                'target': str(r['target_id']),
                'type': r['type'],
                'strength': r['strength'],
                'label': r['type'],
                'source_name': r['source_name'],
                'target_name': r['target_name']
            }
        })

    return jsonify({'nodes': nodes, 'edges': edges})


# ─── Transactions ─────────────────────────────────────────────────────────────

@app.route('/api/transactions', methods=['POST'])
def create_transaction():
    data = request.get_json()
    if not data or not data.get('from_address') or not data.get('to_address'):
        return jsonify({'error': 'from_address and to_address are required'}), 400
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO transactions (from_address, to_address, amount, chain, tx_hash, tx_time, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (data['from_address'], data['to_address'], data.get('amount'),
             data.get('chain', 'ETH'), data.get('tx_hash'),
             data.get('tx_time'), data.get('notes'))
        )
        db.commit()
        return jsonify({'id': cur.lastrowid, 'message': 'Transaction recorded'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Transaction hash already exists'}), 409


@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM transactions ORDER BY tx_time DESC, created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ─── Search ───────────────────────────────────────────────────────────────────

@app.route('/api/search', methods=['GET'])
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    db = get_db()
    persons = db.execute(
        """SELECT id, name, role, organization, risk_level FROM persons
           WHERE name LIKE ? OR role LIKE ? OR organization LIKE ? OR notes LIKE ?""",
        (f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%')
    ).fetchall()
    # Also search by address
    addr_matches = db.execute(
        """SELECT DISTINCT p.id, p.name, p.role, p.organization, p.risk_level
           FROM persons p JOIN addresses a ON p.id = a.person_id
           WHERE a.address LIKE ?""",
        (f'%{q}%',)
    ).fetchall()
    seen = {r['id'] for r in persons}
    result = [dict(r) for r in persons]
    for r in addr_matches:
        if r['id'] not in seen:
            result.append(dict(r))
    return jsonify(result)


# ─── Frontend ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ─── Config (API keys) ────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def get_config():
    cfg = load_config()
    # Mask keys for display
    masked = {k: (v[:4] + '…' + v[-4:] if len(v) > 8 else '****')
              for k, v in cfg.items() if v}
    return jsonify(masked)


@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.get_json()
    cfg = load_config()
    cfg.update({k: v for k, v in data.items() if v is not None})
    save_config(cfg)
    return jsonify({'message': 'Config saved'})


# ─── Background Job Helpers ───────────────────────────────────────────────────

def _new_job():
    jid = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[jid] = {'status': 'running', 'progress': [], 'result': None, 'error': None}
    return jid


def _job_progress(jid, msg):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]['progress'].append(msg)


def _job_done(jid, result):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]['status'] = 'done'
            _jobs[jid]['result'] = result


def _job_error(jid, msg):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]['status'] = 'error'
            _jobs[jid]['error'] = msg


@app.route('/api/jobs/<jid>', methods=['GET'])
def job_status(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(job)


# ─── Find Path between two addresses ─────────────────────────────────────────

@app.route('/api/find-path', methods=['POST'])
def api_find_path():
    data = request.get_json()
    addr_a = (data.get('addr_a') or '').strip()
    addr_b = (data.get('addr_b') or '').strip()
    chain = (data.get('chain') or 'ETH').upper()
    max_depth = min(int(data.get('max_depth', 3)), 4)

    if not addr_a or not addr_b:
        return jsonify({'error': 'addr_a and addr_b are required'}), 400

    cfg = load_config()
    api_key = data.get('api_key') or cfg.get(f'{chain}_KEY') or cfg.get('ETH_KEY') or ''

    jid = _new_job()

    def run():
        try:
            path = find_path(
                addr_a, addr_b, chain, api_key, max_depth,
                progress_cb=lambda m: _job_progress(jid, m)
            )
            if path:
                # Auto-create person nodes for intermediate addresses
                with app.app_context():
                    db = sqlite3.connect(DATABASE)
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA foreign_keys = ON")
                    for hop in path:
                        addr = hop['address']
                        # Check if address already exists
                        existing = db.execute(
                            "SELECT person_id FROM addresses WHERE address=? COLLATE NOCASE", (addr,)
                        ).fetchone()
                        if not existing:
                            # Create anonymous person node
                            cur = db.execute(
                                "INSERT INTO persons (name, role, tags, risk_level) VALUES (?,?,?,?)",
                                (f"{addr[:8]}…{addr[-6:]}", '未知地址', 'auto-discovered', 'unknown')
                            )
                            pid = cur.lastrowid
                            try:
                                db.execute(
                                    "INSERT INTO addresses (person_id, chain, address, label) VALUES (?,?,?,?)",
                                    (pid, chain, addr, '自动发现')
                                )
                            except Exception:
                                pass
                    db.commit()
                    db.close()
            _job_done(jid, {'path': path, 'found': path is not None})
        except Exception as e:
            _job_error(jid, str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': jid})


# ─── Batch analyze multiple addresses ────────────────────────────────────────

@app.route('/api/analyze-addresses', methods=['POST'])
def api_analyze_addresses():
    data = request.get_json()
    addresses = data.get('addresses', [])
    chain = (data.get('chain') or 'ETH').upper()

    if not addresses or len(addresses) < 1:
        return jsonify({'error': 'At least 1 address required'}), 400
    if len(addresses) > 10:
        return jsonify({'error': 'Max 10 addresses at once'}), 400

    cfg = load_config()
    api_key = data.get('api_key') or cfg.get(f'{chain}_KEY') or cfg.get('ETH_KEY') or ''

    jid = _new_job()

    def run():
        try:
            result = analyze_addresses(
                addresses, chain, api_key,
                progress_cb=lambda m: _job_progress(jid, m)
            )
            # Auto-create person nodes for all input addresses
            with app.app_context():
                db = sqlite3.connect(DATABASE)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                for addr in addresses:
                    addr = addr.strip().lower()
                    existing = db.execute(
                        "SELECT person_id FROM addresses WHERE address=? COLLATE NOCASE", (addr,)
                    ).fetchone()
                    if not existing:
                        cur = db.execute(
                            "INSERT INTO persons (name, role, tags, risk_level) VALUES (?,?,?,?)",
                            (f"{addr[:8]}…{addr[-6:]}", '待分析地址', 'auto-discovered', 'unknown')
                        )
                        pid = cur.lastrowid
                        try:
                            db.execute(
                                "INSERT INTO addresses (person_id, chain, address, label) VALUES (?,?,?,?)",
                                (pid, chain, addr, '输入地址')
                            )
                        except Exception:
                            pass
                db.commit()
                db.close()
            _job_done(jid, result)
        except Exception as e:
            _job_error(jid, str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': jid})


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5001)
