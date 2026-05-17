# DataModel AI Explorer — TI Plus 2

A professional web app to explore the Finastra/Misys TI Plus 2 financial data model
using AI-powered SQL queries via Groq.

## Quick Start

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Run the app:
   ```bash
   python app.py
   ```

3. Open http://localhost:5001

4. Click "Configure Groq API" and enter your free API key from https://console.groq.com

## Features

- **Dashboard**: Stats on 801 entities, 13,963+ attributes, 3,668 relationships
- **AI SQL Agent**: Ask questions in French or English — AI generates SQL
- **SQL Editor**: Write raw SQLite queries
- **Entity Browser**: Browse and search all entities with relationships
- **Query History**: All past queries saved

## Database Tables

- `entities` — 801 banking/financial entities
- `attributes` — 13,500+ data fields with types
- `relationships` — 3,668 entity links
- `query_history` — your past queries

## Example Questions for the AI Agent

- "Montre moi toutes les entités de paiement"
- "Show entities with more than 50 attributes"
- "Which entities are related to currency?"
- "List all FX foreign exchange entities"
- "What are the most connected entities?"
