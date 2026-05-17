import os
import json
import sqlite3
import re
import time
from flask import Flask, render_template, request, jsonify, session
from groq import Groq

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── Load model data ──────────────────────────────────────────────────────────
with open(os.path.join(os.path.dirname(__file__), 'model_data.json')) as f:
    MODEL_DATA = json.load(f)

ENTITY_MAP = {e['name']: e for e in MODEL_DATA}

# ── SQLite DB ────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'datamodel.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        parent TEXT,
        discriminator TEXT,
        description TEXT,
        label TEXT,
        attr_count INTEGER,
        rel_count INTEGER,
        key_count INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS attributes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_name TEXT,
        attr_name TEXT,
        attr_type TEXT,
        is_key INTEGER,
        has_domain INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_entity TEXT,
        to_entity TEXT,
        via_attr TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS query_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_question TEXT,
        generated_sql TEXT,
        result_count INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('SELECT COUNT(*) FROM entities')
    if c.fetchone()[0] == 0:
        print("Populating database from model data...")
        for e in MODEL_DATA:
            label = e['labels'][0] if e['labels'] else ''
            c.execute('INSERT OR IGNORE INTO entities VALUES (NULL,?,?,?,?,?,?,?,?)',
                (e['name'], e['parent'], e['discriminator'],
                 e['description'], label,
                 len(e['attributes']), len(e['relationships']), len(e['keys'])))
            key_names = set(e['keys'])
            for a in e['attributes']:
                c.execute('INSERT INTO attributes VALUES (NULL,?,?,?,?,?)',
                    (e['name'], a['name'], a['type'],
                     1 if a['name'] in key_names else 0,
                     1 if a['domain'] else 0))
            for r in e['relationships']:
                c.execute('INSERT INTO relationships VALUES (NULL,?,?,?)',
                    (e['name'], r['entity'], r['attr']))
        conn.commit()
        print("Database populated.")

    conn.close()

init_db()

# ── Schema summary for AI ────────────────────────────────────────────────────
SCHEMA_SUMMARY = """
SQLite Database Schema for TI Plus 2 Financial Data Model (Finastra/Misys):

TABLE: entities
  - id (INTEGER PK)
  - name (TEXT) — entity name e.g. 'CP-master', 'FX-deal', 'currency'
  - parent (TEXT) — parent entity for inheritance
  - discriminator (TEXT) — numeric discriminator
  - description (TEXT) — human readable description
  - label (TEXT) — business label e.g. 'Transaction'
  - attr_count (INTEGER) — number of attributes
  - rel_count (INTEGER) — number of relationships
  - key_count (INTEGER) — number of key attributes

TABLE: attributes
  - id (INTEGER PK)
  - entity_name (TEXT) — references entities.name
  - attr_name (TEXT) — attribute name
  - attr_type (TEXT) — data type e.g. 'string(35)', 'decimal(15,0)', 'date', 'boolean', 'autokey', 'blob', 'timestamp'
  - is_key (INTEGER 0/1) — 1 if this is a key attribute
  - has_domain (INTEGER 0/1) — 1 if this attribute has predefined values

TABLE: relationships
  - id (INTEGER PK)
  - from_entity (TEXT) — source entity name
  - to_entity (TEXT) — target entity name
  - via_attr (TEXT) — the attribute used for the relationship

TABLE: query_history
  - id, user_question, generated_sql, result_count, created_at

Domain: banking/trade finance — Clean Payments (CP-*), Foreign Exchange (FX-*),
Letters of Credit (LC, import-LC, export-LC), Collections, Documentary Credits,
SWIFT messages, Charges, Accounts, Currencies, Parties, Events, Masters, etc.
"""

# ── Groq client ──────────────────────────────────────────────────────────────
def get_groq_client():
    api_key = session.get('groq_api_key', '')
    if not api_key:
        return None
    return Groq(api_key=api_key)

def run_sql(sql):
    """Execute SQL and return results or error."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()
        c.execute(sql)
        rows = c.fetchall()
        cols = [d[0] for d in c.description] if c.description else []
        data = [dict(row) for row in rows]
        return {'success': True, 'columns': cols, 'rows': data, 'count': len(data)}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        conn.close()

def save_history(question, sql, count):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('INSERT INTO query_history (user_question, generated_sql, result_count) VALUES (?,?,?)',
                 (question, sql, count))
    conn.commit()
    conn.close()

FORBIDDEN = re.compile(r'\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|TRUNCATE)\b', re.IGNORECASE)

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/set-key', methods=['POST'])
def set_key():
    data = request.json
    session['groq_api_key'] = data.get('api_key', '')
    return jsonify({'ok': True})

@app.route('/api/stats')
def stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    total_entities = c.execute('SELECT COUNT(*) FROM entities').fetchone()[0]
    total_attrs    = c.execute('SELECT COUNT(*) FROM attributes').fetchone()[0]
    total_rels     = c.execute('SELECT COUNT(*) FROM relationships').fetchone()[0]
    top_parents    = c.execute('''
        SELECT parent, COUNT(*) as cnt FROM entities
        WHERE parent != '' GROUP BY parent ORDER BY cnt DESC LIMIT 10
    ''').fetchall()
    type_dist = c.execute('''
        SELECT SUBSTR(attr_type,1,INSTR(attr_type||"(",'(')-1) as base_type, COUNT(*) as cnt
        FROM attributes GROUP BY base_type ORDER BY cnt DESC LIMIT 8
    ''').fetchall()
    conn.close()
    return jsonify({
        'entities': total_entities,
        'attributes': total_attrs,
        'relationships': total_rels,
        'top_parents': [{'parent': r[0], 'count': r[1]} for r in top_parents],
        'type_dist': [{'type': r[0], 'count': r[1]} for r in type_dist],
    })

@app.route('/api/entities')
def get_entities():
    search   = request.args.get('q', '')
    page     = int(request.args.get('page', 1))
    per_page = 20
    offset   = (page - 1) * per_page
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if search:
        rows  = c.execute('''SELECT * FROM entities WHERE name LIKE ? OR description LIKE ?
            ORDER BY name LIMIT ? OFFSET ?''',
            (f'%{search}%', f'%{search}%', per_page, offset)).fetchall()
        total = c.execute('SELECT COUNT(*) FROM entities WHERE name LIKE ? OR description LIKE ?',
            (f'%{search}%', f'%{search}%')).fetchone()[0]
    else:
        rows  = c.execute('SELECT * FROM entities ORDER BY name LIMIT ? OFFSET ?',
            (per_page, offset)).fetchall()
        total = c.execute('SELECT COUNT(*) FROM entities').fetchone()[0]
    conn.close()
    return jsonify({'entities': [dict(r) for r in rows], 'total': total, 'page': page})

@app.route('/api/entity/<name>')
def get_entity(name):
    entity = ENTITY_MAP.get(name)
    if not entity:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(entity)

@app.route('/api/query', methods=['POST'])
def ai_query():
    data     = request.json
    question = data.get('question', '').strip()
    if not question:
        return jsonify({'error': 'No question provided'}), 400

    client = get_groq_client()
    if not client:
        return jsonify({'error': 'No Groq API key set. Please configure it in Settings.'}), 401

    system_prompt = f"""You are an expert SQL agent for a financial data model database (TI Plus 2 / Finastra).
Your job: Convert natural language questions into SQLite SQL queries.

{SCHEMA_SUMMARY}

RULES:
1. Return ONLY valid SQLite SQL. No markdown, no explanation, no backticks.
2. Always add LIMIT 100 unless user asks for specific count.
3. Use LIKE for fuzzy text matching.
4. Column names are exact: name, parent, discriminator, description, label, attr_count, rel_count, key_count, entity_name, attr_name, attr_type, is_key, has_domain, from_entity, to_entity, via_attr.
5. For type queries: attr_type values look like 'string(35)', 'decimal(15,0)', 'date', 'boolean', 'autokey', 'blob', 'timestamp'
6. Never use DROP, DELETE, INSERT, UPDATE, CREATE — read-only queries only.
7. If the question is about payments use 'CP-' prefix. FX for foreign exchange. LC for letters of credit.
"""

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": question}
            ],
            temperature=0.1,
            max_tokens=512,
        )
        sql = completion.choices[0].message.content.strip()

        # Clean up markdown fences if any
        sql = re.sub(r'^```sql\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'^```\s*',    '', sql)
        sql = re.sub(r'\s*```$',    '', sql)
        sql = sql.strip()

        if FORBIDDEN.search(sql):
            return jsonify({'error': 'Unsafe SQL generated. Only SELECT is allowed.'}), 400

        result = run_sql(sql)
        if result['success']:
            save_history(question, sql, result['count'])

        return jsonify({'sql': sql, 'result': result, 'question': question})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sql', methods=['POST'])
def raw_sql():
    data = request.json
    sql  = data.get('sql', '').strip()
    if not sql:
        return jsonify({'error': 'No SQL provided'}), 400
    if FORBIDDEN.search(sql):
        return jsonify({'error': 'Only SELECT queries allowed.'}), 400
    return jsonify(run_sql(sql))

@app.route('/api/history')
def history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM query_history ORDER BY created_at DESC LIMIT 20').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/relationships/<entity>')
def entity_relationships(entity):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    outgoing = conn.execute('SELECT * FROM relationships WHERE from_entity=?', (entity,)).fetchall()
    incoming = conn.execute('SELECT * FROM relationships WHERE to_entity=?', (entity,)).fetchall()
    conn.close()
    return jsonify({
        'outgoing': [dict(r) for r in outgoing],
        'incoming': [dict(r) for r in incoming],
    })

@app.route('/api/suggest', methods=['POST'])
def suggest_queries():
    client = get_groq_client()
    fallback = [
        "Show all entities related to clean payments (CP-)",
        "Which entities have more than 15 attributes?",
        "List all FX foreign exchange entities with descriptions",
        "What entities are directly related to the 'currency' entity?",
        "Show entities that have boolean type attributes",
        "Which parent entities have the most child entities?",
    ]
    if not client:
        return jsonify({'suggestions': fallback})

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{
                "role": "user",
                "content": f"""Generate 6 interesting natural language questions a banker or data analyst might ask about this financial data model database.
{SCHEMA_SUMMARY}
Return ONLY a JSON array of 6 question strings. No explanation."""
            }],
            temperature=0.8,
            max_tokens=400,
        )
        text = completion.choices[0].message.content.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$',     '', text)
        suggestions = json.loads(text)
        return jsonify({'suggestions': suggestions})
    except Exception:
        return jsonify({'suggestions': fallback})

# ── NEW: Run all pre-defined SQL queries, return only successful ones ─────────
@app.route('/api/all-queries', methods=['GET'])
def all_queries():
    queries = [
        # Counts
        {"label": "Total entities",
         "sql": "SELECT COUNT(*) AS total_entities FROM entities"},
        {"label": "Total attributes",
         "sql": "SELECT COUNT(*) AS total_attributes FROM attributes"},
        {"label": "Total relationships",
         "sql": "SELECT COUNT(*) AS total_relationships FROM relationships"},
        {"label": "Total query history",
         "sql": "SELECT COUNT(*) AS total_history FROM query_history"},

        # Entity listings
        {"label": "All entities (name + description)",
         "sql": "SELECT name, description, parent, label FROM entities ORDER BY name LIMIT 100"},
        {"label": "Root entities (no parent)",
         "sql": "SELECT name, description, attr_count, rel_count FROM entities WHERE parent = '' OR parent IS NULL ORDER BY name LIMIT 100"},
        {"label": "Entities with most attributes",
         "sql": "SELECT name, description, attr_count FROM entities ORDER BY attr_count DESC LIMIT 20"},
        {"label": "Entities with most relationships",
         "sql": "SELECT name, description, rel_count FROM entities ORDER BY rel_count DESC LIMIT 20"},
        {"label": "Entities with no relationships",
         "sql": "SELECT name, description, attr_count FROM entities WHERE rel_count = 0 ORDER BY name LIMIT 50"},
        {"label": "Entities with no attributes",
         "sql": "SELECT name, description, parent FROM entities WHERE attr_count = 0 ORDER BY name LIMIT 50"},
        {"label": "Entities labeled 'Transaction'",
         "sql": "SELECT name, description, parent FROM entities WHERE label = 'Transaction' ORDER BY name LIMIT 100"},
        {"label": "Most complex entities (attr + rel)",
         "sql": "SELECT name, description, attr_count, rel_count, (attr_count + rel_count) AS complexity FROM entities ORDER BY complexity DESC LIMIT 20"},
        {"label": "Entities with more than 50 attributes",
         "sql": "SELECT name, description, attr_count FROM entities WHERE attr_count > 50 ORDER BY attr_count DESC"},

        # Domain groups
        {"label": "Clean Payment entities (CP-*)",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE 'CP-%' ORDER BY name LIMIT 100"},
        {"label": "Foreign Exchange entities (FX-*)",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE 'FX-%' ORDER BY name LIMIT 100"},
        {"label": "Letter of Credit entities",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%LC%' OR name LIKE '%letter%' ORDER BY name LIMIT 100"},
        {"label": "Event entities (*-event)",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%-event' OR name LIKE '%event-%' ORDER BY name LIMIT 100"},
        {"label": "Master entities (*-master)",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%-master' OR name LIKE '%master-%' ORDER BY name LIMIT 100"},
        {"label": "Charge / fee entities",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%charge%' OR description LIKE '%charge%' ORDER BY name LIMIT 100"},
        {"label": "Account entities",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%account%' OR description LIKE '%account%' ORDER BY name LIMIT 100"},
        {"label": "Currency entities",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%currency%' OR description LIKE '%currency%' ORDER BY name LIMIT 100"},
        {"label": "SWIFT message entities",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%swift%' OR name LIKE '%MT%' OR description LIKE '%SWIFT%' ORDER BY name LIMIT 100"},
        {"label": "Guarantee entities",
         "sql": "SELECT name, description, parent, attr_count FROM entities WHERE name LIKE '%guarantee%' OR description LIKE '%guaranty%' ORDER BY name LIMIT 100"},

        # Inheritance / hierarchy
        {"label": "Most-used parent entities",
         "sql": "SELECT parent, COUNT(*) AS child_count FROM entities WHERE parent != '' GROUP BY parent ORDER BY child_count DESC LIMIT 20"},
        {"label": "Direct children of 'event'",
         "sql": "SELECT name, description, discriminator FROM entities WHERE parent = 'event' ORDER BY name LIMIT 100"},
        {"label": "Direct children of 'master'",
         "sql": "SELECT name, description, discriminator FROM entities WHERE parent = 'master' ORDER BY name LIMIT 100"},
        {"label": "Direct children of 'split'",
         "sql": "SELECT name, description, discriminator FROM entities WHERE parent = 'split' ORDER BY name LIMIT 100"},
        {"label": "Parents with more than 3 children",
         "sql": "SELECT parent, COUNT(*) AS count FROM entities WHERE parent != '' GROUP BY parent HAVING count > 3 ORDER BY count DESC LIMIT 20"},
        {"label": "Grandchildren of 'event' (depth 2)",
         "sql": """SELECT e2.name, e2.description, e2.parent
FROM entities e1
JOIN entities e2 ON e2.parent = e1.name
WHERE e1.parent = 'event'
ORDER BY e2.name LIMIT 100"""},

        # Attributes
        {"label": "Attribute type distribution",
         "sql": "SELECT SUBSTR(attr_type,1,INSTR(attr_type||'(','(')-1) AS base_type, COUNT(*) AS count FROM attributes GROUP BY base_type ORDER BY count DESC"},
        {"label": "Key attributes (is_key=1)",
         "sql": "SELECT entity_name, attr_name, attr_type FROM attributes WHERE is_key = 1 ORDER BY entity_name LIMIT 100"},
        {"label": "Attributes with predefined domain",
         "sql": "SELECT entity_name, attr_name, attr_type FROM attributes WHERE has_domain = 1 ORDER BY entity_name LIMIT 100"},
        {"label": "Boolean attributes",
         "sql": "SELECT entity_name, attr_name FROM attributes WHERE attr_type = 'boolean' ORDER BY entity_name LIMIT 100"},
        {"label": "Blob / binary attributes",
         "sql": "SELECT entity_name, attr_name FROM attributes WHERE attr_type = 'blob' ORDER BY entity_name LIMIT 100"},
        {"label": "Timestamp attributes",
         "sql": "SELECT entity_name, attr_name FROM attributes WHERE attr_type = 'timestamp' ORDER BY entity_name LIMIT 100"},
        {"label": "Date attributes",
         "sql": "SELECT entity_name, attr_name FROM attributes WHERE attr_type = 'date' ORDER BY entity_name LIMIT 100"},
        {"label": "Autokey attributes",
         "sql": "SELECT entity_name, attr_name FROM attributes WHERE attr_type = 'autokey' ORDER BY entity_name LIMIT 100"},
        {"label": "Amount attributes (by name)",
         "sql": "SELECT entity_name, attr_name, attr_type FROM attributes WHERE attr_name LIKE '%.Amount' OR attr_name LIKE '%Amount%' ORDER BY entity_name LIMIT 100"},
        {"label": "Currency code attributes",
         "sql": "SELECT entity_name, attr_name FROM attributes WHERE attr_name LIKE '%Currency.code' OR attr_name LIKE '%currency%' ORDER BY entity_name LIMIT 100"},
        {"label": "Top 20 entities by attribute count",
         "sql": "SELECT entity_name, COUNT(*) AS attr_count FROM attributes GROUP BY entity_name ORDER BY attr_count DESC LIMIT 20"},

        # Relationships
        {"label": "Most referenced target entities",
         "sql": "SELECT to_entity, COUNT(*) AS ref_count FROM relationships GROUP BY to_entity ORDER BY ref_count DESC LIMIT 20"},
        {"label": "Entities with most outgoing relationships",
         "sql": "SELECT from_entity, COUNT(*) AS out_count FROM relationships GROUP BY from_entity ORDER BY out_count DESC LIMIT 20"},
        {"label": "All relationships pointing to 'currency'",
         "sql": "SELECT from_entity, to_entity, via_attr FROM relationships WHERE to_entity = 'currency' ORDER BY from_entity LIMIT 100"},
        {"label": "Relationships from CP- entities",
         "sql": "SELECT from_entity, to_entity, via_attr FROM relationships WHERE from_entity LIKE 'CP-%' ORDER BY from_entity LIMIT 100"},
        {"label": "Relationships from FX- entities",
         "sql": "SELECT from_entity, to_entity, via_attr FROM relationships WHERE from_entity LIKE 'FX-%' ORDER BY from_entity LIMIT 100"},
        {"label": "Relationships involving 'account'",
         "sql": "SELECT from_entity, to_entity, via_attr FROM relationships WHERE to_entity LIKE '%account%' OR from_entity LIKE '%account%' ORDER BY from_entity LIMIT 100"},

        # Joins
        {"label": "CP- entities with their key attributes (JOIN)",
         "sql": "SELECT e.name, a.attr_name, a.attr_type FROM entities e JOIN attributes a ON a.entity_name = e.name WHERE e.name LIKE 'CP-%' AND a.is_key = 1 ORDER BY e.name LIMIT 100"},
        {"label": "FX- entities with their key attributes (JOIN)",
         "sql": "SELECT e.name, a.attr_name, a.attr_type FROM entities e JOIN attributes a ON a.entity_name = e.name WHERE e.name LIKE 'FX-%' AND a.is_key = 1 ORDER BY e.name LIMIT 100"},
        {"label": "Entities linked to 'currency' with description (JOIN)",
         "sql": "SELECT e.name, e.description, r.via_attr FROM entities e JOIN relationships r ON r.from_entity = e.name WHERE r.to_entity = 'currency' ORDER BY e.name LIMIT 100"},
        {"label": "Entities with boolean attributes (JOIN)",
         "sql": "SELECT DISTINCT e.name, e.parent, e.description FROM entities e JOIN attributes a ON a.entity_name = e.name WHERE a.attr_type = 'boolean' ORDER BY e.name LIMIT 100"},
        {"label": "All entities + their relationships (JOIN)",
         "sql": "SELECT e.name, e.description, r.to_entity, r.via_attr FROM entities e JOIN relationships r ON r.from_entity = e.name ORDER BY e.name LIMIT 100"},

        # Query history
        {"label": "Last 20 queries in history",
         "sql": "SELECT user_question, generated_sql, result_count, created_at FROM query_history ORDER BY created_at DESC LIMIT 20"},
        {"label": "Queries with most results",
         "sql": "SELECT user_question, generated_sql, result_count FROM query_history ORDER BY result_count DESC LIMIT 20"},
        {"label": "Queries that returned 0 results",
         "sql": "SELECT user_question, generated_sql FROM query_history WHERE result_count = 0 LIMIT 20"},
    ]

    results = []
    for q in queries:
        r = run_sql(q['sql'])
        if r['success']:
            results.append({
                'label':   q['label'],
                'sql':     q['sql'],
                'columns': r['columns'],
                'rows':    r['rows'],
                'count':   r['count'],
            })
        # Failed queries are silently skipped

    return jsonify({'total': len(results), 'queries': results})


if __name__ == '__main__':
    print("🚀 DataModel AI Explorer starting...")
    app.run(debug=True, port=5001)
