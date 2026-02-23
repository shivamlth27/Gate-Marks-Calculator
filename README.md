# GATE DA 2026 Report

A Flask-based web app to calculate and visualize GATE DA response-sheet performance.

## Features

- Paste response-sheet URL and get full report
- Total score, section-wise score (GA/DA), accuracy
- Question-wise status table with filters
- Public rank table (marks + rank)
- Insights chart (distribution + summary stats)
- Dark mode
- CSV export for question-wise results

## Tech Stack

- Python (Flask)
- Frontend: server-rendered HTML/CSS/JS
- Deployment: Vercel
- Persistent storage: Redis via `REDIS_URL`

## Project Structure

- `api/index.py` - Main Flask app + UI rendering + storage logic
- `gate_da_marks_calculator.py` - Parsing + marking engine
- `gate_da_answer_key.py` - Answer key definition
- `vercel.json` - Vercel routing/build config
- `requirements.txt` - Python dependencies

## Local Run

```bash
cd "/Users/shivam/Desktop/colab/gate da"
python3 -m pip install -r requirements.txt
PYTHONPATH="$PWD" flask --app api/index.py run --debug --port 5001
```

Open: `http://127.0.0.1:5001`

Primary URL:

- `https://gate-da-report.vercel.app`





